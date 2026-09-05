"""DSN device / scenes."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from busylib import exceptions

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.device import assets as _device_assets
from apps.dsn_app.device import display as _device_display
from apps.dsn_app.device import scene_policy as _device_scene_policy
from apps.dsn_app.render import distance as _render_distance
from apps.dsn_app.render import instrument as _render_instrument
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_dishes as _render_network_dishes
from apps.dsn_app.render import network_rows as _render_network_rows
from apps.dsn_app.render import network_skies as _render_network_skies
from apps.dsn_app.render import timing as _render_timing


async def prepare_network_page(
    bb,
    state: _model.State,
    links: list[_source.Link],
    page: int,
    names: dict[str, str],
    freshness: str,
    signature: tuple,
) -> None:
    """Warm one captured page during the current page's native dwell."""
    if signature in state.scene_cache:
        return
    frames, fps, hold = _render_network_rows.render_network_page_frames(
        links, page, freshness, names
    )
    blob = _device_assets.encode_native_frames(frames, fps, hold)
    filename = _device_assets.next_scene_filename(state)
    try:
        await bb.assets_upload(_limits.APP_NAME, filename, blob)
    except asyncio.CancelledError:
        raise
    except Exception:
        if not await _device_assets.scene_asset_exists(bb, filename, len(blob)):
            raise
    _device_assets.remember_scene_asset(state, signature, filename)
    await _device_assets.trim_scene_cache(bb, state)


def start_network_page_warm(bb, state: _model.State) -> asyncio.Task | None:
    if _settings.DSN_NETWORK_STYLE != "rows":
        return None
    links = list(state.links)
    count = _render_network_rows.network_page_count(links)
    if count <= 1:
        return None
    if state.network_warm_task is not None and not state.network_warm_task.done():
        return state.network_warm_task
    page = (state.network_page + 1) % count
    names = dict(state.names)
    freshness = _telemetry.feed_freshness(state)
    signature = _render_network_rows.network_signature(
        links, freshness, names, page=page
    )
    if signature in state.scene_cache:
        state.network_warm_signature = None
        return None
    state.network_warm_signature = signature
    state.network_warm_task = asyncio.create_task(
        prepare_network_page(bb, state, links, page, names, freshness, signature)
    )
    state.network_warm_task.add_done_callback(
        lambda task: (
            _limits.logger.debug("network page warm failed: %s", task.exception())
            if not task.cancelled() and task.exception() is not None
            else None
        )
    )
    return state.network_warm_task


async def push_scene(
    bb,
    state: _model.State,
    link: _source.Link,
    signature: tuple | None = None,
    rendered_at: datetime | None = None,
) -> bool:
    rendered_at = rendered_at or datetime.now(timezone.utc)
    if _device_scene_policy.complete_watch_if_due(state, link, rendered_at.timestamp()):
        signature = None
    signature = signature or _device_scene_policy.scene_signature(
        state, link, rendered_at
    )
    intent_token = _selection.scene_intent_token(state)
    intent_view = state.view
    intent_network_page = state.network_page
    intent_network_focus = (
        intent_view == "network"
        and _settings.DSN_NETWORK_STYLE in _limits.NETWORK_FOCUS_STYLES
        and _selection.network_focus_active(state)
        and state.network_focus_key is not None
    )
    intent_network_focus_key = (
        state.network_focus_key if intent_network_focus else link.key
    )
    if intent_network_focus:
        intent_links, intent_names, intent_trails = _selection.network_focus_inputs(
            state
        )
    else:
        intent_links = list(state.links)
        intent_names = dict(state.names)
        intent_trails = {key: list(points) for key, points in state.aim_trails.items()}
    intent_freshness = _telemetry.feed_freshness(state, rendered_at.timestamp())
    intent_realtime_since = state.realtime_since
    intent_rt_generation = state.rt_generation
    intent_watch = state.watch
    intent_contact_state = _selection.watch_contact_state(state)
    live_watch_link = _selection.watch_live_link(state)
    instrument_link = live_watch_link or link if intent_view == "instrument" else link
    distance_link = (
        _selection.distance_display_link(state, link)
        if intent_view == "distance"
        else link
    )
    live_until = (
        state.watch.deadline
        if (intent_view == "distance" and state.watch is not None)
        else intent_realtime_since + link.light_s
        if (
            intent_view == "distance"
            and intent_realtime_since is not None
            and link.light_s
        )
        else None
    )
    element_timeout = (
        _limits.REALTIME_ELEMENT_TIMEOUT_S
        if live_until is not None
        else _device_scene_policy.scene_element_timeout(state, link)
    )
    filename = state.scene_cache.get(signature)
    if (
        filename is None
        and intent_view == "network"
        and _settings.DSN_NETWORK_STYLE == "rows"
        and state.network_warm_signature == signature
        and state.network_warm_task is not None
        and not state.network_warm_task.done()
    ):
        # The page boundary caught its prewarm in flight. Share that exact
        # immutable build instead of racing a second upload/flash write.
        try:
            await state.network_warm_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - foreground can still build
            _limits.logger.debug("network page prewarm did not complete: %s", exc)
        filename = state.scene_cache.get(signature)
    if (
        filename is None
        and signature == state.last_scene_signature
        and state.last_scene_filename
    ):
        filename = state.last_scene_filename
        _device_assets.remember_scene_asset(state, signature, filename)
    if filename is not None:
        state.scene_cache.move_to_end(signature)
    if filename is None:
        if intent_view == "network":
            if _settings.DSN_NETWORK_STYLE == "dishes":
                if intent_network_focus:
                    frames, fps, hold = _render_network_dishes.render_dish_focus_frames(
                        intent_links,
                        intent_freshness,
                        intent_names,
                        intent_network_focus_key,
                    )
                else:
                    frames, fps, hold = (
                        _render_network_dishes.render_dish_network_frames(
                            intent_links, intent_freshness, intent_network_focus_key
                        )
                    )
            elif _settings.DSN_NETWORK_STYLE == "skies":
                frames, fps, hold = _render_network_skies.render_three_skies_frames(
                    intent_links,
                    intent_freshness,
                    intent_names,
                    intent_network_focus_key,
                    intent_trails,
                    intent_network_focus,
                )
            else:
                frames, fps, hold = _render_network_rows.render_network_page_frames(
                    intent_links, intent_network_page, intent_freshness, intent_names
                )
        elif intent_view == "instrument":
            frames, fps, hold = _render_instrument.render_instrument_frames(
                instrument_link,
                state.aim_trails.get(instrument_link.key, []),
                intent_freshness,
                state.names,
                intent_contact_state,
            )
        else:
            _, distance_anchor = _device_scene_policy.distance_render_clock(
                state, distance_link, rendered_at
            )
            frames, fps, hold = _render_distance.render_frames(
                distance_link,
                distance_anchor,
                state.names,
                dish_types=state.dish_types,
                site_lons=state.site_lons,
                realtime_since=intent_realtime_since,
                on_air=(intent_watch.on_air if intent_watch is not None else None),
                handoff=(
                    intent_watch is not None
                    and intent_watch.on_air
                    and bool(intent_watch.live_key)
                    and intent_watch.live_key != intent_watch.link.key
                ),
                freshness=intent_freshness,
            )
        blob = _device_assets.encode_native_frames(frames, fps, hold)
        # Versioned, never reused: the firmware caches by path and may still
        # hold the file it is playing. Timestamp AND counter avoid collisions.
        filename = _device_assets.next_scene_filename(state)
        try:
            await bb.assets_upload(_limits.APP_NAME, filename, blob)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not await _device_assets.scene_asset_exists(bb, filename, len(blob)):
                raise
            _limits.logger.info("adopted ambiguous scene upload: %s", filename)
        # Record ownership BEFORE display_draw. A lost response after upload
        # is ambiguous; retry this exact immutable path instead of leaking a
        # new generation or deleting a file the firmware may have opened.
        _device_assets.remember_scene_asset(state, signature, filename)
        # Ownership refusal happens after upload. Keep the cache bounded even
        # if BUSY/CUSTOM owns the panel for hours and every newer source aim
        # creates another immutable Network signature.
        await _device_assets.trim_scene_cache(bb, state)
    if _selection.scene_intent_token(state) != intent_token:
        state.dirty.set()
        return False
    led = state.led_blink  # acknowledge only after success
    led_generation = state.led_generation
    try:
        async with state.interactive_draw:
            # Upload/encode stays outside the input lock. Only the opaque POST
            # is serialized, then revalidated: if it began first the picker
            # commits last; if input won, this stale scene never posts.
            if _selection.scene_intent_token(state) != intent_token:
                state.dirty.set()
                return False
            await asyncio.wait_for(
                bb.display_draw(
                    _device_display._scene_payload(filename, led, element_timeout)
                ),
                _limits.INTERACTIVE_IO_TIMEOUT_S,
            )
    except exceptions.BusyBarAPIError as exc:
        if _device_assets._is_asset_path_failure(exc):
            await _device_assets.discard_scene_asset(bb, state, signature, filename)
            state.dirty.set()
        raise
    if state.led_generation == led_generation:
        state.led_blink = None
    state.last_scene_signature = signature
    state.last_scene_filename = filename
    if intent_view == "network" and _settings.DSN_NETWORK_STYLE == "rows":
        state.network_page_pending = False
    elif (
        intent_view == "network"
        and _settings.DSN_NETWORK_STYLE in _limits.NETWORK_FOCUS_STYLES
        and intent_network_focus
        and state.network_focus_key == intent_network_focus_key
        and math.isinf(state.network_focus_until)
    ):
        # A refused draw leaves ``inf`` pending. Only an accepted semantic
        # zoom starts its complete native cycle. The retiring picker is an
        # opaque newer layer for up to one second, so count from when that
        # mask expires—not from a POST that may have landed behind it.
        visible_at = max(
            asyncio.get_running_loop().time(), state.interactive_visible_until
        )
        focus_loop_s = (
            _render_network_dishes.dish_focus_loop_s(
                intent_links, intent_names, intent_network_focus_key
            )
            if _settings.DSN_NETWORK_STYLE == "dishes"
            else _render_network_skies.three_skies_loop_s(
                intent_links, intent_names, intent_network_focus_key, True
            )
        )
        state.network_focus_until = visible_at + focus_loop_s
    try:
        async with state.interactive_draw:
            if _selection.scene_intent_token(state) != intent_token:
                state.dirty.set()
            elif live_until is not None:
                countdown_id = f"dsncd{state.rt_nonce}{intent_rt_generation or 0}"
                if state.countdown_up and state.countdown_id != countdown_id:
                    if not await _device_display.retire_countdown(bb, state):
                        raise RuntimeError("old countdown retirement was refused")
                await asyncio.wait_for(
                    bb.display_draw(
                        _device_display._countdown_payload(
                            live_until,
                            _limits.TRACK0 + 1,
                            timeout=element_timeout,
                            element_id=countdown_id,
                        )
                    ),
                    _limits.INTERACTIVE_IO_TIMEOUT_S,
                )
                state.countdown_up = True
                state.countdown_id = countdown_id
            elif state.countdown_up:
                if not await _device_display.retire_countdown(bb, state):
                    raise RuntimeError("countdown retirement was refused")
    except Exception as exc:  # noqa: BLE001 - cosmetic
        _limits.logger.debug("countdown draw failed: %s", exc)
        state.dirty.set()
    crossing = (
        f"{_render_timing.crossing_seconds(link.light_s):.0f}s"
        if link.light_s
        else "unknown"
    )
    _limits.logger.info(
        "%s %s -> %s  %s  light %s  crossing %s  az %.0f el %.0f  %s",
        intent_view,
        link.dish,
        link.craft,
        link.band,
        _render_labels.light_label(link.light_s),
        crossing,
        link.azimuth,
        link.elevation,
        _render_labels.rate_label(link.down_bps),
    )
    await _device_assets.trim_scene_cache(bb, state)
    if intent_view == "network" and _settings.DSN_NETWORK_STYLE == "rows":
        start_network_page_warm(bb, state)
    if led == _limits.LED_ARRIVAL and state.completion_pending == link.key:
        state.completion_pending = None
    return True

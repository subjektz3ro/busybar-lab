"""DSN device / scene policy."""

from __future__ import annotations

import math
from datetime import datetime, timezone

from apps.dsn_app import formatting as _formatting
from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.render import carriers as _render_carriers
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import distance as _render_distance
from apps.dsn_app.render import instrument as _render_instrument
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import network_dishes as _render_network_dishes
from apps.dsn_app.render import network_rows as _render_network_rows
from apps.dsn_app.render import network_skies as _render_network_skies
from apps.dsn_app.render import timing as _render_timing


def distance_render_clock(
    state: _model.State,
    link: _source.Link,
    now: datetime,
) -> tuple[int, datetime]:
    """One cache-key clock and the exact timestamp that its pixels use.

    A coarse bucket is useful only if every render inside it is identical.
    Snapping the globe/daylight/creeping-head inputs to the loop-aligned bucket
    makes that invariant literal instead of merely hoping two close calls land
    on the same framebuffer cells.
    """
    cadence = max(1.0, scene_refresh_s(state, link))
    bucket = int(now.timestamp() / cadence)
    anchor = datetime.fromtimestamp(bucket * cadence, timezone.utc)
    return bucket, anchor


def scene_signature(
    state: _model.State, link: _source.Link, now: datetime | None = None
) -> tuple:
    """The pixels we intend to show, quantized to what the panel can resolve."""
    now = now or datetime.now(timezone.utc)
    if state.view == "network":
        fresh = _telemetry.feed_freshness(state, now.timestamp())
        focus = (
            _selection.network_focus_active(state)
            and state.network_focus_key is not None
        )
        selected_key = state.network_focus_key if focus else link.key
        if focus:
            focus_links, focus_names, focus_trails = _selection.network_focus_inputs(
                state
            )
        else:
            focus_links, focus_names, focus_trails = (
                state.links,
                state.names,
                state.aim_trails,
            )
        if _settings.DSN_NETWORK_STYLE == "dishes":
            if focus:
                return _render_network_dishes.dish_focus_signature(
                    focus_links, fresh, focus_names, selected_key
                )
            return _render_network_dishes.dish_network_signature(
                focus_links, fresh, selected_key
            )
        if _settings.DSN_NETWORK_STYLE == "skies":
            return _render_network_skies.three_skies_signature(
                focus_links, fresh, focus_names, selected_key, focus_trails, focus
            )
        return _render_network_rows.network_signature(
            state.links, fresh, state.names, page=state.network_page
        )
    if state.view == "instrument":
        fresh = _telemetry.feed_freshness(state, now.timestamp())
        visible = _selection.watch_live_link(state) if state.watch is not None else link
        visible = visible or link
        contact_state = _selection.watch_contact_state(state)
        return _render_instrument.instrument_signature(
            visible,
            state.aim_trails.get(visible.key, []),
            fresh,
            state.names,
            contact_state,
        )
    link = _selection.distance_display_link(state, link)
    fresh = _telemetry.feed_freshness(state, now.timestamp())
    # Use the same loop-aligned cadence the scheduler uses. A raw 21-second
    # signature bucket could otherwise wake on a feed poll and restart an
    # eight-second native loop off-seam even though the due timer was aligned.
    clock_bucket, render_anchor = distance_render_clock(state, link, now)
    light_s = link.light_s
    realtime_since = state.realtime_since
    live_distance = realtime_since is not None and light_s is not None
    if (
        realtime_since is not None
        and light_s is not None
        and light_s > _limits.RT_SEAMLESS_MAX_S
    ):
        creeping = True
        progress_units = _render_carriers.represented_progress(
            light_s, render_anchor.timestamp(), realtime_since
        )[0]
    else:
        creeping = False
        progress_units = None
    spacing = (
        None
        if creeping
        else _render_carriers.represented_packet_spacing(light_s, live_distance)
    )
    watch_state = (
        "off_air"
        if state.watch is not None and not state.watch.on_air
        else "handoff"
        if (
            state.watch is not None
            and state.watch.live_key
            and state.watch.live_key != state.watch.link.key
        )
        else "live"
        if state.watch is not None
        else None
    )
    return (
        "distance",
        link.key,
        _render_labels.craft_label(link.craft, state.names),
        (
            _render_dish.dish_tilt(
                link.elevation,
                _formatting.dish_metres(link.dish, state.dish_types) == "70",
            )
            if link.pointing_valid
            else ("unknown",)
        ),
        _source.band_key(link.band),
        _render_carriers.pulse_span(link.down_bps),
        _render_labels.rate_label(link.down_bps),
        _render_labels.light_label(link.light_s),
        spacing,
        progress_units,
        link.up_active,
        link.down_active,
        link.arrayed,
        link.mspa,
        link.ddor,
        state.site_lons.get(_render_network_data._site_name(link.complex_name)),
        state.realtime_since,
        watch_state,
        fresh,
        clock_bucket,
    )


def scene_needs_draw(state: _model.State, intended: tuple, due: bool) -> bool:
    """Whether pixels, an event LED, or the animation timeout need a draw."""
    desired_countdown_id = (
        f"dsncd{state.rt_nonce}{state.rt_generation or 0}"
        if (state.view == "distance" and state.realtime_since is not None)
        else None
    )
    countdown_pending = (
        desired_countdown_id is not None
        and (not state.countdown_up or state.countdown_id != desired_countdown_id)
    ) or (desired_countdown_id is None and state.countdown_up)
    return (
        due
        or intended != state.last_scene_signature
        or state.led_blink is not None
        or countdown_pending
    )


def scene_refresh_s(state: _model.State, link: _source.Link) -> float:
    """Renew only on a native-loop seam; the matching timeout exceeds it."""
    if state.view == "distance" and state.realtime_since is not None:
        loop_s = _render_distance.distance_loop_s(link, state.names)
        raw = _render_timing.realtime_redraw_s(link.light_s)
        return math.ceil(raw / loop_s) * loop_s
    if state.view == "instrument":
        visible = _selection.watch_live_link(state) if state.watch is not None else link
        visible = visible or link
        loop_s = (
            _render_instrument.instrument_frame_count(
                visible, state.names, _selection.watch_contact_state(state)
            )
            / _limits.INSTRUMENT_FPS
        )
    elif state.view == "network":
        if _settings.DSN_NETWORK_STYLE in _limits.NETWORK_FOCUS_STYLES:
            focus = (
                _selection.network_focus_active(state)
                and state.network_focus_key is not None
            )
            selected_key = state.network_focus_key if focus else link.key
            focus_links, focus_names, _ = (
                _selection.network_focus_inputs(state)
                if focus
                else (state.links, state.names, state.aim_trails)
            )
            if _settings.DSN_NETWORK_STYLE == "dishes":
                loop_s = (
                    _render_network_dishes.dish_focus_loop_s(
                        focus_links, focus_names, selected_key
                    )
                    if focus
                    else _render_network_dishes.dish_network_loop_s(focus_links)
                )
            else:
                loop_s = _render_network_skies.three_skies_loop_s(
                    focus_links, focus_names, selected_key, focus
                )
            if focus:
                # Focus is one deliberate native semantic cycle, not an
                # ambient carousel. Its deadline wakes the overview at seam.
                return loop_s
        else:
            # A row page boundary is content, not merely a lease renewal. The
            # next resident page replaces this one after its full marquee.
            loop_s = _render_network_rows.network_page_duration_s(
                state.links, state.network_page, state.names
            )
            if _render_network_rows.network_page_count(state.links) > 1:
                return loop_s
    else:
        loop_s = _render_distance.distance_loop_s(link, state.names)
    cycles = max(1, int(_limits.SCENE_RENEW_TARGET_S // max(loop_s, 0.1)))
    return loop_s * cycles


def scene_element_timeout(state: _model.State, link: _source.Link | None = None) -> int:
    if state.view == "distance" and state.realtime_since is not None:
        return _limits.REALTIME_ELEMENT_TIMEOUT_S
    if link is None:
        return _limits.ELEMENT_TIMEOUT_S
    return max(_limits.ELEMENT_TIMEOUT_S, math.ceil(scene_refresh_s(state, link) + 30))


def advance_network_page_if_due(state: _model.State, due: bool) -> bool:
    """Choose the next page once; a refused draw retries that exact intent."""
    if (
        _settings.DSN_NETWORK_STYLE != "rows"
        or not due
        or state.view != "network"
        or state.network_page_pending
        or not state.last_scene_signature
        or state.last_scene_signature[0] != "network-page"
    ):
        return False
    count = _render_network_rows.network_page_count(state.links)
    if count <= 1:
        return False
    state.network_page = (state.network_page + 1) % count
    state.network_page_pending = True
    return True


def arrival_due(state: _model.State, link: _source.Link, ts: float) -> bool:
    """Has one full light time elapsed since the user started the watch?

    This ENDS the watch: the lock releases and the rotation resumes. The
    countdown really does run out — 19 hours 48 minutes on Voyager, which
    completes tomorrow morning on a device that sits on a desk all day. That
    real elapsed-time boundary is what hands the display back; it does not
    claim the feed identified a particular packet at the spacecraft.
    """
    if state.realtime_since is None:
        return False
    light_s = state.watch.light_s if state.watch is not None else link.light_s
    deadline = (
        state.watch.deadline
        if state.watch is not None
        else state.realtime_since + light_s
        if light_s
        else None
    )
    if deadline is None or ts < deadline:
        return False
    completed_generation = (
        state.watch.generation if state.watch is not None else state.rt_generation
    )
    completed_link = state.watch.link if state.watch is not None else link
    state.realtime_since = None  # the watch is over
    state.focus = None  # back to the live rotation
    state.completion_pending = completed_link.key  # until accepted arrival blink
    state.completion_link = completed_link
    state.completion_generation = completed_generation
    state.watch = None
    if state.view_before_lock is not None:
        state.view = state.view_before_lock
        state.view_before_lock = None
    return True


def complete_watch_if_due(state: _model.State, link: _source.Link, ts: float) -> bool:
    """Finish on the wall-clock boundary, independent of the active view."""
    if not arrival_due(state, link, ts):
        return False
    _model.request_led(state, _limits.LED_ARRIVAL)
    state.dirty.set()
    _limits.logger.info("the %s light-time watch completed", link.craft)
    return True

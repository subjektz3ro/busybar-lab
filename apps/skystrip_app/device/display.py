"""Skystrip device / display."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime

from busylib import exceptions, types
from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import weather_state as _weather_state
from apps.skystrip_app.device import ambient as _device_ambient
from apps.skystrip_app.device import report_status as _device_report_status
from apps.skystrip_app.device import scrubber as _device_scrubber
from apps.skystrip_app.render import status as _render_status
from busybar_dev import anim
from busybar_dev.device import is_refusal as _is_refusal


def _stale_weather_elements(timeout: int = _limits.STALE_ELEMENT_TIMEOUT_S) -> list:
    """The honest stand-in for a sky we are not entitled to draw.

    FIRMWARE LAW: element attributes are immutable after creation, so every
    redraw keeps IDENTICAL ids and geometry and varies only the timeout — the
    same mutation ``draw_scrub_readout`` relies on.  Geometry and colors are
    the proven readout band.
    """
    return [
        types.RectangleElement(
            id="wxstaleb",
            type="rectangle",
            x=8,
            y=3,
            width=56,
            height=11,
            fill="solid",
            fill_colors=["#000000C0"],
            border_width=0,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
        types.TextElement(
            id="wxstalet",
            type="text",
            text=_limits.STALE_WEATHER_TEXT,
            font="condensed",
            color="#FFD98CFF",
            align="center",
            x=36,
            y=8,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
    ]


def _restore_payload(
    state: _model.SkyState,
) -> tuple[types.DisplayElements | None, dict | None, dict | None]:
    """Build the selected live or Time Machine view after an app clear."""
    elements: list = []
    scene_file = state.current_scene_file or (
        state.scene_files[-1] if state.scene_files else None
    )
    # Clearing an alert must not grant an expired weather scene a brand-new
    # native timeout.  Time Machine content below is independently selected
    # model output and can still be restored while the live lease is closed.
    if scene_file is not None and _weather_state.weather_is_fresh(state):
        elements.append(
            types.AnimationElement(
                id="sky",
                type="animation",
                path=scene_file,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=_limits.ELEMENT_TIMEOUT_S,
            )
        )

    restored_reveal = None
    restored_readout = None
    if state.revealed and state.scrub_slot is not None:
        source = state.last_reveal
        if source is None and state.timeline_meta is not None:
            source = {
                "slot": state.scrub_slot,
                "fname": state.timeline_meta["file"],
                "section": f"s{state.scrub_slot:02d}",
            }
        if source is not None:
            # display_clear removed the native registry, so reusing the
            # selected reveal id is safe and preserves the logical view.
            # (Without the clear, geometry/path reuse would be unsafe.)
            eid = source.get("eid")
            if eid is None:
                state.reveal_n += 1
                eid = f"rv{state.reveal_n}"
            restored_reveal = {
                "eid": eid,
                "slot": state.scrub_slot,
                "fname": source["fname"],
                "section": source.get("section"),
            }
            elements.append(
                types.AnimationElement(
                    id=restored_reveal["eid"],
                    type="animation",
                    path=restored_reveal["fname"],
                    section=restored_reveal["section"],
                    loop=True,
                    x=0,
                    y=0,
                    display=types.DisplayName.FRONT,
                    timeout=60,
                )
            )
    elif state.scrub_slot is not None and state.timeline_meta is not None:
        state.readout_gen = (state.readout_gen + 1) % 100
        label = _device_scrubber._slot_label(state.timeline_meta, state.scrub_slot)
        elements.extend(
            _device_scrubber._readout_elements(state.readout_gen, label, timeout=3)
        )
        restored_readout = {
            "generation": state.readout_gen,
            "label": label,
            "timeout": 3,
        }

    if not elements:
        # Nothing truthful to show — but this payload follows a display_clear,
        # so returning None here is not "leave it as it was", it is "leave it
        # black". Say why instead; the scene loop replaces this the moment a
        # real observation lands.
        elements.extend(_stale_weather_elements())
    return (
        types.DisplayElements(
            application_name=_limits.APP_NAME,
            priority=_limits.PRIORITY,
            led_notification_color=(
                "#FF2222FF"
                if state.visual_alert is not None and state.alert_acked
                else None
            ),
            elements=elements,
        ),
        restored_reveal,
        restored_readout,
    )


async def restore_current_view(bb, state: _model.SkyState) -> bool:
    """Remove every same-app transient, then rebuild exactly the selected view."""
    async with state.display_lock:
        try:
            await bb.display_clear(application_name=_limits.APP_NAME)
            # A successful clear removes every same-app transient, including
            # any report POST whose response was lost.
            state.report_statuses.clear()
            payload, restored_reveal, restored_readout = _restore_payload(state)
            if payload is None:
                # The clear already happened, so there is no view left to keep
                # a promise about. Stay armed and let the caller try again
                # rather than reporting a black panel as a restored one.
                state.alert_dismiss_pending = True
                _limits.logger.warning(
                    "restore produced no view after clearing; will retry"
                )
                return False
            await bb.display_draw(payload)
            drew_stale_notice = any(
                element.id == "wxstalet" for element in payload.elements
            )
        except Exception as exc:  # noqa: BLE001
            state.alert_dismiss_pending = True
            if _is_refusal(exc):
                _limits.logger.debug(
                    "alert dismissal yielded to the active device session"
                )
            else:
                _limits.logger.warning(
                    "alert dismissal/restore failed; will retry: %s", exc
                )
            return False
        state.last_reveal = restored_reveal
        state.last_readout = restored_readout
        if drew_stale_notice:
            # Claim the notice so the next good scene retires it by id.
            state.stale_notice_at = asyncio.get_running_loop().time()
        state.alert_dismiss_pending = False
        state.alert_drawn_generation = -1
        # The payload above re-creates "sky" pointing at the file the scene
        # loop last uploaded — an asset the firmware cached BY PATH and held
        # open across the clear we just issued. Rather than trust that redraw,
        # demand a fresh minute: the scene loop wakes within a second and
        # pushes a NEW versioned filename, which always renders. Without this
        # the panel is dark until the next wall-clock boundary, up to a minute
        # of black immediately after acknowledging a warning.
        state.scene_change.set()
        return True


def _scene_payload(
    filename: str,
    led_color: str | None = None,
    prefix: tuple = (),
) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        led_notification_color=led_color,
        elements=[
            *prefix,
            types.AnimationElement(
                id="sky",
                type="animation",
                path=filename,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=_limits.ELEMENT_TIMEOUT_S,
            ),
        ],
    )


def _retired_stale_notice_elements(state: _model.SkyState) -> list:
    """Same ids and geometry, one-second lease — the sanctioned retirement."""
    if not state.stale_notice_at:
        return []
    return _stale_weather_elements(timeout=1)


async def keep_stale_notice(bb, state: _model.SkyState) -> bool:
    """Hold an honest 'no live weather' card while the sources are down.

    Refreshed on its own cadence rather than every tick: the element carries a
    real lease, and redrawing an unchanged card once a second is device
    traffic that buys nothing.  Returns whether a draw was accepted, so the
    caller can tell a refusal from a no-op.
    """
    now = asyncio.get_running_loop().time()
    if not state.stale_since:
        state.stale_since = now
    if now - state.stale_since < _limits.STALE_NOTICE_GRACE_S:
        return False
    if state.stale_notice_at and now - state.stale_notice_at < _limits.STALE_REDRAW_S:
        return False
    try:
        async with state.display_lock:
            # Re-check under the lock: a scene or alert draw may have won the
            # lane while this coroutine waited for it, and this card must
            # never land on top of content that outranks it.
            if state.visual_alert is not None or state.scrub_slot is not None:
                return False
            await bb.display_draw(
                types.DisplayElements(
                    application_name=_limits.APP_NAME,
                    priority=_limits.PRIORITY,
                    elements=_stale_weather_elements(),
                )
            )
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.warning("stale-weather notice rejected: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - offline is a state
        _limits.logger.debug("stale-weather notice failed: %s", exc)
        return False
    state.stale_notice_at = now
    return True


async def push_scene(
    bb, state: _model.SkyState, now: datetime, frames: list[Image.Image]
) -> None:
    """Encode the loop to .anim, upload, and (re)draw the scene elements."""
    scene = state.scene
    intent = state.view_generation
    # ``frames`` were rendered from the current weather immediately before
    # this coroutine was entered. Snapshot the matching LED before the upload
    # yields; a poll completing during that await must not make the two halves
    # of one scene payload describe different weather.
    led_color = _device_ambient._weather_led(state.weather)
    fps = max(1, round(len(frames) * _limits.ANIM_FPS / _limits.ANIM_FRAMES))
    blob = anim.encode_anim(frames, fps=fps)
    # Versioned filenames, never reused (firmware caches assets BY PATH and
    # can hold the played file open across element clears — a fixed a/b
    # alternation 508s after cross-instance restarts). Keep the live file
    # plus one predecessor; reap older generations once safely off-screen.
    state.scene_gen += 1
    filename = f"sky_{int(time.time()) % 100000:05d}_{state.scene_gen}.anim"
    await bb.assets_upload(_limits.APP_NAME, filename, blob)
    if state.scene != scene or state.view_generation != intent:
        # Upload definitely completed and no draw was attempted.
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
        return
    try:
        async with state.display_lock:
            if state.scene != scene or state.view_generation != intent:
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                    )
                return
            stale_report_statuses = _device_report_status._stale_report_statuses(state)
            await bb.display_draw(
                _scene_payload(
                    filename,
                    led_color,
                    (
                        *_device_report_status._retired_report_status_elements(
                            stale_report_statuses
                        ),
                        # Retire the "no live weather" card in the same payload
                        # that replaces it. Elements merge by id and accumulate,
                        # so without this the notice rides on top of a good sky
                        # until its own lease runs out.
                        *_retired_stale_notice_elements(state),
                    ),
                )
            )
            _device_report_status._forget_report_statuses(state, stale_report_statuses)
            state.stale_notice_at = 0.0
            state.stale_since = 0.0
            # Commit the token while the accepted draw still owns the display
            # lane. A flash waiting on this lock must never cover a newer
            # same-scene asset merely because this bookkeeping moved later.
            state.last_drawn_at = asyncio.get_running_loop().time()
            state.current_scene_file = filename
            state.current_scene_frames = tuple(frame.copy() for frame in frames)
            state.scene_files.append(filename)
    except exceptions.BusyBarAPIError as exc:
        # 409: a BUSY/CUSTOM session owns the display. The device refused the
        # draw outright, so it never opened this file — reclaim it now, or one
        # ~113kB orphan accrues every minute for the length of the session.
        # Only on a refusal: after a transport error the draw may have landed,
        # and deleting a file the firmware holds open is the 508 trap.
        if _is_refusal(exc):
            try:
                await bb.storage_remove(
                    f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                )
            except Exception:  # noqa: BLE001 - reclaiming is best-effort
                pass
        raise
    _limits.logger.info(
        "scene updated on the bar (%s, %d frames, %.1fkB)",
        _render_status.clock_str(now),
        len(frames),
        len(blob) / 1024,
    )
    while len(state.scene_files) > 2:
        stale = state.scene_files.pop(0)
        try:
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{stale}")
        except Exception:  # noqa: BLE001 - reaping is best-effort
            pass

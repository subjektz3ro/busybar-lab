"""Wheel/button transitions and immediate feedback, independent of the main loop.

Selection intent changes before scene uploads. Narration requests identify the
current generation; synthesis never blocks a button press.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from apps.dsn_app import config as _config
from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.audio import narration as _audio_narration
from apps.dsn_app.audio import output as _audio_output
from apps.dsn_app.audio import policy as _audio_policy
from apps.dsn_app.device import display as _device_display


def encoder_delta(update: dict) -> int:
    """Wheel detents. Events are nested under `input` inside each update —
    reading them off the message itself silently never fires."""
    event = (update.get("input") or {}).get("encoder_event") or {}
    try:
        return int(event.get("delta") or 0)
    except (TypeError, ValueError):
        return 0


def is_ok_press(update: dict) -> bool:
    """OK is the wheel's down-click. Proto3 omits zero-valued fields, so an
    OK PRESS legitimately arrives as an EMPTY button_event."""
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return event.get("button") in (None, 0, "OK") and event.get("action") in (
        None,
        0,
        "PRESS",
    )


def is_ok_release(update: dict) -> bool:
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return event.get("button") in (None, 0, "OK") and event.get("action") in (
        1,
        "RELEASE",
    )


def is_start_press(update: dict) -> bool:
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return event.get("button") in (2, "START") and event.get("action") in (
        None,
        0,
        "PRESS",
    )


def release_realtime(state: _model.State) -> None:
    state.focus = None
    state.realtime_since = None
    state.rt_generation = None
    state.watch = None
    if state.view_before_lock is not None:
        state.view = state.view_before_lock
        state.view_before_lock = None


def toggle_realtime(state: _model.State, now: float | None = None) -> bool:
    """A tap keeps its old meaning and takes the user to the distance view."""
    _audio_policy.clear_narration_request(state)
    state.narration_return_view = None
    _selection.clear_network_focus(state)
    link = state.current()
    if link is None:
        return False
    locking = state.realtime_since is None
    if locking:
        if not link.light_s:
            return False
        state.completion_pending = None
        state.completion_link = None
        state.completion_generation = None
        _model.clear_led(state, _limits.LED_ARRIVAL)
        state.view_before_lock = state.view
        state.focus = link.key
        state.realtime_since = now if now is not None else time.time()
        state.rt_counter += 1
        state.rt_generation = state.rt_counter
        frozen = replace(link)
        state.watch = _model.Watch(
            link=frozen,
            started_at=state.realtime_since,
            light_s=link.light_s,
            deadline=state.realtime_since + link.light_s,
            generation=state.rt_counter,
            return_view=state.view,
            live_key=link.key,
        )
        state.view = "distance"
        _model.request_led(state, _limits.LED_LOCKED)
    else:
        release_realtime(state)
        _model.request_led(state, _limits.LED_RELEASED)
    state.dirty.set()
    return locking


def toggle_view(state: _model.State) -> str:
    _audio_policy.clear_narration_request(state)
    state.narration_return_view = None
    _selection.clear_network_focus(state)
    if state.realtime_since is not None:
        # Preserve the established watch gesture: hold compares the literal
        # distance journey with its selected-link instrument and back.
        state.view = "distance" if state.view == "instrument" else "instrument"
    else:
        try:
            index = _config.VIEW_ORDER.index(state.view)
        except ValueError:
            index = 0
        state.view = _config.VIEW_ORDER[(index + 1) % len(_config.VIEW_ORDER)]
    state.dirty.set()
    return state.view


async def _fire_ok_hold(bb, state: _model.State, pressed_at: float) -> None:
    try:
        await asyncio.sleep(_limits.OK_HOLD_S)
        if state.ok_down_at != pressed_at:
            return
        state.ok_hold_fired = True
        view = toggle_view(state)
        await _device_display.draw_readout(bb, state, view.upper())
        _limits.logger.info("view: %s", view)
    except asyncio.CancelledError:
        raise


def cancel_ok_hold(state: _model.State) -> None:
    if state.ok_hold_task is not None:
        state.ok_hold_task.cancel()
    state.ok_hold_task = None
    state.ok_down_at = None
    state.ok_hold_fired = False


async def cancel_narration(bb, state: _model.State) -> None:
    """A deliberate wheel move interrupts audio and releases its private hold."""
    active = list(state.speech_tasks)
    if not active and not state.speaking and not state.audio_stop_pending:
        return
    for task in active:
        task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)
    # STOP follows cancellation/gather so an in-flight PLAY cannot commit
    # after the stop and leave the old craft talking under the new picker.
    await _audio_output.stop_audio_bounded(bb, state, "navigation")
    state.speaking = False
    state.narration_focus = None
    state.dirty.set()


async def listen_input(bb, state: _model.State) -> None:
    """Wheel cycles links; tap follows light time; hold switches view."""
    backoff = 1.0
    while True:
        try:
            connected = asyncio.get_running_loop().time()
            async for message in bb.stream_status_ws():
                backoff = 1.0
                if not isinstance(message, dict):
                    continue
                moved = False
                for update in message.get("updates", []):
                    delta = encoder_delta(update)
                    if delta:
                        _selection.note_manual_selection(state)
                        _audio_policy.clear_narration_request(state)
                        # The first detent closes any prior Focus Lens.  A
                        # new one is armed only after the picker has rested.
                        _selection.clear_network_focus(state)
                        cancel_ok_hold(state)
                        # Gate the renderer before audio_stop or any other
                        # await can let the main loop commit under the picker.
                        state.picking = True
                        state.pick_at = asyncio.get_running_loop().time()
                        state.completion_pending = None
                        _model.clear_led(state, _limits.LED_ARRIVAL)
                        await cancel_narration(bb, state)
                        state.enc_accum += delta
                        while abs(state.enc_accum) >= _limits.DETENT_COUNTS:
                            step = 1 if state.enc_accum > 0 else -1
                            state.enc_accum -= _limits.DETENT_COUNTS * step
                            release_realtime(state)  # scrolling releases a lock
                            if state.links:
                                state.cursor = (state.cursor + step) % len(state.links)
                            moved = True
                        continue
                    if is_ok_press(update):
                        if state.ok_down_at is None:
                            pressed_at = asyncio.get_running_loop().time()
                            state.ok_down_at = pressed_at
                            state.ok_hold_fired = False
                            state.ok_hold_task = asyncio.create_task(
                                _fire_ok_hold(bb, state, pressed_at)
                            )
                    elif is_ok_release(update):
                        hold_task = state.ok_hold_task
                        if hold_task is not None:
                            hold_task.cancel()
                        if state.ok_down_at is not None and not state.ok_hold_fired:
                            selected = state.current()
                            was_locked = state.realtime_since is not None
                            from_network = state.view == "network"
                            locking = toggle_realtime(state)
                            link = state.current()
                            if not was_locked and not locking:
                                await _device_display.draw_readout(
                                    bb, state, "NO RANGE" if selected else "NO LINK"
                                )
                            elif locking and from_network and selected is not None:
                                # The global board's action target is made
                                # explicit before its Distance asset arrives.
                                await _device_display.draw_readout(
                                    bb, state, selected.craft.upper()
                                )
                            _limits.logger.info(
                                "focus: %s",
                                "real-time on " + link.key
                                if locking and link
                                else "auto-rotate",
                            )
                        state.ok_down_at = None
                        state.ok_hold_fired = False
                        state.ok_hold_task = None
                    elif is_start_press(update):
                        if state.speaking or state.speech_tasks:
                            await _device_display.draw_readout(
                                bb, state, _limits.NARRATION_BUSY
                            )
                            continue
                        fresh = _telemetry.feed_freshness(state)
                        if fresh != "fresh":
                            await _device_display.draw_readout(
                                bb, state, _device_display.feed_status_label(fresh)
                            )
                            continue
                        link = _selection.narration_target_link(state)
                        if link is None:
                            await _device_display.draw_readout(
                                bb,
                                state,
                                "OFF AIR" if state.watch is not None else "NO LINK",
                            )
                            continue
                        task = asyncio.create_task(
                            _audio_narration.speak(bb, state, link)
                        )
                        state.speech_tasks.add(task)
                        task.add_done_callback(state.speech_tasks.discard)
                if moved:
                    # Reveal-on-stop: the picker tracks the wheel with no lag,
                    # and the scene follows only once you rest. Rendering per
                    # detent would stall on an 80 KB upload. Drawn once per
                    # message, not once per detent, so a fast spin coalesces.
                    await _device_display.draw_picker(bb, state)
            cancel_ok_hold(state)
            # A clean close ends the loop without raising; back off on a
            # short-lived session or this becomes a reconnect hot loop.
            if asyncio.get_running_loop().time() - connected < 5.0:
                _limits.logger.warning("input stream closed immediately, backing off")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                _limits.logger.info("input stream closed cleanly, reconnecting")
                backoff = 1.0
        except asyncio.CancelledError:
            cancel_ok_hold(state)
            raise
        except Exception as exc:  # noqa: BLE001
            cancel_ok_hold(state)
            _limits.logger.warning("input stream dropped (%s), retrying", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

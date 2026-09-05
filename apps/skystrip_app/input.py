"""Wheel scrubbing, scene selection and report/alert button transitions.

Native readouts acknowledge intent immediately; heavy rendering and uploads
wait until the wheel rests. Request generations fence asynchronous completion.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import selection as _selection
from apps.skystrip_app import settings as _settings
from apps.skystrip_app.audio import report as _audio_report
from apps.skystrip_app.device import alerts as _device_alerts
from apps.skystrip_app.device import scrubber as _device_scrubber


def _encoder_delta(update: dict) -> int:
    event = (update.get("input") or {}).get("encoder_event") or {}
    return int(event.get("delta") or 0)


def _is_ok_press(update: dict) -> bool:
    """OK is the wheel's down-click. Proto3 omits zero enums, so an OK
    PRESS can arrive as an empty button_event."""
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return event.get("button") in (None, 0, "OK") and event.get("action") in (
        None,
        0,
        "PRESS",
    )


def _is_start_press(update: dict) -> bool:
    """Match a START press in a state-stream input update, tolerating both
    proto enum names and raw values (proto3 omits zero-valued fields)."""
    event = (update.get("input") or {}).get("button_event") or {}
    button = event.get("button")
    action = event.get("action")
    return (button in ("START", 2)) and (action in (None, 0, "PRESS"))


def _switch_position(update: dict) -> str | None:
    """Decode the physical slider event from the vendored protobuf schema.

    Proto3 omits BUSY (enum zero), so an explicitly present empty
    ``switch_event`` means BUSY.  Absence means no new ownership evidence.
    """
    inp = update.get("input") or {}
    if "switch_event" not in inp:
        return None
    event = inp.get("switch_event") or {}
    value = event.get("position", 0)
    if isinstance(value, str):
        value = value.upper()
        return value if value in {"BUSY", "CUSTOM", "OFF", "APPS", "SETTINGS"} else None
    return {0: "BUSY", 1: "CUSTOM", 2: "OFF", 3: "APPS", 4: "SETTINGS"}.get(value)


async def _start_is_owned(
    bb,
    state: _model.SkyState,
    switch_generation: int,
    switch_position: str | None,
) -> bool:
    """Resolve START without claiming a firmware BUSY/CUSTOM button press.

    An explicit OFF event is authoritative and stays on the no-I/O fast path.
    UNKNOWN is accepted only when Skystrip has committed a view and the
    device's current Busy snapshot is NOT_STARTED.  Switch evidence changing
    during the settle/query window invalidates the press; errors and timeouts
    deliberately fail closed.
    """
    if (
        state.shutting_down
        or state.switch_generation != switch_generation
        or state.switch_position != switch_position
    ):
        return False
    if switch_position == "OFF":
        return True
    if switch_position is not None or not _selection._has_committed_start_view(state):
        return False

    await asyncio.sleep(_limits.START_OWNERSHIP_SETTLE_S)
    if (
        state.shutting_down
        or state.switch_generation != switch_generation
        or state.switch_position is not None
        or not _selection._has_committed_start_view(state)
    ):
        return False
    try:
        snapshot = await asyncio.wait_for(
            bb.busy_snapshot(), timeout=_limits.START_OWNERSHIP_TIMEOUT_S
        )
        not_started = snapshot.snapshot.type == "NOT_STARTED"
    except Exception as exc:  # noqa: BLE001 - ambiguity must not claim START
        _limits.logger.warning("START ownership check failed; ignoring press: %s", exc)
        return False
    return (
        not_started
        and not state.shutting_down
        and state.switch_generation == switch_generation
        and state.switch_position is None
        and _selection._has_committed_start_view(state)
    )


async def listen_buttons(bb, state: _model.SkyState) -> None:
    """Coalesce one status message into one owned user intent.

    START is ours after an explicit OFF slider event.  Because the status
    stream is delta-only, reconnecting while the slider is already OFF leaves
    its position unknown; that case additionally requires a committed
    Skystrip view and a NOT_STARTED Busy snapshot.  Wheel gestures are app
    controls, and any available gesture first acknowledges an alert without
    also navigating the view underneath it.
    """
    backoff = 1.0
    last_press = 0.0
    pending: asyncio.Task | None = None

    def cancel_pending() -> None:
        nonlocal pending
        if pending is not None:
            pending.cancel()
            pending = None

    def invalidate_switch_evidence() -> None:
        cancel_pending()
        state.switch_position = None
        state.switch_generation += 1

    async def single_press_later(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        # Fires if no second press arrives inside the double window
        await asyncio.sleep(_limits.DOUBLE_PRESS_S)
        if (
            state.shutting_down
            or _alerts._unacknowledged_alert_active(state)
            or not await _start_is_owned(bb, state, switch_generation, switch_position)
        ):
            return
        if pending is asyncio.current_task():
            pending = None
        state.scene_idx = (state.scene_idx + 1) % len(_settings.ENABLED_SCENES)
        state.view_generation += 1
        _selection.save_scene_idx(state.scene_idx)
        state.scene_change.set()
        _limits.logger.info("START — scene: %s", state.scene)

    async def acknowledge_unknown_start(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        if not await _start_is_owned(bb, state, switch_generation, switch_position):
            return
        if pending is asyncio.current_task():
            pending = None
        if _alerts._unacknowledged_alert_active(state):
            await _device_alerts.acknowledge_alert(bb, state, "START")

    async def report_after_unknown_start(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        if not await _start_is_owned(bb, state, switch_generation, switch_position):
            return
        if pending is asyncio.current_task():
            pending = None
        if not _alerts._unacknowledged_alert_active(state):
            _model.spawn_owned(state, _audio_report.weather_report(bb, state))

    try:
        while True:
            try:
                connected_at = asyncio.get_running_loop().time()
                async for message in bb.stream_status_ws():
                    backoff = 1.0
                    if not isinstance(message, dict):
                        continue
                    updates = message.get("updates", [])
                    if not isinstance(updates, list):
                        continue

                    # Preserve input order inside one protobuf State message:
                    # a selector event before START can establish ownership,
                    # but one after START must not retroactively claim it.
                    # Encoder deltas are one detent per count and collapse to
                    # one final readout draw.
                    delta = 0
                    ok_pressed = False
                    start_pressed = False
                    start_switch_generation: int | None = None
                    start_switch_position: str | None = None
                    for update in updates:
                        if not isinstance(update, dict):
                            continue
                        inp = update.get("input") or {}
                        if "switch_event" in inp:
                            # Every selector event supersedes ownership captured
                            # by an earlier START, including OFF arriving after
                            # the press and malformed/unknown enum values.
                            cancel_pending()
                            state.switch_generation += 1
                            state.switch_position = _switch_position(update)
                        delta += _encoder_delta(update)
                        ok_pressed = ok_pressed or _is_ok_press(update)
                        is_start = _is_start_press(update)
                        if is_start and not start_pressed:
                            start_switch_generation = state.switch_generation
                            start_switch_position = state.switch_position
                        start_pressed = start_pressed or is_start

                    active_alert = _alerts._unacknowledged_alert_active(state)
                    if active_alert and (delta != 0 or ok_pressed):
                        cancel_pending()
                        reason = "wheel" if delta else "wheel click"
                        await _device_alerts.acknowledge_alert(bb, state, reason)
                        continue
                    if active_alert and start_pressed:
                        assert start_switch_generation is not None
                        switch_generation = start_switch_generation
                        position = start_switch_position
                        if (
                            state.switch_generation != switch_generation
                            or state.switch_position != position
                        ):
                            continue
                        if position == "OFF":
                            cancel_pending()
                            await _device_alerts.acknowledge_alert(bb, state, "START")
                        elif position is None and _selection._has_committed_start_view(
                            state
                        ):
                            cancel_pending()
                            pending = _model.spawn_owned(
                                state,
                                acknowledge_unknown_start(switch_generation, position),
                            )
                        # An alert gesture is consumed even when ownership
                        # cannot be established; it must never navigate below.
                        continue

                    meta = state.timeline_meta
                    if delta and meta is not None:
                        if state.scrub_slot is None:
                            here = datetime.now(_settings.TZ)
                            state.scrub_slot = max(
                                0,
                                min(
                                    _limits.TIMELINE_SLOTS - 1,
                                    round(
                                        (here - meta["start"]).total_seconds()
                                        / _limits.TIMELINE_STEP_S
                                    ),
                                ),
                            )
                        state.scrub_slot = max(
                            0,
                            min(_limits.TIMELINE_SLOTS - 1, state.scrub_slot + delta),
                        )
                        state.scrub_touched = asyncio.get_running_loop().time()
                        state.view_generation += 1
                        state.revealed = False

                    if ok_pressed and state.scrub_slot is not None:
                        state.scrub_slot = None
                        state.revealed = False
                        state.view_generation += 1
                        _limits.logger.info("wheel click — back to now")
                        try:
                            await _device_scrubber.draw_scrub_readout(
                                bb, state, "NOW", timeout=1
                            )
                        except Exception as exc:  # noqa: BLE001
                            _limits.logger.debug("NOW readout yielded: %s", exc)
                    elif delta and meta is not None and state.scrub_slot is not None:
                        try:
                            await _device_scrubber.draw_scrub_readout(
                                bb,
                                state,
                                _device_scrubber._slot_label(meta, state.scrub_slot),
                            )
                        except Exception as exc:  # noqa: BLE001
                            _limits.logger.debug("time readout yielded: %s", exc)

                    if not start_pressed:
                        continue
                    assert start_switch_generation is not None
                    switch_generation = start_switch_generation
                    position = start_switch_position
                    if (
                        state.switch_generation != switch_generation
                        or state.switch_position != position
                    ):
                        continue
                    if position not in (None, "OFF"):
                        continue
                    if position is None and not _selection._has_committed_start_view(
                        state
                    ):
                        continue
                    now = asyncio.get_running_loop().time()
                    if now - last_press < _limits.START_BOUNCE_S:
                        continue
                    last_press = now
                    if pending is not None and not pending.done():
                        pending.cancel()
                        pending = None
                        if position == "OFF":
                            _model.spawn_owned(
                                state, _audio_report.weather_report(bb, state)
                            )
                        else:
                            pending = _model.spawn_owned(
                                state,
                                report_after_unknown_start(switch_generation, position),
                            )
                    else:
                        pending = _model.spawn_owned(
                            state,
                            single_press_later(switch_generation, position),
                        )

                invalidate_switch_evidence()
                # A clean close ends the loop without raising; a short session
                # means the bar isn't ready, so back off rather than spin.
                if asyncio.get_running_loop().time() - connected_at < 5.0:
                    _limits.logger.warning(
                        "button stream closed immediately, backing off"
                    )
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    _limits.logger.info("button stream closed cleanly, reconnecting")
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                invalidate_switch_evidence()
                _limits.logger.warning("button stream dropped (%s), retrying", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        cancel_pending()

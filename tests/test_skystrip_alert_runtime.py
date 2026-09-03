"""Runtime contracts for Skystrip's secondary severe-weather alert layer.

These tests use deterministic in-memory BUSY Bar fakes.  They deliberately
exercise ordering at ``await`` boundaries: no network, speech engine, device,
or wall-clock sleep participates.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

import pytest
from busylib import exceptions, types

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
skystrip = pytest.importorskip("skystrip")


def _extreme_alert(identifier: str = "urn:alert:a", event: str = "Tornado Warning"):
    """Build the production alert type once the alert-domain module exists."""
    from busybar_dev.weather_alerts import Alert

    now = datetime.now().astimezone()
    return Alert(
        identifier=identifier,
        references=(),
        event=event,
        headline=event,
        status="Actual",
        message_type="Alert",
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
        effective=now - timedelta(minutes=1),
        onset=now - timedelta(minutes=1),
        expires=now + timedelta(minutes=20),
        ends=now + timedelta(minutes=20),
    )


def _arm(state, *, identifier: str = "urn:alert:a") -> None:
    """Populate both the legacy presentation fields and typed runtime state."""
    # Alert-runtime tests start from a resident, truthful live scene.  Tests
    # for the expired-weather restore boundary live with the weather contracts.
    state.weather_ready.set()
    state.weather_updated_at = asyncio.get_running_loop().time()
    state.weather.severe = True
    state.weather.severe_event = "Tornado Warning"
    state.alert_acked = False
    state.switch_position = "OFF"
    state.siren_file = "siren_test.snd"
    state.alert_asset_file = "alert_test.anim"
    try:
        alert = _extreme_alert(identifier)
    except ImportError:
        return
    state.visual_alert = alert
    state.siren_alert = alert
    state.alert_generation = max(getattr(state, "alert_generation", 0), 1)


def _severe_thunderstorm_alert(identifier: str = "urn:alert:severe"):
    return replace(
        _extreme_alert(identifier, "Severe Thunderstorm Warning"),
        severity="Severe",
    )


def _clear(state) -> None:
    state.weather.severe = False
    state.weather.severe_event = ""
    if hasattr(state, "visual_alert"):
        state.visual_alert = None
    if hasattr(state, "siren_alert"):
        state.siren_alert = None
    if hasattr(state, "alert_generation"):
        state.alert_generation += 1


class InputBar:
    """Deliver one status message and expose when the listener asks for more."""

    def __init__(self, message: dict):
        self.message = message
        self.consumed = asyncio.Event()
        self.draws = []
        self.clears = 0
        self.uploads = []
        self.removals = []
        self.stops = 0
        self.plays = []
        self.operations: list[str] = []

    async def stream_status_ws(self):
        yield self.message
        # Resuming the generator proves every update in the yielded message
        # has been processed, including any awaited device operation.
        self.consumed.set()
        await asyncio.Event().wait()

    async def display_draw(self, payload):
        self.draws.append(payload)
        self.operations.append("DRAW")

    async def display_clear(self, *, application_name: str):
        self.clears += 1
        self.operations.append("CLEAR")

    async def audio_stop(self):
        self.stops += 1
        self.operations.append("STOP")

    async def audio_play(self, *, application_name: str, path: str):
        self.plays.append(path)
        self.operations.append("PLAY")

    async def assets_upload(self, application_name: str, name: str, blob: bytes):
        self.uploads.append((application_name, name, blob))

    async def storage_remove(self, path: str):
        self.removals.append(path)


class InputSequenceBar(InputBar):
    """Deliver several status messages and signal after all were handled."""

    def __init__(self, messages: list[dict]):
        super().__init__({})
        self.messages = messages

    async def stream_status_ws(self):
        for message in self.messages:
            yield message
        # Reaching this point proves the listener finished every yielded
        # message, including the device I/O used to acknowledge an alert.
        self.consumed.set()
        await asyncio.Event().wait()


async def _run_one_input(bb: InputBar, state) -> None:
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.consumed.wait(), 0.3)
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


class SnapshotInputBar(InputBar):
    """Return the real vendored BusySnapshot shape and expose query progress."""

    def __init__(
        self,
        message: dict,
        snapshot_type: str = "NOT_STARTED",
        error: Exception | None = None,
    ):
        super().__init__(message)
        self.snapshot_type = snapshot_type
        self.snapshot_error = error
        self.snapshot_calls = 0
        self.snapshot_started = asyncio.Event()
        self.snapshot_finished = asyncio.Event()
        self.stop_called = asyncio.Event()

    async def busy_snapshot(self):
        self.snapshot_calls += 1
        self.snapshot_started.set()
        try:
            await asyncio.sleep(0)
            if self.snapshot_error is not None:
                raise self.snapshot_error
            snapshot = {"type": self.snapshot_type}
            if self.snapshot_type == "INFINITE":
                snapshot.update(card_id="test", is_paused=False)
            return types.BusySnapshot.model_validate({
                "snapshot": snapshot,
                "snapshot_timestamp_ms": 1,
            })
        finally:
            self.snapshot_finished.set()

    async def audio_stop(self):
        await super().audio_stop()
        self.stop_called.set()


async def _run_until_scene_change(bb: InputBar, state) -> None:
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(state.scene_change.wait(), 0.5)
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


@pytest.mark.parametrize("update", [
    {"input": {"encoder_event": {"delta": 1}}},
    {"input": {"button_event": {}}},
    {"input": {"button_event": {"button": "START", "action": "PRESS"}}},
])
async def test_any_available_control_acknowledges_without_also_navigating(update):
    state = skystrip.SkyState()
    _arm(state)
    # Model a siren that has actually committed.  Merely having an eligible
    # alert is not audio ownership: audio_stop is global on the device.
    state.audio_owner = "alert"
    state.scrub_slot = 20
    state.revealed = True
    state.last_reveal = {
        "eid": "rv7", "slot": 20, "fname": "timeline.anim", "section": "s20"}
    original_view = (state.scrub_slot, state.revealed, dict(state.last_reveal))
    bb = InputBar({"updates": [update]})

    await _run_one_input(bb, state)

    assert state.alert_acked is True
    assert bb.stops == 1
    assert (state.scrub_slot, state.revealed, state.last_reveal) == original_view, (
        "acknowledgement must not also turn/click/cycle the underlying view")
    assert state.scrub_touched > 0, (
        "the restored Time Machine view would snap to NOW immediately"
    )


@pytest.mark.parametrize("switch_position", [None, "BUSY", "CUSTOM"])
async def test_start_fails_closed_without_off_or_a_committed_unknown_view(
        switch_position):
    state = skystrip.SkyState()
    _arm(state)
    state.switch_position = switch_position
    before = (state.scene_idx, state.scrub_slot, state.alert_acked)
    bb = InputBar({"updates": [{"input": {"button_event": {
        "button": "START", "action": "PRESS"}}}]})

    await _run_one_input(bb, state)

    assert (state.scene_idx, state.scrub_slot, state.alert_acked) == before
    assert bb.stops == 0
    assert bb.draws == []


async def test_explicit_off_single_start_cycles_scene_without_snapshot(
        monkeypatch):
    """The explicit selector event remains the authoritative fast path."""
    state = skystrip.SkyState()
    state.switch_position = "OFF"
    before = state.scene_idx
    bb = InputBar({"updates": [{"input": {"button_event": {
        "button": "START", "action": "PRESS"}}}]})
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)

    await _run_until_scene_change(bb, state)

    assert state.scene_idx == (before + 1) % len(skystrip.ENABLED_SCENES)
    # InputBar intentionally has no busy_snapshot method. Reaching the scene
    # proves explicit OFF did not add a device round trip.


async def test_start_before_off_in_one_status_message_is_not_retroactively_owned(
        monkeypatch):
    state = skystrip.SkyState()
    state.current_scene_file = "sky.anim"
    before = state.scene_idx
    bb = SnapshotInputBar({"updates": [
        {"input": {"button_event": {
            "button": "START", "action": "PRESS"}}},
        {"input": {"switch_event": {"position": "OFF"}}},
    ]})
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.consumed.wait(), 0.5)
        await asyncio.sleep(0)
        assert state.switch_position == "OFF"
        assert state.scene_idx == before
        assert not state.scene_change.is_set()
        assert bb.snapshot_calls == 0
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_off_before_start_in_one_status_message_owns_the_press(monkeypatch):
    state = skystrip.SkyState()
    before = state.scene_idx
    bb = InputBar({"updates": [
        {"input": {"switch_event": {"position": "OFF"}}},
        {"input": {"button_event": {
            "button": "START", "action": "PRESS"}}},
    ]})
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)

    await _run_until_scene_change(bb, state)

    assert state.switch_position == "OFF"
    assert state.scene_idx == (before + 1) % len(skystrip.ENABLED_SCENES)


async def test_unknown_start_cycles_after_committed_view_and_not_started_snapshot(
        monkeypatch):
    state = skystrip.SkyState()
    state.current_scene_file = "sky.anim"  # set only after an accepted draw
    before = state.scene_idx
    bb = SnapshotInputBar({"updates": [{"input": {"button_event": {
        "button": "START", "action": "PRESS"}}}]})
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)

    await _run_until_scene_change(bb, state)

    assert state.scene_idx == (before + 1) % len(skystrip.ENABLED_SCENES)
    assert bb.snapshot_calls == 1


@pytest.mark.parametrize(("snapshot_type", "expected_report"), [
    ("NOT_STARTED", True),
    ("INFINITE", False),
])
async def test_unknown_double_start_report_requires_not_started_snapshot(
        monkeypatch, snapshot_type, expected_report):
    state = skystrip.SkyState()
    state.current_scene_file = "sky.anim"
    report_called = asyncio.Event()
    report_calls = 0

    async def fake_weather_report(_bb, _state):
        nonlocal report_calls
        report_calls += 1
        report_called.set()

    class DoublePressBar(SnapshotInputBar):
        async def stream_status_ws(self):
            press = {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            yield press
            yield press
            self.consumed.set()
            await asyncio.Event().wait()

    bb = DoublePressBar({"updates": []}, snapshot_type=snapshot_type)
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 10)
    monkeypatch.setattr(skystrip, "START_BOUNCE_S", 0)
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    monkeypatch.setattr(skystrip, "weather_report", fake_weather_report)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.snapshot_finished.wait(), 0.5)
        if expected_report:
            await asyncio.wait_for(report_called.wait(), 0.5)
        else:
            await asyncio.sleep(0)
            assert not report_called.is_set()
        assert report_calls == int(expected_report)
        assert bb.snapshot_calls == 1
        assert not state.scene_change.is_set()
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_unknown_start_does_not_cycle_during_active_busy_snapshot(
        monkeypatch):
    state = skystrip.SkyState()
    state.current_scene_file = "sky.anim"
    before = state.scene_idx
    bb = SnapshotInputBar(
        {"updates": [{"input": {"button_event": {
            "button": "START", "action": "PRESS"}}}]},
        snapshot_type="INFINITE",
    )
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.snapshot_finished.wait(), 0.5)
        await asyncio.sleep(0)
        assert state.scene_idx == before
        assert not state.scene_change.is_set()
        assert bb.snapshot_calls == 1
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_alert_first_unknown_start_acks_only_when_busy_is_not_started(
        monkeypatch):
    state = skystrip.SkyState()
    _arm(state)
    state.switch_position = None
    state.alert_drawn_generation = state.alert_generation
    state.audio_owner = "alert"
    bb = SnapshotInputBar({"updates": [{"input": {"button_event": {
        "button": "START", "action": "PRESS"}}}]})
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.stop_called.wait(), 0.5)
        assert state.alert_acked is True
        assert bb.snapshot_calls == 1
        assert bb.stops == 1
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


@pytest.mark.parametrize("snapshot_type,error", [
    ("INFINITE", None),
    ("NOT_STARTED", RuntimeError("snapshot unavailable")),
])
async def test_unknown_alert_start_fails_closed_without_not_started_snapshot(
        monkeypatch, snapshot_type, error):
    state = skystrip.SkyState()
    _arm(state)
    state.switch_position = None
    state.alert_drawn_generation = state.alert_generation
    state.audio_owner = "alert"
    bb = SnapshotInputBar(
        {"updates": [{"input": {"button_event": {
            "button": "START", "action": "PRESS"}}}]},
        snapshot_type=snapshot_type,
        error=error,
    )
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.snapshot_finished.wait(), 0.5)
        await asyncio.sleep(0)
        assert state.alert_acked is False
        assert bb.stops == 0
        assert bb.snapshot_calls == 1
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


@pytest.mark.parametrize("late_position", ["BUSY", "OFF"])
async def test_switch_event_cannot_reclassify_an_unknown_start_in_flight(
        monkeypatch, late_position):
    state = skystrip.SkyState()
    state.current_scene_file = "sky.anim"
    before = state.scene_idx

    class RacingBar(SnapshotInputBar):
        def __init__(self):
            super().__init__({"updates": []})
            self.release_snapshot = asyncio.Event()

        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await self.snapshot_started.wait()
            yield {"updates": [{"input": {"switch_event": {
                "position": late_position}}}]}
            self.consumed.set()
            await asyncio.Event().wait()

        async def busy_snapshot(self):
            self.snapshot_calls += 1
            self.snapshot_started.set()
            try:
                await self.release_snapshot.wait()
                return types.BusySnapshot.model_validate({
                    "snapshot": {"type": "NOT_STARTED"},
                    "snapshot_timestamp_ms": 1,
                })
            finally:
                self.snapshot_finished.set()

    bb = RacingBar()
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 0)
    monkeypatch.setattr(skystrip, "START_OWNERSHIP_SETTLE_S", 0)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.consumed.wait(), 0.5)
        await asyncio.wait_for(bb.snapshot_finished.wait(), 0.5)
        bb.release_snapshot.set()
        await asyncio.sleep(0)
        assert state.switch_position == late_position
        assert state.scene_idx == before
        assert not state.scene_change.is_set()
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


async def test_status_stream_gap_revokes_explicit_off_and_pending_start(
        monkeypatch):
    state = skystrip.SkyState()
    state.switch_position = "OFF"
    before = state.scene_idx

    class ClosingBar(InputBar):
        def __init__(self):
            super().__init__({"updates": []})
            self.closed = asyncio.Event()

        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            self.closed.set()

    bb = ClosingBar()
    monkeypatch.setattr(skystrip, "DOUBLE_PRESS_S", 10)
    monkeypatch.setattr(skystrip, "save_scene_idx", lambda _idx: None)
    listening = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.closed.wait(), 0.5)
        await asyncio.sleep(0)
        assert state.switch_position is None
        assert state.switch_generation == 1
        assert state.scene_idx == before
        assert not state.scene_change.is_set()
    finally:
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)


@pytest.mark.parametrize(("position", "expected"), [
    ("BUSY", "BUSY"),
    ("CUSTOM", "CUSTOM"),
    (0, "BUSY"),
    (1, "CUSTOM"),
])
async def test_busy_and_custom_switch_events_never_ack_or_touch_global_audio(
        position, expected):
    state = skystrip.SkyState()
    _arm(state)
    before = (state.scene_idx, state.scrub_slot, state.alert_acked)
    bb = InputBar({"updates": [{"input": {"switch_event": {
        "position": position}}}]})

    await _run_one_input(bb, state)

    assert (state.scene_idx, state.scrub_slot, state.alert_acked) == before
    assert state.switch_position == expected
    assert bb.stops == 0
    assert bb.draws == []


class ResidentBar(InputBar):
    """Small model of merge-by-id and native whole-second element expiry."""

    def __init__(self, message: dict | None = None):
        super().__init__(message or {"updates": []})
        self.clock = 0.0
        self.resident: dict[str, float] = {}

    def prime(self, element_id: str, timeout: int = 60) -> None:
        self.resident[element_id] = self.clock + timeout

    async def display_draw(self, payload):
        await super().display_draw(payload)
        for element in payload.elements:
            timeout = getattr(element, "timeout", None)
            self.resident[element.id] = (
                float("inf") if timeout is None else self.clock + timeout)

    async def display_clear(self, *, application_name: str):
        await super().display_clear(application_name=application_name)
        self.resident.clear()

    def advance(self, seconds: float) -> set[str]:
        self.clock += seconds
        return {eid for eid, expires in self.resident.items()
                if expires > self.clock}


@pytest.mark.parametrize("revealed,expected", [
    (False, {"sky"}),
    # The live sky remains underneath the opaque reveal so its native expiry
    # cannot leave the app blank; creation order still makes rv7 the view.
    (True, {"sky", "rv7"}),
])
async def test_ack_retires_every_older_layer_and_restores_the_exact_view(
        revealed, expected):
    state = skystrip.SkyState()
    _arm(state)
    state.readout_gen = 3
    state.revealed = revealed
    state.scrub_slot = 20 if revealed else None
    state.last_reveal = {
        "eid": "rv7", "slot": 20, "fname": "timeline.anim", "section": "s20"}
    state.scene_files = ["sky.anim"]
    state.current_scene_file = "sky.anim"

    bb = ResidentBar()
    bb.prime("sky")
    bb.prime("rv7")
    bb.prime("ror3", timeout=8)
    bb.prime("rot3", timeout=8)
    bb.prime("alert", timeout=8)

    await skystrip.acknowledge_alert(bb, state, "test")

    # Stable ids are creation-ordered.  An opaque alert created above ``sky``
    # cannot be hidden immediately by changing only its timeout, so dismissal
    # clears the app and rebuilds exactly one selected full-screen view.
    assert bb.clears == 1
    assert bb.advance(1.01) == expected
    assert bb.advance(5.0) == expected, "an interrupted older layer resurfaced"
    assert state.last_reveal is not None if revealed else state.last_reveal is None


class GatedAlarmBar(InputBar):
    def __init__(self, message: dict):
        super().__init__(message)
        self.alarm_draw_started = asyncio.Event()
        self.release_alarm_draw = asyncio.Event()
        self.stop_called = asyncio.Event()
        self.order: list[str] = []
        self._gated = False

    async def display_draw(self, payload):
        self.draws.append(payload)
        ids = {element.id for element in payload.elements}
        if "alert" in ids and not self._gated:
            self._gated = True
            self.order.append("ALARM_START")
            self.alarm_draw_started.set()
            await self.release_alarm_draw.wait()
            self.order.append("ALARM_COMMIT")
        else:
            self.order.append("DRAW")

    async def display_clear(self, *, application_name: str):
        self.clears += 1
        self.order.append("CLEAR")

    async def audio_stop(self):
        self.stops += 1
        self.order.append("STOP")
        self.stop_called.set()

    async def audio_play(self, *, application_name: str, path: str):
        self.plays.append(path)
        self.order.append("PLAY")


async def test_ack_during_a_gated_alarm_draw_cannot_be_overtaken_by_play():
    state = skystrip.SkyState()
    _arm(state)
    # A lost/late PLAY response means audio may already have committed even
    # while the alert draw is gated.  That ambiguous ownership requires STOP.
    state.audio_owner = "alert-pending"
    bb = GatedAlarmBar({"updates": [{"input": {"button_event": {
        "button": "START", "action": "PRESS"}}}]})
    alarm = asyncio.create_task(skystrip.severe_alarm(bb, state))
    await asyncio.wait_for(bb.alarm_draw_started.wait(), 0.3)
    listener = asyncio.create_task(skystrip.listen_buttons(bb, state))
    try:
        await asyncio.wait_for(bb.stop_called.wait(), 0.3)
        assert bb.order == ["ALARM_START", "STOP"]
        bb.release_alarm_draw.set()
        await asyncio.wait_for(bb.consumed.wait(), 0.3)
        await asyncio.sleep(0)
        assert bb.plays == [], "an obsolete alarm PLAY crossed acknowledgement STOP"
        assert bb.order.index("ALARM_COMMIT") < bb.order.index("CLEAR"), (
            "dismissal cleared before the in-flight alert could commit")
    finally:
        alarm.cancel()
        listener.cancel()
        await asyncio.gather(alarm, listener, return_exceptions=True)


class AlarmLifecycleBar:
    def __init__(self):
        self.draws = []
        self.order: list[str] = []
        self.played = asyncio.Event()
        self.stopped = asyncio.Event()

    async def display_draw(self, payload):
        self.draws.append(payload)

    async def audio_play(self, *, application_name: str, path: str):
        self.order.append("PLAY")
        self.played.set()

    async def audio_stop(self):
        self.order.append("STOP")
        self.stopped.set()


async def test_warning_clear_stops_audio_after_the_last_play():
    state = skystrip.SkyState()
    _arm(state)
    bb = AlarmLifecycleBar()
    alarm = asyncio.create_task(skystrip.severe_alarm(bb, state))
    try:
        await asyncio.wait_for(bb.played.wait(), 0.3)
        _clear(state)
        skystrip._signal_alert_change(state)
        await asyncio.wait_for(bb.stopped.wait(), 0.3)
        play_index = max(i for i, operation in enumerate(bb.order)
                         if operation == "PLAY")
        assert "STOP" in bb.order[play_index + 1:]
    finally:
        alarm.cancel()
        await asyncio.gather(alarm, return_exceptions=True)


async def test_alert_wait_cannot_clear_a_transition_racing_its_setup():
    state = skystrip.SkyState()
    observed = state.alert_wake_generation

    class RacingEvent(asyncio.Event):
        fired = False

        def clear(self):
            if not self.fired:
                self.fired = True
                skystrip._signal_alert_change(state)
            super().clear()

    state.alert_changed = RacingEvent()
    await asyncio.wait_for(
        skystrip._wait_for_alert_change(state, 10.0, observed),
        0.3,
    )
    assert state.alert_wake_generation == observed + 1


async def test_failed_alert_marquee_upload_uses_bounded_retry_backoff(
        monkeypatch):
    state = skystrip.SkyState()
    _arm(state)
    delays: list[float] = []

    async def failed_asset(*_args, **_kwargs):
        return None

    async def capture_wait(_state, timeout, _observed):
        delays.append(timeout)
        raise asyncio.CancelledError

    monkeypatch.setattr(skystrip, "ensure_alert_asset", failed_asset)
    monkeypatch.setattr(skystrip, "_wait_for_alert_change", capture_wait)

    with pytest.raises(asyncio.CancelledError):
        await skystrip.severe_alarm(object(), state)
    assert delays == [skystrip.ALERT_ASSET_RETRY_S]
    assert delays[0] >= 2.0


async def test_shutdown_stop_is_serialized_after_a_cancellation_resistant_play():
    state = skystrip.SkyState()
    _arm(state)

    class LatePlayBar(AlarmLifecycleBar):
        def __init__(self):
            super().__init__()
            self.play_entered = asyncio.Event()
            self.play_cancelled = asyncio.Event()
            self.release_play = asyncio.Event()

        async def audio_play(self, *, application_name: str, path: str):
            self.play_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # A request may commit after local cancellation.  Shutdown's
                # STOP must queue behind that request, never race ahead of it.
                self.play_cancelled.set()
                await self.release_play.wait()
                self.order.append("PLAY")
                raise

    bb = LatePlayBar()
    alarm = asyncio.create_task(skystrip.severe_alarm(bb, state))
    await asyncio.wait_for(bb.play_entered.wait(), 0.3)
    alarm.cancel()
    await asyncio.wait_for(bb.play_cancelled.wait(), 0.3)
    assert bb.order == []

    bb.release_play.set()
    await asyncio.gather(alarm, return_exceptions=True)
    assert bb.order == ["PLAY", "STOP"]


@pytest.mark.parametrize("refused_operation", ["clear", "draw"])
async def test_ack_409_keeps_the_restore_intent_for_the_next_attempt(
        refused_operation):
    state = skystrip.SkyState()
    _arm(state)
    state.revealed = False
    state.scrub_slot = None
    state.last_reveal = {
        "eid": "rv7", "slot": 20, "fname": "timeline.anim", "section": "s20"}
    state.scene_files = ["sky.anim"]
    state.current_scene_file = "sky.anim"

    class RefuseOnceBar(ResidentBar):
        def __init__(self):
            super().__init__()
            self.clear_attempts = 0
            self.draw_attempts = 0

        async def display_draw(self, payload):
            self.draw_attempts += 1
            if refused_operation == "draw" and self.draw_attempts == 1:
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)
            await super().display_draw(payload)

        async def display_clear(self, *, application_name: str):
            self.clear_attempts += 1
            if refused_operation == "clear" and self.clear_attempts == 1:
                raise exceptions.BusyBarAPIError(
                    "Not cleared due to low priority", status_code=409)
            await super().display_clear(application_name=application_name)

    bb = RefuseOnceBar()
    bb.prime("sky")
    bb.prime("rv7")
    bb.prime("alert", timeout=8)

    await skystrip.acknowledge_alert(bb, state, "first")
    await skystrip.acknowledge_alert(bb, state, "retry")

    expected_attempts = (2, 1) if refused_operation == "clear" else (2, 2)
    assert (bb.clear_attempts, bb.draw_attempts) == expected_attempts, (
        "409 consumed the pending alert-dismiss intent")
    assert bb.advance(1.01) == {"sky"}


async def test_multiple_input_updates_are_coalesced_into_one_readout_draw():
    state = skystrip.SkyState()
    state.switch_position = "OFF"
    state.scrub_slot = 20
    state.timeline_meta = {
        "start": datetime.now(skystrip.TZ) - timedelta(hours=10),
        "scene": state.scene,
        "built": datetime.now(skystrip.TZ),
        "file": "timeline.anim",
    }
    bb = InputBar({"updates": [
        {"input": {"encoder_event": {"delta": 1}}},
        {"input": {"encoder_event": {"delta": 2}}},
        {"input": {"encoder_event": {"delta": -1}}},
    ]})

    await _run_one_input(bb, state)

    assert state.scrub_slot == 22
    assert len(bb.draws) == 1, "one WebSocket message caused per-update redraws"


async def test_wheel_scrubs_after_its_first_gesture_acknowledges_an_alert():
    state = skystrip.SkyState()
    _arm(state)
    state.scrub_slot = 20
    state.timeline_meta = {
        "start": datetime.now(skystrip.TZ) - timedelta(hours=10),
        "scene": state.scene,
        "built": datetime.now(skystrip.TZ),
        "file": "timeline.anim",
    }
    turn = {"updates": [{"input": {"encoder_event": {"delta": 1}}}]}
    bb = InputSequenceBar([turn, turn])

    await _run_one_input(bb, state)

    assert state.alert_acked is True
    assert state.scrub_slot == 21, (
        "the alert acknowledgement kept consuming later wheel turns"
    )


def test_acknowledged_alert_allows_the_time_machine_reveal_gate():
    state = skystrip.SkyState()
    state.scrub_slot = 20
    state.scrub_touched = 10.0
    state.timeline_meta = {"file": "timeline.anim"}
    state.visual_alert = _severe_thunderstorm_alert()
    state.weather.severe = True

    assert not skystrip._scrub_reveal_ready(
        state, 10.0 + skystrip.REVEAL_REST_S + 1
    )

    state.alert_acked = True

    assert skystrip._scrub_reveal_ready(
        state, 10.0 + skystrip.REVEAL_REST_S + 1
    ), "an acknowledged alert blocked every past/future scene reveal"


async def test_rearmed_alert_aborts_an_inflight_animated_reveal(monkeypatch):
    state = skystrip.SkyState()
    state.visual_alert = _severe_thunderstorm_alert("urn:alert:old")
    state.weather.severe = True
    state.alert_acked = True
    state.scrub_slot = 20
    state.reveal_pending = True
    state.timeline_meta = {
        "start": datetime.now(skystrip.TZ) - timedelta(hours=10),
        "scene": state.scene,
        "built": datetime.now(skystrip.TZ),
        "file": "timeline.anim",
    }

    class RearmingUploadBar(InputBar):
        async def assets_upload(
            self, application_name: str, name: str, blob: bytes
        ):
            await super().assets_upload(application_name, name, blob)
            state.visual_alert = _severe_thunderstorm_alert("urn:alert:new")
            state.alert_acked = False

    bb = RearmingUploadBar({"updates": []})
    monkeypatch.setattr(skystrip, "render_loop_frames", lambda *_a, **_k: [])
    monkeypatch.setattr(skystrip.anim, "encode_anim", lambda *_a, **_k: b"anim")

    await skystrip.animate_reveal(bb, state, 20, initial=True)

    assert len(bb.uploads) == 1
    uploaded_name = bb.uploads[0][1]
    assert bb.draws == [], "the future scene covered a newly re-armed alert"
    assert bb.removals == [f"/ext/user_assets/{skystrip.APP_NAME}/{uploaded_name}"]
    assert state.revealed is False
    assert state.reveal_pending is False


async def test_unacknowledged_alert_blocks_a_racing_scrub_readout():
    state = skystrip.SkyState()
    _arm(state)
    bb = InputBar({"updates": []})

    await skystrip.draw_scrub_readout(bb, state, "TMW 3:00 PM")

    assert bb.draws == [], "a stale wheel readout covered the active alert card"


def test_new_extreme_episode_rearms_after_the_previous_episode_was_acked():
    previous = _extreme_alert("urn:alert:a")
    replacement = _extreme_alert("urn:alert:b")
    state = skystrip.SkyState()
    skystrip._apply_alert_selection(state, previous, previous, (previous,))
    state.alert_acked = True
    alert_generation = state.alert_generation
    audio_generation = state.audio_generation

    skystrip._apply_alert_selection(
        state, replacement, replacement, (replacement,))

    assert state.visual_alert is replacement
    assert state.siren_alert is replacement
    assert state.alert_acked is False
    assert state.alert_generation == alert_generation + 1
    assert state.audio_generation == audio_generation + 1


async def test_fresh_non_extreme_warning_never_issues_a_global_audio_stop():
    """Visual-only warnings must not interfere with audio they do not own.

    ``audio_stop`` is global on the device.  Calling it merely because a
    Severe Thunderstorm Warning arrived or was acknowledged could silence a
    BUSY/CUSTOM session even though Skystrip never sounded its siren.
    """
    state = skystrip.SkyState()
    warning = _severe_thunderstorm_alert()

    skystrip._apply_alert_selection(state, warning, None, (warning,))

    # Generation invalidation is local and safe; the global STOP is not.
    assert state.audio_generation > 0
    assert state.audio_stop_pending is False
    bb = InputBar({"updates": []})
    await skystrip.acknowledge_alert(bb, state, "test")
    assert bb.stops == 0
    assert bb.plays == []


async def test_definitively_refused_play_does_not_create_global_stop_intent():
    """HTTP 409 means PLAY did not commit; STOP would target another owner."""
    state = skystrip.SkyState()

    class RefusedPlayBar:
        async def audio_play(self, *, application_name: str, path: str):
            raise exceptions.BusyBarAPIError(
                "Not played due to low priority", status_code=409)

    with pytest.raises(exceptions.BusyBarAPIError) as caught:
        await skystrip._play_audio(
            RefusedPlayBar(),
            state,
            "siren.snd",
            "alert",
            lambda: True,
        )

    assert caught.value.status_code == 409
    assert state.audio_owner is None
    assert state.audio_stop_pending is False


async def test_cap_deadline_clears_before_the_next_http_request_can_hang(
        monkeypatch):
    """A request crossing CAP expiry cannot extend the siren by 20 seconds."""
    now = datetime.now().astimezone()
    deadline = now + timedelta(seconds=0.05)
    expiring = replace(
        _extreme_alert(),
        expires=deadline,
        ends=deadline,
    )
    state = skystrip.SkyState()
    state.active_alerts = (expiring,)
    state.visual_alert = expiring
    state.siren_alert = expiring
    state.weather.severe = True
    state.weather.severe_event = expiring.event
    get_entered = asyncio.Event()

    class ControlledDatetime:
        current = None

        @classmethod
        def now(cls, tz=None):
            return cls.current if tz is None else cls.current.astimezone(tz)

    ControlledDatetime.current = now

    class HangingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            get_entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        skystrip.httpx,
        "AsyncClient",
        lambda **_kwargs: HangingClient(),
    )
    monkeypatch.setattr(skystrip, "datetime", ControlledDatetime)
    poller = asyncio.create_task(skystrip.poll_alerts(state))
    try:
        await asyncio.wait_for(get_entered.wait(), 0.3)
        # The request began while the alert was active. Advance the CAP clock
        # past its deadline while that transport remains permanently hung.
        ControlledDatetime.current = deadline + timedelta(seconds=1)
        await asyncio.wait_for(state.alert_changed.wait(), 0.3)
        assert state.active_alerts == ()
        assert state.visual_alert is None
        assert state.siren_alert is None
        assert state.weather.severe is False
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


async def test_outside_point_coverage_clears_alerts_without_cap_request(
        monkeypatch):
    """A failed NWS point lookup is the boundary for every NWS feature."""
    state = skystrip.SkyState()
    _arm(state)
    alert = state.visual_alert
    assert alert is not None
    state.active_alerts = (alert,)
    state.nws_point_covered = False
    state.nws_point_checked.set()
    gets: list[tuple[tuple, dict]] = []

    class NoRequestClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *args, **kwargs):
            gets.append((args, kwargs))
            raise AssertionError("CAP must stand down outside /points coverage")

    monkeypatch.setattr(
        skystrip.httpx,
        "AsyncClient",
        lambda **_kwargs: NoRequestClient(),
    )
    poller = asyncio.create_task(
        skystrip.poll_alerts(state, wait_for_point_check=True))
    try:
        for _ in range(20):
            if state.alert_known:
                break
            await asyncio.sleep(0)
        assert state.alert_known is True
        assert state.active_alerts == ()
        assert state.visual_alert is None
        assert state.siren_alert is None
        assert state.weather.severe is False
        assert gets == []
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


async def test_managed_cap_poll_waits_for_initial_point_coverage(monkeypatch):
    """Startup cannot briefly present CAP before `/points` defines locality."""
    state = skystrip.SkyState()
    request_started = asyncio.Event()

    class HangingClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            request_started.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(
        skystrip.httpx,
        "AsyncClient",
        lambda **_kwargs: HangingClient(),
    )
    poller = asyncio.create_task(
        skystrip.poll_alerts(state, wait_for_point_check=True))
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert request_started.is_set() is False

        state.nws_point_covered = True
        state.nws_point_checked.set()
        await asyncio.wait_for(request_started.wait(), 0.3)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


async def test_point_coverage_loss_fences_an_inflight_cap_response(monkeypatch):
    """A late CAP response cannot re-arm after `/points` says unsupported."""
    state = skystrip.SkyState()
    state.nws_point_covered = True
    state.nws_point_checked.set()
    request_started = asyncio.Event()
    release_response = asyncio.Event()

    class LateResponse:
        def raise_for_status(self):
            return None

        def json(self):
            raise AssertionError("unsupported-point CAP must not be parsed")

    class DelayedClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            request_started.set()
            await release_response.wait()
            return LateResponse()

    monkeypatch.setattr(
        skystrip.httpx,
        "AsyncClient",
        lambda **_kwargs: DelayedClient(),
    )
    poller = asyncio.create_task(
        skystrip.poll_alerts(state, wait_for_point_check=True))
    try:
        await asyncio.wait_for(request_started.wait(), 0.3)
        state.nws_point_covered = False
        release_response.set()
        for _ in range(20):
            if state.alert_known:
                break
            await asyncio.sleep(0)
        assert state.alert_known is True
        assert state.active_alerts == ()
        assert state.visual_alert is None
        assert state.siren_alert is None
    finally:
        poller.cancel()
        results = await asyncio.gather(poller, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)


def _source_columns_seen(text: str, frame_count: int, box_width: int) -> set[int]:
    """Independently map each marquee viewport back to source columns."""
    width = skystrip.text_width(text)
    cycle = width + 8
    seen: set[int] = set()
    for index in range(frame_count):
        offset = int(index * cycle / frame_count) % cycle
        for viewport_x in range(box_width):
            source_x = (offset + viewport_x) % cycle
            if source_x < width:
                seen.add(source_x)
    return seen


def test_alert_marquee_exposes_every_source_column_and_stays_on_panel():
    alert = replace(
        _extreme_alert(event="Severe Thunderstorm Warning"),
        # Force the dated form of the second row too; both rows then scroll.
        expires=datetime.now().astimezone() + timedelta(days=1, minutes=37),
        ends=datetime.now().astimezone() + timedelta(days=1, minutes=37),
    )
    frames = skystrip.alert_animation_frames(alert)
    event = skystrip.device_text(alert.event)
    expiry = skystrip.alert_expiry_label(alert)
    box_width = skystrip.W - 4

    assert frames
    assert all(frame.size == (72, 16) for frame in frames)
    assert _source_columns_seen(event, len(frames), box_width) == set(
        range(skystrip.text_width(event))
    )
    assert _source_columns_seen(expiry, len(frames), box_width) == set(
        range(skystrip.text_width(expiry))
    )
    # The final composed pixels retain both complete semantic rows; neither
    # can be silently clipped out by a misplaced y coordinate.
    assert all(
        any(
            frame.getpixel((x, y)) == (255, 54, 42)
            for y in range(1, 6) for x in range(2, 70)
        )
        and any(
            frame.getpixel((x, y)) == (255, 184, 64)
            for y in range(9, 14) for x in range(2, 70)
        )
        for frame in frames
    )

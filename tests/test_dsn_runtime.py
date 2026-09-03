"""Runtime contracts for DSN feed reconciliation, narration and device I/O.

These are deliberately host-side tests.  They exercise the state transitions
around the live feed without requiring NASA, a BUSY Bar, or a speech model.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def link(
    craft: str = "VGR2",
    dish: str = "DSS43",
    **changes,
) -> dsn.Link:
    """A complete, narration-ready link whose live fields can be varied."""
    base = dsn.Link(
        complex_name="Canberra",
        dish=dish,
        craft=craft,
        elevation=30.0,
        band="X",
        down_bps=20_000.0,
        up_active=True,
        range_km=2.1e10,
        naif=-32,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        azimuth=120.0,
        down_streams=(dsn.DownStream("X", 20_000.0, -140.0),),
        up_band="X",
    )
    return replace(base, **changes)


def seeded_state(*links: dsn.Link, cursor: int = 0) -> dsn.State:
    state = dsn.State()
    state.links = list(links)
    state.cursor = cursor
    state.feed_seeded = True
    state.feed_timestamp_ms = int(time.time() * 1000)
    state.feed_advanced_at = time.time()
    return state


def event_kinds(events: list[dict]) -> list[str]:
    return [event["event"] for event in events]


def test_reconcile_preserves_the_selected_link_across_a_feed_reorder():
    a, selected, c = link("JNO", "DSS25"), link(), link("LRO", "DSS34")
    state = seeded_state(a, selected, c, cursor=1)

    events = dsn.reconcile_links(state, [c, a, selected], now=100.0)

    assert events == []
    assert state.current() is selected
    assert state.cursor == 2


def test_reconcile_transfers_a_realtime_focus_on_an_unambiguous_handoff():
    old = link(dish="DSS43")
    new = link(dish="DSS14", complex_name="Goldstone")
    state = seeded_state(old, link("JNO", "DSS25"))
    state.focus = old.key
    state.realtime_since = 1234.5
    state.rt_generation = 7
    state.view = "distance"
    state.view_before_lock = "instrument"

    events = dsn.reconcile_links(
        state, [link("JNO", "DSS25"), new], now=200.0)

    assert event_kinds(events) == ["handoff"]
    assert state.focus == new.key
    assert state.current() is new
    assert state.realtime_since == 1234.5
    assert state.rt_generation == 7
    assert state.view == "distance"
    assert state.view_before_lock == "instrument"


def test_narration_handoff_is_orthogonal_and_cannot_strand_a_user_lock():
    old = link(dish="DSS43")
    new = link(dish="DSS14", complex_name="Goldstone")
    state = seeded_state(old)
    state.narration_focus = old.key

    dsn.reconcile_links(state, [new], now=200.0)

    assert state.narration_focus == new.key
    assert state.focus is None
    assert state.current() is new


def test_reconcile_releases_realtime_state_when_the_focused_craft_is_gone():
    old = link()
    remaining = link("JNO", "DSS25")
    state = seeded_state(old, remaining)
    state.focus = old.key
    state.realtime_since = 1234.5
    state.rt_generation = 7
    state.view = "distance"
    state.view_before_lock = "instrument"

    events = dsn.reconcile_links(state, [remaining], now=300.0)

    assert event_kinds(events) == ["loss"]
    assert state.focus is None
    assert state.realtime_since is None
    assert state.rt_generation is None
    assert state.view == "instrument"
    assert state.view_before_lock is None
    assert state.current() is remaining


def test_reconcile_seeds_the_first_snapshot_without_inventing_events():
    state = dsn.State()

    assert dsn.reconcile_links(state, [link()], now=100.0) == []
    assert state.feed_seeded is True

    acquired = link("JNO", "DSS25")
    assert event_kinds(
        dsn.reconcile_links(state, [state.links[0], acquired], now=110.0)
    ) == ["acquire"]


def test_visual_events_coalesces_a_same_craft_move_into_one_handoff():
    old = link(dish="DSS43")
    new = link(dish="DSS14", complex_name="Goldstone")

    events = dsn.visual_events([old], [new], now=123.45)

    assert event_kinds(events) == ["handoff"]
    assert events[0] == {
        "t": 123.5,
        "event": "handoff",
        "craft": "VGR2",
        "dish": "DSS14",
        "from_dish": "DSS43",
        "complex": "Goldstone",
        "azimuth": new.azimuth,
        "elevation": new.elevation,
        "pointing_valid": new.pointing_valid,
        "from_complex": old.complex_name,
        "from_azimuth": old.azimuth,
        "from_elevation": old.elevation,
        "from_pointing_valid": old.pointing_valid,
    }


def test_visual_events_reports_stream_count_and_band_changes():
    before = link()
    extra_stream = replace(
        before,
        down_bps=120_000.0,
        streams=2,
        down_streams=(
            dsn.DownStream("X", 20_000.0, -140.0),
            dsn.DownStream("S", 100_000.0, -130.0),
        ),
    )
    band_change = replace(
        before,
        band="Ka",
        down_streams=(dsn.DownStream("Ka", 20_000.0, -140.0),),
    )

    stream_events = dsn.visual_events([before], [extra_stream], now=1.0)
    band_events = dsn.visual_events([before], [band_change], now=2.0)

    assert event_kinds(stream_events) == ["streams"]
    assert stream_events[0]["streams"] == 2
    assert stream_events[0]["bands"] == ("S", "X")
    # A carrier changing band is represented by the same streams event, with
    # the published band tuple carrying the semantic change.
    assert event_kinds(band_events) == ["streams"]
    assert band_events[0]["bands"] == ("KA",)


def test_visual_events_reports_direction_and_special_mode_changes():
    before = link()
    direction = replace(before, up_active=False)
    modes = replace(before, arrayed=True, mspa=True, ddor=True)

    direction_events = dsn.visual_events([before], [direction], now=1.0)
    mode_events = dsn.visual_events([before], [modes], now=2.0)

    assert event_kinds(direction_events) == ["direction"]
    assert direction_events[0]["t"] == 1.0
    assert (direction_events[0]["up"], direction_events[0]["down"]) == (
        False, True)
    assert event_kinds(mode_events) == ["modes"]
    assert mode_events[0]["flags"] == (True, True, True)


def test_visual_events_ignores_raw_telemetry_jitter_inside_the_same_buckets():
    before = link()
    jittered = replace(
        before,
        elevation=30.2,
        azimuth=120.2,
        down_bps=20_020.0,
        down_dbm=-140.2,
        up_kw=18.2,
        down_streams=(dsn.DownStream("X", 20_020.0, -140.2),),
    )

    assert dsn.rate_bucket(before.down_bps) == dsn.rate_bucket(jittered.down_bps)
    assert dsn.receive_power_bucket(before.down_dbm) == \
        dsn.receive_power_bucket(jittered.down_dbm)
    assert dsn.transmit_power_bucket(before.up_kw) == \
        dsn.transmit_power_bucket(jittered.up_kw)
    assert dsn.visual_events([before], [jittered], now=1.0) == []


def test_feed_freshness_ages_from_the_last_source_advance_not_http_receipt():
    base = 1_800_000_000.0
    state = dsn.State()
    assert dsn.feed_freshness(state, now=base) == "offline"

    state.feed_timestamp_ms = int(base * 1000)
    state.feed_advanced_at = base
    assert dsn.feed_freshness(state, now=base + dsn.FEED_DELAYED_S) == "fresh"
    assert dsn.feed_freshness(
        state, now=base + dsn.FEED_DELAYED_S + 0.01) == "delayed"
    assert dsn.feed_freshness(state, now=base + dsn.FEED_STALE_S) == "delayed"
    assert dsn.feed_freshness(
        state, now=base + dsn.FEED_STALE_S + 0.01) == "stale"

    # Merely receiving another HTTP response has no field to touch here. Only
    # observing a greater NASA timestamp advances this clock.
    state.feed_timestamp_ms = int((base + 100.0) * 1000)
    state.feed_advanced_at = base + 100.0
    assert dsn.feed_freshness(state, now=base + 100.0) == "fresh"


def test_polling_does_not_refresh_freshness_until_the_source_advances(monkeypatch):
    def feed(timestamp: int) -> bytes:
        return f"""<dsn>
          <station name="cdscc" friendlyName="Canberra"/>
          <dish name="DSS43" elevationAngle="30" azimuthAngle="120">
            <downSignal active="true" spacecraft="VGR2" spacecraftID="-32"
                        band="X" dataRate="160" power="-140"/>
            <target name="VGR2" id="32" downlegRange="21000000000"/>
          </dish>
          <timestamp>{timestamp}</timestamp>
        </dsn>""".encode()

    class Response:
        def __init__(self, content: bytes):
            self.content = content

        def raise_for_status(self):
            return None

    base = 1_800_000_000.0
    first = int(base * 1000)
    second = first + 1_000
    responses = iter((feed(first), feed(first), feed(second)))

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response(next(responses))

    state = dsn.State()
    observed = []
    received_times = iter((base, base + 10.0, base + 20.0))
    real_sleep = asyncio.sleep

    async def inspect_after_poll(delay):
        observed.append((state.feed_timestamp_ms, state.feed_advanced_at))
        if len(observed) == 3:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: next(received_times))
    monkeypatch.setattr(dsn.asyncio, "sleep", inspect_after_poll)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert observed == [
        (first, base),
        (first, base),  # HTTP succeeded, but NASA's snapshot did not advance
        (second, base + 20.0),
    ]


def test_polling_ignores_a_backward_snapshot_without_false_transitions(monkeypatch):
    def feed(timestamp: int, craft: str, dish: str) -> bytes:
        return f"""<dsn>
          <station friendlyName="Canberra"/>
          <dish name="{dish}" elevationAngle="30" azimuthAngle="120">
            <downSignal active="true" spacecraft="{craft}" spacecraftID="-32"
                        band="X" dataRate="160" power="-140"/>
            <target name="{craft}" id="32" downlegRange="21000000000"/>
          </dish>
          <timestamp>{timestamp}</timestamp>
        </dsn>""".encode()

    class Response:
        def __init__(self, content):
            self.content = content

        def raise_for_status(self):
            return None

    current = int(time.time() * 1000)
    older = current - 1_000
    responses = iter((feed(current, "VGR2", "DSS43"),
                      feed(older, "JNO", "DSS25")))

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response(next(responses))

    state = dsn.State()
    observed = []

    async def inspect_after_poll(delay):
        observed.append((state.feed_timestamp_ms,
                         [item.key for item in state.links],
                         list(state.event_queue)))
        if len(observed) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", inspect_after_poll)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert observed == [
        (current, ["DSS43/VGR2"], []),
        (current, ["DSS43/VGR2"], []),
    ]


def test_observe_narration_freezes_after_two_matching_observations():
    state = dsn.State()
    state.names = {"vgr2": "Voyager 2"}
    state.dish_types = {"DSS43": "70M"}
    stable = link()

    state.feed_timestamp_ms = 1_000
    assert dsn.observe_narration(state, stable) is None
    # Worker ticks inside one NASA snapshot do not count as stability.
    assert dsn.observe_narration(state, stable) is None
    state.feed_timestamp_ms = 2_000
    frozen = dsn.observe_narration(state, stable)
    assert frozen == dsn.spoken(stable, state.names, state.dish_types)

    churned = replace(
        stable,
        down_bps=900.0,
        down_dbm=-125.0,
        up_kw=5.0,
        down_streams=(dsn.DownStream("X", 900.0, -125.0),),
    )
    assert dsn.spoken(churned, state.names, state.dish_types) != frozen
    assert dsn.observe_narration(state, churned) == frozen
    # Even two later NASA source versions with the same changed telemetry do
    # not mint a second script during this pass.
    state.feed_timestamp_ms = 3_000
    assert dsn.observe_narration(state, churned) == frozen
    state.feed_timestamp_ms = 4_000
    assert dsn.observe_narration(state, churned) == frozen
    assert state.narration_texts[stable.key] == frozen
    assert stable.key not in state.narration_candidates


class RecordingBar:
    def __init__(self, *, refuse_draw: bool = False):
        self.refuse_draw = refuse_draw
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.removed: list[str] = []
        self.played: list[str] = []
        self.stops = 0

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draws.append(payload)
        if self.refuse_draw:
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    async def storage_remove(self, path: str):
        self.removed.append(path)

    async def audio_play(self, *, application_name: str, path: str):
        self.played.append(path)

    async def audio_stop(self):
        self.stops += 1


def test_speak_cache_miss_is_immediate_and_never_waits_for_synthesis(monkeypatch):
    state = seeded_state(link())
    state.speech_cache_ready = True
    state.names = {"vgr2": "Voyager 2"}
    state.dish_types = {"DSS43": "70M"}
    bb = RecordingBar()
    calls = []

    async def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("an input handler tried to synthesize or upload")

    monkeypatch.setattr(dsn, "ensure_speech", forbidden)
    monkeypatch.setattr(dsn, "synth_off_loop", forbidden)

    async def scenario():
        await state.synth.acquire()  # a background bake is already in progress
        try:
            await asyncio.wait_for(dsn.speak(bb, state, state.links[0]), 0.1)
        finally:
            state.synth.release()

    asyncio.run(scenario())

    assert calls == []
    assert bb.uploads == []
    assert bb.played == []
    assert len(bb.draws) == 1
    assert bb.draws[0].elements[1].text == dsn.NARRATION_PREPARING
    assert state.narration_priority == state.links[0].key
    assert state.speaking is False
    assert state.focus is None


def test_cached_narration_reports_a_device_409_without_retrying(monkeypatch):
    selected = link()
    state = seeded_state(selected)
    text = dsn.spoken(selected, state.names, state.dish_types)
    name = dsn.speech_name(text)
    state.speech[name] = 0.0
    class BusyAudioBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def audio_play(self, *, application_name: str, path: str):
            self.attempts += 1
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    async def forbidden_sleep(delay):
        raise AssertionError(f"playback 409 retried after {delay}s")

    monkeypatch.setattr(dsn.asyncio, "sleep", forbidden_sleep)
    bb = BusyAudioBar()
    asyncio.run(dsn.speak(bb, state, selected))

    assert bb.attempts == 1
    assert bb.played == []
    assert bb.uploads == []
    assert bb.draws[-1].elements[1].text == dsn.NARRATION_BUSY
    assert state.speaking is False
    assert state.narration_focus is None


def test_network_narration_drilldown_returns_to_the_global_view():
    selected = link()
    state = seeded_state(selected)
    state.view = "instrument"          # listen_input performed the drilldown
    state.narration_return_view = "network"
    text = dsn.spoken(selected, state.names, state.dish_types)
    state.speech[dsn.speech_name(text)] = 0.0
    bb = RecordingBar()

    asyncio.run(dsn.speak(bb, state, selected))

    assert bb.played
    assert state.view == "network"
    assert state.narration_return_view is None
    assert state.narration_focus is None


def test_network_start_keeps_the_global_view_until_playback_is_accepted(monkeypatch):
    state = seeded_state(link())
    state.view = "network"
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_speak(bb, current, selected):
        assert current.view == "network"
        started.set()
        await finish.wait()

    monkeypatch.setattr(dsn, "speak", fake_speak)

    class BlockedReadoutBar:
        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await asyncio.Event().wait()

        async def display_draw(self, payload):
            await asyncio.Event().wait()

    async def scenario():
        listener = asyncio.create_task(dsn.listen_input(BlockedReadoutBar(), state))
        await asyncio.wait_for(started.wait(), 0.1)
        assert state.manual_until == 0.0
        finish.set()
        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener
        for task in list(state.speech_tasks):
            task.cancel()
        if state.speech_tasks:
            await asyncio.gather(*state.speech_tasks, return_exceptions=True)

    asyncio.run(scenario())


def test_start_tasks_are_tracked_until_their_narration_finishes(monkeypatch):
    state = seeded_state(link())
    state.view = "instrument"
    started = asyncio.Event()
    finish = asyncio.Event()

    async def fake_speak(bb, state, selected):
        started.set()
        await finish.wait()

    monkeypatch.setattr(dsn, "speak", fake_speak)

    class InputBar:
        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await asyncio.Event().wait()

    async def scenario():
        listener = asyncio.create_task(dsn.listen_input(InputBar(), state))
        await asyncio.wait_for(started.wait(), 0.1)
        assert len(state.speech_tasks) == 1
        tracked = next(iter(state.speech_tasks))
        assert not tracked.done()

        finish.set()
        await asyncio.wait_for(tracked, 0.1)
        await asyncio.sleep(0)  # let the done callback discard it
        assert state.speech_tasks == set()

        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    asyncio.run(scenario())


def test_a_cancelled_start_task_is_removed_from_tracking(monkeypatch):
    state = seeded_state(link())
    state.view = "instrument"
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_speak(bb, state, selected):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    monkeypatch.setattr(dsn, "speak", fake_speak)

    class InputBar:
        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await asyncio.Event().wait()

    async def scenario():
        listener = asyncio.create_task(dsn.listen_input(InputBar(), state))
        await asyncio.wait_for(started.wait(), 0.1)
        tracked = next(iter(state.speech_tasks))
        tracked.cancel()
        with pytest.raises(asyncio.CancelledError):
            await tracked
        await asyncio.wait_for(cancelled.wait(), 0.1)
        await asyncio.sleep(0)
        assert state.speech_tasks == set()

        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    asyncio.run(scenario())


def test_wheel_override_stops_device_audio_and_releases_narration_hold():
    state = seeded_state(link())
    state.speaking = True
    state.narration_focus = state.links[0].key
    bb = RecordingBar()

    async def scenario():
        task = asyncio.create_task(asyncio.Event().wait())
        state.speech_tasks.add(task)
        task.add_done_callback(state.speech_tasks.discard)
        await dsn.cancel_narration(bb, state)
        assert task.cancelled()

    asyncio.run(scenario())

    assert bb.stops == 1
    assert state.narration_focus is None


def test_wheel_override_stops_only_after_an_inflight_play_task_settles():
    state = seeded_state(link())
    state.speaking = True
    state.narration_focus = state.links[0].key
    order = []

    class OrderedBar(RecordingBar):
        async def audio_stop(self):
            order.append("stop")

    async def late_play():
        try:
            await asyncio.Event().wait()
        finally:
            # Model a client call whose cancellation cleanup completes the
            # in-flight PLAY before returning control to its caller.
            await asyncio.sleep(0)
            order.append("play settled")

    async def scenario():
        task = asyncio.create_task(late_play())
        state.speech_tasks.add(task)
        task.add_done_callback(state.speech_tasks.discard)
        await asyncio.sleep(0)
        await dsn.cancel_narration(OrderedBar(), state)

    asyncio.run(scenario())
    assert order == ["play settled", "stop"]


def test_scene_signature_is_stable_for_instrument_jitter_within_one_bucket():
    state = seeded_state(link())
    state.view = "instrument"
    state.feed_timestamp_ms = 123_000
    state.feed_advanced_at = 1_000.0
    state.aim_trails[state.links[0].key] = [(9, 4)]
    now = datetime.fromtimestamp(1_001.0, tz=timezone.utc)
    before = state.links[0]
    jittered = replace(
        before,
        azimuth=120.1,
        elevation=30.1,
        down_bps=20_010.0,
        down_dbm=-140.1,
        up_kw=18.1,
        down_streams=(dsn.DownStream("X", 20_010.0, -140.1),),
    )

    assert dsn.pointing_pixel(before.azimuth, before.elevation) == \
        dsn.pointing_pixel(jittered.azimuth, jittered.elevation)
    assert dsn.scene_signature(state, before, now) == \
        dsn.scene_signature(state, jittered, now)


def test_ok_release_is_distinct_from_the_proto3_empty_press():
    press = {"input": {"button_event": {}}}
    named = {"input": {"button_event": {"button": "OK", "action": "RELEASE"}}}
    numeric = {"input": {"button_event": {"button": 0, "action": 1}}}
    assert dsn.is_ok_press(press)
    assert not dsn.is_ok_release(press)
    assert dsn.is_ok_release(named)
    assert dsn.is_ok_release(numeric)


def test_tap_enters_the_existing_distance_watch_and_returns_to_instrument():
    selected = link()
    state = seeded_state(selected)
    state.view = "instrument"

    assert dsn.toggle_realtime(state, now=1234.0) is True
    assert state.focus == selected.key
    assert state.realtime_since == 1234.0
    assert state.view == "distance"
    assert state.view_before_lock == "instrument"
    assert state.led_blink == dsn.LED_LOCKED

    assert dsn.toggle_realtime(state, now=9999.0) is False
    assert state.focus is None
    assert state.realtime_since is None
    assert state.view == "instrument"
    assert state.view_before_lock is None
    assert state.led_blink == dsn.LED_RELEASED


def test_tap_during_narration_starts_a_user_watch_instead_of_unlocking_it():
    selected = link()
    state = seeded_state(selected)
    state.narration_focus = selected.key

    assert dsn.toggle_realtime(state, now=1234.0) is True
    assert state.focus == selected.key
    assert state.narration_focus == selected.key
    assert state.realtime_since == 1234.0


def test_holding_toggles_views_without_destroying_a_realtime_watch():
    selected = link()
    state = seeded_state(selected)
    dsn.toggle_realtime(state, now=1234.0)

    assert dsn.toggle_view(state) == "instrument"
    assert state.focus == selected.key
    assert state.realtime_since == 1234.0
    assert dsn.toggle_view(state) == "distance"
    assert state.focus == selected.key
    assert state.realtime_since == 1234.0


def test_distant_watch_renews_before_animation_and_countdown_expire():
    state = seeded_state(link())
    state.view = "distance"
    state.realtime_since = 1.0

    assert dsn.realtime_redraw_s(state.links[0].light_s) > dsn.ELEMENT_TIMEOUT_S
    assert (dsn.scene_refresh_s(state, state.links[0])
            < dsn.scene_element_timeout(state))


def test_watch_completes_on_time_even_while_instrument_view_is_showing():
    selected = replace(link(), range_km=dsn.C_KM_S * 2.0)
    state = seeded_state(selected)
    state.focus = selected.key
    state.realtime_since = 100.0
    state.view = "instrument"
    state.view_before_lock = "instrument"

    assert dsn.complete_watch_if_due(state, selected, 101.9) is False
    assert dsn.complete_watch_if_due(state, selected, 102.0) is True
    assert state.realtime_since is None
    assert state.focus is None
    assert state.view == "instrument"
    assert state.led_blink == dsn.LED_ARRIVAL


def test_every_native_popup_label_is_ascii_and_long_values_scroll_complete():
    examples = [
        {"event": "acquire", "craft": "VGR2", "dish": "DSS43"},
        {"event": "loss", "craft": "VGR2", "dish": "DSS43"},
        {"event": "handoff", "craft": "VGR2", "from_dish": "DSS43",
         "dish": "DSS14"},
        {"event": "streams", "craft": "VGR2", "streams": 3},
        {"event": "direction", "craft": "VGR2", "up": True, "down": True},
        {"event": "modes", "craft": "VGR2", "flags": (True, True, True)},
        {"event": "stale"},
        {"event": "recovered"},
    ]
    for event in examples:
        label = dsn.event_label(event)
        assert label.isascii()
        text = dsn._event_payload(label).elements[-1]
        assert text.text == label
        assert text.scroll_rate == 1400


def test_freshness_events_coalesce_and_old_transition_cards_expire():
    state = dsn.State()
    dsn.queue_events(state, [{"event": "stale", "t": 1.0}])
    dsn.queue_events(state, [{"event": "recovered", "t": 2.0}])
    assert [event["event"] for event in state.event_queue] == ["recovered"]

    bb = RecordingBar()
    assert asyncio.run(dsn.show_next_event(bb, state)) is False
    assert state.event_queue == []
    assert bb.draws == []


def test_event_intent_survives_a_non_priority_draw_failure():
    state = dsn.State()
    state.event_queue = [{"event": "acquire", "craft": "JNO",
                          "dish": "DSS25", "t": 9_999_999_999.0}]

    class BrokenBar:
        async def display_draw(self, payload):
            raise OSError("temporary transport failure")

    assert asyncio.run(dsn.show_next_event(BrokenBar(), state)) is False
    assert len(state.event_queue) == 1


def test_event_acknowledges_the_exact_card_when_queue_coalesces_mid_draw():
    state = dsn.State()
    stale = {"event": "stale", "t": 9_999_999_998.0}
    recovered = {"event": "recovered", "t": 9_999_999_999.0}
    state.event_queue = [stale]

    class GatedBar:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def display_draw(self, payload):
            self.started.set()
            await self.release.wait()

    async def scenario():
        bb = GatedBar()
        draw = asyncio.create_task(dsn.show_next_event(bb, state))
        await bb.started.wait()
        dsn.queue_events(state, [recovered])
        bb.release.set()
        assert await draw is True

    asyncio.run(scenario())
    assert state.event_queue == [recovered]


def test_newer_picker_commits_after_an_event_card_already_in_flight():
    state = seeded_state(link(), link("JNO", "DSS25"))
    target = {"event": "acquire", "craft": "VGR2", "dish": "DSS43",
              "t": time.time()}
    state.event_queue = [target]
    state.event_assets["acquire"] = "dsnevt_test.anim"

    class OrderedBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.event_started = asyncio.Event()
            self.release_event = asyncio.Event()
            self.commits: list[str] = []
            self.payloads: list = []

        async def display_draw(self, payload):
            labels = [element.text for element in payload.elements
                      if getattr(element, "text", None) is not None]
            label = labels[-1]
            if label == dsn.event_label(target):
                self.event_started.set()
                await self.release_event.wait()
            self.payloads.append(payload)
            self.commits.append(label)

    async def scenario():
        bb = OrderedBar()
        showing = asyncio.create_task(dsn.show_next_event(bb, state))
        await asyncio.wait_for(bb.event_started.wait(), 0.1)

        # Model a detent arriving while the event POST is still in flight.
        # The wheel readout is the newer user intent and must commit last.
        state.picking = True
        state.cursor = 1
        picking = asyncio.create_task(dsn.draw_picker(bb, state))
        await asyncio.sleep(0)
        picker_overtook_event = picking.done()

        bb.release_event.set()
        assert await showing is True
        await picking
        return bb, picker_overtook_event

    bb, picker_overtook_event = asyncio.run(scenario())
    assert picker_overtook_event is False
    assert bb.commits == [dsn.event_label(target), "JNO 2/2"]
    retired = {element.id: element for element in bb.payloads[1].elements
               if element.id in {"eventbg", "eventanim", "eventtx"}}
    assert set(retired) == {"eventbg", "eventanim", "eventtx"}
    assert all(element.timeout == 1 for element in retired.values())
    assert state.active_event_label is None


def test_newer_picker_commits_after_an_opaque_scene_already_in_flight():
    state = seeded_state(link(), link("JNO", "DSS25"))
    state.view = "instrument"

    class OrderedBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.scene_started = asyncio.Event()
            self.release_scene = asyncio.Event()
            self.commits: list[str] = []

        async def display_draw(self, payload):
            animations = [element for element in payload.elements
                          if getattr(element, "type", None) == "animation"]
            if animations:
                self.scene_started.set()
                await self.release_scene.wait()
                self.commits.append("SCENE")
                return
            labels = [element.text for element in payload.elements
                      if getattr(element, "text", None) is not None]
            self.commits.append(labels[-1])

    async def scenario():
        bb = OrderedBar()
        pushing = asyncio.create_task(dsn.push_scene(
            bb, state, state.links[0], ("ordered-scene",)))
        await asyncio.wait_for(bb.scene_started.wait(), 0.2)

        state.picking = True
        state.cursor = 1
        picking = asyncio.create_task(dsn.draw_picker(bb, state))
        await asyncio.sleep(0)
        assert not picking.done()

        bb.release_scene.set()
        assert await pushing is True
        await picking
        assert bb.commits == ["SCENE", "JNO 2/2"]

    asyncio.run(scenario())


def test_push_scene_renews_an_identical_scene_without_another_upload():
    state = seeded_state(link())
    state.view = "instrument"
    state.feed_timestamp_ms = 123_000
    state.feed_advanced_at = 1_000.0
    signature = ("instrument", "stable")
    bb = RecordingBar()

    async def scenario():
        await dsn.push_scene(bb, state, state.links[0], signature)
        first_filename = state.last_scene_filename
        await dsn.push_scene(bb, state, state.links[0], signature)
        return first_filename

    first_filename = asyncio.run(scenario())

    assert len(bb.uploads) == 1
    assert len(bb.draws) == 2
    assert bb.draws[0].elements[0].path == first_filename
    assert bb.draws[1].elements[0].path == first_filename
    assert state.last_scene_signature == signature
    assert state.scene_files == [first_filename]
    assert bb.removed == []


def test_an_advancing_source_renews_the_fresh_lease_without_changing_pixels():
    state = seeded_state(link())
    state.feed_timestamp_ms = 123_000
    signature = ("instrument", "stable")
    state.last_scene_signature = signature
    state.last_scene_filename = "dsn_10000_1.anim"

    bb = RecordingBar()
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))

    assert bb.uploads == []
    assert len(bb.draws) == 1
    assert all(element.timeout == dsn.LIVE_LEASE_TIMEOUT_S
               for element in bb.draws[0].elements)
    assert state.last_live_lease_timestamp_ms == 123_000
    assert state.live_lease_up is True
    assert not dsn.scene_needs_draw(state, signature, False)

    # The same source snapshot is a no-op; a greater timestamp renews only
    # these native pixels, never the AnimationElement.
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))
    assert len(bb.draws) == 1
    state.feed_timestamp_ms = 124_000
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))
    assert len(bb.draws) == 2
    assert all(element.type == "rectangle" for element in bb.draws[1].elements)

    assert asyncio.run(dsn.sync_live_lease(bb, state, "delayed"))
    assert state.live_lease_up is False
    assert all(element.timeout == 1 for element in bb.draws[-1].elements)


def test_every_distinct_source_timestamp_visibly_advances_the_native_heartbeat():
    state = seeded_state(link())
    state.view = "instrument"
    state.feed_timestamp_ms = 123_000
    bb = RecordingBar()

    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))
    first_id, first_y = state.heartbeat_id, state.heartbeat_y
    assert bb.draws[-1].elements[0].height == 2

    # Even a sub-five-second source advance must move; the old timestamp
    # quantisation could renew the lease while leaving an identical pixel.
    state.feed_timestamp_ms = 123_001
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))
    assert state.heartbeat_id != first_id
    assert state.heartbeat_y != first_y
    assert len(bb.draws[-1].elements) == 2  # new dash + exact old-id retirement

    draws = len(bb.draws)
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh"))
    assert len(bb.draws) == draws, "HTTP success without a newer source is no-op"


def test_push_scene_409_preserves_intent_and_reclaims_only_a_new_upload():
    old_signature = ("instrument", "old")
    old_filename = "dsn_10000_1.anim"
    state = seeded_state(link())
    state.view = "instrument"
    state.last_scene_signature = old_signature
    state.last_scene_filename = old_filename
    state.scene_files = [old_filename]
    state.led_blink = dsn.LED_LOCKED

    # Renewing an existing scene does not own that asset, so a refusal must not
    # delete it.
    renewal = RecordingBar(refuse_draw=True)
    with pytest.raises(exceptions.BusyBarAPIError):
        asyncio.run(dsn.push_scene(
            renewal, state, state.links[0], old_signature))
    assert renewal.uploads == []
    assert renewal.removed == []
    assert state.led_blink == dsn.LED_LOCKED
    assert state.last_scene_signature == old_signature
    assert state.last_scene_filename == old_filename

    # A changed scene was uploaded for this rejected attempt. Retain that
    # immutable generation in the bounded signature cache so the retry does
    # not pay another render/upload, while leaving accepted state untouched.
    changed_signature = ("instrument", "new")
    changed = RecordingBar(refuse_draw=True)
    with pytest.raises(exceptions.BusyBarAPIError):
        asyncio.run(dsn.push_scene(
            changed, state, state.links[0], changed_signature))
    assert len(changed.uploads) == 1
    uploaded_name = changed.uploads[0][1]
    assert changed.removed == []
    assert state.scene_cache[changed_signature] == uploaded_name
    assert old_filename not in changed.removed
    assert state.scene_files == [old_filename, uploaded_name]
    assert state.led_blink == dsn.LED_LOCKED
    assert state.last_scene_signature == old_signature
    assert state.last_scene_filename == old_filename


def test_scene_upload_aborts_if_selection_changes_during_the_await():
    first = link("VGR2", "DSS43")
    second = link("JNO", "DSS25")
    state = seeded_state(first, second)
    state.view = "instrument"

    class UploadGate(RecordingBar):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def assets_upload(self, app, name, blob):
            await super().assets_upload(app, name, blob)
            self.started.set()
            await self.release.wait()

    async def scenario():
        bb = UploadGate()
        pushing = asyncio.create_task(
            dsn.push_scene(bb, state, first, ("instrument", "first")))
        await bb.started.wait()
        state.cursor = 1
        state.dirty.set()
        bb.release.set()
        assert await pushing is False
        return bb

    bb = asyncio.run(scenario())
    assert bb.draws == []
    assert len(bb.uploads) == 1
    assert bb.removed == []
    assert state.scene_cache[("instrument", "first")] == bb.uploads[0][1]
    assert state.last_scene_signature is None


def test_scene_upload_aborts_if_the_wheel_picker_opens_during_the_await():
    selected = link()
    state = seeded_state(selected)
    state.view = "instrument"

    class UploadGate(RecordingBar):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def assets_upload(self, app, name, blob):
            await super().assets_upload(app, name, blob)
            self.started.set()
            await self.release.wait()

    async def scenario():
        bb = UploadGate()
        pushing = asyncio.create_task(
            dsn.push_scene(bb, state, selected, ("instrument", "first")))
        await bb.started.wait()
        state.picking = True
        bb.release.set()
        assert await pushing is False
        return bb

    bb = asyncio.run(scenario())
    assert bb.draws == []
    assert bb.removed == []
    assert state.scene_cache[("instrument", "first")] == bb.uploads[0][1]
    assert state.last_scene_signature is None


def test_a_new_led_intent_is_not_cleared_by_an_inflight_scene_draw():
    state = seeded_state(link())
    state.view = "instrument"
    signature = ("instrument", "stable")
    state.last_scene_signature = signature
    state.last_scene_filename = "dsn_10000_1.anim"

    class DrawGate(RecordingBar):
        def __init__(self):
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def display_draw(self, payload):
            self.draws.append(payload)
            self.started.set()
            await self.release.wait()

    async def scenario():
        bb = DrawGate()
        pushing = asyncio.create_task(
            dsn.push_scene(bb, state, state.links[0], signature))
        await bb.started.wait()
        dsn.request_led(state, dsn.LED_LOCKED)
        bb.release.set()
        assert await pushing is True

    asyncio.run(scenario())
    assert state.led_blink == dsn.LED_LOCKED


def test_countdown_retirement_commits_only_after_an_accepted_draw():
    state = dsn.State()
    state.countdown_up = True
    accepted = RecordingBar()

    assert asyncio.run(dsn.retire_countdown(accepted, state)) is True
    assert state.countdown_up is False
    assert len(accepted.draws) == 1
    assert accepted.draws[0].elements[0].timeout == 1

    state.countdown_up = True
    refused = RecordingBar(refuse_draw=True)
    assert asyncio.run(dsn.retire_countdown(refused, state)) is False
    assert state.countdown_up is True


def test_every_realtime_watch_gets_a_new_immutable_countdown_id():
    state = seeded_state(link())
    dsn.toggle_realtime(state, now=100.0)
    first = f"dsncd{state.rt_nonce}{state.rt_generation}"
    dsn.toggle_realtime(state, now=101.0)
    dsn.toggle_realtime(state, now=102.0)
    second = f"dsncd{state.rt_nonce}{state.rt_generation}"
    assert first != second


def test_refused_countdown_remains_in_the_scene_draw_predicate():
    state = seeded_state(link())
    dsn.toggle_realtime(state, now=100.0)
    signature = ("distance", "watch")

    class RefuseCountdown(RecordingBar):
        async def display_draw(self, payload):
            self.draws.append(payload)
            if payload.elements[0].type == "countdown":
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)

    bb = RefuseCountdown()
    assert asyncio.run(dsn.push_scene(bb, state, state.links[0], signature)) is True
    assert state.countdown_up is False
    assert dsn.scene_needs_draw(state, signature, False) is True


def test_a_stop_request_cancels_a_long_startup_operation_immediately():
    stop = asyncio.Event()
    cancelled = asyncio.Event()

    async def blocked():
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    async def scenario():
        waiting = asyncio.create_task(dsn.await_or_stop(blocked(), stop))
        await asyncio.sleep(0)
        stop.set()
        assert await waiting is None
        assert cancelled.is_set()

    asyncio.run(scenario())

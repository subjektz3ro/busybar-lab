"""Focused regressions for audited DSN runtime/device interleavings."""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def link(craft: str = "VGR2", dish: str = "DSS43", **changes) -> dsn.Link:
    base = dsn.Link(
        complex_name="Canberra", dish=dish, craft=craft, elevation=30.0,
        band="X", down_bps=20_000.0, up_active=True, range_km=2.1e10,
        naif=-32, down_dbm=-140.0, up_kw=18.0, streams=1,
        azimuth=120.0,
        down_streams=(dsn.DownStream("X", 20_000.0, -140.0),),
        up_band="X",
    )
    return replace(base, **changes)


def fresh_state(selected: dsn.Link, *, view: str = "instrument") -> dsn.State:
    now = time.time()
    state = dsn.State(links=[selected], view=view, feed_seeded=True)
    state.feed_timestamp_ms = int(now * 1000)
    state.feed_advanced_at = now
    return state


def test_selection_handoff_is_invariant_to_equivalent_record_order():
    steady = link("JNO", "DSS25", complex_name="Goldstone")
    old = link()
    handoff = replace(old, dish="DSS14", complex_name="Goldstone")

    selected = []
    for incoming in ([steady, handoff], [handoff, steady]):
        state = dsn.State(
            links=[steady, old], cursor=1, feed_seeded=True)
        dsn.reconcile_links(state, list(incoming), now=100.0)
        selected.append(state.current().key)

    assert selected == [handoff.key, handoff.key]


def test_selection_departure_fallback_ignores_both_snapshots_record_order():
    first = link("JNO", "DSS25", complex_name="Goldstone")
    selected = link()
    second = link("LRO", "DSS34")
    outcomes = set()

    for before in ([first, selected, second], [second, selected, first]):
        for after in ([first, second], [second, first]):
            state = dsn.State(
                links=list(before), cursor=before.index(selected),
                feed_seeded=True)
            dsn.reconcile_links(state, list(after), now=100.0)
            outcomes.add(state.current().key)

    assert outcomes == {min(first.key, second.key)}


def test_new_source_retires_every_possibly_committed_heartbeat_id():
    state = fresh_state(link(), view="network")
    state.feed_timestamp_ms = 123_000

    class CommitThenLoseResponse:
        def __init__(self) -> None:
            self.draws = []

        async def display_draw(self, payload):
            self.draws.append(payload)
            if len(self.draws) == 1:
                raise OSError("response lost after commit")

    async def scenario():
        bb = CommitThenLoseResponse()
        assert await dsn.sync_live_lease(bb, state, "fresh") is False
        uncertain = state.heartbeat_pending_id
        assert uncertain in state.heartbeat_uncertain

        state.feed_timestamp_ms += 1
        assert await dsn.sync_live_lease(bb, state, "fresh") is True
        return bb, uncertain

    bb, uncertain = asyncio.run(scenario())
    second = {element.id: element for element in bb.draws[1].elements}
    assert state.heartbeat_id != uncertain
    assert second[uncertain].timeout == 1
    assert second[state.heartbeat_id].timeout == dsn.LIVE_LEASE_TIMEOUT_S
    assert state.heartbeat_uncertain == {}
    assert state.heartbeat_uncertain_until == {}


def test_expired_ambiguous_heartbeat_ids_do_not_grow_recovery_payloads():
    state = fresh_state(link(), view="network")
    state.feed_timestamp_ms = 123_000
    state.heartbeat_uncertain["expired"] = 7
    state.heartbeat_uncertain_until["expired"] = 0.0

    class RecordingBar:
        def __init__(self) -> None:
            self.draws = []

        async def display_draw(self, payload):
            self.draws.append(payload)

    bb = RecordingBar()
    assert asyncio.run(dsn.sync_live_lease(bb, state, "fresh")) is True

    assert "expired" not in {
        element.id for element in bb.draws[0].elements}
    assert state.heartbeat_uncertain == {}
    assert state.heartbeat_uncertain_until == {}


def test_deferred_stop_settles_before_a_new_generation_can_play():
    selected = link()
    state = fresh_state(selected)
    text = dsn.spoken(selected, state.names, state.dish_types)
    state.speech[dsn.speech_name(text)] = 60.0
    state.audio_stop_pending = True

    class GatedAudioBar:
        def __init__(self) -> None:
            self.stop_entered = asyncio.Event()
            self.release_stop = asyncio.Event()
            self.played = asyncio.Event()
            self.order: list[str] = []

        async def audio_stop(self):
            self.stop_entered.set()
            await self.release_stop.wait()
            self.order.append("STOP")

        async def audio_play(self, *, application_name: str, path: str):
            self.order.append("PLAY")
            self.played.set()

        async def display_draw(self, payload):
            return None

    async def scenario():
        bb = GatedAudioBar()
        deferred = asyncio.create_task(
            dsn.stop_audio_bounded(bb, state, "deferred"))
        await bb.stop_entered.wait()
        narration = asyncio.create_task(dsn.speak(bb, state, selected))
        await asyncio.sleep(0)
        assert not bb.played.is_set(), "PLAY crossed an older in-flight STOP"

        bb.release_stop.set()
        await deferred
        await asyncio.wait_for(bb.played.wait(), 0.2)
        assert bb.order == ["STOP", "PLAY"]

        narration.cancel()
        await asyncio.gather(narration, return_exceptions=True)

    asyncio.run(scenario())


def test_obsolete_deferred_stop_token_cannot_become_a_new_stop():
    state = dsn.State(
        audio_generation=2, audio_stop_pending=False,
        audio_stop_generation=None)

    class AudioBar:
        def __init__(self) -> None:
            self.stops = 0

        async def audio_stop(self):
            self.stops += 1

    bb = AudioBar()
    asyncio.run(dsn.stop_audio_bounded(
        bb, state, "obsolete deferred", generation=1))

    assert bb.stops == 0
    assert state.audio_generation == 2


def test_shutdown_stop_cannot_be_overtaken_by_a_cancellation_resistant_play(
        monkeypatch):
    selected = link()
    state = fresh_state(selected)
    text = dsn.spoken(selected, state.names, state.dish_types)
    state.speech[dsn.speech_name(text)] = 60.0
    monkeypatch.setattr(dsn, "SHUTDOWN_TIMEOUT_S", 0.1)

    class ObservedLock:
        """Expose the final STOP waiting behind PLAY without using sleeps."""

        def __init__(self) -> None:
            self.lock = asyncio.Lock()
            self.contended = asyncio.Event()
            self.waiter_cancelled = asyncio.Event()

        async def __aenter__(self):
            if self.lock.locked():
                self.contended.set()
            try:
                await self.lock.acquire()
            except asyncio.CancelledError:
                self.waiter_cancelled.set()
                raise
            return self

        async def __aexit__(self, exc_type, exc, tb):
            self.lock.release()

    class LatePlayBar:
        def __init__(self) -> None:
            self.play_entered = asyncio.Event()
            self.play_cancelled = asyncio.Event()
            self.release_play = asyncio.Event()
            self.order: list[str] = []

        async def audio_play(self, *, application_name: str, path: str):
            self.play_entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # Model a transport which observes cancellation only after a
                # request can already have committed on the device.
                self.play_cancelled.set()
                await self.release_play.wait()
                self.order.append("PLAY")

        async def audio_stop(self):
            self.order.append("STOP")

        async def display_draw(self, payload):
            return None

    async def scenario():
        bb = LatePlayBar()
        state.audio_io = ObservedLock()
        speaking = asyncio.create_task(dsn.speak(bb, state, selected))
        state.speech_tasks.add(speaking)
        speaking.add_done_callback(state.speech_tasks.discard)
        await bb.play_entered.wait()

        shutdown = asyncio.create_task(
            dsn.shutdown_audio_bounded(bb, state, [speaking]))
        await bb.play_cancelled.wait()
        # The first cancellation-settlement window has elapsed and the final
        # STOP is now queued on the same lock, still unable to cross PLAY.
        await asyncio.wait_for(state.audio_io.contended.wait(), 0.2)
        assert bb.order == []

        # Force the first STOP across its deadline before PLAY releases. This
        # is the boundary that used to settle PLAY in the grace window but
        # then return without retrying its still-current STOP generation.
        await asyncio.wait_for(state.audio_io.waiter_cancelled.wait(), 0.3)
        bb.release_play.set()
        pending = await asyncio.wait_for(shutdown, 0.5)
        await asyncio.gather(speaking, return_exceptions=True)
        assert pending == set()
        assert bb.order == ["PLAY", "STOP"]
        assert state.audio_stop_pending is False
        assert state.audio_stop_generation is None

    asyncio.run(scenario())


def test_unplayable_speech_gets_a_new_immutable_resident_path(monkeypatch):
    selected = link()
    state = fresh_state(selected)
    text = dsn.spoken(selected, state.names, state.dish_types)
    broken = dsn.speech_name(text)
    state.speech[broken] = 1.0

    class RepairingBar:
        def __init__(self) -> None:
            self.files = {broken: b"x"}
            self.operations: list[tuple[str, str]] = []

        async def audio_play(self, *, application_name: str, path: str):
            raise exceptions.BusyBarAPIError("unplayable", status_code=404)

        async def display_draw(self, payload):
            return None

        async def assets_upload(self, app: str, name: str, data: bytes):
            self.operations.append(("upload", name))
            if name in self.files:
                raise exceptions.BusyBarAPIError(
                    "failed to open file for writing", status_code=508)
            self.files[name] = data

        async def storage_list(self, path: str):
            return SimpleNamespace(list=[
                SimpleNamespace(type="file", name=name, size=len(data))
                for name, data in self.files.items()
            ])

        async def storage_remove(self, path: str):
            name = PurePosixPath(path).name
            self.operations.append(("remove", name))
            self.files.pop(name, None)

    async def fake_synth(_text: str) -> bytes:
        return b"\0\0" * 100

    monkeypatch.setattr(dsn, "synth_off_loop", fake_synth)

    async def scenario():
        bb = RepairingBar()
        await dsn.speak(bb, state, selected)
        repaired = dsn.speech_asset_name(state, text)
        assert repaired != broken and repaired.endswith("_r01.snd")

        result = await dsn.ensure_speech(bb, state, text)
        assert result is not None and result[0] == repaired
        assert bb.operations.index(("upload", repaired)) < \
            bb.operations.index(("remove", broken))
        assert broken not in bb.files
        assert repaired in bb.files

        # The suffix encodes its base generation, so restart adoption keeps
        # using the repair rather than rediscovering the corrupt base name.
        restarted = dsn.State()
        await dsn.load_speech_cache(bb, restarted)
        assert dsn.speech_asset_name(restarted, text) == repaired
        assert repaired in restarted.speech

    asyncio.run(scenario())


def test_failed_bad_ancestor_retirement_protects_its_repaired_successor(
        monkeypatch):
    text = "Canberra is receiving Voyager 2."
    broken = dsn.speech_name(text)
    repaired = dsn.speech_name(text, repair=1)
    monkeypatch.setattr(dsn, "SPEECH_CACHE_MAX", 0)

    class StickyAncestorBar:
        def __init__(self) -> None:
            self.files = {broken: b"bad", repaired: b"\0\0" * 100}
            self.removal_attempts: list[str] = []

        async def storage_list(self, path: str):
            return SimpleNamespace(list=[
                SimpleNamespace(type="file", name=name, size=len(data))
                for name, data in self.files.items()
            ])

        async def storage_remove(self, path: str):
            name = PurePosixPath(path).name
            self.removal_attempts.append(name)
            if name == broken:
                raise OSError("firmware still owns corrupt ancestor")
            self.files.pop(name, None)

    bb = StickyAncestorBar()
    state = dsn.State()
    asyncio.run(dsn.load_speech_cache(bb, state))

    assert state.speech_repairs[broken] == 1
    assert state.speech_retire == {broken}
    assert repaired in state.speech
    assert repaired in bb.files
    assert repaired not in bb.removal_attempts


def test_zero_byte_resident_speech_advances_to_a_repair_generation():
    text = "Madrid is receiving Juno."
    broken = dsn.speech_name(text)

    class StickyZeroBar:
        async def storage_list(self, path: str):
            return SimpleNamespace(list=[
                SimpleNamespace(type="file", name=broken, size=0)])

        async def storage_remove(self, path: str):
            raise OSError("zero-byte path could not be removed")

    state = dsn.State()
    asyncio.run(dsn.load_speech_cache(StickyZeroBar(), state))

    speech = dsn.speech_asset_name(state, text)
    assert speech == dsn.speech_name(text, repair=1)
    assert broken in state.speech_retire
    assert broken not in state.speech


@pytest.mark.parametrize(
    ("freshness", "label"),
    [("offline", "DSN OFFLINE"), ("delayed", "FEED DELAY"),
     ("stale", "FEED STALE"), ("fresh", "NO LINK DATA")],
)
def test_start_and_ambient_share_truthful_feed_status(freshness, label):
    assert dsn.feed_status_label(freshness) == label


def test_start_reports_never_online_as_offline_not_stale():
    state = dsn.State()

    class InputBar:
        def __init__(self) -> None:
            self.drawn = asyncio.Event()
            self.draws = []

        async def display_draw(self, payload):
            self.draws.append(payload)
            self.drawn.set()

        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await asyncio.Event().wait()

    async def scenario():
        bb = InputBar()
        listening = asyncio.create_task(dsn.listen_input(bb, state))
        await asyncio.wait_for(bb.drawn.wait(), 0.2)
        listening.cancel()
        await asyncio.gather(listening, return_exceptions=True)
        return bb

    bb = asyncio.run(scenario())
    labels = [element.text for element in bb.draws[-1].elements
              if element.type == "text"]
    assert labels == ["DSN OFFLINE"]

"""Final host-only resilience contracts for the DSN live instrument.

These tests pin device-boundary failure handling and async input/narration
interleavings.  They use fakes only: no NASA endpoint, speech model, BUSY Bar,
or Pi is contacted.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path, PurePosixPath
from types import SimpleNamespace

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def link(
    craft: str = "VGR2",
    dish: str = "DSS43",
    **changes,
) -> dsn.Link:
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


def fresh_state(*links: dsn.Link, view: str = "instrument") -> dsn.State:
    now = time.time()
    state = dsn.State(links=list(links), view=view)
    state.feed_seeded = True
    state.feed_timestamp_ms = int(now * 1000)
    state.feed_advanced_at = now
    state.freshness = "fresh"
    return state


class SceneBar:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.removed: list[str] = []

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draws.append(payload)

    async def storage_remove(self, path: str):
        self.removed.append(path)


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        (SimpleNamespace(type="file", name="scene.anim", size=81_234), True),
        (SimpleNamespace(type="file", name="scene.anim", size=0), False),
        (SimpleNamespace(type="file", name="scene.anim", size=81_233), False),
        (SimpleNamespace(type="dir", name="scene.anim"), False),
        (SimpleNamespace(name="scene.anim", size=81_234), False),
    ],
)
def test_scene_upload_adoption_requires_an_exact_sized_file(entry, expected):
    class StorageBar:
        async def storage_list(self, path: str):
            return SimpleNamespace(list=[entry])

    adopted = asyncio.run(
        dsn.scene_asset_exists(StorageBar(), "scene.anim", 81_234))

    assert adopted is expected


def test_cached_scene_404_is_evicted_and_retry_uploads_a_new_generation():
    selected = link()
    state = fresh_state(selected)
    signature = ("instrument", "cached-but-missing")
    missing = "dsn_missing.anim"
    state.scene_cache[signature] = missing
    state.scene_files.append(missing)
    state.last_scene_signature = signature
    state.last_scene_filename = missing

    class MissingThenHealthy(SceneBar):
        async def display_draw(self, payload):
            self.draws.append(payload)
            animation = payload.elements[0]
            if animation.type == "animation" and animation.path == missing:
                raise exceptions.BusyBarAPIError(
                    "animation asset not found", status_code=404)

    bb = MissingThenHealthy()

    async def scenario():
        with pytest.raises(exceptions.BusyBarAPIError):
            await dsn.push_scene(bb, state, selected, signature)

        assert signature not in state.scene_cache
        assert state.last_scene_filename is None
        assert missing not in state.scene_files
        assert state.dirty.is_set()

        state.dirty.clear()
        assert await dsn.push_scene(bb, state, selected, signature)

    asyncio.run(scenario())

    assert bb.removed == [f"/ext/user_assets/{dsn.APP_NAME}/{missing}"]
    assert len(bb.uploads) == 1
    assert bb.uploads[0][1] != missing
    assert bb.draws[-1].elements[0].path == bb.uploads[0][1]


def encoded_event_asset(effect: str) -> tuple[str, bytes]:
    frames, fps, hold = dsn.render_event_frames(effect)
    blob = dsn.anim.encode_anim(
        frames, fps=fps, durations=[hold] * len(frames))
    return dsn.event_asset_name(effect, blob), blob


def test_event_prewarm_replaces_a_zero_byte_content_addressed_file():
    acquire_name, acquire_blob = encoded_event_asset("acquire")

    class ZeroByteEventBar:
        def __init__(self) -> None:
            self.files = {acquire_name: b""}
            self.uploads: list[tuple[str, str, bytes]] = []
            self.removed: list[str] = []

        async def storage_list(self, path: str):
            return SimpleNamespace(list=[
                SimpleNamespace(type="file", name=name, size=len(blob))
                for name, blob in self.files.items()
            ])

        async def storage_remove(self, path: str):
            self.removed.append(path)
            self.files.pop(PurePosixPath(path).name, None)

        async def assets_upload(self, app: str, name: str, blob: bytes):
            self.uploads.append((app, name, blob))
            self.files[name] = blob

    bb = ZeroByteEventBar()
    state = dsn.State()
    asyncio.run(dsn.prepare_event_assets(bb, state))

    assert bb.removed[0] == f"/ext/user_assets/{dsn.APP_NAME}/{acquire_name}"
    assert (dsn.APP_NAME, acquire_name, acquire_blob) in bb.uploads
    assert state.event_assets["acquire"] == acquire_name
    assert len(state.event_assets) == len(dsn.EVENT_EFFECTS)


def test_event_asset_404_falls_back_to_text_and_rewarms_in_background():
    broken = "dsnevt_broken.anim"
    event = {
        "event": "acquire",
        "craft": "JNO",
        "dish": "DSS25",
        "t": time.time(),
    }

    class RepairingEventBar:
        def __init__(self) -> None:
            self.files = {broken: b"broken"}
            self.uploads: list[tuple[str, str, bytes]] = []
            self.removed: list[str] = []
            self.draws = []

        async def display_draw(self, payload):
            self.draws.append(payload)
            animations = [element for element in payload.elements
                          if element.type == "animation"]
            if animations and animations[0].path == broken:
                raise exceptions.BusyBarAPIError(
                    "animation asset not found", status_code=404)

        async def storage_list(self, path: str):
            return SimpleNamespace(list=[
                SimpleNamespace(type="file", name=name, size=len(blob))
                for name, blob in self.files.items()
            ])

        async def storage_remove(self, path: str):
            self.removed.append(path)
            self.files.pop(PurePosixPath(path).name, None)

        async def assets_upload(self, app: str, name: str, blob: bytes):
            self.uploads.append((app, name, blob))
            self.files[name] = blob

    async def scenario():
        bb = RepairingEventBar()
        state = dsn.State()
        state.event_assets["acquire"] = broken
        state.event_queue = [event]

        assert await dsn.show_next_event(bb, state) is True
        assert state.event_queue == []
        assert len(bb.draws) == 2
        assert any(element.type == "animation"
                   for element in bb.draws[0].elements)
        assert not any(element.type == "animation"
                       for element in bb.draws[1].elements)

        assert state.event_warm_task is not None
        await state.event_warm_task
        return bb, state

    bb, state = asyncio.run(scenario())

    assert f"/ext/user_assets/{dsn.APP_NAME}/{broken}" in bb.removed
    assert state.event_assets["acquire"] != broken
    assert len(state.event_assets) == len(dsn.EVENT_EFFECTS)
    assert len(bb.uploads) == len(dsn.EVENT_EFFECTS)


def test_ambiguous_heartbeat_draw_retries_the_exact_same_element_id():
    state = fresh_state(link(), view="network")

    class CommitThenLoseResponse:
        def __init__(self) -> None:
            self.draws = []

        async def display_draw(self, payload):
            self.draws.append(payload)
            if len(self.draws) == 1:
                raise OSError("response lost after device commit")

    bb = CommitThenLoseResponse()

    async def scenario():
        assert await dsn.sync_live_lease(bb, state, "fresh") is False
        pending_id = state.heartbeat_pending_id
        pending_y = state.heartbeat_pending_y
        assert pending_id is not None

        assert await dsn.sync_live_lease(bb, state, "fresh") is True
        return pending_id, pending_y

    pending_id, pending_y = asyncio.run(scenario())
    first = bb.draws[0].elements[0]
    second = bb.draws[1].elements[0]

    assert (first.id, first.y) == (second.id, second.y) == \
        (pending_id, pending_y)
    assert state.heartbeat_generation == 1
    assert state.heartbeat_id == pending_id
    assert state.heartbeat_pending_id is None


def test_network_cursor_does_not_auto_rotate_without_a_visible_picker(monkeypatch):
    state = fresh_state(link(), link("JNO", "DSS25"), view="network")
    real_sleep = asyncio.sleep
    blocked = asyncio.Event()
    calls = 0

    async def one_tick_then_block(delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            await blocked.wait()

    monkeypatch.setattr(dsn.asyncio, "sleep", one_tick_then_block)

    async def scenario():
        task = asyncio.create_task(dsn.rotate(state))
        for _ in range(5):
            await real_sleep(0)
            if calls > 1:
                break
        assert calls > 1
        assert state.cursor == 0
        assert not state.dirty.is_set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())


class AudioBar:
    def __init__(self) -> None:
        self.played: list[str] = []
        self.stops = 0

    async def audio_play(self, *, application_name: str, path: str):
        self.played.append(path)

    async def audio_stop(self):
        self.stops += 1


def cache_narration(state: dsn.State, selected: dsn.Link,
                    seconds: float = 0.0) -> str:
    text = dsn.spoken(selected, state.names, state.dish_types)
    name = dsn.speech_name(text)
    state.speech[name] = seconds
    return name


def test_cached_narration_is_blocked_when_the_source_is_already_stale():
    selected = link()
    state = fresh_state(selected)
    cache_narration(state, selected)
    old = time.time() - dsn.FEED_STALE_S - 1
    state.feed_timestamp_ms = int(old * 1000)
    state.feed_advanced_at = old
    bb = AudioBar()

    asyncio.run(dsn.speak(bb, state, selected))

    assert bb.played == []
    assert bb.stops == 0
    assert state.speaking is False
    assert state.narration_focus is None


def test_narration_task_revalidates_the_exact_link_key_before_playing():
    old = link()
    handoff = replace(old, complex_name="Goldstone", dish="DSS14")
    state = fresh_state(old)
    cache_narration(state, old)
    bb = AudioBar()

    async def scenario():
        # create_task does not run inline.  This is the real START race: another
        # ready task may reconcile the feed before speak() gets its first turn.
        narration = asyncio.create_task(dsn.speak(bb, state, old))
        dsn.reconcile_links(state, [handoff], now=time.time())
        await narration

    asyncio.run(scenario())

    assert state.links[0].key == handoff.key
    assert bb.played == []
    assert bb.stops == 0
    assert state.narration_focus is None


def test_accepted_cached_narration_stops_when_the_feed_becomes_stale(monkeypatch):
    selected = link()

    class RuntimeBar(SceneBar):
        def __init__(self) -> None:
            super().__init__()
            self.played = asyncio.Event()
            self.stopped = asyncio.Event()
            self.closed = False

        async def audio_play(self, *, application_name: str, path: str):
            self.played.set()

        async def audio_stop(self):
            self.stopped.set()

        async def display_clear(self, *, application_name: str):
            return None

        async def aclose(self):
            self.closed = True

    bb = RuntimeBar()
    handlers: dict[int, tuple] = {}

    async def connect():
        return bb

    async def noop(*args, **kwargs):
        return None

    async def park(*args, **kwargs):
        await asyncio.Event().wait()

    async def seed_feed(state: dsn.State):
        now = time.time()
        state.links = [selected]
        state.feed_seeded = True
        state.feed_timestamp_ms = int(now * 1000)
        state.feed_advanced_at = now
        state.dirty.set()
        await asyncio.Event().wait()

    async def start_then_stale(runtime_bb, state: dsn.State):
        while not state.feed_seeded:
            await asyncio.sleep(0)
        cache_narration(state, selected, seconds=60.0)
        narration = asyncio.create_task(dsn.speak(runtime_bb, state, selected))
        state.speech_tasks.add(narration)
        narration.add_done_callback(state.speech_tasks.discard)
        await bb.played.wait()
        old = time.time() - dsn.FEED_STALE_S - 1
        state.feed_timestamp_ms = int(old * 1000)
        state.feed_advanced_at = old
        state.dirty.set()
        await asyncio.Event().wait()

    async def prepare_cache(*args, **kwargs):
        await asyncio.Event().wait()

    def start_warm(runtime_bb, state: dsn.State):
        task = asyncio.create_task(park())
        state.event_warm_task = task
        return task

    monkeypatch.setattr(dsn, "aconnect", connect)
    monkeypatch.setattr(dsn, "sweep_stale_assets", noop)
    monkeypatch.setattr(dsn, "load_ranges", lambda state: None)
    monkeypatch.setattr(dsn, "load_history", lambda state: None)
    monkeypatch.setattr(dsn, "poll_names", park)
    monkeypatch.setattr(dsn, "poll_feed", seed_feed)
    monkeypatch.setattr(dsn, "poll_ranges", park)
    monkeypatch.setattr(dsn, "listen_input", start_then_stale)
    monkeypatch.setattr(dsn, "rotate", park)
    monkeypatch.setattr(dsn, "prepare_narration_cache", prepare_cache)
    monkeypatch.setattr(dsn, "start_event_asset_warm", start_warm)

    async def scenario():
        loop = asyncio.get_running_loop()

        def add_signal_handler(sig, callback, *args):
            handlers[sig] = (callback, args)

        monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
        running = asyncio.create_task(dsn.run(False))
        await asyncio.wait_for(bb.stopped.wait(), 3.0)
        callback, args = handlers[signal.SIGTERM]
        callback(*args)
        await asyncio.wait_for(running, 3.0)

    asyncio.run(scenario())

    assert bb.played.is_set()
    assert bb.stopped.is_set()
    assert bb.closed is True

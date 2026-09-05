"""Host-only resilience contracts for the DSN runtime.

These tests deliberately exercise failures and interleavings at the device
boundary.  They use fakes only: no NASA request, speech model, BUSY Bar, or Pi
is touched.
"""

from __future__ import annotations

import asyncio
import signal
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import feed as dsn_feed
from apps.dsn_app import history as dsn_history
from apps.dsn_app import input as dsn_input
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import ranges as dsn_ranges
from apps.dsn_app import reconcile as dsn_reconcile
from apps.dsn_app import runtime as dsn_runtime
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import source as dsn_source
from apps.dsn_app.audio import assets as dsn_audio_assets
from apps.dsn_app.audio import narration as dsn_audio_narration
from apps.dsn_app.audio import words as dsn_audio_words
from apps.dsn_app.device import assets as dsn_device_assets
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.device import scenes as dsn_device_scenes
from apps.dsn_app.render import instrument as dsn_render_instrument


def link(
    craft: str = "VGR2",
    dish: str = "DSS43",
    **changes,
) -> dsn_source.Link:
    base = dsn_source.Link(
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
        down_streams=(dsn_source.DownStream("X", 20_000.0, -140.0),),
        up_band="X",
    )
    return replace(base, **changes)


def seeded_state(*links: dsn_source.Link, cursor: int = 0) -> dsn_model.State:
    state = dsn_model.State()
    state.links = list(links)
    state.cursor = cursor
    state.feed_seeded = True
    now = time.time()
    state.feed_timestamp_ms = int(now * 1000)
    state.feed_advanced_at = now
    return state


class RecordingBar:
    def __init__(self) -> None:
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.removed: list[str] = []
        self.played: list[str] = []
        self.stops = 0
        self.clears = 0
        self.closed = False

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draws.append(payload)

    async def storage_remove(self, path: str):
        self.removed.append(path)

    async def audio_play(self, *, application_name: str, path: str):
        self.played.append(path)

    async def audio_stop(self):
        self.stops += 1

    async def display_clear(self, *, application_name: str):
        self.clears += 1

    async def aclose(self):
        self.closed = True


def animation_draws(bb: RecordingBar) -> list:
    return [payload for payload in bb.draws
            if payload.elements and payload.elements[0].type == "animation"]


def live_lease_draws(bb: RecordingBar) -> list:
    return [payload for payload in bb.draws
            if payload.elements
            and all(element.type == "rectangle" for element in payload.elements)
            and all(element.id.startswith("dsnlive") for element in payload.elements)]


def test_a_to_b_to_a_reuses_the_resident_a_scene_asset():
    a = link("VGR2", "DSS43")
    b = link("JNO", "DSS25", complex_name="Goldstone", naif=-61)
    state = seeded_state(a, b)
    state.view = "instrument"
    bb = RecordingBar()

    async def scenario():
        state.cursor = 0
        a_signature = dsn_device_scene_policy.scene_signature(state, a)
        assert await dsn_device_scenes.push_scene(bb, state, a, a_signature)
        a_path = animation_draws(bb)[-1].elements[0].path

        state.cursor = 1
        b_signature = dsn_device_scene_policy.scene_signature(state, b)
        assert await dsn_device_scenes.push_scene(bb, state, b, b_signature)

        state.cursor = 0
        assert await dsn_device_scenes.push_scene(bb, state, a, a_signature)
        return a_path

    a_path = asyncio.run(scenario())

    assert len(bb.uploads) == 2, "returning to A re-uploaded a resident asset"
    assert [draw.elements[0].path for draw in animation_draws(bb)] == [
        a_path,
        bb.uploads[1][1],
        a_path,
    ]
    assert not any(path.endswith(f"/{a_path}") for path in bb.removed)


def test_instrument_signature_changes_if_and_only_if_up_band_changes_pixels():
    duplex = link(
        up_band="S",
        down_streams=(dsn_source.DownStream("X", 20_000.0, -140.0),),
    )
    changed = replace(duplex, up_band="X")

    before, _, _ = dsn_render_instrument.render_instrument_frames(duplex, freshness="fresh")
    after, _, _ = dsn_render_instrument.render_instrument_frames(changed, freshness="fresh")
    same_pixels = [frame.tobytes() for frame in before] == [
        frame.tobytes() for frame in after]

    assert same_pixels, "the fixture must isolate a non-visible uplink-band change"
    assert dsn_render_instrument.instrument_signature(duplex, [], "fresh") == \
        dsn_render_instrument.instrument_signature(changed, [], "fresh")


def test_non_409_failure_after_upload_reuses_then_eventually_reclaims_the_asset():
    state = seeded_state(link())
    state.view = "instrument"
    signature = ("instrument", "ambiguous-draw")

    class FailFirstDraw(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        async def display_draw(self, payload):
            self.draws.append(payload)
            if payload.elements[0].type == "animation" and not self.failed:
                self.failed = True
                raise OSError("response lost after upload")

    bb = FailFirstDraw()

    async def scenario():
        with pytest.raises(OSError, match="response lost"):
            await dsn_device_scenes.push_scene(bb, state, state.links[0], signature)
        first_path = bb.uploads[0][1]

        # A transport failure is ambiguous: the draw may have landed.  Keep
        # ownership and retry the exact immutable path instead of writing a new
        # generation or deleting a file the firmware may hold open.
        assert await dsn_device_scenes.push_scene(bb, state, state.links[0], signature)
        assert len(bb.uploads) == 1
        assert animation_draws(bb)[-1].elements[0].path == first_path

        # The cache must still be bounded.  Once this old signature has fallen
        # well outside any reasonable working set, it must be either live in a
        # bounded cache or reclaimed; 64 generations catches an unbounded leak
        # without prescribing a particular cache representation.
        for generation in range(64):
            await dsn_device_scenes.push_scene(
                bb, state, state.links[0], ("instrument", "generation", generation))
        return first_path

    first_path = asyncio.run(scenario())

    assert any(path.endswith(f"/{first_path}") for path in bb.removed), (
        "the post-upload failure asset was forgotten instead of being reused "
        "and later reclaimed")


def install_run_harness(monkeypatch, bb, links: list[dsn_source.Link], *, input_task=None):
    """Replace every external/background dependency used by ``run``."""
    state_seen: dict[str, dsn_model.State] = {}

    async def connect():
        return bb

    async def noop(*args, **kwargs):
        return None

    async def park(*args, **kwargs):
        await asyncio.Event().wait()

    async def seed_feed(state: dsn_model.State):
        state_seen["state"] = state
        state.links = list(links)
        state.feed_seeded = True
        now = time.time()
        state.feed_timestamp_ms = int(now * 1000)
        state.feed_advanced_at = now
        state.dirty.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(dsn_runtime, "aconnect", connect)
    monkeypatch.setattr(dsn_device_assets, "sweep_stale_assets", noop)
    monkeypatch.setattr(dsn_feed, "fetch_names", noop)
    monkeypatch.setattr(dsn_audio_assets, "load_speech_cache", noop)
    monkeypatch.setattr(dsn_ranges, "load_ranges", lambda state: None)
    monkeypatch.setattr(dsn_history, "load_history", lambda state: None)
    monkeypatch.setattr(dsn_feed, "poll_feed", seed_feed)
    monkeypatch.setattr(dsn_feed, "poll_names", park)
    monkeypatch.setattr(dsn_ranges, "poll_ranges", park)
    monkeypatch.setattr(dsn_audio_narration, "prebake", park)
    monkeypatch.setattr(dsn_selection, "rotate", park)
    if input_task is not None:
        monkeypatch.setattr(dsn_input, "listen_input", input_task)
    return state_seen


def capture_signal_handlers(monkeypatch) -> dict[int, tuple]:
    loop = asyncio.get_running_loop()
    handlers: dict[int, tuple] = {}

    def add_signal_handler(sig, callback, *args):
        handlers[sig] = (callback, args)

    monkeypatch.setattr(loop, "add_signal_handler", add_signal_handler)
    return handlers


def request_term(handlers: dict[int, tuple]) -> None:
    callback, args = handlers[signal.SIGTERM]
    callback(*args)


def test_stop_interrupts_a_blocked_main_loop_upload(monkeypatch):
    selected = link()

    class BlockedUploadBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.upload_started = asyncio.Event()
            self.release_upload = asyncio.Event()

        async def assets_upload(self, app: str, name: str, blob: bytes):
            self.uploads.append((app, name, blob))
            self.upload_started.set()
            await self.release_upload.wait()

    async def scenario():
        bb = BlockedUploadBar()
        install_run_harness(monkeypatch, bb, [selected], input_task=_park_forever)
        handlers = capture_signal_handlers(monkeypatch)
        running = asyncio.create_task(dsn_runtime.run(False))
        await asyncio.wait_for(bb.upload_started.wait(), 2.0)
        request_term(handlers)
        try:
            await asyncio.wait_for(asyncio.shield(running), 0.5)
            stopped_promptly = True
        except asyncio.TimeoutError:
            stopped_promptly = False
        finally:
            bb.release_upload.set()
            await asyncio.wait_for(running, 2.0)
        return stopped_promptly, bb

    stopped_promptly, bb = asyncio.run(scenario())
    assert stopped_promptly, "SIGTERM waited for an in-flight asset upload"
    assert bb.closed


async def _park_forever(*args, **kwargs):
    await asyncio.Event().wait()


def test_wheel_intent_invalidates_a_previous_scene_409_backoff(monkeypatch):
    a = link("VGR2", "DSS43")
    b = link("JNO", "DSS25", complex_name="Goldstone", naif=-61)

    class RefuseFirstSceneBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.first_refused = asyncio.Event()
            self.second_scene = asyncio.Event()

        async def display_draw(self, payload):
            self.draws.append(payload)
            if payload.elements[0].type != "animation":
                return
            scenes = animation_draws(self)
            if len(scenes) == 1:
                self.first_refused.set()
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)
            self.second_scene.set()

        async def stream_status_ws(self):
            await self.first_refused.wait()
            yield {"updates": [{"input": {"encoder_event": {"delta": 1}}}]}
            await asyncio.Event().wait()

    async def scenario():
        bb = RefuseFirstSceneBar()
        install_run_harness(monkeypatch, bb, [a, b])
        handlers = capture_signal_handlers(monkeypatch)
        running = asyncio.create_task(dsn_runtime.run(False))
        try:
            await asyncio.wait_for(bb.first_refused.wait(), 2.0)
            await asyncio.wait_for(bb.second_scene.wait(), 2.0)
            retried = True
        except asyncio.TimeoutError:
            retried = False
        finally:
            request_term(handlers)
            await asyncio.wait_for(running, 2.0)
        return retried, bb

    retried, bb = asyncio.run(scenario())
    assert retried, "the selected link inherited an unrelated 30-second backoff"
    assert len(animation_draws(bb)) >= 2


def test_elapsed_live_lease_backoff_retries_without_a_scene_redraw(monkeypatch):
    selected = link()

    class RefuseFirstLeaseBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.first_lease = asyncio.Event()
            self.second_lease = asyncio.Event()

        async def display_draw(self, payload):
            self.draws.append(payload)
            leases = live_lease_draws(self)
            if payload in leases and len(leases) == 1:
                self.first_lease.set()
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)
            if payload in leases and len(leases) == 2:
                self.second_lease.set()

    async def scenario():
        bb = RefuseFirstLeaseBar()
        install_run_harness(monkeypatch, bb, [selected], input_task=_park_forever)
        handlers = capture_signal_handlers(monkeypatch)
        loop = asyncio.get_running_loop()
        real_time = loop.time
        clock = {"offset": 0.0}
        monkeypatch.setattr(loop, "time", lambda: real_time() + clock["offset"])
        running = asyncio.create_task(dsn_runtime.run(False))
        try:
            await asyncio.wait_for(bb.first_lease.wait(), 2.0)
            clock["offset"] = 31.0
            await asyncio.wait_for(bb.second_lease.wait(), 1.5)
            retried = True
        except asyncio.TimeoutError:
            retried = False
        finally:
            request_term(handlers)
            await asyncio.wait_for(running, 2.0)
        return retried, bb

    retried, bb = asyncio.run(scenario())
    assert retried, "an elapsed lease backoff stayed latched until scene refresh"
    assert len(live_lease_draws(bb)) >= 2
    assert len(animation_draws(bb)) == 1


def test_arrival_acknowledgement_survives_a_handoff_during_the_draw():
    old = link(dish="DSS43")
    new = link(dish="DSS14", complex_name="Goldstone")
    state = seeded_state(old)
    state.view = "instrument"
    state.completion_pending = old.key
    dsn_model.request_led(state, dsn_limits.LED_ARRIVAL)

    class GatedDrawBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.draw_started = asyncio.Event()
            self.release_draw = asyncio.Event()

        async def display_draw(self, payload):
            self.draws.append(payload)
            if payload.elements[0].type == "animation":
                self.draw_started.set()
                await self.release_draw.wait()

    async def scenario():
        bb = GatedDrawBar()
        pushing = asyncio.create_task(dsn_device_scenes.push_scene(
            bb, state, old, ("instrument", "arrival")))
        await bb.draw_started.wait()
        dsn_reconcile.reconcile_links(state, [new], now=200.0)
        bb.release_draw.set()
        await pushing

    asyncio.run(scenario())

    assert state.completion_pending is None
    assert state.led_blink is None


@pytest.mark.parametrize("replacement", [
    link(dish="DSS14", complex_name="Goldstone"),
    link("JNO", "DSS25", complex_name="Goldstone", naif=-61),
], ids=["handoff", "loss"])
def test_accepted_narration_stops_when_its_live_link_changes(replacement):
    old = link()
    state = seeded_state(old)
    state.names = {"vgr2": "Voyager 2", "jno": "Juno"}
    text = dsn_audio_words.spoken(old, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 60.0

    class PlayingBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.play_started = asyncio.Event()

        async def audio_play(self, *, application_name: str, path: str):
            self.played.append(path)
            self.play_started.set()

    async def scenario():
        bb = PlayingBar()
        speaking = asyncio.create_task(dsn_audio_narration.speak(bb, state, old))
        state.speech_tasks.add(speaking)
        speaking.add_done_callback(state.speech_tasks.discard)
        await bb.play_started.wait()
        dsn_reconcile.reconcile_links(state, [replacement], now=200.0)
        done, _ = await asyncio.wait({speaking}, timeout=1.0)
        finished = speaking in done
        if not finished:
            speaking.cancel()
            await asyncio.gather(speaking, return_exceptions=True)
        return finished, bb

    finished, bb = asyncio.run(scenario())

    assert finished, "old narration continued after its dish/craft left the display"
    assert bb.stops == 1


def test_audio_404_evicts_the_stale_cache_entry_for_background_rebake():
    selected = link()
    state = seeded_state(selected)
    state.names = {"vgr2": "Voyager 2"}
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0

    class MissingAudioBar(RecordingBar):
        async def audio_play(self, *, application_name: str, path: str):
            raise exceptions.BusyBarAPIError("asset not found", status_code=404)

    asyncio.run(dsn_audio_narration.speak(MissingAudioBar(), state, selected))

    assert name not in state.speech
    assert state.narration_priority == selected.key

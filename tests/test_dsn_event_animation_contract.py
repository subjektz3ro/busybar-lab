"""Contracts for DSN's finite, pre-baked live-transition animations.

The event path must stay cheap: generic effects are rendered and uploaded in
the background, while a live transition only references a resident native
``.anim`` asset (or falls back to the existing native-text card while warming).
All tests are host-only and use no network, BUSY Bar, or Pi.
"""

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


EVENT_EFFECTS = (
    "acquire", "loss", "handoff", "split", "merge", "array", "unarray",
)


@pytest.fixture(autouse=True)
def legacy_rows_event_grammar(monkeypatch):
    """This module owns the finite generic assets kept by rollback rows.

    Three Skies has a separate data-specific Handoff Echo contract in
    ``test_dsn_three_skies_contract.py``. Pinning the style here prevents a
    generic prewarm assertion from accidentally approving that live path.
    """
    monkeypatch.setattr(dsn, "DSN_NETWORK_STYLE", "rows")


def link(
    craft: str = "VGR2",
    dish: str = "DSS43",
    *,
    stream_count: int = 1,
    **changes,
) -> dsn.Link:
    bands = ("X", "S", "Ka")
    streams = tuple(
        dsn.DownStream(bands[index % len(bands)], 20_000.0 * (index + 1),
                       -140.0 + index * 5)
        for index in range(stream_count)
    )
    base = dsn.Link(
        complex_name="Canberra",
        dish=dish,
        craft=craft,
        elevation=30.0,
        band=streams[0].band if streams else "",
        down_bps=sum(stream.bps for stream in streams),
        up_active=True,
        range_km=2.1e10,
        naif=-32,
        down_dbm=max((stream.dbm for stream in streams), default=None),
        up_kw=18.0,
        streams=len(streams),
        azimuth=120.0,
        down_streams=streams,
        up_band="X",
    )
    return replace(base, **changes)


def event(kind: str, **fields) -> dict:
    return {"event": kind, "t": time.time(), "craft": "VGR2",
            "dish": "DSS43", **fields}


def transition_events() -> dict[str, dict]:
    now = time.time()
    one = link(stream_count=1)
    three = link(stream_count=3)
    two = link(stream_count=2)
    handoff = replace(one, dish="DSS14", complex_name="Goldstone")
    arrayed = replace(one, arrayed=True)

    return {
        "acquire": dsn.visual_events([], [one], now=now)[0],
        "loss": dsn.visual_events([one], [], now=now + 0.1)[0],
        "handoff": dsn.visual_events([one], [handoff], now=now + 0.2)[0],
        "split": next(item for item in dsn.visual_events([one], [three], now + 0.3)
                      if item["event"] == "streams"),
        "merge": next(item for item in dsn.visual_events([three], [two], now + 0.4)
                      if item["event"] == "streams"),
        "array": next(item for item in dsn.visual_events([one], [arrayed], now + 0.5)
                      if item["event"] == "modes"),
        "unarray": next(item for item in dsn.visual_events([arrayed], [one], now + 0.6)
                        if item["event"] == "modes"),
    }


class AssetBar:
    """In-memory device storage plus draw recording."""

    def __init__(self, existing: dict[str, bytes] | None = None) -> None:
        self.files = dict(existing or {})
        self.uploads: list[tuple[str, str, bytes]] = []
        self.removed: list[str] = []
        self.draws = []
        self.clears = 0
        self.closed = False

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))
        self.files[name] = blob

    async def storage_list(self, path: str):
        return SimpleNamespace(list=[
            SimpleNamespace(type="file", name=name, size=len(blob))
            for name, blob in self.files.items()
        ])

    async def storage_remove(self, path: str):
        self.removed.append(path)
        self.files.pop(PurePosixPath(path).name, None)

    async def display_draw(self, payload):
        self.draws.append(payload)

    async def display_clear(self, *, application_name: str):
        self.clears += 1

    async def aclose(self):
        self.closed = True


def animation_element(payload):
    elements = [element for element in payload.elements
                if element.type == "animation"]
    return elements[0] if len(elements) == 1 else None


def text_elements(payload) -> list:
    return [element for element in payload.elements if element.type == "text"]


def test_event_effect_maps_truthful_transition_direction_to_seven_finite_assets():
    events = transition_events()

    assert events["split"]["before_streams"] == 1
    assert events["split"]["streams"] == 3
    assert events["merge"]["before_streams"] == 3
    assert events["merge"]["streams"] == 2
    assert {name: dsn.event_effect(item) for name, item in events.items()} == {
        name: name for name in EVENT_EFFECTS
    }

    # Only an actual array-flag transition uses multi-dish convergence art.
    assert dsn.event_effect(event(
        "modes", before_flags=(False, False, False),
        flags=(True, False, False))) == "array"
    assert dsn.event_effect(event(
        "modes", before_flags=(True, True, False),
        flags=(False, True, False))) == "unarray"
    for before, after in (
        ((False, False, False), (False, True, False)),
        ((False, False, False), (False, False, True)),
        ((False, True, False), (False, False, True)),
    ):
        assert dsn.event_effect(event(
            "modes", before_flags=before, flags=after)) is None

    # Direction deltas stay native text. Generic acquire art cannot know
    # whether TX, RX, or both changed without inventing a lane.
    assert dsn.event_effect(event("direction", up=True, down=True)) is None
    assert dsn.event_effect(event("direction", up=False, down=False)) is None
    # Freshness remains a truthful native-text card rather than pretending the
    # finite RF grammar encodes source age.
    assert dsn.event_effect(event("stale")) is None
    assert dsn.event_effect(event("recovered")) is None

    assert dsn.event_label(event("streams", streams=2, bands=("X", "S"))) == \
        "VGR2 2 X/S"
    assert dsn.event_label(event("streams", streams=2, bands=("", "X"))) == \
        "VGR2 2 ?/X"
    assert dsn.event_label(event("streams", streams=2, bands=())) == \
        "VGR2 2 SIGNALS"


def test_every_event_effect_is_a_distinct_72x16_four_second_native_sequence():
    rendered: dict[str, tuple[bytes, ...]] = {}

    for effect in EVENT_EFFECTS:
        frames, fps, hold = dsn.render_event_frames(effect)
        assert frames
        assert fps > 0 and hold > 0
        assert len(frames) * hold / fps == pytest.approx(dsn.EVENT_TIMEOUT_S)
        assert {frame.size for frame in frames} == {(dsn.W, dsn.H)} == {(72, 16)}
        assert {frame.mode for frame in frames} == {"RGB"}
        sequence = tuple(frame.tobytes() for frame in frames)
        assert len(set(sequence)) > 1, f"{effect} is a static card, not an effect"
        rendered[effect] = sequence

    assert len(set(rendered.values())) == len(EVENT_EFFECTS), \
        "two named transitions render the same native animation"


def test_generic_transition_art_never_invents_direction_or_band_colour():
    semantic_rf = {dsn.UPLINK, *dsn.BAND_PULSE.values(), dsn.UNKNOWN_PULSE}
    panel_step = 77
    assert all(max(abs(a - b) for a, b in zip(dsn.EVENT_LINK, colour))
               >= panel_step for colour in semantic_rf)
    for effect in ("acquire", "loss", "handoff", "split", "merge"):
        frames, _, _ = dsn.render_event_frames(effect)
        colours = {
            pixel for frame in frames for pixel in frame.get_flattened_data()
        }
        assert colours.isdisjoint(semantic_rf), effect


def test_stream_art_has_one_dish_and_handoff_has_one_spacecraft_endpoint():
    split = dsn.render_event_frames("split")[0][-1]
    handoff_frames = dsn.render_event_frames("handoff")[0]

    assert sum(pixel == dsn.EVENT_DISH
               for pixel in split.get_flattened_data()) == 5
    assert all(sum(pixel == dsn.EVENT_DISH
                   for pixel in frame.get_flattened_data()) == 10
               for frame in handoff_frames)
    assert all(sum(pixel == dsn.EVENT_CRAFT
                   for pixel in frame.get_flattened_data()) == 5
               for frame in handoff_frames)


def test_prepare_event_assets_is_deterministic_idempotent_and_bounded():
    assert set(dsn.EVENT_ASSET_CODES) == set(EVENT_EFFECTS)
    assert len(set(dsn.EVENT_ASSET_CODES.values())) == len(EVENT_EFFECTS)
    stale = b"old event generation"
    unrelated = b"voice cache"
    first = AssetBar({"dsnevt_legacy.anim": stale,
                      "v2_afnova_1234567890.snd": unrelated})
    first_state = dsn.State()

    async def prepare_twice():
        await dsn.prepare_event_assets(first, first_state)
        uploaded_once = list(first.uploads)
        await dsn.prepare_event_assets(first, first_state)
        return uploaded_once

    uploaded_once = asyncio.run(prepare_twice())
    names = [name for _app, name, _blob in uploaded_once]

    assert len(names) == len(set(names)) == len(EVENT_EFFECTS)
    assert all(name.startswith("dsnevt_") and name.endswith(".anim") for name in names)
    assert all(
        len(name.encode("ascii")) <= dsn.DEVICE_ASSET_FILENAME_MAX
        for name in names
    ), "firmware rejects longer names with HTTP 400"
    assert all(blob.startswith(b"bicycle0") for _app, _name, blob in uploaded_once)
    assert first.uploads == uploaded_once, "a second prepare re-uploaded immutable assets"
    assert "dsnevt_legacy.anim" not in first.files
    assert first.files["v2_afnova_1234567890.snd"] == unrelated
    assert {name for name in first.files if name.startswith("dsnevt_")} == set(names)

    # A fresh process builds the same immutable paths, rather than timestamped
    # per-run names that accumulate forever.
    second = AssetBar()
    asyncio.run(dsn.prepare_event_assets(second, dsn.State()))
    assert [name for _app, name, _blob in second.uploads] == names


def test_warm_event_draw_references_resident_asset_and_never_uploads():
    state = dsn.State()
    bb = AssetBar()
    events = transition_events()

    async def scenario():
        await dsn.prepare_event_assets(bb, state)
        prepared_uploads = len(bb.uploads)
        payloads = {}
        for effect in ("acquire", "handoff"):
            state.event_queue[:] = [events[effect]]
            assert await dsn.show_next_event(bb, state)
            payloads[effect] = bb.draws[-1]
            assert not state.event_queue
            assert len(bb.uploads) == prepared_uploads
        return payloads

    payloads = asyncio.run(scenario())
    acquire = animation_element(payloads["acquire"])
    handoff = animation_element(payloads["handoff"])

    assert acquire is not None and handoff is not None
    assert acquire.path in bb.files and handoff.path in bb.files
    assert acquire.path != handoff.path
    # A transition plays once. Looping would falsely repeat an acquisition or
    # handoff while its four-second card still owns the display.
    assert acquire.loop is False and handoff.loop is False
    assert acquire.timeout == handoff.timeout == dsn.EVENT_TIMEOUT_S
    assert (acquire.id, acquire.x, acquire.y, acquire.display) == \
        (handoff.id, handoff.x, handoff.y, handoff.display), \
        "changing a native effect may mutate path, not element geometry"


def test_cold_event_uses_text_fallback_without_rendering_or_uploading(monkeypatch):
    state = dsn.State()
    target = event(
        "handoff", craft="ARTEMIS1", from_dish="DSS43", dish="DSS14")
    state.event_queue = [target]
    bb = AssetBar()

    def forbidden_render(*args, **kwargs):
        raise AssertionError("the event path tried to compile an animation")

    monkeypatch.setattr(dsn, "render_event_frames", forbidden_render)
    assert asyncio.run(dsn.show_next_event(bb, state)) is True

    assert bb.uploads == []
    assert state.event_queue == []
    assert animation_element(bb.draws[-1]) is None
    text = text_elements(bb.draws[-1])
    assert [element.text for element in text] == [dsn.event_label(target)]
    assert len(text[0].text) > 12
    assert text[0].scroll_rate == 1400


def test_event_409_preserves_exact_card_and_reuses_the_preloaded_path():
    state = dsn.State()
    target = event("handoff", from_dish="DSS43", dish="DSS14")
    following = event("acquire", craft="JNO", dish="DSS25")
    state.event_queue = [target, following]

    class RefuseOnceBar(AssetBar):
        def __init__(self) -> None:
            super().__init__()
            self.refused = False

        async def display_draw(self, payload):
            self.draws.append(payload)
            if not self.refused:
                self.refused = True
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)

    bb = RefuseOnceBar()

    async def scenario():
        await dsn.prepare_event_assets(bb, state)
        uploaded = len(bb.uploads)
        assert await dsn.show_next_event(bb, state) is False
        first_path = animation_element(bb.draws[-1]).path
        assert state.event_queue[0] is target
        assert state.event_queue[1] is following
        assert len(bb.uploads) == uploaded

        assert await dsn.show_next_event(bb, state) is True
        assert state.event_queue == [following]
        assert animation_element(bb.draws[-1]).path == first_path
        assert len(bb.uploads) == uploaded

    asyncio.run(scenario())


def test_different_craft_acquisitions_share_one_finite_effect_asset():
    state = dsn.State()
    bb = AssetBar()
    first = event("acquire", craft="VGR2", dish="DSS43")
    second = event("acquire", craft="JNO", dish="DSS25")

    async def scenario():
        await dsn.prepare_event_assets(bb, state)
        upload_count = len(bb.uploads)
        paths = []
        for target in (first, second):
            state.event_queue[:] = [target]
            assert await dsn.show_next_event(bb, state)
            paths.append(animation_element(bb.draws[-1]).path)
        return upload_count, paths

    upload_count, paths = asyncio.run(scenario())

    assert upload_count == len(EVENT_EFFECTS)
    assert paths[0] == paths[1]
    assert len(bb.uploads) == upload_count


async def park(*args, **kwargs):
    await asyncio.Event().wait()


def test_run_prepares_event_assets_in_background_without_delaying_first_scene(
        monkeypatch):
    selected = link()
    prepare_started = asyncio.Event()
    prepare_release = asyncio.Event()
    scene_drawn = asyncio.Event()

    class RunBar(AssetBar):
        async def display_draw(self, payload):
            self.draws.append(payload)
            if animation_element(payload) is not None:
                scene_drawn.set()

    bb = RunBar()

    async def connect():
        return bb

    async def noop(*args, **kwargs):
        return None

    async def seed_feed(state: dsn.State):
        state.links = [selected]
        state.feed_seeded = True
        state.feed_timestamp_ms = int(time.time() * 1000)
        state.feed_advanced_at = time.time()
        state.dirty.set()
        await asyncio.Event().wait()

    async def blocked_prepare(_bb, _state):
        prepare_started.set()
        await prepare_release.wait()

    monkeypatch.setattr(dsn, "aconnect", connect)
    monkeypatch.setattr(dsn, "sweep_stale_assets", noop)
    monkeypatch.setattr(dsn, "fetch_names", noop)
    monkeypatch.setattr(dsn, "load_speech_cache", noop)
    monkeypatch.setattr(dsn, "load_ranges", lambda _state: None)
    monkeypatch.setattr(dsn, "load_history", lambda _state: None)
    monkeypatch.setattr(dsn, "poll_feed", seed_feed)
    monkeypatch.setattr(dsn, "poll_names", park)
    monkeypatch.setattr(dsn, "poll_ranges", park)
    monkeypatch.setattr(dsn, "listen_input", park)
    monkeypatch.setattr(dsn, "rotate", park)
    monkeypatch.setattr(dsn, "prebake", park)
    monkeypatch.setattr(dsn, "prepare_event_assets", blocked_prepare)

    async def scenario():
        loop = asyncio.get_running_loop()
        original_add_signal_handler = loop.add_signal_handler
        loop.add_signal_handler = lambda *args, **kwargs: None
        running = asyncio.create_task(dsn.run(False))
        try:
            await asyncio.wait_for(prepare_started.wait(), 1.0)
            await asyncio.wait_for(scene_drawn.wait(), 1.0)
            concurrent = not prepare_release.is_set()
        except asyncio.TimeoutError:
            concurrent = False
        finally:
            prepare_release.set()
            running.cancel()
            await asyncio.gather(running, return_exceptions=True)
            loop.add_signal_handler = original_add_signal_handler
        return concurrent

    assert asyncio.run(scenario()), \
        "event preloading blocked startup/first scene instead of running in background"

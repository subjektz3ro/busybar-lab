"""The scrub timeline must never mix scenes.

Rendering 97 slots takes seconds and yields to the event loop, so a START
press lands mid-render. If the frames are sampled per-slot and the file is
labelled with whatever scene is current when it FINISHES, the label lies:
the reveal guard compares the label, serves the file, and the bar flashes
the previous theme before the animated render corrects it.
"""

import asyncio
import sys
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app import weather_timeline as sky_weather_timeline
from apps.skystrip_app.device import scrubber as sky_device_scrubber
from apps.skystrip_app.render import scene as sky_render_scene
from busybar_dev import anim


class FakeBar:
    def __init__(self):
        self.uploads = []

    async def assets_upload(self, app, name, blob):
        self.uploads.append(name)

    async def display_draw(self, payload):
        pass

    async def storage_remove(self, path):
        pass


async def run_one_rebuild(state, bb, timeout=10.0):
    task = asyncio.create_task(sky_device_scrubber.build_timeline(bb, state))
    try:
        async with asyncio.timeout(timeout):
            while not bb.uploads and state.timeline_meta is None:
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_timeline_never_mixes_scenes(monkeypatch):
    """A scene change mid-render must not produce a mislabelled timeline.

    The fix may discard and re-render, so several batches are legitimate —
    what must never happen is a single ENCODED file containing two scenes,
    because that file is what a scrub reveals.
    """
    encoded_batches, total = [], []
    state = sky_model.SkyState()
    state.hourly = [(None, {})]          # non-empty: the rebuild gate
    bb = FakeBar()

    monkeypatch.setattr(sky_settings, "ENABLED_SCENES", ("house", "skyline"))
    monkeypatch.setattr(sky_weather_timeline, "wx_at",
                        lambda st, t: sky_weather.WeatherState())

    def fake_encode(frames, **kw):
        # Exactly the frames that went into THIS file — a discarded render
        # never reaches encode, so slicing beats a running buffer.
        encoded_batches.append(total[-sky_limits.TIMELINE_SLOTS:])
        return b"fake-anim"
    monkeypatch.setattr(anim, "encode_anim", fake_encode)

    def fake_render(now, wx, seed, phase=0.0, scene="house", scrubbed=False):
        total.append(scene)
        # Simulate the operator pressing START partway through the render.
        if len(total) == 20:
            state.scene_idx += 1
        return Image.new("RGB", (sky_limits.W, sky_limits.H))
    monkeypatch.setattr(sky_render_scene, "render_scene", fake_render)

    await run_one_rebuild(state, bb)

    assert encoded_batches, "no timeline was ever encoded"
    for i, scenes in enumerate(encoded_batches):
        assert len(set(scenes)) == 1, (
            f"encoded timeline #{i} mixes scenes {sorted(set(scenes))} — "
            f"scrubbing it would flash the wrong theme")
    assert state.timeline_meta is not None
    assert state.timeline_meta["scene"] == encoded_batches[-1][0], (
        "timeline is labelled with a scene it was not rendered in; the "
        "reveal guard trusts this label")
    assert state.timeline_meta["scene"] == state.scene


async def test_timeline_labels_match_what_was_rendered(monkeypatch):
    """With no interference the happy path still labels honestly."""
    rendered_scenes = []
    state = sky_model.SkyState()
    state.hourly = [(None, {})]
    bb = FakeBar()

    monkeypatch.setattr(sky_settings, "ENABLED_SCENES", ("house", "skyline"))
    monkeypatch.setattr(sky_weather_timeline, "wx_at",
                        lambda st, t: sky_weather.WeatherState())
    monkeypatch.setattr(anim, "encode_anim",
                        lambda frames, **kw: b"fake-anim")

    def fake_render(now, wx, seed, phase=0.0, scene="house", scrubbed=False):
        rendered_scenes.append(scene)
        return Image.new("RGB", (sky_limits.W, sky_limits.H))
    monkeypatch.setattr(sky_render_scene, "render_scene", fake_render)

    await run_one_rebuild(state, bb)

    assert set(rendered_scenes) == {"house"}
    assert state.timeline_meta["scene"] == "house"
    assert bb.uploads and bb.uploads[0].startswith("tl_")

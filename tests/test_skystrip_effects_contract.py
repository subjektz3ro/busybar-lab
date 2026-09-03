"""Contracts for Skystrip's finite lightning and meteor effects.

These tests stay entirely host-side.  They inspect the exact PIL frames handed
to the native ``.anim`` encoder and the BUSY Bar payload that references the
uploaded asset; no network, device, or wall-clock delay participates.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
skystrip = pytest.importorskip("skystrip")


NOW = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)


def _luminance(pixel: tuple[int, int, int]) -> float:
    red, green, blue = pixel
    return 0.30 * red + 0.59 * green + 0.11 * blue


def _status_ink(now: datetime, phase: float) -> set[tuple[int, int]]:
    if phase >= 0.7:
        text = f"{round(10.0 * 9 / 5 + 32)}°"
    else:
        text = skystrip.clock_str(now)
    coordinates: set[tuple[int, int]] = set()
    # Mirrors _bake_status: text centered in the corner's reserved span.
    text_w = sum(len(skystrip.DIGITS_3X5[ch][0]) + 1 for ch in text) - 1
    cursor = max(1, (skystrip.STATUS_CARD_W - text_w) // 2)
    for character in text:
        glyph = skystrip.DIGITS_3X5[character]
        for row_index, row in enumerate(glyph):
            for column_index, bit in enumerate(row):
                if bit == "1":
                    coordinates.add((cursor + column_index, 1 + row_index))
        cursor += len(glyph[0]) + 1
    return coordinates


def _assert_backdrop_only(
    dark: Image.Image,
    lit: Image.Image,
    *,
    now: datetime,
    phase: float,
) -> None:
    """Prove semantic foreground ink survives while open sky gets brighter."""
    assert dark.size == lit.size == (72, 16)

    # The house sprite, the whole ground line, and the baked clock are all
    # composed after backdrop lightning.  They are a stable semantic mask,
    # independent of whatever color the sky happens to have tonight.
    foreground = {
        *((x, y) for x, y, _color in skystrip.HOUSE_SPRITE),
        *((x, 15) for x in range(skystrip.W)),
        *_status_ink(now, phase),
    }
    assert all(dark.getpixel(point) == lit.getpixel(point)
               for point in foreground)

    # Keep clear of the clock and the house: this is open backdrop in the
    # house scene.  A meaningful panel-visible lift changes many pixels and
    # gains far more than the panel's subtle-noise floor.
    sky = [(x, y) for y in range(0, 7) for x in range(24, 47)]
    changed = [point for point in sky
               if dark.getpixel(point) != lit.getpixel(point)]
    gain = sum(_luminance(lit.getpixel(point))
               - _luminance(dark.getpixel(point)) for point in sky) / len(sky)
    assert len(changed) >= len(sky) * 0.80
    assert gain >= 20.0

    # A white wash formerly filled all 1,152 pixels.  Sparse scene highlights
    # are legitimate, but an effect frame must never approach a white panel.
    near_white = sum(
        min(pixel) >= 245 for pixel in lit.get_flattened_data()
    )
    assert near_white < skystrip.W * skystrip.H * 0.10


def test_explicit_strike_brightens_only_the_sky_backdrop(monkeypatch):
    monkeypatch.setattr(skystrip, "elevation", lambda *_args: -12.0)
    weather = skystrip.WeatherState(
        cloud_frac=0.65,
        temp_c=10.0,
        humidity=50.0,
        visibility_m=16_000.0,
    )
    phase = 0.20
    dark = skystrip.render_scene(
        NOW, weather, seed=17, phase=phase, scene="house", lightning=0.0,
    )
    lit = skystrip.render_scene(
        NOW, weather, seed=17, phase=phase, scene="house", lightning=1.0,
    )

    _assert_backdrop_only(dark, lit, now=NOW, phase=phase)


def test_observed_thunder_loop_has_no_synthetic_lightning(monkeypatch):
    """A baked storm loop cannot synchronize the top LEDs, so it stays dark.

    Lightning belongs only to ``flash()``, where the front animation and top
    LED pulse are submitted in one device payload for a validated live strike.
    Normal moving rain may perturb a few pixels here; it must never create the
    broad luminance jump of the removed recurring sheet-lightning frames.
    """
    monkeypatch.setattr(skystrip, "elevation", lambda *_args: -12.0)
    weather = skystrip.WeatherState(
        cloud_frac=1.0,
        thunder=True,
        temp_c=10.0,
        humidity=50.0,
        visibility_m=16_000.0,
    )
    frames = skystrip.render_loop_frames(
        NOW,
        weather,
        seed=17,
        scene="house",
        n_frames=skystrip.ANIM_FRAMES,
    )
    sky = [(x, y) for y in range(0, 7) for x in range(24, 47)]
    means = [
        sum(_luminance(frame.getpixel(point)) for point in sky) / len(sky)
        for frame in frames
    ]
    assert max(means) - min(means) < 5.0


def test_lightning_segment_is_deterministic_and_owns_near_led_policy(
    monkeypatch,
):
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    weather = skystrip.WeatherState(cloud_frac=1.0, thunder=True)
    kwargs = {
        "phase0": 0.25,
        "scene": "house",
        "dist_km": skystrip.STRIKE_NEAR_KM,
    }

    first = skystrip.render_lightning_segment(NOW, weather, 17, **kwargs)
    second = skystrip.render_lightning_segment(NOW, weather, 17, **kwargs)
    distant = skystrip.render_lightning_segment(
        NOW,
        weather,
        17,
        phase0=0.25,
        scene="house",
        dist_km=skystrip.STRIKE_NEAR_KM + 0.01,
    )

    assert isinstance(first.frames, tuple)
    assert first.fps == distant.fps == 12
    assert first.timeout_s == distant.timeout_s == 2
    assert len(first.frames) == len(distant.frames) == 24
    assert len(first.frames) / first.fps == first.timeout_s
    assert [frame.tobytes() for frame in first.frames] == [
        frame.tobytes() for frame in second.frames
    ]
    assert first.led_notification_color == "#BBDDFFFF"
    assert distant.led_notification_color is None
    assert first.frames[0].getpixel((0, 0))[0] \
        < first.frames[1].getpixel((0, 0))[0]


def test_led_ping_draw_is_transparent_after_busylib_serialization():
    payload = skystrip._led_ping_payload("#3377EEFF")
    rectangle = payload.elements[0]

    assert rectangle.fill_colors == ["#00000000"]
    wire = payload.model_dump(mode="json", exclude_none=True)
    assert wire["elements"][0]["fill_colors"] == ["#00000000"]


def test_lightning_segment_fills_lease_with_original_pulse_and_moving_tail(
    monkeypatch,
):
    calls: list[tuple[float, float]] = []

    def capture_scene(
        _now,
        _weather,
        _seed,
        *,
        phase: float,
        scene: str,
        scrubbed: bool = False,
        lightning: float = 0.0,
    ) -> Image.Image:
        del scene, scrubbed
        calls.append((phase, lightning))
        # Encode phase into the fixture so repeated tail buffers would expose a
        # renderer that stopped advancing after the six-frame pulse.
        phase_color = round(phase * 10_000) % 256
        return Image.new(
            "RGB",
            (skystrip.W, skystrip.H),
            (phase_color, round(lightning * 255), 0),
        )

    monkeypatch.setattr(skystrip, "render_scene", capture_scene)
    distance = skystrip.STRIKE_NEAR_KM
    phase0 = 0.20
    segment = skystrip.render_lightning_segment(
        NOW,
        skystrip.WeatherState(cloud_frac=1.0, thunder=True),
        17,
        phase0=phase0,
        scene="house",
        dist_km=distance,
    )

    assert segment.fps == skystrip.FLASH_ANIM_FPS == 12
    assert segment.timeout_s == skystrip.FLASH_ELEMENT_TIMEOUT_S == 2
    assert len(segment.frames) == segment.fps * segment.timeout_s == 24

    peak = 0.42 + 0.58 * (
        1.0 - distance / skystrip.STRIKE_RADIUS_KM
    )
    assert [lightning for _phase, lightning in calls[:6]] == pytest.approx([
        0.0,
        peak,
        peak * 0.22,
        peak * 0.86,
        peak * 0.08,
        0.0,
    ])
    assert [lightning for _phase, lightning in calls[6:]] == [0.0] * 18

    loop_duration_s = skystrip.ANIM_FRAMES / skystrip.ANIM_FPS
    phase_step = 1.0 / (loop_duration_s * segment.fps)
    assert [phase for phase, _lightning in calls] == pytest.approx([
        (phase0 + index * phase_step) % 1.0
        for index in range(len(segment.frames))
    ])
    tail = segment.frames[6:]
    assert len({frame.tobytes() for frame in tail}) == len(tail)


class EffectBar:
    def __init__(self, after_upload=None) -> None:
        self.after_upload = after_upload
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.removed: list[str] = []
        self.operations: list[tuple[str, object]] = []

    async def assets_upload(self, application_name: str, name: str, blob: bytes):
        self.uploads.append((application_name, name, blob))
        self.operations.append(("upload", name))
        if self.after_upload is not None:
            self.after_upload()

    async def display_draw(self, payload):
        self.draws.append(payload)
        self.operations.append((
            "draw",
            tuple(element.id for element in payload.elements),
        ))

    async def storage_remove(self, path: str):
        self.removed.append(path)
        self.operations.append(("remove", path))


def _fake_scene(
    _now,
    _weather,
    _seed,
    *,
    phase: float,
    scene: str,
    scrubbed: bool = False,
    lightning: float = 0.0,
) -> Image.Image:
    del phase, scene, scrubbed
    sky = 20 + round(lightning * 120)
    image = Image.new("RGB", (skystrip.W, skystrip.H), (sky, sky, sky + 10))
    for x in range(skystrip.W):
        image.putpixel((x, 15), (18, 42, 16))
    return image


async def _no_sleep(_seconds: float) -> None:
    return None


def _capture_encoder(monkeypatch):
    encoded: list[tuple[list[Image.Image], int]] = []

    def capture(frames, fps, **_kwargs):
        copies = [frame.copy() for frame in frames]
        encoded.append((copies, fps))
        return b"bicycle0" + bytes([len(encoded)])

    monkeypatch.setattr(skystrip.anim, "encode_anim", capture)
    return encoded


def _mark_live_scene(state) -> None:
    state.current_scene_file = "sky-live.anim"
    state.weather_ready.set()
    state.weather_updated_at = asyncio.get_running_loop().time()


def test_flash_is_one_native_animation_with_near_only_top_led(monkeypatch):
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        bar.operations.append(("sleep", seconds))

    monkeypatch.setattr(skystrip.asyncio, "sleep", record_sleep)
    rendered = []
    render_segment = skystrip.render_lightning_segment

    def capture_segment(*args, **kwargs):
        segment = render_segment(*args, **kwargs)
        rendered.append(segment)
        return segment

    monkeypatch.setattr(skystrip, "render_lightning_segment", capture_segment)
    encoded = _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    bar = EffectBar()

    async def scenario():
        _mark_live_scene(state)
        await skystrip.flash(bar, state, skystrip.STRIKE_NEAR_KM)
        await skystrip.flash(bar, state, 40.0)

    asyncio.run(scenario())

    flash_draws = [
        payload for payload in bar.draws
        if any(element.id.startswith("flash") for element in payload.elements)
    ]
    assert len(encoded) == len(bar.uploads) == len(flash_draws) == 2
    assert len(rendered) == 2
    for (frames, fps), segment, payload in zip(encoded, rendered, flash_draws):
        assert fps == segment.fps
        assert [frame.tobytes() for frame in frames] == [
            frame.tobytes() for frame in segment.frames
        ]
        assert payload.led_notification_color == segment.led_notification_color
    assert all(
        fps == segment.fps
        and len(frames) == segment.fps * segment.timeout_s
        for (frames, fps), segment in zip(encoded, rendered)
    )
    elements = [payload.elements[0] for payload in flash_draws]
    assert all(len(payload.elements) == 1 for payload in flash_draws)
    assert all(element.type == "animation" for element in elements)
    assert all(
        element.loop is False and element.timeout == segment.timeout_s
        for element, segment in zip(elements, rendered)
    )
    assert len({element.id for element in elements}) == 2
    assert [payload.led_notification_color for payload in flash_draws] == [
        "#BBDDFFFF", None,
    ]
    near_frames, _near_fps = encoded[0]
    far_frames, _far_fps = encoded[1]
    baseline = far_frames[0].getpixel((0, 0))[0]
    assert baseline < far_frames[1].getpixel((0, 0))[0]
    assert far_frames[1].getpixel((0, 0))[0] \
        < near_frames[1].getpixel((0, 0))[0]
    assert not any(element.type == "rectangle" for payload in flash_draws
                   for element in payload.elements)
    assert all(
        any(pixel != (255, 255, 255)
            for pixel in frame.get_flattened_data())
        for frames, _fps in encoded for frame in frames
    )
    expected_operations = []
    for index, (_app, filename, _blob) in enumerate(bar.uploads, start=1):
        expected_operations.extend([
            ("upload", filename),
            ("draw", (f"flash{index}",)),
            (
                "sleep",
                rendered[index - 1].timeout_s
                + skystrip.FLASH_ASSET_RETIRE_GRACE_S,
            ),
            ("remove", f"/ext/user_assets/{skystrip.APP_NAME}/{filename}"),
        ])
    assert bar.operations == expected_operations
    assert sleeps == pytest.approx([
        segment.timeout_s + skystrip.FLASH_ASSET_RETIRE_GRACE_S
        for segment in rendered
    ])


def test_stale_queued_strike_is_not_replayed_after_weather_recovers():
    state = skystrip.SkyState()

    async def scenario():
        now = asyncio.get_running_loop().time()
        # The strike arrived while no fresh scene was eligible to show it.
        skystrip._enqueue_flash(
            state.flash_queue,
            5.0,
            observed_at=now - skystrip.FLASH_EVENT_TTL_S - 0.1,
        )
        _mark_live_scene(state)  # weather recovers after the old observation
        event = state.flash_queue.get_nowait()
        return skystrip._coalesce_fresh_flashes(
            state.flash_queue, event, now=now,
        )

    assert asyncio.run(scenario()) is None


def test_stale_near_strike_cannot_override_a_fresh_distant_strike():
    state = skystrip.SkyState()

    async def scenario():
        now = asyncio.get_running_loop().time()
        skystrip._enqueue_flash(
            state.flash_queue,
            5.0,
            observed_at=now - skystrip.FLASH_EVENT_TTL_S - 0.1,
        )
        skystrip._enqueue_flash(
            state.flash_queue, 40.0, observed_at=now,
        )
        first = state.flash_queue.get_nowait()
        event = skystrip._coalesce_fresh_flashes(
            state.flash_queue, first, now=now,
        )
        assert event is not None
        return skystrip._flash_distance(event)

    assert asyncio.run(scenario()) == 40.0


def test_push_scene_led_uses_the_same_weather_snapshot_as_its_frames(
    monkeypatch,
):
    _capture_encoder(monkeypatch)
    state = skystrip.SkyState(weather=skystrip.WeatherState(rain=True))
    bar = EffectBar(after_upload=lambda: setattr(
        state, "weather", skystrip.WeatherState(snow=True),
    ))
    frames = [Image.new("RGB", (skystrip.W, skystrip.H), (20, 30, 40))]

    asyncio.run(skystrip.push_scene(bar, state, NOW, frames))

    assert state.weather.snow is True
    assert len(bar.draws) == 1
    assert bar.draws[0].led_notification_color == "#3377EEFF"


def test_stale_flash_view_reclaims_upload_without_drawing(monkeypatch):
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    monkeypatch.setattr(skystrip.asyncio, "sleep", _no_sleep)
    _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    bar = EffectBar(after_upload=lambda: setattr(
        state, "view_generation", state.view_generation + 1,
    ))

    async def scenario():
        _mark_live_scene(state)
        await skystrip.flash(bar, state, 10.0)

    asyncio.run(scenario())

    assert len(bar.uploads) == 1
    filename = bar.uploads[0][1]
    assert bar.draws == []
    assert bar.removed == [f"/ext/user_assets/{skystrip.APP_NAME}/{filename}"]


def test_flash_does_not_cover_a_newer_same_scene_asset(monkeypatch):
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    monkeypatch.setattr(skystrip.asyncio, "sleep", _no_sleep)
    _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    bar = EffectBar(after_upload=lambda: setattr(
        state, "current_scene_file", "sky-newer.anim",
    ))

    async def scenario():
        _mark_live_scene(state)
        await skystrip.flash(bar, state, 10.0)

    asyncio.run(scenario())

    assert len(bar.uploads) == 1
    filename = bar.uploads[0][1]
    assert bar.draws == []
    assert state.current_scene_file == "sky-newer.anim"
    assert bar.removed == [f"/ext/user_assets/{skystrip.APP_NAME}/{filename}"]


class _FixedMeteorRandom:
    def __init__(self) -> None:
        self._ranges = iter((6, 0))

    def randrange(self, *_args, **_kwargs) -> int:
        return next(self._ranges)

    def choice(self, _values) -> int:
        return 3


def test_meteor_geometry_moves_inside_one_native_animation(monkeypatch):
    monkeypatch.setattr(skystrip.random, "Random", _FixedMeteorRandom)
    monkeypatch.setattr(
        skystrip,
        "render_scene",
        lambda *_args, **_kwargs: Image.new(
            "RGB", (skystrip.W, skystrip.H), (0, 0, 0),
        ),
    )
    monkeypatch.setattr(skystrip.asyncio, "sleep", _no_sleep)
    encoded = _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    bar = EffectBar()

    async def scenario():
        _mark_live_scene(state)
        await skystrip.meteor(bar, state)

    asyncio.run(scenario())

    assert len(encoded) == len(bar.draws) == 1
    frames, fps = encoded[0]
    assert fps == 12 and len(frames) == 8
    head = (242, 246, 255)
    heads = [
        next((x, y) for y in range(skystrip.H) for x in range(skystrip.W)
             if frame.getpixel((x, y)) == head)
        for frame in frames
    ]
    assert heads == [(6 + 3 * index, index) for index in range(8)]
    assert len({frame.tobytes() for frame in frames}) == len(frames)

    element = bar.draws[0].elements[0]
    assert len(bar.draws[0].elements) == 1
    assert element.type == "animation"
    assert element.loop is False and element.timeout == 2
    assert element.id.startswith("meteor")


def test_alert_arriving_during_meteor_upload_reclaims_it(monkeypatch):
    monkeypatch.setattr(skystrip.random, "Random", _FixedMeteorRandom)
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    monkeypatch.setattr(skystrip.asyncio, "sleep", _no_sleep)
    _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    marker = object()
    bar = EffectBar(after_upload=lambda: setattr(state, "visual_alert", marker))

    async def scenario():
        _mark_live_scene(state)
        await skystrip.meteor(bar, state)

    asyncio.run(scenario())

    assert len(bar.uploads) == 1
    filename = bar.uploads[0][1]
    assert bar.draws == []
    assert bar.removed == [f"/ext/user_assets/{skystrip.APP_NAME}/{filename}"]


def test_full_scene_effects_stay_dark_before_fresh_live_scene(monkeypatch):
    monkeypatch.setattr(skystrip, "render_scene", _fake_scene)
    monkeypatch.setattr(skystrip.asyncio, "sleep", _no_sleep)
    _capture_encoder(monkeypatch)
    state = skystrip.SkyState()
    bar = EffectBar()

    async def scenario():
        await skystrip.flash(bar, state, 5.0)
        await skystrip.meteor(bar, state)

    asyncio.run(scenario())

    assert bar.uploads == []
    assert bar.draws == []

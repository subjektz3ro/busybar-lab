"""Deterministic Skystrip scenarios backed by its production renderers."""

from __future__ import annotations

import importlib
import math
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import ModuleType
from typing import cast

from astral import Observer
from PIL import Image

from busybar_viz.models import (
    CheckSpec,
    Confidence,
    ControlSpec,
    DisplayTrack,
    EvidenceLevel,
    RegionSpec,
    RenderedSegment,
    RenderRequest,
    ScenarioSpec,
    SignalEvent,
    ensure_rgb_frames,
)
from busybar_viz.sources import app_source_paths

_RENDER_LOCK = threading.RLock()


def _find_checkout(start: Path | None = None) -> Path:
    """Locate the active checkout independently of this package's install path."""

    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        if (
            (directory / "AGENTS.md").is_file()
            and (directory / "apps" / "skystrip.py").is_file()
        ):
            return directory
    raise ValueError(
        "the Skystrip visualizer scenario must run inside a BUSY Bar Lab checkout"
    )


@dataclass(frozen=True)
class _SkystripRenderers:
    """Checkout-local production owners, without importing device or provider I/O."""

    settings: ModuleType
    config: ModuleType
    weather: ModuleType
    limits: ModuleType
    scene: ModuleType
    effects: ModuleType
    status: ModuleType
    art: ModuleType
    primitives: ModuleType


@lru_cache(maxsize=1)
def _skystrip() -> _SkystripRenderers:
    root = str(_find_checkout())
    if root not in sys.path:
        sys.path.insert(0, root)
    prefix = "apps.skystrip_app"
    return _SkystripRenderers(
        **{name: importlib.import_module(f"{prefix}.{name}")
           for name in ("settings", "config", "weather", "limits")},
        **{name: importlib.import_module(f"{prefix}.render.{name}")
           for name in ("scene", "effects", "status", "art", "primitives")},
    )


@contextmanager
def _deterministic_skystrip():
    """Fence renderer globals even when another test imported Skystrip first."""

    module = _skystrip()
    replacements = {
        "OBSERVER": Observer(latitude=0.0, longitude=0.0),
        "TZ": module.config.ZoneInfo("UTC"),
        "UNITS": "f",
        "CLOCK_INK": module.config.STATUS_INKS["orange"],
        "STYLE": "plain",
        "CHRISTMAS_WINDOW": "off",
        "CHRISTMAS_FORCED": False,
    }
    with _RENDER_LOCK:
        previous = {name: getattr(module.settings, name) for name in replacements}
        try:
            for name, value in replacements.items():
                setattr(module.settings, name, value)
            yield module
        finally:
            for name, value in previous.items():
                setattr(module.settings, name, value)


def _status_points(module, now: datetime, phase: float, temp_c: float):
    if phase >= 0.7:
        text = f"{round(temp_c * 9 / 5 + 32)}°"
    else:
        text = module.status.clock_str(now)
    points: set[tuple[int, int]] = set()
    # Mirrors _bake_status: text centered in the corner's reserved span.
    # The ink cells are the flash-invariant foreground; the shadow around
    # them is deliberately translucent scene and must NOT be pinned here.
    text_w = sum(len(module.art.DIGITS_3X5[ch][0]) + 1 for ch in text) - 1
    cursor = max(1, (module.limits.STATUS_CARD_W - text_w) // 2)
    for character in text:
        glyph = module.art.DIGITS_3X5[character]
        for row_index, row in enumerate(glyph):
            for column_index, bit in enumerate(row):
                if bit == "1":
                    points.add((cursor + column_index, 1 + row_index))
        cursor += len(glyph[0]) + 1
    return points


def _lightning_segment(request: RenderRequest) -> RenderedSegment:
    if request.inputs:
        raise ValueError("the Skystrip lightning scenarios do not accept input events")
    parameters = dict(request.parameters)
    allowed = {"distance_km", "fault"}
    unknown = sorted(set(parameters) - allowed)
    if unknown:
        raise ValueError(f"unknown Skystrip controls: {', '.join(unknown)}")
    distance_value = parameters.get(
        "distance_km",
        5.0 if request.scenario_id.endswith("near") else 40.0,
    )
    if isinstance(distance_value, bool):
        raise ValueError("distance_km must be a number from 0 through 60")
    try:
        distance = float(cast(str | int | float, distance_value))
    except (TypeError, ValueError) as exc:
        raise ValueError("distance_km must be a number from 0 through 60") from exc
    if not math.isfinite(distance) or not 0.0 <= distance <= 60.0:
        raise ValueError("distance_km must be a number from 0 through 60")
    fault = str(parameters.get("fault", "none"))
    if fault not in {"none", "white_wash"}:
        raise ValueError("fault must be 'none' or 'white_wash'")

    with _deterministic_skystrip() as module:
        now = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
        weather = module.weather.WeatherState(
            cloud_frac=0.65,
            temp_c=10.0,
            humidity=50.0,
            visibility_m=16_000.0,
        )
        seed, phase0, scene = 17, 0.20, "house"
        rendered = module.effects.render_lightning_segment(
            now, weather, seed, phase0=phase0, scene=scene, dist_km=distance,
        )
        frames = list(ensure_rgb_frames(rendered.frames))
        loop_duration_s = module.limits.ANIM_FRAMES / module.limits.ANIM_FPS
        phase_step = 1.0 / (loop_duration_s * rendered.fps)
        baselines = tuple(module.scene.render_scene(
            now,
            weather,
            seed,
            phase=(phase0 + index * phase_step) % 1.0,
            scene=scene,
            lightning=0.0,
        ).convert("RGB") for index in range(len(frames)))
        if fault == "white_wash":
            frames[1] = Image.new("RGB", (module.limits.W, module.limits.H), (255, 255, 255))

        foreground = {
            *((x, y) for x, y, _color in module.art.HOUSE_SPRITE),
            *((x, module.limits.H - 1) for x in range(module.limits.W)),
            *_status_points(module, now, phase0, weather.temp_c),
        }
        signals: tuple[SignalEvent, ...] = ()
        if rendered.led_notification_color is not None:
            signals = (SignalEvent(
                t_us=round(1_000_000 / rendered.fps),
                kind="top_led.pulse",
                value=rendered.led_notification_color,
            ),)
        near = distance <= module.limits.STRIKE_NEAR_KM
        return RenderedSegment(
            displays=(DisplayTrack(
                "front",
                tuple(frames),
                rendered.fps,
                Confidence.SOURCE_EXACT,
                baselines,
            ),),
            evidence_level=EvidenceLevel.RENDERER_VERIFIED,
            signals=signals,
            regions=(
                RegionSpec("foreground", points=tuple(sorted(foreground))),
                RegionSpec("open-sky", rect=(24, 0, 47, 7)),
            ),
            checks=(
                CheckSpec.create("dimensions", "frame.dimensions", size=(72, 16)),
                CheckSpec.create(
                    "near-white", "frame.near_white_fraction",
                    channel_min=245, max_fraction=0.10,
                ),
                CheckSpec.create(
                    "global-luminance", "frame.global_luminance_jump",
                    max_mean_jump=80.0,
                ),
                CheckSpec.create(
                    "foreground-stable", "region.foreground_preserved",
                    region="foreground",
                ),
                CheckSpec.create(
                    "sky-visible", "region.motion_required",
                    region="open-sky", min_changed_fraction=0.80,
                ),
                CheckSpec.create(
                    "native-lease", "animation.duration",
                    duration_us=rendered.timeout_s * 1_000_000,
                ),
                CheckSpec.create(
                    "post-flash-motion", "animation.max_static_run",
                    start_frame=rendered.pulse_frames, maximum=1,
                ),
                CheckSpec.create("effect-states", "animation.unique_frames", minimum=4),
                CheckSpec.create(
                    "top-led-policy", "top_led.allowed_condition", present=near,
                ),
            ),
            notes=(
                "Front pixels are exact production-rendered RGB frames.",
                "The animation fills its native lease with advancing scene frames.",
                "Top-LED evidence is logical intent, not physical observation.",
            ),
            source_paths=(*app_source_paths(_find_checkout()),
                          "busybar_viz/adapters/skystrip.py"),
        )


def _status_clock(request: RenderRequest) -> RenderedSegment:
    """The status clock against the sky it has to be read on.

    This region was the app's most legibility-sensitive element and had no
    registered coverage at all — the three lightning scenarios never asserted
    anything about it. It sat below the panel's contrast floor (a delta of 9
    against a bright sky) until somebody looked at the physical bar, which is
    exactly the failure the visualizer exists to prevent.

    `hour` is a control because the failure is time-dependent: the ink is a
    function of solar elevation and the sky it sits on moves with the same
    variable, so a single timestamp proves nothing about the other half of the
    day. `scrubbed` covers the Time Machine ink, which is a different colour
    with the same job.
    """
    if request.inputs:
        raise ValueError("the Skystrip status-clock scenario accepts no inputs")
    hour = float(cast(str | int | float, request.parameters.get("hour", 12.0)))
    if not 0.0 <= hour <= 23.0:
        raise ValueError("hour must be between 0 and 23")
    cloud = float(cast(
        str | int | float,
        request.parameters.get("cloud_frac", 0.2),
    ))
    if not 0.0 <= cloud <= 1.0:
        raise ValueError("cloud_frac must be between 0 and 1")
    scrubbed = bool(request.parameters.get("scrubbed", False))
    fault = str(request.parameters.get("fault", "") or "")
    if fault not in ("", "legacy_amber"):
        raise ValueError("unknown status-clock fault")

    with _deterministic_skystrip() as module:
        minute = int(round((hour - int(hour)) * 60)) % 60
        now = datetime(2026, 6, 15, int(hour), minute, tzinfo=timezone.utc)
        weather = module.weather.WeatherState(
            cloud_frac=cloud, temp_c=20.0, humidity=50.0,
            visibility_m=16_000.0,
        )
        if fault == "legacy_amber":
            # The historical behaviour, reproduced deliberately: amber ink
            # lerped IN as daylight rose, and no outline. This is what shipped
            # for months, and what a person found on the physical panel rather
            # than any tool. Keeping it renderable is what proves the contrast
            # check is worth having — a check nobody has watched fail is a
            # check nobody should trust.
            def legacy_status(px, now_, wx_, phase, scene="house",
                              scrubbed_=False, **kwargs):
                text = module.status.clock_str(now_)
                cursor = 2
                for character in text:
                    glyph = module.art.DIGITS_3X5[character]
                    for row_index, row in enumerate(glyph):
                        for column, bit in enumerate(row):
                            if bit == "1" and 0 <= cursor + column < module.limits.W:
                                px[cursor + column, 1 + row_index] = (224, 160, 70)
                    cursor += len(glyph[0]) + 1

            original = module.status._bake_status
            module.status._bake_status = legacy_status
            try:
                frames = ensure_rgb_frames(module.scene.render_loop_frames(
                    now, weather, seed=17, scene="house", scrubbed=scrubbed,
                ))
            finally:
                module.status._bake_status = original
        else:
            frames = ensure_rgb_frames(module.scene.render_loop_frames(
                now, weather, seed=17, scene="house", scrubbed=scrubbed,
            ))
        fps = max(1, round(len(frames) * module.limits.ANIM_FPS / module.limits.ANIM_FRAMES))

        if fault == "legacy_amber":
            ink = (224, 160, 70)
        elif scrubbed:
            ink = module.status.STATUS_INK_SCRUBBED
        else:
            ink = cast(tuple[int, int, int], module.config.STATUS_INKS["orange"])

        # The box the clock is drawn into, plus the one-pixel margin its
        # outline needs. Declared as a rect rather than the glyph points so the
        # check still means something if the glyphs move.
        return RenderedSegment(
            displays=(DisplayTrack(
                "front", frames, fps, Confidence.SOURCE_EXACT,
            ),),
            source_paths=(*app_source_paths(_find_checkout()),
                          "busybar_viz/adapters/skystrip.py"),
            evidence_level=EvidenceLevel.RENDERER_VERIFIED,
            regions=(
                RegionSpec("status-clock", rect=(1, 0, 22, 6)),
            ),
            checks=(
                CheckSpec.create("dimensions", "frame.dimensions", size=(72, 16)),
                # The law this scenario exists for. Hue or luminance: red's
                # contrast is channel separation (the skill's escape hatch),
                # and amber's Time-Machine tell clears the same criterion, so
                # both are required checks. The legacy fault keeps the
                # luminance-only criterion of its own era, so the audit still
                # demonstrably rejects what actually shipped for months.
                CheckSpec.create(
                    "clock-contrast", "region.contrast_floor",
                    region="status-clock", ink=[list(ink)],
                    contrast_mode=("luminance" if fault == "legacy_amber"
                                   else "luminance_or_channel"),
                ),
                # An outline that ate the glyphs would satisfy the contrast
                # check perfectly, so assert the strokes survive too.
                CheckSpec.create(
                    "clock-has-body", "region.min_feature_size",
                    region="status-clock", max_isolated=2,
                ),
                CheckSpec.create(
                    "loop-seams", "animation.loop_seam", max_delta=40.0,
                ),
            ),
        )


def _thunder_loop(request: RenderRequest) -> RenderedSegment:
    if request.inputs:
        raise ValueError("the Skystrip thunder-loop scenario does not accept inputs")
    with _deterministic_skystrip() as module:
        now = datetime(2026, 1, 15, 6, 0, tzinfo=timezone.utc)
        weather = module.weather.WeatherState(
            cloud_frac=1.0,
            thunder=True,
            rain=True,
            rain_tier=1,
            temp_c=10.0,
            humidity=50.0,
            visibility_m=16_000.0,
        )
        frames = ensure_rgb_frames(module.scene.render_loop_frames(
            now, weather, seed=17, scene="house",
        ))
        fps = max(1, round(len(frames) * module.limits.ANIM_FPS / module.limits.ANIM_FRAMES))
        return RenderedSegment(
            displays=(DisplayTrack(
                "front", frames, fps, Confidence.SOURCE_EXACT,
            ),),
            evidence_level=EvidenceLevel.RENDERER_VERIFIED,
            checks=(
                CheckSpec.create("dimensions", "frame.dimensions", size=(72, 16)),
                CheckSpec.create(
                    "near-white", "frame.near_white_fraction",
                    channel_min=245, max_fraction=0.10,
                ),
                CheckSpec.create(
                    "no-sheet-flash", "frame.global_luminance_jump",
                    max_mean_jump=8.0,
                ),
                CheckSpec.create("rain-moves", "animation.unique_frames", minimum=4),
                CheckSpec.create(
                    "no-unsynchronised-led", "top_led.allowed_condition", present=False,
                ),
            ),
            notes=(
                "Observed-thunder loops contain rain but no synthetic sheet lightning.",
            ),
            source_paths=(*app_source_paths(_find_checkout()),
                          "busybar_viz/adapters/skystrip.py"),
        )


class SkystripAdapter:
    id = "skystrip"

    def scenarios(self) -> tuple[ScenarioSpec, ...]:
        near_distance = ControlSpec(
            "distance_km", "Strike distance", "number", 5.0,
            minimum=0.0, maximum=60.0,
        )
        distant_distance = ControlSpec(
            "distance_km", "Strike distance", "number", 40.0,
            minimum=0.0, maximum=60.0,
        )
        return (
            ScenarioSpec(
                "skystrip/lightning-near",
                "Skystrip: nearby lightning",
                "Backdrop-only lightning with one logical top-LED pulse.",
                self.id,
                (near_distance,),
            ),
            ScenarioSpec(
                "skystrip/lightning-distant",
                "Skystrip: distant lightning",
                "A subtler backdrop flash with no top-LED notification.",
                self.id,
                (distant_distance,),
            ),
            ScenarioSpec(
                "skystrip/status-clock",
                "Skystrip: status clock legibility",
                "The clock against the sky it must be read on, at any hour.",
                self.id,
                (
                    ControlSpec("hour", "Local hour", "number", 12.0,
                                minimum=0.0, maximum=23.0),
                    ControlSpec("cloud_frac", "Cloud cover", "number", 0.2,
                                minimum=0.0, maximum=1.0),
                    ControlSpec("scrubbed", "Time Machine ink", "boolean", False),
                    ControlSpec("fault", "Fault injection", "choice", "",
                                choices=("", "legacy_amber")),
                ),
            ),
            ScenarioSpec(
                "skystrip/thunder-loop",
                "Skystrip: observed thunder loop",
                "Rain motion without recurring synthetic sheet lightning.",
                self.id,
            ),
        )

    def render(self, request: RenderRequest) -> RenderedSegment:
        if request.scenario_id in {
            "skystrip/lightning-near", "skystrip/lightning-distant",
        }:
            return _lightning_segment(request)
        if request.scenario_id == "skystrip/status-clock":
            return _status_clock(request)
        if request.scenario_id == "skystrip/thunder-loop":
            if request.parameters:
                raise ValueError("the thunder-loop scenario has no controls")
            return _thunder_loop(request)
        raise KeyError(f"unknown Skystrip scenario: {request.scenario_id}")

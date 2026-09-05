"""Skystrip render / effects."""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime

from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import scene as _render_scene


@dataclass(frozen=True)
class LightningSegment:
    """Exact host-rendered strike frames plus the native top-LED intent."""

    frames: tuple[Image.Image, ...]
    fps: int
    timeout_s: int
    pulse_frames: int
    led_notification_color: str | None


def render_lightning_segment(
    now: datetime,
    wx: _weather.WeatherState,
    seed: int,
    *,
    phase0: float,
    scene: str,
    dist_km: float,
) -> LightningSegment:
    """Render one deterministic lightning burst without touching the device.

    The front frames are the exact RGB buffers passed to the native animation
    encoder.  ``led_notification_color`` records the accompanying firmware
    blink request; only a strike inside the measured near radius earns one.
    """
    distance = min(max(dist_km, 0.0), _limits.STRIKE_RADIUS_KM)
    closeness = 1.0 - distance / _limits.STRIKE_RADIUS_KM
    peak = 0.42 + 0.58 * closeness
    pulse = (0.0, peak, peak * 0.22, peak * 0.86, peak * 0.08, 0.0)
    # A one-shot animation holds its final frame until the native element
    # timeout.  The pulse itself is only 0.5 s, while firmware timeouts are
    # whole seconds; ending the asset there pinned a static storm frame over
    # the still-running sky for the remaining 1.5 s.  Fill the complete lease
    # with ordinary advancing scene frames so rain/cloud motion never pauses.
    frame_count = _limits.FLASH_ANIM_FPS * _limits.FLASH_ELEMENT_TIMEOUT_S
    intensities = pulse + (0.0,) * (frame_count - len(pulse))
    loop_duration_s = _limits.ANIM_FRAMES / _limits.ANIM_FPS
    phase_step = 1.0 / (loop_duration_s * _limits.FLASH_ANIM_FPS)
    frames = tuple(
        _render_scene.render_scene(
            now,
            wx,
            seed,
            phase=(phase0 + index * phase_step) % 1.0,
            scene=scene,
            lightning=intensity,
        )
        for index, intensity in enumerate(intensities)
    )
    return LightningSegment(
        frames=frames,
        fps=_limits.FLASH_ANIM_FPS,
        timeout_s=_limits.FLASH_ELEMENT_TIMEOUT_S,
        pulse_frames=len(pulse),
        led_notification_color=(
            "#BBDDFFFF" if distance <= _limits.STRIKE_NEAR_KM else None
        ),
    )

    # A session owns the display; the sky keeps its secret.


# A small rural passenger train, Ghibli-flavoured (the operator asked for
# "my neighbor totoro", 2026-08-15): a few short carriages in one dark
# livery with warm lit windows, not a mile of American boxcars. The read
# comes from the window rhythm — evenly spaced warm squares sliding along a
# dark body — so the body stays dark enough at every hour for them to glow.
RAILCAR_LIVERIES = ((36, 58, 44), (58, 40, 36), (40, 46, 62))

RAILCAR_WINDOW_NIGHT = (255, 206, 128)

RAILCAR_WINDOW_DAY = (150, 170, 186)  # daylight on glass, not lamplight

RAILCAR_LEN = 6  # 5 body columns and a coupling gap


def _freight_frames(
    band: Image.Image,
    night: bool,
    rng: random.Random,
    foreground: frozenset = frozenset(),
) -> list[Image.Image]:
    """Render a short passenger train crossing the full-width sky band
    (scene rows 0-5) at 12fps, one pixel per frame.

    Carriages ride rows 2-4 on the rail at row 5; the caller repaints the
    status digits and the foreground trees on top, so the train passes
    behind both. It enters and leaves at TRUE screen edges.
    """
    livery = RAILCAR_LIVERIES[rng.randrange(len(RAILCAR_LIVERIES))]
    roof = _render_primitives._rgb_int(v * 0.65 for v in livery)
    window = RAILCAR_WINDOW_NIGHT if night else RAILCAR_WINDOW_DAY
    n_cars = rng.randint(3, 5)
    # unroll the consist into columns: (col-within-car, is_last_car)
    cols: list[int | None] = []
    for _ in range(n_cars):
        for c in range(RAILCAR_LEN - 1):
            cols.append(c)
        cols.append(None)  # the gap between carriages
    dirn = rng.choice((1, -1))
    total = len(cols)
    frames: list[Image.Image] = []
    bw = band.width
    base_px = _render_primitives._rgb_pixels(band)
    for f in range(bw + total + 6):
        im = band.copy()
        pxb = _render_primitives._rgb_pixels(im)
        for i, col in enumerate(cols):
            x = (f - i) if dirn > 0 else (bw - 1 - (f - i))
            if col is None or not (0 <= x < bw):
                continue
            pxb[x, 2] = roof
            pxb[x, 3] = livery
            pxb[x, 4] = livery
            # Two warm windows per carriage, always in the same places, so
            # the rhythm reads as a train rather than as noise.
            if col in (1, 3):
                pxb[x, 3] = window
            # The leading edge of the leading carriage carries the lamp.
            leading = i == 0 if dirn > 0 else i == total - 1
            if leading and col == 0 and night:
                pxb[x, 4] = (255, 236, 176)
        for fx, fy in foreground:
            if 0 <= fx < bw and 0 <= fy < im.height:
                pxb[fx, fy] = base_px[fx, fy]
        frames.append(im)
    return frames

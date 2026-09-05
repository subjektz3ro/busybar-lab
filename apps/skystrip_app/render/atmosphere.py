"""Skystrip render / atmosphere."""

from __future__ import annotations

import random
from datetime import datetime

from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import primitives as _render_primitives

# What the air itself glows when it is full of something. These are
# AIRLIGHT colours — the light the medium scatters toward you — not paint
# laid over the scene. See _apply_obscuration for why that distinction is
# the whole design.
# Tuned so every pair clears ~30% on at least two channels, which is what
# the panel needs to tell two colours apart at all. The first attempt put
# haze and dust one channel apart and they were the same colour on the
# strip; the fix was to separate the two grey ones by LIGHTNESS and the two
# warm ones by saturation, rather than nudging all four around a hue wheel
# they were too crowded on. `test_the_four_tints_are_distinguishable` pins it.
OBSCURATION_TINT = {
    "haze": (190, 194, 204),  # bright milky white, faintly blue
    "smoke": (150, 62, 24),  # deep orange-red: a wildfire day
    "dust": (176, 124, 48),  # warm tan; sand shares it
    "ash": (100, 96, 100),  # dark neutral mineral grey, no warmth
}

# How much light survives each medium. Lower = thicker. These are not one
# constant because the media are not equally opaque, and a shared value
# made volcanic ash read as "a slightly dimmer blue day": ash is a dark
# tint, so at high transmission the blue sky simply showed through it.
# Density is what separates an ashfall from a haze, so it belongs here
# rather than in the colour.
OBSCURATION_TRANSMISSION = {
    "haze": 0.55,  # milky, but you can still see the sky through it
    "smoke": 0.32,
    "dust": 0.30,
    "ash": 0.18,  # blots the sky out; the sun becomes a disc you can look at
}


def _apply_obscuration(px, kind: str, daylight: float) -> None:
    """Haze, smoke, dust or volcanic ash filling the whole air column.

    This is the standard atmospheric model, and using the real one is what
    keeps it legal on this panel:

        observed = object * transmission + airlight * (1 - transmission)

    The tempting version — lerp every pixel toward a smoke colour — lights
    up a black night sky, which is precisely the filled-row haze failure
    that `settle_snow` and SNOW_FRACTION exist to prevent. Airlight is
    *scattered sunlight*, so scaling it by daylight makes the formula do
    the right thing at both ends of the day for free: at noon the air
    glows and distance dissolves into it, at midnight there is nothing to
    scatter, so the medium only subtracts and the sky stays properly dark
    with its stars swallowed.

    Nothing here knows which scene it is drawing. Depth comes from the art
    itself: distant things were already drawn dim, so they land closest to
    the airlight and disappear first, while bright emissive marks — lit
    windows, lamps, the campfire — stay above it and survive. That is
    aerial perspective, and it falls out of the physics rather than out of
    a per-scene table.
    """
    tint = OBSCURATION_TINT.get(kind)
    if tint is None:
        return
    t = OBSCURATION_TRANSMISSION[kind]
    airlight = [c * daylight * (1.0 - t) for c in tint]
    for y in range(_limits.H):
        for x in range(_limits.W):
            px[x, y] = tuple(
                min(255, int(c * t + a)) for c, a in zip(px[x, y], airlight)
            )


def _draw_clouds(
    px, now: datetime, cloud: float, daylight: float, stormy: bool
) -> None:
    """Soft puffs drifting slowly right-to-left, count scaled by cover."""
    count = min(5, round(cloud * 5) + (1 if cloud > 0.1 else 0))
    if count == 0:
        return
    col: _render_primitives.Color
    if stormy:
        col = (52, 62, 58)
    else:
        col = _render_primitives._lerp_rgb((36, 40, 54), (198, 202, 208), daylight)
    drift = (now.timestamp() / 60.0) * 1.2  # px per minute, continuous
    puff_rng = random.Random(31)
    span = _limits.W + 28
    for _ in range(count):
        base_x = puff_rng.randrange(0, span)
        cw = puff_rng.randrange(7, 12)
        ch = puff_rng.choice((2, 2, 3))
        cy = puff_rng.randrange(1, 6)
        cx = int((base_x - drift) % span) - 14
        strength = 0.35 + 0.3 * cloud
        for dy in range(-ch, ch + 1):
            for dx in range(-cw, cw + 1):
                x, y = cx + dx, cy + dy
                if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                    continue
                d = (dx / cw) ** 2 + (dy / ch) ** 2
                if d >= 1.0:
                    continue
                f = strength * (1.0 - d) ** 1.5
                px[x, y] = tuple(
                    int(o * (1 - f) + c * f) for o, c in zip(px[x, y], col)
                )

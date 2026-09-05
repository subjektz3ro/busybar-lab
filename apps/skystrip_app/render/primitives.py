"""Skystrip render / primitives."""

from __future__ import annotations

import io
import math
from collections.abc import Iterable
from typing import Protocol, cast

from PIL import Image

from apps.skystrip_app import limits as _limits

RGB = tuple[int, int, int]

Color = tuple[float, float, float]


class RGBPixels(Protocol):
    """Typed view of ``Image.load()`` for images created or converted as RGB."""

    def __getitem__(self, position: tuple[int, int]) -> RGB: ...

    def __setitem__(self, position: tuple[int, int], value: object) -> None: ...


def _rgb_pixels(image: Image.Image) -> RGBPixels:
    """Narrow Pillow's mode-dependent pixel union at the explicit RGB boundary."""
    pixels = image.load()
    if pixels is None:  # Pillow RGB images expose storage; retain a clear guard.
        raise RuntimeError("RGB image has no pixel storage")
    return cast(RGBPixels, pixels)


def _rgb_int(values: Iterable[float]) -> RGB:
    """Materialize the app's three-channel color calculations as RGB integers."""
    red, green, blue = values
    return int(red), int(green), int(blue)


# (solar elevation°, horizon RGB, zenith RGB) — interpolated between rows
SKY_KEYFRAMES = [
    (-90.0, (6, 8, 20), (1, 2, 8)),  # deep night
    (-18.0, (8, 10, 26), (2, 3, 12)),  # astronomical twilight begins
    (-12.0, (38, 26, 62), (6, 9, 30)),  # nautical: mauve horizon
    (-6.0, (150, 70, 70), (18, 26, 66)),  # civil: ember horizon, indigo up
    (-2.0, (232, 116, 62), (44, 58, 110)),  # sunset ember
    (2.0, (244, 168, 88), (92, 124, 176)),  # golden hour
    (10.0, (196, 200, 190), (74, 128, 196)),  # low sun, hazy horizon
    (30.0, (158, 200, 232), (52, 110, 186)),  # midday
    (90.0, (170, 208, 236), (46, 104, 182)),
]


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(c1: Color, c2: Color, t: float) -> Color:
    return (
        _lerp(c1[0], c2[0], t),
        _lerp(c1[1], c2[1], t),
        _lerp(c1[2], c2[2], t),
    )


def _sky_colors(elev: float) -> tuple[Color, Color]:
    frames = SKY_KEYFRAMES
    for (e1, h1, z1), (e2, h2, z2) in zip(frames, frames[1:]):
        if e1 <= elev <= e2:
            t = (elev - e1) / (e2 - e1)
            return _lerp_rgb(h1, h2, t), _lerp_rgb(z1, z2, t)
    return frames[-1][1], frames[-1][2]


def _add_glow(
    px, cx: int, cy: int, color: tuple, radius: float, strength: float
) -> None:
    """Additively blend a soft radial glow into the frame."""
    r = int(radius) + 1
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            x, y = cx + dx, cy + dy
            if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                continue
            d = math.hypot(dx, dy)
            if d > radius:
                continue
            f = strength * (1.0 - d / radius) ** 2
            old = px[x, y]
            px[x, y] = tuple(min(255, int(o + c * f)) for o, c in zip(old, color))


MOON_DAY_OVERRIDE: float | None = None  # preview-only phase forcing

# Shadowed side: dark enough that a thin crescent's lit sliver visibly
# outshines it. At (52, 54, 68) the earthshine body was within a whisker of
# the crescent's apparent brightness on the panel near new moon, and the
# whole disc read as one flat grey blob (called from the physical bar,
# 2026-08-11) — the sphere hint survives at a third the level.
MOON_EARTHSHINE = (30, 32, 42)

MOON_TERMINATOR = (128, 128, 122)  # one soft step between light and shadow

# The face never changes: fixed maria patches, (dx, dy, darkening factor).
# LED gamma crushes subtle deltas — anything under ~30% darker vanishes on
# the physical panel, so these are far stronger than they look in preview.
MOON_MARIA = {(-1, -1): 0.60, (1, 0): 0.52, (0, 1): 0.68, (-2, 1): 0.64}

# Earth's shadow is not black. What reaches the eclipsed Moon is sunlight
# bent through the whole rim of Earth's atmosphere, with the blue scattered
# out of it long before it arrives — every sunrise on the planet at once,
# which is why a shadowed Moon goes copper rather than dark.
#
# Two levels, not a ramp: the panel crushes anything under ~30% per channel,
# and these clear it (R -51%, G -62%, B -45%) so the gradient survives the
# LEDs. The rim is the bright orange fringe just inside the shadow's edge;
# the ember is the deeper interior. Against MOON_COLOR the rim is a -34% /
# -77% / -89% step, so the shadow's boundary reads as a hard bite.
MOON_UMBRA_RIM = (150, 52, 22)

MOON_UMBRA_EMBER = (74, 20, 12)

# Depth in Moon-radii past the umbra's edge at which rim becomes ember.
MOON_UMBRA_EMBER_DEPTH = 0.9


def _ambient(elev: float, cloud: float, wx) -> Color:
    """RGB multipliers for light falling on STRUCTURES (never on things
    that emit their own light). Golden hour warms, overcast flattens and
    cools, storms drop everything into green-leaning gloom, rain dims."""
    daylight = min(max((elev + 6) / 18, 0.0), 1.0)
    r = g = b = 1.0
    if daylight > 0:
        if elev < 14 and cloud < 0.7:  # low visible sun gilds what it hits
            w = max(0.0, 1 - max(elev, 0) / 14) * (1 - cloud) * daylight
            r *= 1 + 0.60 * w
            g *= 1 + 0.10 * w
            b *= 1 - 0.50 * w
        dim = 0.32 * cloud * daylight  # overcast flattens the day
        r *= 1 - dim
        g *= 1 - dim * 0.95
        b *= 1 - dim * 0.85
    if wx.stormy:
        r *= 0.55
        g *= 0.62
        b *= 0.58
    elif wx.rain:
        r *= 0.78
        g *= 0.80
        b *= 0.82
    return (r, g, b)


def _shade(color: Color, amb: Color) -> RGB:
    return (
        max(0, min(255, int(color[0] * amb[0]))),
        max(0, min(255, int(color[1] * amb[1]))),
        max(0, min(255, int(color[2] * amb[2]))),
    )


# Status-clock inks: ONE saturated hue at a time, every hour, every scene.
# Contrast is hue, not brightness — the panel resolves a >=30%
# single-channel separation as readily as a luminance delta (the skill's
# escape hatch; red proved it on hardware, 2026-08-12: "reads very well").
# Red then failed on DESIGN — it is this product's alarm colour — so the
# ink is a closed operator choice: every entry below is pre-proven against
# the corner's measured backgrounds (blue sky, overcast white, the sun's
# cream, night, pine dark, the steel rail) by the contract tests, which
# sweep them all. Barkeep's enum validation refuses anything else, so no
# unreadable clock is even configurable.


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()

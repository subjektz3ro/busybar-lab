"""Skystrip render / precipitation."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import primitives as _render_primitives


def _streak_color(sky: _render_primitives.RGB) -> _render_primitives.RGB:
    """Rain against bright cloud is darker than it; against night, lighter.

    Either way the streak clears the panel's ~30%-per-channel floor. Picking
    one direction cannot: additive dies on a bright sky, subtractive on night.
    """
    lum = 0.3 * sky[0] + 0.59 * sky[1] + 0.11 * sky[2]
    if lum > _render_art.RAIN_DARK_SKY_LUM:
        return _render_primitives._rgb_int(c * 0.62 for c in sky)
    return (
        min(255, sky[0] + 55),
        min(255, sky[1] + 55),
        min(255, sky[2] + 55),
    )


def _rain_tier(wx) -> int:
    """A storm is never a drizzle, but it does not erase a measured intensity.

    This used to pin every storm to tier 2. That was fine while the only
    source was radar dBZ, and wrong as soon as scrubbing began replaying
    observed history: the station recorded "Thunderstorms and Rain" -- plain
    moderate rain -- and pinning turned it into a downpour on the panel.
    Floor it instead, so an observed heavy stays heavy and an observed
    moderate stays moderate.
    """
    tier = min(max(wx.rain_tier, 0), 2)
    return max(tier, 1) if wx.stormy else tier


def is_raining(wx) -> bool:
    """Snow wins: the two never fall together, and snow draws its own flakes."""
    return (wx.rain or wx.stormy) and not wx.snow


SNOW_FLAKES = 12

SNOW_FLAKE_COLOR = (235, 235, 240)

SNOW_CROSSINGS = 2  # a drifting descent every four seconds


def draw_snow(px, seed: int, phase: float) -> None:
    """Drift flakes down the whole panel.

    Same two guarantees as draw_rain, for the same reasons: coverage comes
    from stratified buckets rather than from the seed being kind, and `fall`
    rounds rather than truncates. Neither was visibly broken here -- snow
    moves at 0.75 rows/frame, where truncation and rounding agree, and 12
    flakes clump onto one side in only ~0.7% of seeds. They are matched to
    the rain path so the next person to change SNOW_CROSSINGS does not
    rediscover both defects the hard way.
    """
    flake_rng = random.Random(seed * 17 + 3)
    span = _limits.H - 1
    fall = round(phase * SNOW_CROSSINGS * span)
    for i in range(SNOW_FLAKES):
        lo, hi = i * _limits.W // SNOW_FLAKES, (i + 1) * _limits.W // SNOW_FLAKES
        fx0 = flake_rng.randrange(lo, hi)
        fy0 = flake_rng.randrange(0, span)
        # Sway is +-1, so a flake can lean one column into its neighbour.
        # That is drift, not a coverage hole: the buckets are 6 wide.
        sway = round(math.sin(math.tau * 2 * phase + i * 1.3))
        px[(fx0 + sway) % _limits.W, (fy0 + fall) % span] = SNOW_FLAKE_COLOR


def draw_rain(px, wx, seed: int, phase: float) -> None:
    """Streak rain across the whole panel at the intensity `wx` implies.

    Split out of render_scene so the coverage and seam guarantees can be
    asserted on a blank canvas instead of inferred from a composed frame.
    """
    drops, crossings, length = _render_art.RAIN_TIERS[_rain_tier(wx)]
    drop_rng = random.Random(seed * 13 + 5)
    streak = _streak_color(px[36, 4])
    span = _limits.H - 1
    # round, not int: phase is i/n, which is inexact in binary, so truncation
    # turns a steady 3 rows/frame into a 2-3-4 stutter. Rounding still lands
    # on crossings*span at phase 1.0, so the loop seam stays exact.
    fall = round(phase * crossings * span)
    slant = 1 if wx.wind_kmh >= 25 else 0
    for i in range(drops):
        # One drop per column bucket, jittered inside it. Sampling the full
        # width instead let all of them land on one side of the panel and
        # stay there for the whole ten-minute seed window.
        rx = drop_rng.randrange(i * _limits.W // drops, (i + 1) * _limits.W // drops)
        y0 = drop_rng.randrange(0, span)
        ry = (y0 + fall) % span
        for j in range(length):
            # The streak trails UP from the falling head, and is clipped at
            # the top rather than wrapped: a drop entering frame from above
            # is right, a streak split across both edges is not.
            y, x = ry - j, rx + j * slant
            if y < 0 or not 0 <= x < _limits.W:
                break
            # _lerp_rgb returns floats; the framebuffer wants ints.
            px[x, y] = tuple(
                int(c)
                for c in _render_primitives._lerp_rgb(
                    px[x, y], streak, _render_art.RAIN_TAPER[j]
                )
            )


def settle_snow(px, tops: dict[int, int], tier: int, amb: tuple = (1, 1, 1)) -> None:
    """Lay settled snow on whatever surface `tops` describes.

    `tops[x]` is the y of the topmost surface pixel in column x; snow lands on
    that pixel. Mutates px in place.

    Drawn MOSTLY-OFF on purpose. The panel's LEDs are 1.23mm lit on a 2.2mm
    pitch, so a filled bright row reads as a haze of separated dots rather
    than as a surface, and it drowns the scene above it. Sparse bright marks
    are what actually read as snow.
    """
    if tier <= 0:
        return
    # Fall back to the deepest DEFINED tier, never to 1.0: an unrecognized
    # tier must degrade to "as much snow as we ever draw", not to "every
    # column lit", which is the haze failure this whole function exists to
    # prevent. Don't "simplify" this back to 1.0.
    take = int(
        len(_render_art._SNOW_ORDER)
        * _render_art.SNOW_FRACTION.get(tier, _render_art.SNOW_FRACTION[3])
    )
    lit = _render_primitives._shade(_render_art.SNOW_LIT, amb)
    shade = _render_primitives._shade(_render_art.SNOW_SHADE, amb)
    for i, x in enumerate(_render_art._SNOW_ORDER[:take]):
        y = tops.get(x)
        if y is None or not (0 <= y < 16):
            continue
        px[x, y] = lit if i % 3 else shade  # broken, not a solid bar
        if tier >= 3 and y + 1 < 16:
            px[x, y + 1] = shade  # depth: a second, darker row


def surface_tops(px, x_range, y_range, sky: set) -> dict[int, int]:
    """The topmost non-sky pixel in each column: where snow would land.

    Rooftops, banks and road shoulders are all the same question asked of
    different scenes, so they share one answer. Columns that are sky all the
    way down are omitted rather than defaulted, so nothing snows in mid-air.
    """
    tops = {}
    for x in x_range:
        for y in y_range:
            if px[x, y] not in sky:
                tops[x] = y
                break
    return tops


def snow_tier(depth_m: float | None) -> int:
    """Settled snow depth as one of four looks: 0 none, 1 dusting,
    2 covered, 3 deep.

    Three visible steps rather than a ramp because the panel's ~30% contrast
    floor crushes anything finer -- a smooth scale would be both invisible
    and untestable.
    """
    if not depth_m or depth_m < _render_art.SNOW_DUSTING_M:
        return 0
    if depth_m < _render_art.SNOW_COVERED_M:
        return 1
    if depth_m < _render_art.SNOW_DEEP_M:
        return 2
    return 3

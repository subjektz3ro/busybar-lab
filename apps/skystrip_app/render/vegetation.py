"""Skystrip render / vegetation."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits

# The backroads verge: individual grass tufts, not a comb of blades.
#
# Three attempts got here. A brightness ripple was invisible (the panel
# crushes sub-30% luminance deltas). Per-column blade heights driven by one
# sine read as "weird green lines" — every column occupied, one wavelength,
# one hard threshold, so the whole verge pulsed like a metronome.
#
# What the lakefront's water gets right is not its formula but its
# IRREGULARITY: every row there carries its own wavelength and its own
# phase offset, so nothing lines up into a stripe. Grass needs the same
# irregularity with its own physics — discrete tufts that bend, at
# irregular spacings, each with its own stiffness, so identical wind moves
# them by different amounts. Gaps between tufts are what make the ones that
# remain read as objects rather than as a texture.
_verge_rng = random.Random(4127)

# Base height of the grass in each column, above the ground row. Irregular
# so the standing silhouette is ragged rather than a comb; the wind adds to
# these, and a sparse version (11 tufts) left the gust nothing to show
# itself on — 0.7 changed pixels per frame, invisible.
VERGE_BLADES = tuple(_verge_rng.choice((1, 1, 2, 2, 2)) for _ in range(_limits.W))

VERGE_FLEX = tuple(0.55 + 0.45 * _verge_rng.random() for _ in range(_limits.W))

# Per-column tone: real grass is not one flat green. Without this the verge
# was two colours and read as painted-on rather than grown.
VERGE_TONE = tuple(0.82 + 0.34 * _verge_rng.random() for _ in range(_limits.W))

VERGE_GUST_WIDTH = 18.0  # half-width of the front, in columns


# Static field texture. Deliberately LOW FREQUENCY: per-pixel randomness
# put 57 distinct colours across 72 pixels, which is the definition of
# white noise and read as static. What the lakefront gets right is that
# its variation happens over several pixels, so neighbours stay related
# and the eye sees a surface instead of speckle. Two incommensurable
# wavelengths keep the patches from repeating on any visible period.
def field_mottle(x: int, row: int) -> float:
    return 1.0 + 0.17 * math.sin(x * 0.42 + row * 1.1) + 0.11 * math.sin(x * 0.17 + 2.3)


def verge_gust(x: int, phase: float) -> float:
    """One coherent gust front travelling the scene, left to right.

    Real wind in open country arrives as a single front, not as
    independent local motion — everything it passes bends at roughly the
    same moment, which is why the grass, the crop and the trees here all
    read this one function.

    The front starts and ends FAR enough off-panel that the seam is
    silent. At one width off the edge exp(-1) is still 0.37, so the old
    range jumped the left edge from nothing to a third of full gust
    across the loop join.
    """
    reach = 2.6 * VERGE_GUST_WIDTH
    front = -reach + phase * (_limits.W + 2 * reach)
    d = (x - front) / VERGE_GUST_WIDTH
    return math.exp(-d * d)


def verge_shimmer(
    x: int, row: int, phase: float, wind_kmh: float, gust: float
) -> float:
    """Per-pixel glint, driven BY the gust front rather than beside it.

    This used to advance on its own clock — `wraps` gave it two or three
    cycles per loop above 18 km/h while the front crossed once — so at any
    real wind the grass shimmered at a rate the poplars did not share, and
    the two read as moving out of sync ("the grass and the tree wind don't
    always move in sync", from the panel).

    Wind speed still sets how hard the grass glints; it no longer sets the
    RATE. The ripple advances once per loop with the front, and its
    amplitude follows the local gust, so grass sparkles where the wind is
    actually pressing and lies quiet where it is not. One event crossing
    the scene, which is what the trees are already doing.
    """
    amp = (4.0 + min(11.0, wind_kmh * 0.42)) / 100.0
    ripple = math.sin(x * (0.62 + 0.17 * row) + math.tau * phase + row * 2.3)
    return 1.0 + amp * (0.2 + 0.8 * gust) * ripple


GRASS_COLOR = (36, 46, 30)

GRASS_COLOR_2 = (28, 38, 24)

# Two trees in the yard: (trunk x, size) — size 1 small, 2 big.
# Canopies go orange in autumn and bare in winter.
TREES = ((11, 2), (31, 1))

TRUNK_NIGHT, TRUNK_DAY = (34, 26, 20), (88, 66, 46)

CANOPY_NIGHT, CANOPY_DAY = (30, 52, 28), (66, 108, 50)

CANOPY_FALL = ((150, 76, 26), (196, 128, 40), (128, 60, 22))

WISP_SHAPE = [(0, 0), (1, 0), (2, 0), (3, -1), (4, -1), (5, 0), (6, 0)]

# Seasonal detail layers
CHIMNEY = (62, 6)  # drawn only in smoke weather; the original art has none

FIREFLY_COLOR = (168, 210, 70)

LEAF_COLORS = [(170, 92, 30), (140, 70, 24), (196, 128, 40)]

BIRD_COLOR = (22, 20, 26)

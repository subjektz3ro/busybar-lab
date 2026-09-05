"""Skystrip render / art."""

from __future__ import annotations

import math
import random

from PIL import Image

from apps.skystrip_app import config as _config
from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation

HOUSE_ART = _config.REPO_ROOT / "apps" / "assets" / "house.png"

STORM_HORIZON, STORM_ZENITH = (74, 92, 82), (30, 38, 42)  # green-grey cell

GROUND_NIGHT = (24, 28, 22)  # the original art's moss row

MOON_COLOR = (226, 226, 206)  # the original art's moon cream

SUN_COLOR = (255, 214, 120)

WINDOW_COLOR = (255, 157, 69)

STAR_COLOR = (150, 150, 170)

# colors in the house region of the source art that are sky, not house
HOUSE_SKIP = {(150, 150, 170), (160, 160, 180), (226, 226, 206), (8, 10, 26)}

# Settled-snow depth thresholds, in metres. Deliberately constants and not
# config keys: they want a winter of watching before they are worth a knob.
SNOW_DUSTING_M = 0.01  # 1 cm: the grass still shows through

SNOW_COVERED_M = 0.08  # 8 cm: the ground is gone

SNOW_DEEP_M = 0.25  # 25 cm: tufts and low detail are buried

SNOW_LIT = (232, 238, 246)  # sunlit crust: cold white, faintly blue

SNOW_SHADE = (120, 134, 156)  # the shaded side of the same crust

# Which columns take snow at each tier. Fixed seed: previews and tests must
# not shimmer between runs, and the pattern reads as drift, not as noise.
_snow_rng = random.Random(1224)

_SNOW_ORDER = list(range(_limits.W))

_snow_rng.shuffle(_SNOW_ORDER)

# Never 1.0, even at the deepest tier. A fully lit row IS the haze failure --
# it is the thing the global constraint forbids and the thing
# test_settle_snow_never_fills_a_row asserts against. Depth is carried by the
# second row below, not by closing the last gaps in the first.
SNOW_FRACTION = {1: 0.40, 2: 0.70, 3: 0.90}

# Rain intensity: tier -> (drops, crossings per loop, streak length in px).
#
# Coverage is NOT one of the channels. Drops are dealt one per stratified
# column bucket (below), so every tier wets the whole panel; what changes is
# how much falls past a row per second, how fast, and how long the streak is.
# Drop count used to be the only channel, and at 5 drops for 72 columns a
# seeded draw put all five in the right half and stayed there for the whole
# ten-minute seed window -- it read as a broken panel, not as light rain.
#
# `crossings` MUST stay a whole number: fall returns to y0 at phase 1.0 only
# when it is, and that is what makes the device's loop seam invisible.
RAIN_TIERS = {0: (12, 4, 2), 1: (18, 8, 3), 2: (24, 16, 4)}

# Head is the streak colour outright; the rest fades toward whatever is behind
# it. Steps are coarse on purpose -- finer than ~30% is invisible on the panel.
RAIN_TAPER = (1.0, 0.65, 0.45, 0.35)

# Above this sky luminance rain reads as a DARK streak. sky+55 clamps to white
# on a bright sky and lands at +0..11% contrast, i.e. no rain at all.
RAIN_DARK_SKY_LUM = 110


def _load_house() -> list[tuple[int, int, _render_primitives.RGB]]:
    """Lift the house sprite (and chimney and window) from the original art."""
    img = Image.open(HOUSE_ART).convert("RGB")
    px = _render_primitives._rgb_pixels(img)
    return [
        (x, y, px[x, y])
        for y in range(4, 15)
        for x in range(48, 72)
        if px[x, y] not in HOUSE_SKIP
    ]


HOUSE_SPRITE = _load_house()

WINDOW_PIXELS = [(x, y) for x, y, c in HOUSE_SPRITE if c == WINDOW_COLOR]

WINDOW_CENTER = (
    round(sum(x for x, _ in WINDOW_PIXELS) / len(WINDOW_PIXELS)),
    round(sum(y for _, y in WINDOW_PIXELS) / len(WINDOW_PIXELS)),
)

# Top silhouette of the house, one pixel per column: what moonlight lands
# on, and where a string of Christmas lights hangs. One derivation, two
# readers -- a second copy of this loop would be a copy that can rot.
HOUSE_TOP: dict[int, int] = {}

for _hx, _hy, _hc in HOUSE_SPRITE:
    if _hx not in HOUSE_TOP or _hy < HOUSE_TOP[_hx]:
        HOUSE_TOP[_hx] = _hy

# Fixed constellation across the whole sky (foregrounds occlude naturally).
# Stratified in x so stars never bunch; each has its own static brightness,
# most faint with a few bright anchors.
_star_rng = random.Random(7)

STARS = [
    star
    for star in (
        (
            col * 4 + _star_rng.randrange(1, 4),  # x, one star per 4px band
            _star_rng.randrange(0, 11),  # y
            0.3 + 0.7 * _star_rng.random() ** 1.5,  # magnitude, skewed dim
        )
        for col in range(17)
    )
    # The status corner is a quiet zone: a white star one pixel from a
    # white digit welds onto the letterform now that no shadow separates
    # text from scene. The generator draw stays inside the comprehension
    # so the surviving stars' positions are unchanged.
    if not (star[0] < _limits.STATUS_CARD_W and star[1] <= 6)
]

# Grass fringe along the ground, varied heights (0-2 px above the ground
# row), clear of the house; a wind wave travels through the tall blades
_fringe_rng = random.Random(29)

GRASS_FRINGE = [(x, _fringe_rng.choice((0, 0, 0, 1, 1, 1, 2))) for x in range(2, 47)]

FLOCK_DIR = _config.REPO_ROOT / "apps" / "assets" / "flock"


def _load_flock() -> list[list[tuple[int, int]]]:
    """The August 2026 murmuration, one dot list per frame."""
    frames = []
    for i in range(8):
        img = Image.open(FLOCK_DIR / f"flock_{i}.png").convert("RGB")
        px = _render_primitives._rgb_pixels(img)
        frames.append(
            [
                (x, y)
                for y in range(14)
                for x in range(_limits.W)
                if 0.3 * px[x, y][0] + 0.59 * px[x, y][1] + 0.11 * px[x, y][2] > 60
            ]
        )
    return frames


FLOCK_FRAMES = _load_flock()

# Forest: tall pines flanking a clearing (x34-49) with a lime tent and a
# campfire. (trunk x, canopy top row) — smaller top = taller tree.
FOREST_PINES = (
    (3, 4),
    (9, 2),
    (16, 6),
    (25, 3),
    (31, 7),
    (52, 2),
    (58, 5),
    (65, 3),
    (70, 6),
)

FOREST_ASPENS = (21, 49)  # deciduous pair: autumn/bare-winter

PINE_NIGHT, PINE_DAY = (16, 32, 26), (44, 88, 56)

FOREST_FLOOR_NIGHT, FOREST_FLOOR_DAY = (26, 24, 18), (74, 64, 44)

TENT_APEX = 41  # ridge tent, rows 10-13

# Grove: a broadleaf wood — the scene that earns its autumn.
# (trunk x, crown radius); big crowns ride higher than small ones.
# Six trees you can count, with real sky between the crowns, and a hazy
# back row standing in the gaps for density. The first grove packed eight
# touching crowns into 72 columns and read as one continuous wash; the
# second's five separated trees read but sparse ("more trees" — the
# operator, 2026-08-12). Density comes from DEPTH now: the back row is
# small, dim, trunkless, and never touches a front crown, so every tree
# stays countable. Western trees (any column < STATUS_CARD_W) are small
# (r=2, crown top row 7): autumn crowns are orange-family, the clock's
# own hue, so no crown may enter the corner's airspace.
GROVE_TREES = ((5, 2), (16, 2), (29, 3), (41, 2), (53, 3), (66, 2))

# Back row: (x, crown center y), radius 1, standing offset from the
# dapple pools at the gap centers.
GROVE_BACKROW = ((11, 10), (23, 10), (35, 10), (47, 10), (59, 10), (71, 10))

GROVE_FALL = _render_vegetation.CANOPY_FALL + ((214, 160, 60), (170, 96, 56))


# Backroads: bird's-eye canopy split by a winding two-lane road.
def _road_y(x: int) -> int:
    y = 8 + 2.2 * math.sin(x * 0.09 + 0.9) + 1.1 * math.sin(x * 0.19 + 2.3)
    return max(4, min(11, int(round(y))))


TENT_NIGHT, TENT_DAY = (52, 96, 22), (128, 198, 52)  # lime nylon

TENT_GLOW = (222, 236, 132)  # flashlight through the fabric

FIRE_X = 34  # campfire in the clearing

# Skyline: (x0, x1, height) back row; (x0, x1, height, kind) front row.
# kind 0 = flat roof, 1 = stepped with twin masts (Willis-ish),
# 2 = tapered with twin masts (Hancock-ish)
SKYLINE_BACK = [(0, 8, 6), (10, 17, 8), (26, 33, 7), (44, 52, 9), (60, 71, 6)]

SKYLINE_FRONT = [
    # The Willis-ish tower starts at 19, not 18: its west edge reached the
    # clock's halo, where a storm-dimmed wall sat under the contrast floor
    # beside the day ink. Structures stay out of the status corner.
    (2, 7, 8, 0),
    (9, 14, 8, 0),
    (19, 24, 12, 1),
    (27, 32, 8, 0),
    (36, 42, 11, 2),
    (46, 51, 7, 0),
    (54, 60, 10, 0),
    (63, 71, 8, 0),
]

WINDOW_WARM = (255, 190, 90)

WINDOW_COOL = (180, 200, 220)

BEACON_RED = (255, 60, 50)

HEADLIGHT = (232, 232, 208)

TAILLIGHT = (205, 52, 40)

CAR_DARK = (28, 30, 34)

# 3x5 clock digits — drawn into the frames so we control every pixel
# (the device's tiny-font 9 reads like a 4)
DIGITS_3X5 = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "011", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ":": ("0", "1", "0", "1", "0"),
    "°": ("11", "11", "00", "00", "00"),
    "-": ("00", "00", "11", "00", "00"),
}

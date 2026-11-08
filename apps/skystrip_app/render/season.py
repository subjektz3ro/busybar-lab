"""Skystrip render / season."""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from datetime import datetime

from apps.skystrip_app import settings as _settings
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation


def _in_window(
    when: datetime, forced: bool | None, windows: Iterable[tuple[int, int, int, int]]
) -> bool:
    """Shared by the two holiday predicates: the preview override wins, then
    the local calendar date is tested against inclusive (month, day) spans.

    Normalised here, not at the call sites. Eight scene call sites between
    the two holidays would otherwise each have to remember that a UTC
    datetime shifts the window by the tz offset -- 19:00 on the 24th in
    Chicago is already the 25th in UTC. Making the predicate own the
    conversion means no call site can get it wrong.
    """
    if forced is not None:
        return forced
    local = when.astimezone(_settings.TZ)
    for m0, d0, m1, d1 in windows:
        if (m0, d0) <= (local.month, local.day) <= (m1, d1):
            return True
    return False


def is_christmas(when: datetime) -> bool:
    """Is the Christmas treatment showing on this date?

    One of exactly two date-driven looks in the app (is_halloween is the
    other): everything else here is a measurement. Each is kept narrow so it
    stays a surprise rather than a mode, and switchable because somebody will
    not want it.

    An unknown window reads as off. A hand-edited config.env can hold
    anything, and a crash loop leaves the display dark.
    """
    return _in_window(
        when,
        _settings.CHRISTMAS_FORCED,
        _settings.CHRISTMAS_WINDOWS.get(_settings.CHRISTMAS_WINDOW, ()),
    )


def is_halloween(when: datetime) -> bool:
    """Is the Halloween treatment showing on this date? See is_christmas."""
    return _in_window(
        when,
        _settings.HALLOWEEN_FORCED,
        _settings.HALLOWEEN_WINDOWS.get(_settings.HALLOWEEN_WINDOW, ()),
    )


def active_holiday(when: datetime) -> str | None:
    """Which treatment the scenes draw on this date: "christmas",
    "halloween" or None.

    The scenes dispatch on this and nothing else, so precedence is decided
    exactly once. The two window tables never share a date, so it can only
    matter in --preview with both flags forced -- and there Christmas wins:
    it was here first, and every scene already read that way.
    """
    if is_christmas(when):
        return "christmas"
    if is_halloween(when):
        return "halloween"
    return None


# Red, green, warm white. Chosen for panel separation, not for accuracy: the
# test asserts >= 30% per-channel difference between neighbours, because below
# that the panel's gamma renders them as one colour and the string reads as a
# smear of warm dots.
#
# The warm bulb is (255, 255, 180), not the more obvious (255, 190, 90) --
# that value is WINDOW_WARM, byte-for-byte, and the lakefront/backroads
# guard tests build `decor = set(XMAS_BULBS) | {XMAS_TREE}` to assert none
# of it ever lands on water or road. A collision there doesn't just look
# wrong, it makes the guard match window light instead of decorations,
# passing only by luck (no lamp happens to sit on a guarded row today).
# (255, 255, 180) keeps >= 30% separation from every neighbour above,
# INCLUDING WINDOW_WARM, so it also reads as a distinct paler warm-white
# next to the windows' amber glow rather than folding into them.
XMAS_BULBS = ((235, 40, 40), (40, 200, 70), (255, 255, 180))

XMAS_SPACING = 3  # one bulb every N points: real strings have gaps

XMAS_TWINKLE = 2  # whole cycles per .anim loop, or the seam jumps


def string_lights(
    px,
    points,
    phase: float,
    amb: tuple = (1, 1, 1),
    bulbs: Sequence[tuple[int, int, int]] = XMAS_BULBS,
) -> None:
    """Hang a string of bulbs along `points`, an ordered list of (x, y).

    `bulbs` is the palette the string cycles through -- the December
    red/green/white by default, HALLOWEEN_BULBS in October. Same spacing,
    same twinkle, so the two holidays share one rhythm on the one roofline.

    Single pixels on purpose. Everywhere else on this panel a one-pixel detail
    vanishes -- but a light string is read as a RHYTHM rather than as
    individual lamps, so the gaps are the shape. Fattening the bulbs turns it
    into a lit bar, which is the haze failure.
    """
    # Reduced before it ever reaches math.sin: phase=1.0 must produce the
    # exact same float as phase=0.0, and 4*pi is not bit-identical to 0 even
    # though sin() is mathematically periodic -- so the seam only closes if
    # the two calls land on the identical input, not just an equivalent one.
    phase %= 1.0
    for i, (x, y) in enumerate(points):
        if i % XMAS_SPACING:
            continue
        bulb = bulbs[(i // XMAS_SPACING) % len(bulbs)]
        # Closes exactly at phase 1.0, so the loop seam is invisible.
        swell = 0.75 + 0.25 * math.sin(
            math.tau * (XMAS_TWINKLE * phase + i / len(bulbs))
        )
        _render_primitives._put(
            px, x, y, _render_primitives._rgb_int(c * swell for c in bulb), amb
        )


XMAS_TREE = (26, 92, 46)  # conifer green, dark enough that bulbs pop


def draw_lit_tree(
    px, base_x: int, base_y: int, phase: float, amb: tuple = (1, 1, 1)
) -> None:
    """A small conifer with a string on it: 3 px wide, 4 tall plus a trunk.

    Wide enough to read as a shape -- a one-pixel-wide tree is an isolated
    dot on this panel, not a tree. The BULBS are single pixels, which is the
    one sanctioned exception, because a string is read as a rhythm.
    """
    rows = ((0, 0), (-1, 1), (0, 1), (1, 1), (-1, 2), (0, 2), (1, 2))
    for dx, dy in rows:
        _render_primitives._put(px, base_x + dx, base_y - 3 + dy, XMAS_TREE, amb)
    _render_primitives._put(px, base_x, base_y, _render_vegetation.TRUNK_NIGHT, amb)
    string_lights(
        px,
        [
            (base_x + dx, base_y - 3 + dy)
            for dx, dy in ((0, 0), (-1, 1), (1, 1), (-1, 2), (1, 2))
        ],
        phase,
        amb,
    )


# Halloween. One pumpkin orange and one witch purple for the skyline's window
# recolour: >= 30% per-channel from each other AND from the warm/cool windows
# they sit between, since the recolour leaves a window's neighbours alone and
# the ink has to read against them. Neither is a colour any scene paints on
# its own -- test_the_pumpkin_palette_is_separable_and_collides_with_nothing
# pins that, for the reason XMAS_BULBS[2] was moved off WINDOW_WARM above.
HALLOWEEN_INKS = ((255, 104, 0), (150, 40, 220))

# The roofline string in October: orange, purple and the same warm white the
# December string ends on. Cycles with the wraparound separable, like
# XMAS_BULBS, so a string longer than three bulbs never puts two alike or two
# too-close colours side by side.
HALLOWEEN_BULBS = (HALLOWEEN_INKS[0], HALLOWEEN_INKS[1], XMAS_BULBS[2])

# The jack-o'-lantern. Skin dark enough that the cut-outs pop; the candle is
# the light inside, so it carries the only motion. The stem is a muted olive:
# a stem the colour of grass is no stem at all against a lawn, and one the
# colour of a trunk is a trunk.
PUMPKIN = (214, 86, 8)

CANDLE = (255, 196, 48)

STEM = (70, 84, 30)

# (dx, dy) from the base. A stem over a body 5 wide and 3 tall: a rounded
# top row, two eyes on the middle row with skin between them, a three-wide
# mouth on the bottom row with skin at both ends. The first cut was 3 wide
# and 2 tall with the eyes in the corners and the mouth bottom centre -- a
# checkerboard -- and the physical panel showed it as an orange blob, not a
# face (owner, 2026-10-30). Twice the pixels buys the two things a face
# needs at this pitch: eyes that are two separate lights, and a mouth that
# is a bar beneath them rather than a third light.
_PUMPKIN_STEM = (0, -3)

_PUMPKIN_SKIN = (
    (-1, -2), (0, -2), (1, -2),
    (-2, -1), (0, -1), (2, -1),
    (-2, 0), (2, 0),
)

_PUMPKIN_CANDLE = ((-1, -1), (1, -1), (-1, 0), (0, 0), (1, 0))


def draw_jack_o_lantern(
    px,
    base_x: int,
    base_y: int,
    phase: float,
    amb: tuple = (1, 1, 1),
    stem: bool = True,
) -> None:
    """A carved pumpkin with a candle in it: 5 px wide, 3 tall, rounded on
    top, plus a stem where the scene has a row for one.

    `base_y` is the body's bottom row: the row the pumpkin stands in, which
    is wherever the scene's other standing things have their feet -- above
    the lawn row at the house, in the trunk row of the conifer it replaces
    at the lakefront and on the backroads. `stem=False` is for a verge too
    shallow to hold one; the face is the pumpkin, the stem is a courtesy.
    The candle flickers and, like every other periodic motion here,
    completes whole cycles per loop: phase 1.0 reduces to the identical
    input as 0.0, so the device's loop seam is invisible.
    """
    phase %= 1.0
    # Two harmonics, both whole cycles per loop, so the flame wanders rather
    # than pulsing. 0.40..1.00 clears the panel's ~30% floor between its dim
    # and bright extremes; anything subtler would read as a steady light.
    flicker = (
        0.70
        + 0.18 * math.sin(math.tau * 3 * phase)
        + 0.12 * math.sin(math.tau * 7 * phase + 0.9)
    )
    put = _render_primitives._put
    if stem:
        put(px, base_x + _PUMPKIN_STEM[0], base_y + _PUMPKIN_STEM[1], STEM, amb)
    for dx, dy in _PUMPKIN_SKIN:
        put(px, base_x + dx, base_y + dy, PUMPKIN, amb)
    lit = _render_primitives._rgb_int(c * flicker for c in CANDLE)
    for dx, dy in _PUMPKIN_CANDLE:
        put(px, base_x + dx, base_y + dy, lit, amb)

"""Skystrip render / season."""

from __future__ import annotations

import math
from datetime import datetime

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation


def is_christmas(when: datetime) -> bool:
    """Is the decorative treatment showing on this date?

    Deliberately the only date-driven look in the app: everything else here is
    a measurement. Kept narrow so it stays a surprise rather than a mode, and
    switchable because somebody will not want it.

    An unknown window reads as off. A hand-edited config.env can hold
    anything, and a crash loop leaves the display dark.
    """
    if _settings.CHRISTMAS_FORCED is not None:
        return _settings.CHRISTMAS_FORCED
    # Normalise here, not at the call sites. Six scenes are about to call
    # this, and a UTC datetime would shift the window by the tz offset --
    # 19:00 on the 24th in Chicago is already the 25th in UTC. Making the
    # predicate own the conversion means no call site can get it wrong.
    local = when.astimezone(_settings.TZ)
    for m0, d0, m1, d1 in _settings.CHRISTMAS_WINDOWS.get(
        _settings.CHRISTMAS_WINDOW, ()
    ):
        if (m0, d0) <= (local.month, local.day) <= (m1, d1):
            return True
    return False


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


def string_lights(px, points, phase: float, amb: tuple = (1, 1, 1)) -> None:
    """Hang a string of bulbs along `points`, an ordered list of (x, y).

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
        if not (0 <= x < _limits.W and 0 <= y < _limits.H):
            continue
        bulb = XMAS_BULBS[(i // XMAS_SPACING) % len(XMAS_BULBS)]
        # Closes exactly at phase 1.0, so the loop seam is invisible.
        swell = 0.75 + 0.25 * math.sin(
            math.tau * (XMAS_TWINKLE * phase + i / len(XMAS_BULBS))
        )
        px[x, y] = _render_primitives._shade(
            _render_primitives._rgb_int(c * swell for c in bulb), amb
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
    body = _render_primitives._shade(XMAS_TREE, amb)
    rows = ((0, 0), (-1, 1), (0, 1), (1, 1), (-1, 2), (0, 2), (1, 2))
    for dx, dy in rows:
        x, y = base_x + dx, base_y - 3 + dy
        if 0 <= x < _limits.W and 0 <= y < _limits.H:
            px[x, y] = body
    if 0 <= base_x < _limits.W and 0 <= base_y < _limits.H:
        px[base_x, base_y] = _render_primitives._shade(
            _render_vegetation.TRUNK_NIGHT, amb
        )
    string_lights(
        px,
        [
            (base_x + dx, base_y - 3 + dy)
            for dx, dy in ((0, 0), (-1, 1), (1, 1), (-1, 2), (1, 2))
        ],
        phase,
        amb,
    )

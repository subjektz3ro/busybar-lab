"""DSN render / dish."""

from __future__ import annotations

from apps.dsn_app import limits as _limits
from apps.dsn_app.render import text as _render_text

# The ground antenna beside the dish number — and it LEANS THE WAY THE REAL
# DISH LEANS. Elevation was parsed off the feed for every link and used
# nowhere but a log line; here it becomes the one thing the icon says.
#
# Two rules were learned the hard way drawing about forty of these, and both
# are why the previous 4x5 glyph read as a letter Y rather than an antenna:
#
#   1. HUE separates more than shape does. In DISH_NO — the digits' own blue
#      — the glyph is read as another character in "43". A hue of its own is
#      read as a picture. Do not fold this back into the text colour.
#   2. A cup on a CENTRED stem is a letter Y at every size that fits here.
#      The mast has to run off-centre for the same pixels to read as an
#      object.
#
# Six rows, not five: y5 is free at this x (the tether does not start until
# y6), and a base line one row below the digits is what makes the thing look
# ground-mounted instead of floating.
ANTENNA = (200, 170, 110)

DISH_ICON_W = 6

MODE_X = 14  # the one free column between the globe's limb and the icon

DDOR_MARK = (150, 230, 160)

# (elevation ceiling, rows). THREE steps, not five: a fourth would differ by
# a single pixel and this panel's gamma erases differences that small, the
# same rule that keeps texture above 30% contrast.
# 34 m dishes. Same six-column footprint as the 70 m set below, so the label
# layout never shifts — only the aperture grows.
DISH_TILTS = (
    # near the horizon — a pass just starting, or nearly over
    (30.0, ("......", "1.....", "11....", "1.1...", "..1...", ".11111")),
    # the working middle of a pass
    (60.0, ("11....", "1.1...", "11....", "..1...", "..1...", ".11111")),
    # overhead, the best part of the pass
    (91.0, ("1..1..", ".11...", "..1...", "..1...", "..1...", ".11111")),
)

# The 70 m antennas — DSS-14, 43 and 63 — have four times the collecting area
# of a 34 m, and it is not a decorative difference: Voyager can essentially
# only be heard by these three. A bigger cup on the same mount says so without
# costing a column of the spacecraft name.
DISH_TILTS_70 = (
    (30.0, ("1.....", "11....", "1..1..", "1..1..", ".11...", ".11111")),
    (60.0, ("11....", "1..1..", "1..1..", "11....", "..1...", ".11111")),
    (91.0, ("1...1.", "1...1.", ".111..", "..1...", "..1...", ".11111")),
)


def dish_tilt(elevation: float, big: bool = False) -> tuple[str, ...]:
    """The antenna leaning the way the real dish is leaning.

    A parked dish reports elevation 90 with azimuth 0 and no signals, but it
    also reports its target as DSN or DSS, so NOT_SPACECRAFT has already
    dropped it before a Link exists. Missing pointing is intercepted by
    `_dish_icon` and rendered as unknown rather than passed here as zero.
    """
    table = DISH_TILTS_70 if big else DISH_TILTS
    for ceiling, rows in table:
        if elevation < ceiling:
            return rows
    return table[-1][1]


def _dish_icon(px, x: int, y: int, elevation: float | None, big: bool = False) -> None:
    if elevation is None:
        # Missing pointing is not a horizon-pointing dish. A question mark in
        # the antenna's own ink preserves the footprint and says exactly what
        # is unknown.
        _render_text._text(px, x + 1, y, "?", ANTENNA)
        return
    for dy, row in enumerate(dish_tilt(elevation, big)):
        for dx, bit in enumerate(row):
            if bit == "1" and 0 <= x + dx < _limits.W and 0 <= y + dy < _limits.H:
                px[x + dx, y + dy] = ANTENNA

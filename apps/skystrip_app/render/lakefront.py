"""Skystrip render / lakefront."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import season as _render_season

LAKE_TOP = 7  # horizon row at the bend — the lake dominates

LAKE_DEEP = (18, 40, 56)  # teal, clearly apart from sky and towers on LEDs

GLITTER_MOON = (210, 220, 240)

GLITTER_SUN = (255, 214, 120)

FOAM = (225, 230, 235)

# The Oak Street bend as a gentle S: the water's edge eases right to the
# elbow (row 11) and back, in small 3-5px steps — the corners get a
# blend pixel so the shoreline reads as a curve, not a staircase.
BEND_WATER_END = {7: 46, 8: 50, 9: 54, 10: 58, 11: 60, 12: 56, 13: 51, 14: 46, 15: 41}

# The trail is a thin ribbon on the near rows only; at the bend it
# disappears behind the treeline like in the photos
BEND_PATH = {11: (60, 61), 12: (56, 58), 13: (51, 53), 14: (46, 49), 15: (41, 45)}

BEND_LADDER = ((56, 12), (46, 14))  # railing dots along the edge curve

BEND_LAMPS_FAR = (52, 58, 64, 70)  # light string on the treeline, y7

BEND_LAMPS_NEAR = ((60, 10), (51, 12), (41, 14))

LAMP_WARM = (255, 200, 120)

TREE_DARK = (16, 24, 16)

CONCRETE_NIGHT = (108, 104, 94)  # the lit path glows pale against dark water

CONCRETE_DAY = (160, 158, 150)

# Tower cluster at the bend: (x0, x1, top_row, is_hancock); bodies end at y6,
# the treeline strip hides their feet
BEND_TOWERS = (
    (40, 45, 4, False),
    (46, 49, 5, False),
    (50, 58, 1, True),
    (59, 63, 3, False),
    (64, 67, 5, False),
    (68, 71, 2, False),
)

# Navy Pier's wheel, tiny above the horizon. East of the status corner:
# at (4, 5) its rim sat one pixel from the clock strokes — dark steel by
# day beside black ink, lit gondolas by night beside white ink, both
# under the contrast floor. The pier genuinely extends into the lake, so
# standing it over the water edge is also the truer picture.
WHEEL_HUB = (23, 5)

# The lit conifer on the bank, near the wheel. Bank rows 13-15 are NOT
# water-free (BEND_WATER_END shows real open water in all three), so this
# is anchored past every row the tree's 3-wide/4-tall footprint touches
# (12 through 15) -- past BEND_PATH's apron too, so it stands on the grass
# beyond the trail rather than in the middle of it. (59, 15) is the
# leftmost -- closest to the wheel -- column where that holds.
LAKEFRONT_TREE = (59, 15)


def _draw_lakefront(
    px,
    local,
    elev: float,
    daylight: float,
    seed: int,
    phase: float,
    wx: _weather.WeatherState,
    horizon: tuple,
    sun_pos,
    moon_pos,
    moon_ill: float,
    cloud: float,
) -> None:
    """The Oak Street bend, water-first: teal lake over ~70% of the frame,
    the shoreline curving out to the elbow and back, the trail a thin lit
    ribbon along the near edge, the Hancock cluster rim-lit by the moon,
    The Drake in pink, Navy Pier's wheel turning far left. Everything moves
    with real weather; every motion wraps integer cycles."""
    night = elev < 0
    hour = local.hour
    amb = _render_primitives._ambient(elev, cloud, wx)

    # Moonlight over the whole lake: a silver sheen scaled by the real lit
    # fraction, shimmering with the same ripples the wind drives
    sheen = 0.0
    if moon_pos is not None:
        sheen = 0.30 * moon_ill * (1.0 - cloud)

    speed_wraps = 1 + min(2, int(wx.wind_kmh // 20))
    amp = (5.0 + min(14.0, wx.wind_kmh * 0.5)) / 100.0
    for y in range(LAKE_TOP, _limits.H):
        depth = (y - LAKE_TOP) / (_limits.H - 1 - LAKE_TOP)
        base = _render_primitives._lerp_rgb(horizon, LAKE_DEEP, 0.40 + 0.45 * depth)
        base = _render_primitives._lerp_rgb(base, (12, 30, 44), 0.35 * (1.0 - daylight))
        for x in range(BEND_WATER_END[y]):
            ripple = math.sin(
                x * (0.55 + 0.12 * depth) + math.tau * (speed_wraps * phase) + y * 1.9
            )
            f = 1.0 + amp * ripple
            c = tuple(max(0, min(255, int(v * f))) for v in base)
            if sheen > 0.03:
                s = sheen * (0.7 + 0.3 * ripple)
                c = tuple(min(255, int(v + g * s)) for v, g in zip(c, (120, 130, 150)))
            px[x, y] = c

    # The concentrated glade under the moon or sun, kept inside the water
    glow = moon_pos if moon_pos is not None else sun_pos
    if glow is not None:
        col = GLITTER_MOON if moon_pos is not None else GLITTER_SUN
        strength = (moon_ill if moon_pos is not None else 1.0) * (1.0 - cloud)
        if strength > 0.1:
            g_rng = random.Random(seed * 37 + int(phase * _limits.ANIM_FRAMES))
            for y in range(LAKE_TOP, _limits.H):
                width = 1.5 + (y - LAKE_TOP) * 0.45
                for dx in range(-4, 5):
                    p = math.exp(-((dx / width) ** 2)) * strength
                    x = glow[0] + dx
                    if 0 <= x < BEND_WATER_END[y] and g_rng.random() < 0.75 * p:
                        b = 0.45 + 0.55 * g_rng.random()
                        px[x, y] = tuple(
                            min(255, int(c0 + c * b)) for c0, c in zip(px[x, y], col)
                        )

    # Whitecaps once the wind means it
    if wx.wind_kmh >= 25:
        cap_rng = random.Random(seed * 41 + 7)
        for _ in range(min(7, int(wx.wind_kmh // 10))):
            cy0 = cap_rng.randrange(LAKE_TOP, 14)
            cx0 = cap_rng.randrange(0, BEND_WATER_END[cy0])
            x = (cx0 - int(phase * speed_wraps * _limits.W)) % BEND_WATER_END[cy0]
            px[x, cy0] = FOAM

    # Rain pricks the water with little bright splashes
    if wx.rain or wx.stormy:
        sp_rng = random.Random(seed * 43 + int(phase * _limits.ANIM_FRAMES) * 3)
        for _ in range(10 if wx.stormy else 6):
            y = sp_rng.randrange(LAKE_TOP, _limits.H)
            x = sp_rng.randrange(0, BEND_WATER_END[y])
            px[x, y] = tuple(min(255, c + 46) for c in px[x, y])

    # A boat out on the lake: running lights by night, hull by day
    if night and (seed % 3) < 2:
        lane = BEND_WATER_END[8]
        pos = (int(seed * 29) + int(phase * lane)) % lane
        px[pos, 8] = (200, 200, 190)
        if pos > 0:
            px[pos - 1, 8] = (150, 52, 44)  # port light trailing
    elif not night and (seed % 4) < 2:
        hull_x = 4 + (seed * 17) % 30
        for hx in range(hull_x, hull_x + 4):
            px[hx, 8] = (30, 32, 40)
        px[hull_x + 1, 7] = (42, 44, 52)

    # Far shore: treeline strip the trail vanishes behind, rows 7-10
    tree = _render_primitives._shade(
        _render_primitives._lerp_rgb(TREE_DARK, (88, 122, 66), daylight), amb
    )
    tree_rng = random.Random(19)
    for y in (7, 8, 9, 10):
        for x in range(BEND_WATER_END[y], _limits.W):
            tree_color: _render_primitives.Color = (
                tree
                if tree_rng.random() < 0.75
                else _render_primitives._lerp_rgb(tree, (0, 0, 0), 0.3)
            )
            px[x, y] = _render_primitives._rgb_int(tree_color)

    # Near ground: the concrete revetment apron right of the ribbon —
    # pavement and stone blocks, a shade rougher and darker than the
    # lit trail so the curve still reads. Cracks break the slab up.
    apron = _render_primitives._shade(
        _render_primitives._lerp_rgb((26, 26, 30), (134, 130, 122), daylight), amb
    )
    crack = _render_primitives._shade(
        _render_primitives._lerp_rgb((18, 18, 22), (108, 104, 98), daylight), amb
    )
    apron_rng = random.Random(83)
    for y, (_p0, p1) in BEND_PATH.items():
        for x in range(p1 + 1, _limits.W):
            c = crack if apron_rng.random() < 0.18 else apron
            px[x, y] = tuple(int(v) for v in c)

    # The trail itself: a thin lit ribbon tracing the bend
    concrete = _render_primitives._shade(
        _render_primitives._lerp_rgb(CONCRETE_NIGHT, CONCRETE_DAY, daylight), amb
    )
    for y, (p0, p1) in BEND_PATH.items():
        for x in range(p0, p1 + 1):
            px[x, y] = tuple(int(v) for v in concrete)

    # Soften every shoreline corner: the first pixel past the water is a
    # half-blend with the water beside it, so steps become a curve
    for y in range(LAKE_TOP + 1, _limits.H):
        edge = BEND_WATER_END[y]
        if 0 < edge < _limits.W:
            px[edge, y] = tuple(
                (a + b) // 2 for a, b in zip(px[edge - 1, y], px[edge, y])
            )

    # The tower cluster above the treeline; Hancock crowned, Drake in pink
    front_col = _render_primitives._shade(
        _render_primitives._lerp_rgb((20, 20, 28), (120, 126, 136), daylight), amb
    )
    if not night:
        lit_frac = 0.25 if (wx.stormy and elev >= 0) else 0.0
    elif 17 <= hour <= 22:
        lit_frac = 0.5
    elif hour >= 23 or hour < 5:
        lit_frac = 0.18
    else:
        lit_frac = 0.32
    moon_rim = 0.0
    if moon_pos is not None and night:
        moon_rim = 0.55 * moon_ill * (1.0 - cloud)
    for bi, (x0, x1, top, hancock) in enumerate(BEND_TOWERS):
        shade = 16 if bi % 2 else -12
        body = tuple(max(0, min(255, c + int(shade * daylight))) for c in front_col)
        for y in range(top, 7):
            for x in range(x0, x1 + 1):
                px[x, y] = body
        win_rng = random.Random(seed * 17 + bi * 5)
        for wy in range(top + 1, 7, 2):
            for wxx in range(x0 + 1, x1, 2):
                if lit_frac > 0 and win_rng.random() < lit_frac:
                    px[wxx, wy] = (
                        _render_art.WINDOW_WARM
                        if win_rng.random() < 0.7
                        else _render_art.WINDOW_COOL
                    )
        # Moonlight rims the moon-facing (left) edge and the roofline
        if moon_rim > 0.1:
            for y in range(top, 7):
                px[x0, y] = tuple(min(255, int(c + 95 * moon_rim)) for c in px[x0, y])
            for x in range(x0, x1 + 1):
                px[x, top] = tuple(min(255, int(c + 60 * moon_rim)) for c in px[x, top])
        if hancock:
            for mi, mx in enumerate((x0 + 2, x1 - 2)):  # the twin masts
                for my in range(max(0, top - 2), top):
                    px[mx, my] = front_col
                if math.sin(math.tau * 4 * phase + mi * 1.7) > 0.7:
                    px[mx, max(0, top - 2)] = _render_art.BEACON_RED
            if night:  # crown lights
                for x in range(x0 + 1, x1):
                    px[x, top] = (150, 140, 110)
    # The Drake's pink neon, buzzing very occasionally like real neon
    if night:
        drake_dim = int(phase * _limits.ANIM_FRAMES) == (seed * 7) % _limits.ANIM_FRAMES
        neon = (140, 45, 85) if drake_dim else (255, 80, 150)
        px[47, 6] = neon
        px[48, 6] = neon
        _render_primitives._add_glow(px, 47, 6, (255, 80, 150), 2.0, 0.10)

    # Trail lamps: the string curving with the ribbon, plus treeline dots
    if night or elev < 4:
        for lx in BEND_LAMPS_FAR:
            px[lx, 7] = LAMP_WARM
        for i, (hx, hy) in enumerate(BEND_LAMPS_NEAR):
            px[hx, hy] = LAMP_WARM
            if i >= 3:  # nearest lamps glow visibly on the path
                _render_primitives._add_glow(px, hx, hy, LAMP_WARM, 2.0, 0.14)
        # lamplight shimmers in the water just off the near edge
        sh_rng = random.Random(seed * 53 + int(phase * _limits.ANIM_FRAMES))
        for y in range(11, _limits.H):
            edge = BEND_WATER_END[y]
            for x in range(max(0, edge - 3), edge):
                if sh_rng.random() < 0.12:
                    px[x, y] = tuple(
                        min(255, int(c0 + c * 0.35))
                        for c0, c in zip(px[x, y], LAMP_WARM)
                    )

    # Railing dots along the seawall edge
    for lx, ly in BEND_LADDER:
        px[lx, ly] = (200, 120, 40)

    # Navy Pier: a few warm lights and the wheel, turning once per loop
    wx0, wy0 = WHEEL_HUB
    if night:
        for pier_x in (wx0 - 3, wx0 - 1, wx0 + 2):  # deck lights under the wheel
            px[pier_x, 7] = (120, 96, 60)
    rim = [(wx0, wy0 - 1), (wx0 + 1, wy0), (wx0, wy0 + 1), (wx0 - 1, wy0)]
    if night:
        lit_i = int(phase * 4) % 4
        for i, (rx, ry) in enumerate(rim):
            px[rx, ry] = (240, 200, 255) if i == lit_i else (130, 110, 140)
        px[wx0, wy0] = (180, 160, 190)
    else:
        for rx, ry in rim + [(wx0, wy0)]:
            px[rx, ry] = (38, 40, 46)

    # Settled snow: the near bank only -- open water must never take it.
    # bank_rows (13-15) is a search-space narrowing, NOT a water-free
    # zone: BEND_WATER_END shows real open water inside all three of
    # those rows (row 13 is water for x in 0..50, bank only from x=51
    # on) -- it only rules out the open lake proper in rows 7-12. The
    # actual defence is water_colors. There is no fixed "water" palette
    # to exclude by -- ripples, sheen, whitecaps, rain splashes, the
    # boat and the lamp shimmer have all repainted it by now -- so
    # water_colors is read back from exactly the pixels BEND_WATER_END
    # says are water, taken this late, after every one of those effects
    # has already run. Each water pixel's own current color is
    # therefore guaranteed to be in the set that excludes it: no code
    # path can mistake it for a bank -- bank_rows narrows where we look,
    # water_colors is what keeps the lake itself bare.
    tier = _render_precipitation.snow_tier(wx.snow_depth_m)
    if tier:
        bank_rows = range(13, 16)  # shore in front of the water
        water_colors = {px[x, y] for y in bank_rows for x in range(BEND_WATER_END[y])}
        tops = _render_precipitation.surface_tops(
            px, range(_limits.W), bank_rows, water_colors
        )
        _render_precipitation.settle_snow(px, tops, tier, amb)

    # A tree on the bank, drawn dead last so nothing painted above --
    # settled snow included -- lands on top of it. LAKEFRONT_TREE was
    # chosen against BEND_WATER_END itself (see its comment) so it stands
    # on real bank, not merely in a bank row -- the same problem the
    # settled-snow block above solves with water_colors instead of a
    # column range.
    if _render_season.is_christmas(local):
        tx, ty = LAKEFRONT_TREE
        _render_season.draw_lit_tree(px, tx, ty, phase, amb)

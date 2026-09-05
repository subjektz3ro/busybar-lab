"""Skystrip render / grove."""

from __future__ import annotations

import math
import random
from datetime import datetime

from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import astronomy as _render_astronomy
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation


def is_winter(when: datetime) -> bool:
    """Bare-limb season. Northern-hemisphere meteorological winter -- the same
    assumption the scene art has always made, now stated in one place."""
    return when.month in (12, 1, 2)


def _draw_grove(
    px,
    local,
    elev: float,
    daylight: float,
    seed: int,
    phase: float,
    wx,
    amb: tuple,
    moon_pos: tuple | None,
    moon_ill: float,
    cloud: float,
) -> None:
    """A grove you can count: five separated broadleaf trees on a dark
    meadow, real sky in the gaps between the crowns, and warm dapples on
    the ground beneath those gaps while the sun is up.

    The first version filled rows 9-15 with a haze wall, a back row of
    crowns, and a lit floor, and every crown touched its neighbour: the
    2026-08-11 audit artifacts read as one continuous wash with not a
    single distinguishable tree. Mostly-OFF is the law this scene now
    obeys — the trees are the only filled shapes, the meadow goes truly
    dark at night, and the gaps stay sky."""
    # Snapshot the living sky before this scene paints over it. The
    # gradient carries per-pixel noise (and wisps, stars, birds), so there
    # is no fixed palette to match against -- only "whatever was here a
    # moment ago, before we drew the wood on top of it."
    sky_before = {px[x, y] for x in range(_limits.W) for y in range(6, 16)}
    mm = local.month
    fall = mm in (9, 10, 11)
    winter = is_winter(local)
    spring = mm in (3, 4, 5)
    dl = max(0.25, daylight)
    moonf = 0.0
    if moon_pos is not None:
        moonf = moon_ill * max(0.0, 1.0 - cloud * 1.2)
    wind_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
    rustle = wx.wind_kmh >= 8

    # Distant treeline: a one-row silhouette with a ragged top. Dark
    # against the sky reads as depth; the old haze wall read as a wash.
    if fall:
        line_n, line_d = (26, 16, 8), (84, 56, 26)
    elif winter:
        line_n, line_d = (22, 20, 18), (72, 66, 60)
    else:
        line_n, line_d = (10, 18, 10), (36, 58, 30)
    tl_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(line_n, line_d, dl * 0.6), amb
    )
    for x in range(_limits.W):
        px[x, 12] = tl_c
        if math.sin(x * 0.53 + 0.4) > 0.55:
            px[x, 11] = tl_c

    # Meadow: real daylight, no floor value — near-black on a moonless
    # night, which is what a meadow is. Rows recede downward.
    if winter:
        meadow_n, meadow_d = (12, 12, 12), (118, 116, 110)
    elif fall:
        # Dark enough that the trunks stay distinct: at (96, 66, 30) the
        # fall meadow was byte-close to trunk brown and the trees floated
        # all over again, one season later.
        meadow_n, meadow_d = (18, 13, 7), (58, 40, 18)
    else:
        meadow_n, meadow_d = (8, 10, 6), (52, 74, 36)
    ground = _render_primitives._shade(
        _render_primitives._lerp_rgb(meadow_n, meadow_d, daylight), amb
    )
    for x in range(_limits.W):
        px[x, 13] = ground
        px[x, 14] = _render_primitives._rgb_int(v * 0.85 for v in ground)
        px[x, 15] = _render_primitives._rgb_int(v * 0.65 for v in ground)

    # The back row: small hazy crowns standing in the front gaps, half
    # blended toward the treeline so they read as depth, not clutter.
    # Trunkless and low — distance eats trunks and height first. Winter
    # bares them entirely (the treeline silhouette carries that season).
    if not winter:
        for bi, (bx, bcy) in enumerate(_render_art.GROVE_BACKROW):
            b_rng = random.Random(53 * bi + 7)
            if fall:
                b_day = _render_art.GROVE_FALL[
                    b_rng.randrange(len(_render_art.GROVE_FALL))
                ]
                b_night = _render_primitives._rgb_int(v * 0.30 for v in b_day)
            elif spring:
                b_day, b_night = (98, 150, 62), (26, 40, 22)
            else:
                b_day, b_night = (86, 140, 58), (17, 28, 16)
            b_c = tuple(
                int(c)
                for c in _render_primitives._lerp_rgb(
                    _render_primitives._shade(
                        _render_primitives._lerp_rgb(
                            b_night, b_day, max(0.25, daylight) * 0.8
                        ),
                        amb,
                    ),
                    tl_c,
                    0.45,
                )
            )
            for dy in range(-1, 2):
                half = 1 if dy == 0 else 0
                for dx in range(-half, half + 1):
                    x, y = bx + dx, bcy + dy
                    if 0 <= x < _limits.W and 0 <= y < _limits.H:
                        px[x, y] = b_c

    # Grove trunks: two pixels wide and warmer than the shared constants,
    # at night as well as by day. A one-pixel trunk in shared browns
    # vanished against the treeline and the crowns read as floating in
    # the sky (caught by a fresh-eyes review, 2026-08-11) — and a tree
    # that is not visibly attached to the ground is not a tree.
    trunk_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            (36, 28, 21),
            _render_primitives._rgb_int(
                min(255, v * 1.5) for v in _render_vegetation.TRUNK_DAY
            ),
            dl,
        ),
        amb,
    )
    for ti, (tx, r) in enumerate(_render_art.GROVE_TREES):
        t_rng = random.Random(47 * ti + 3)
        # Crowns sit ON the treeline (bottom row 11), not above it.
        cy = 8 if r == 3 else 9
        # From the crown's bottom row: leafy seasons overpaint that row,
        # and winter's bare lattice needs the trunk to reach it.
        for txx in (tx, tx + 1):
            for ty in range(cy + r, 14):
                if 0 <= txx < _limits.W:
                    px[txx, ty] = trunk_c
        if winter:  # bare lattice: limbs against the sky
            for lx, ly in (
                (tx - 1, cy),
                (tx + 1, cy),
                (tx, cy - 1),
                (tx - r, cy + 1),
                (tx + r, cy + 1),
                (tx - 1, cy + 2),
                (tx + 1, cy + 2),
            ):
                if 0 <= lx < _limits.W:
                    px[lx, ly] = trunk_c
            continue
        if fall:  # each tree commits to its own autumn hue
            base_day = _render_art.GROVE_FALL[
                t_rng.randrange(len(_render_art.GROVE_FALL))
            ]
            base_night = _render_primitives._rgb_int(v * 0.30 for v in base_day)
        elif spring:
            base_day, base_night = (98, 150, 62), (26, 40, 22)
        else:
            # Brighter than the shared CANOPY_DAY by day — a crown dimmer
            # than the midday sky reads as a hole — and dimmer than
            # CANOPY_NIGHT after dark: night trees are silhouettes, and
            # the blind review called the brighter version "glowing".
            base_day, base_night = (86, 140, 58), (17, 28, 16)
        sway = 0
        if rustle:
            gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
            sway = (wind_lean or 1) if gust > 0.3 else 0
        for dy in range(-r, r + 1):
            half = int(math.sqrt(max(0, r * r - dy * dy)) + 0.5)
            row_sway = sway if dy <= -r + 1 else 0
            for dx in range(-half, half + 1):
                x, y = tx + dx + row_sway, cy + dy
                if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                    continue
                deep = t_rng.random() < 0.3
                lit = (0.5 if deep else 1.0) * max(0.25, daylight)
                if dy == -r and daylight > 0.4:
                    lit = min(1.0, lit * 1.3)  # sun on the crown's top row
                leaf_color = _render_primitives._lerp_rgb(base_night, base_day, lit)
                px[x, y] = _render_primitives._shade(leaf_color, amb)
        # Spring: a few blossom flecks, hue-separated from the leaves —
        # pink against green survives the panel where a tint would not
        if spring and t_rng.random() < 0.7:
            for _ in range(2):
                bx2 = tx + t_rng.randint(-(r - 1), r - 1)
                by2 = cy - t_rng.randint(0, 1)
                if 0 <= bx2 < _limits.W and 0 <= by2 < _limits.H:
                    px[bx2, by2] = _render_primitives._shade((216, 168, 190), amb)
        # Moonlit rim on the crown, sliding with the moon
        if moonf > 0.12 and moon_pos is not None:
            d = math.hypot(tx - moon_pos[0], (cy - r) - moon_pos[1])
            mix = 0.5 * moonf * math.exp(-((d / 20.0) ** 2))
            if mix >= 0.02 and 0 <= tx < _limits.W and 0 <= cy - r < _limits.H:
                px[tx, cy - r] = tuple(
                    int(c)
                    for c in _render_primitives._lerp_rgb(
                        px[tx, cy - r], _render_astronomy.MOONLIGHT, mix
                    )
                )

    # Light through the gaps — the thing that says "grove". By day, warm
    # dapples on the meadow under each sky gap; by night, if the moon is
    # out, a silver pool under the gap nearest the moon. Both sit on
    # ground that is otherwise dark, so two pixels are enough to read.
    gaps = [
        ((a + ra + b - rb) // 2, abs(a - b))
        for (a, ra), (b, rb) in zip(
            _render_art.GROVE_TREES, _render_art.GROVE_TREES[1:]
        )
    ]
    if not winter and daylight > 0.45:
        # A tint OF the ground, not a foreign colour: fixed tan patches
        # read as orphan trunk stubs in the blind review, and stayed tan
        # when autumn recoloured everything around them.
        dap = tuple(
            int(c)
            for c in _render_primitives._lerp_rgb(
                ground, (255, 238, 170), 0.5 * daylight
            )
        )
        for gx, _ in gaps:
            for dx in (-1, 0, 1):
                if 0 <= gx + dx < _limits.W:
                    px[gx + dx, 13] = dap
    elif moonf > 0.25 and moon_pos is not None:
        gx = min(gaps, key=lambda g: abs(g[0] - moon_pos[0]))[0]
        pool = _render_primitives._lerp_rgb(
            ground, _render_astronomy.MOONLIGHT, 0.45 * moonf
        )
        px[gx, 13] = tuple(int(c) for c in pool)
        px[gx + 1, 13] = tuple(int(c) for c in pool)

    # Autumn: leaves ride the wind down through the wood
    if fall:
        leaf_rng = random.Random(31)
        for i in range(5):
            off = leaf_rng.random()
            x0 = leaf_rng.randrange(0, _limits.W)
            prog = (phase + off) % 1.0
            ly = 3 + int(prog * 10)
            lx = (
                x0
                + int(prog * (4 + wx.wind_kmh / 6)) * (wind_lean or -1)
                + round(math.sin(math.tau * 2 * phase + i * 1.9))
            )
            if 0 <= lx < _limits.W and 0 <= ly < _limits.H:
                px[lx, ly] = _render_vegetation.LEAF_COLORS[i % 3]

    # Settled snow: everything below the sky takes it on the top edge --
    # the floor, and the upper face of every crown that catches it.
    tier = _render_precipitation.snow_tier(wx.snow_depth_m)
    if tier:
        snow_tops = _render_precipitation.surface_tops(
            px, range(_limits.W), range(6, 16), sky_before
        )
        _render_precipitation.settle_snow(px, snow_tops, tier, amb)

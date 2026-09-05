"""Skystrip render / forest."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import astronomy as _render_astronomy
from apps.skystrip_app.render import grove as _render_grove
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation


def _draw_forest(
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
    """Deep woods around a clearing: lime tent, campfire, tall pines."""
    # Snapshot the living sky before this scene paints over it -- same
    # reasoning as _draw_grove's sky_before: the gradient is noisy, so
    # there's no fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(_limits.W) for y in range(6, 16)}
    mm = local.month

    wind_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
    rustle = wx.wind_kmh >= 8

    # Back tree line: a hazy unbroken ridge the tall pines stand against
    ridge = _render_primitives._shade(
        _render_primitives._lerp_rgb((16, 24, 20), (52, 78, 46), daylight), amb
    )
    # Fold sky into the ridge from the gradient itself — sampling a live
    # pixel here once made the whole ridge blink when a wisp crossed it
    sky_top, sky_bot = _render_primitives._sky_colors(elev)
    sky_ref = _render_primitives._lerp_rgb(sky_top, sky_bot, 0.2)
    haze = tuple(int(r * 0.55 + s * 0.45) for r, s in zip(ridge, sky_ref))
    for x in range(_limits.W):
        top = 11 + int(1.0 + 1.4 * math.sin(x * 0.55 + 1.3))
        for y in range(top, 15):
            px[x, y] = haze

    # Forest floor: needle duff, warmer than the moss row beneath it
    floor = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            _render_art.FOREST_FLOOR_NIGHT, _render_art.FOREST_FLOOR_DAY, daylight
        ),
        amb,
    )
    for x in range(_limits.W):
        px[x, 14] = floor

    # Aspen pair among the pines: the deciduous accent that keeps seasons
    trunk_c = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            _render_vegetation.TRUNK_NIGHT, _render_vegetation.TRUNK_DAY, daylight
        ),
        amb,
    )
    asp_rng = random.Random(43)
    for ax in _render_art.FOREST_ASPENS:
        for ty in range(11, 14):
            px[ax, ty] = trunk_c
        if _render_grove.is_winter(local):  # bare winter limbs
            for lx, ly in ((ax - 1, 10), (ax + 1, 10), (ax, 9)):
                if 0 <= lx < _limits.W:
                    px[lx, ly] = trunk_c
            continue
        for cy in range(9, 12):
            half = 1 if cy > 9 else 0
            for cx in range(ax - half, ax + half + 1):
                if not (0 <= cx < _limits.W):
                    continue
                if mm in (9, 10, 11):
                    base = _render_vegetation.CANOPY_FALL[asp_rng.randrange(3)]
                else:
                    base = _render_vegetation.CANOPY_DAY
                px[cx, cy] = _render_primitives._shade(
                    _render_primitives._lerp_rgb(
                        (22, 34, 20), base, max(0.25, daylight)
                    ),
                    amb,
                )

    # Tall pines: triangular evergreens, tips gusting downwind
    pine_rng = random.Random(41)
    for tx, top in _render_art.FOREST_PINES:
        for ty in range(13, 15):
            px[tx, ty] = trunk_c
        sway = 0
        if rustle:
            gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
            sway = (wind_lean or 1) if gust > 0.3 else 0
        rows = 13 - top
        for i, cy in enumerate(range(top, 13)):
            half = min(2, i // 2)
            if i >= rows - 2:
                half = 3  # the skirt flares at the forest floor
            row_sway = sway if i < 3 else 0  # only the crown moves
            for cx in range(tx - half + row_sway, tx + half + 1 + row_sway):
                if not (0 <= cx < _limits.W):
                    continue
                deep = pine_rng.random() < 0.35
                pine_color = _render_primitives._lerp_rgb(
                    _render_art.PINE_NIGHT,
                    _render_art.PINE_DAY,
                    0.55 if deep else 1.0,
                )
                px[cx, cy] = _render_primitives._shade(
                    _render_primitives._lerp_rgb(
                        (12, 22, 18),
                        pine_color,
                        max(0.25, daylight),
                    ),
                    amb,
                )

    # The tent: lime ridge tent, door toward the fire. At night a
    # flashlight breathes inside — someone is up late with a book.
    tent_lit = elev < -2 or (wx.stormy and elev >= 0)
    breath = 0.5 + 0.5 * math.sin(math.tau * phase)
    page = 0.12 * math.sin(math.tau * 3 * phase + 0.7)  # page-turn flicker
    fabric = _render_primitives._shade(
        _render_primitives._lerp_rgb(
            _render_art.TENT_NIGHT, _render_art.TENT_DAY, daylight
        ),
        amb,
    )
    for i, ty in enumerate(range(10, 14)):
        half = i
        for tx2 in range(
            _render_art.TENT_APEX - half, _render_art.TENT_APEX + half + 1
        ):
            if not (0 <= tx2 < _limits.W):
                continue
            edge = abs(tx2 - _render_art.TENT_APEX) == half
            tent_color: _render_primitives.Color = (
                fabric
                if edge
                else _render_primitives._rgb_int(v * 0.82 for v in fabric)
            )
            if tent_lit and i >= 1:
                # interior light through the nylon, strongest mid-panel;
                # even seams transmit a little — nylon glows whole
                lit = (0.55 + 0.40 * breath + page) * (1.0 - i * 0.10)
                if edge:
                    lit *= 0.45
                tent_color = _render_primitives._lerp_rgb(
                    tent_color,
                    _render_art.TENT_GLOW,
                    max(0.0, min(1.0, lit)),
                )
            px[tx2, ty] = _render_primitives._rgb_int(tent_color)
    door = (_render_art.TENT_APEX - 2, 13)  # door pixel faces the campfire
    if tent_lit:
        px[door] = tuple(
            int(v)
            for v in _render_primitives._lerp_rgb(
                (30, 40, 14), _render_art.TENT_GLOW, 0.65 + 0.30 * breath
            )
        )
        _render_primitives._add_glow(
            px, _render_art.TENT_APEX, 12, (150, 190, 80), 3.0, 0.10 + 0.09 * breath
        )
    else:
        px[door] = tuple(int(v * 0.5) for v in fabric)

    # Campfire: lit after dusk (and in storm-dark daytime), rained out by
    # real rain or snow. Flames are emissive — never shaded.
    fire_on = elev < 2 and not (wx.rain or wx.snow or wx.stormy)
    log_c = _render_primitives._shade(
        _render_primitives._lerp_rgb((40, 26, 14), (92, 62, 36), daylight), amb
    )
    for lx in (_render_art.FIRE_X - 1, _render_art.FIRE_X, _render_art.FIRE_X + 1):
        px[lx, 14] = log_c
    if fire_on:
        flick = math.sin(math.tau * 5 * phase)  # five licks a loop
        flick2 = math.sin(math.tau * 5 * phase + 2.1)
        lean = wind_lean if wx.wind_kmh >= 12 else 0
        px[_render_art.FIRE_X, 13] = (255, 176, 56)
        px[_render_art.FIRE_X - 1, 13] = (
            (238, 120, 30) if flick > -0.3 else (150, 70, 20)
        )
        px[_render_art.FIRE_X + 1, 13] = (
            (238, 120, 30) if flick2 > -0.3 else (150, 70, 20)
        )
        tip_x = _render_art.FIRE_X + (lean if flick > 0.4 else 0)
        if flick > -0.15:
            px[tip_x, 12] = (255, 140, 36)
        if flick2 > 0.55:
            px[min(_limits.W - 1, max(0, tip_x + lean)), 11] = (222, 96, 26)
        _render_primitives._add_glow(
            px, _render_art.FIRE_X, 13, (255, 150, 50), 3.4, 0.10 + 0.05 * flick
        )
        # Sparks ride the heat into the dark, each on its own schedule:
        # launch time, climb, wobble, and lifespan all rolled fresh at
        # every rebuild, so no two minutes burn alike. Within a loop
        # every cycle count is an integer — the seam stays invisible.
        spark_rng = random.Random(seed * 11 + 4)
        for k in range(5):
            cycles = spark_rng.choice((1, 1, 2))
            off = spark_rng.random()
            climb = spark_rng.randint(5, 9)
            die = 0.55 + spark_rng.random() * 0.35
            prog = (phase * cycles + off) % 1.0
            if prog >= die:
                continue
            ey = 13 - int(prog * climb)
            ex = (
                _render_art.FIRE_X
                + round(math.sin(math.tau * 2 * phase + k * 2.1))
                + int(prog * (1 + wx.wind_kmh / 15)) * lean
            )
            if 0 <= ex < _limits.W and 0 <= ey < _limits.H:
                f = 1.0 - prog / die
                px[ex, ey] = (int(60 + 195 * f), int(130 * f), int(30 * f))
        # Woodsmoke climbs all the way to the sky, thinning as it goes
        for i in range(4):
            prog = (phase + i / 4) % 1.0
            sy = 11 - int(prog * 8)
            sx = (
                _render_art.FIRE_X
                + int(prog * (2 + wx.wind_kmh / 10)) * (lean or -1)
                + round(math.sin(math.tau * phase + i * 1.7) * prog)
            )
            if 0 <= sx < _limits.W and 0 <= sy < _limits.H:
                fade = (1.0 - prog) * 0.38 + 0.06
                px[sx, sy] = tuple(int(o * (1 - fade) + 122 * fade) for o in px[sx, sy])

    # A bunny works the bottom-right patch: grazes the near tuft, turns,
    # hops nose-first to the far tuft, grazes, turns, hops home. The loop
    # ends the way it starts, so the seam is invisible. Sits out the rain.
    if not (wx.rain or wx.snow or wx.stormy):
        base_x = 60 + (seed % 3)  # the patch drifts a little day to day
        tuft_c = _render_primitives._shade(
            _render_primitives._lerp_rgb((30, 44, 24), (62, 92, 44), daylight), amb
        )
        for tx2 in (base_x - 1, base_x + 5):
            if 0 <= tx2 < _limits.W:
                px[tx2, 13] = tuft_c
        fur = _render_primitives._shade(
            _render_primitives._lerp_rgb((96, 90, 80), (176, 158, 136), daylight), amb
        )
        hind = tuple(int(v * 0.78) for v in fur)
        head_c = tuple(min(255, int(v * 1.18)) for v in fur)
        frame = phase * 40
        # (left cell of the 2px body, facing, pose) — the v1 sprite with
        # direction: ear above the leading cell, turns before each hop
        if frame < 13:
            hx, d, pose = base_x, -1, "graze"  # home, at the near tuft
        elif frame < 15:
            hx, d, pose = base_x, 1, "graze"  # turns, eyes the far tuft
        elif frame < 17:
            hx, d, pose = base_x + 2, 1, "hop"  # airborne, ear leading
        elif frame < 29:
            hx, d, pose = base_x + 3, 1, "graze"  # far tuft
        elif frame < 31:
            hx, d, pose = base_x + 3, -1, "graze"  # turns for home
        elif frame < 33:
            hx, d, pose = base_x + 1, -1, "hop"  # hopping home
        else:
            hx, d, pose = base_x, -1, "graze"  # home again == frame 0
        nib = math.sin(math.tau * 3 * phase) > 0.45

        def _put(x, y, c):
            if 0 <= x < _limits.W and 0 <= y < _limits.H:
                px[x, y] = c

        lead = hx + (1 if d > 0 else 0)  # front cell of the body
        rear = hx + (0 if d > 0 else 1)
        by = 13 if pose == "hop" else 14  # the whole body lifts mid-hop
        _put(rear, by, hind)
        _put(lead, by, fur)
        if not (nib and pose == "graze"):  # ear drops while nibbling
            _put(lead, by - 1, head_c)

        # Fireflies orbit the firelight on summer nights — drawn to the
        # glow, never into it (hard exclusion around the flame columns)
        if elev < -6 and mm in (6, 7, 8) and wx.temp_c > 15:
            fly_rng2 = random.Random(seed * 23 + 9)
            for i in range(3):
                off = fly_rng2.random() * math.tau
                a = math.tau * phase + off  # one lazy orbit per loop
                rx2 = 4.5 + 1.3 * math.sin(math.tau * 2 * phase + i * 2.1)
                fx2 = _render_art.FIRE_X + round(rx2 * math.cos(a))
                fy2 = 11 - round(1.6 * math.sin(a)) - i % 2
                blink = math.sin(math.tau * 2 * phase + off * 3)
                if blink < 0.1:
                    continue
                if abs(fx2 - _render_art.FIRE_X) < 3 and fy2 >= 8:
                    fx2 = _render_art.FIRE_X + (3 if math.cos(a) >= 0 else -3)
                if 0 <= fx2 < _limits.W and 6 <= fy2 <= 13:
                    b = 0.45 + 0.55 * blink
                    px[fx2, fy2] = tuple(
                        int(c * b) for c in _render_vegetation.FIREFLY_COLOR
                    )

    # Settled snow: everything below the sky takes it on the top edge --
    # the floor, and the upper face of every bough that catches it.
    tier = _render_precipitation.snow_tier(wx.snow_depth_m)
    if tier:
        snow_tops = _render_precipitation.surface_tops(
            px, range(_limits.W), range(6, 16), sky_before
        )
        _render_precipitation.settle_snow(px, snow_tops, tier, amb)

    # Moonlight over the whole scene: pool, pine rims, tent sheen
    if moon_pos is not None:
        moon_x, moon_y = moon_pos
        _render_astronomy._apply_moonlight_forest(
            px,
            moon_x,
            moon_y,
            moon_ill,
            cloud,
            phase,
            fire_on,
        )

"""Skystrip render / scene."""

from __future__ import annotations

import math
import random
from datetime import datetime

from astral.sun import elevation
from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import astronomy as _render_astronomy
from apps.skystrip_app.render import atmosphere as _render_atmosphere
from apps.skystrip_app.render import backroads as _render_backroads
from apps.skystrip_app.render import city as _render_city
from apps.skystrip_app.render import forest as _render_forest
from apps.skystrip_app.render import grove as _render_grove
from apps.skystrip_app.render import lakefront as _render_lakefront
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import season as _render_season
from apps.skystrip_app.render import status as _render_status
from apps.skystrip_app.render import vegetation as _render_vegetation


def render_scene(
    now: datetime,
    wx: _weather.WeatherState,
    seed: int,
    phase: float = 0.0,
    scene: str = "house",
    scrubbed: bool = False,
    lightning: float = 0.0,
    lane: bool = True,
) -> Image.Image:
    """Compose one 72x16 frame; `phase` in [0,1) animates a seamless loop."""
    elev = elevation(_settings.OBSERVER, now)
    horizon, zenith = _render_primitives._sky_colors(elev)

    cloud = 1.0 if wx.stormy else wx.cloud_frac
    if wx.stormy:
        pull = 0.9 if wx.severe else 0.7
        horizon = _render_primitives._lerp_rgb(horizon, _render_art.STORM_HORIZON, pull)
        zenith = _render_primitives._lerp_rgb(zenith, _render_art.STORM_ZENITH, pull)
    elif cloud > 0:
        for which in ("h", "z"):
            c = horizon if which == "h" else zenith
            lum = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
            mixed = _render_primitives._lerp_rgb(c, (lum, lum, lum), 0.65 * cloud)
            if which == "h":
                horizon = mixed
            else:
                zenith = mixed

    dim = 0.6 if (wx.rain or wx.snow) else 1.0
    if wx.snow:
        horizon = _render_primitives._lerp_rgb(horizon, (200, 200, 205), 0.5)
        zenith = _render_primitives._lerp_rgb(zenith, (150, 150, 158), 0.5)

    rng = random.Random(seed)
    img = Image.new("RGB", (_limits.W, _limits.H))
    px = _render_primitives._rgb_pixels(img)
    for y in range(_limits.H):
        t = 1.0 - (y / (_limits.H - 1))
        base = _render_primitives._lerp_rgb(zenith, horizon, 1.0 - t * t)
        for x in range(_limits.W):
            wave = 1.0 + 0.06 * math.sin(
                (x / _limits.W) * math.tau + seed * 0.7 + y * 0.18
            )
            n = rng.uniform(-3.5, 3.5)
            px[x, y] = tuple(max(0, min(255, int(c * wave * dim + n))) for c in base)

    # Stars: a static field, like real life — and once in a while a single
    # star flashes for one frame. Three twinkles per 8s loop, total.
    if elev < -8 and cloud < 0.85 and not wx.stormy:
        depth = min(1.0, (-elev - 8) / 6)
        frame_i = int(phase * _limits.ANIM_FRAMES)
        glint_rng = random.Random(seed * 31 + 4)
        glints = {
            (
                glint_rng.randrange(len(_render_art.STARS)),
                glint_rng.randrange(_limits.ANIM_FRAMES),
            )
            for _ in range(3)
        }
        # An obscuration takes the stars first — they are the dimmest thing
        # in the frame, so they are the first casualty of anything in the air.
        star_clear = 0.25 if wx.obscuration else 1.0
        for si, (sx, sy, mag) in enumerate(_render_art.STARS):
            b = depth * (1.0 - cloud) * mag * star_clear
            if (si, frame_i) in glints:
                b = min(1.6 * b + 0.5, 1.8)
            if b > 0.1:
                px[sx, sy] = tuple(
                    min(255, int(c0 + s * b))
                    for c0, s in zip(px[sx, sy], _render_art.STAR_COLOR)
                )

    # Sun by day, moon by night, nothing during handover twilight
    local = now.astimezone(_settings.TZ)
    day_frac = (local.hour * 60 + local.minute) / 1440
    moon_pos: tuple[int, int] | None = None
    moon_ill = 0.0
    sun_pos = _render_astronomy._sun_screen_pos(now, elev, wx, cloud)
    if sun_pos is not None:
        strength = max(0.2, 1.0 - cloud)
        _render_astronomy._draw_sun(
            px, sun_pos[0], sun_pos[1], strength, math.sin(math.tau * phase)
        )
    elif elev < -4 and cloud < 0.85 and not wx.stormy:
        night_f = (day_frac + 0.5) % 1.0  # 0 at ~noon; ~.33-.66 covers night
        f = min(max((night_f - 0.30) / 0.40, 0.0), 1.0)
        cx = int(20 + f * 24)  # starts right of the clock's corner
        cy = int(6 - 4 * math.sin(math.pi * f)) + 1
        if scene == "skyline":
            cy += 3  # ride low behind the towers: the big city moon
        elif scene == "lakefront":
            cx = int(25 + f * 9)  # over the open water, clear of the clock
        breath = math.sin(math.tau * phase)
        phase_days = (
            _render_primitives.MOON_DAY_OVERRIDE
            if _render_primitives.MOON_DAY_OVERRIDE is not None
            else _render_astronomy._moon_age_days(local.date())
        )
        eclipse = _render_astronomy._eclipse_now(now)
        _render_astronomy._draw_moon(px, cx, cy, phase_days, breath, eclipse)
        moon_pos = (cx, cy)
        moon_ill = (1.0 - math.cos(math.tau * phase_days / 29.53)) / 2
        if eclipse is not None:
            # Everything the moon lights — the silver pool, the rim on the
            # roofline, the sheen on the towers — fades with the disc that
            # is still in sunlight. At totality the landscape goes dark,
            # which is the whole reason people go outside to watch.
            moon_ill *= 1.0 - eclipse.obscuration

    # Cloud puffs drift over the stars and under the precipitation
    daylight_now = min(max((elev + 6) / 18, 0.0), 1.0)
    if cloud > 0.1:
        _render_atmosphere._draw_clouds(px, now, cloud, daylight_now, wx.stormy)

    if lightning > 0:
        # Illuminate the sky *before* any ground, buildings, trees, water, or
        # status ink is composed.  The result is a lit storm backdrop, not the
        # old opaque white rectangle over the entire product.
        strength = min(1.0, max(0.0, lightning)) * 0.72
        lightning_sky = (176, 196, 224)
        for ly in range(_limits.H):
            for lx in range(_limits.W):
                px[lx, ly] = tuple(
                    min(255, int(base + (lit - base) * strength))
                    for base, lit in zip(px[lx, ly], lightning_sky)
                )

    # Ground: the original moss row, a touch lighter in daylight
    daylight = min(max((elev + 6) / 18, 0.0), 1.0)
    ground = _render_primitives._shade(
        _render_primitives._lerp_rgb(_render_art.GROUND_NIGHT, (86, 108, 60), daylight),
        _render_primitives._ambient(elev, cloud, wx),
    )
    for x in range(_limits.W):
        px[x, 15] = tuple(int(c) for c in ground)

    # Wind: wisp streaks riding the actual wind speed
    if wx.wind_kmh >= 8:
        n = min(3, 1 + int(wx.wind_kmh // 15))
        # one drift across per 8s loop in a breeze, up to three in a gale
        wraps = min(3, 1 + int(wx.wind_kmh // 25))
        wisp_rng = random.Random(23)
        span = _limits.W + 12
        for _ in range(n):
            x0 = wisp_rng.randrange(0, span)
            wy = wisp_rng.randrange(2, 10)
            head = int(x0 - phase * wraps * span)
            for dx, dy in _render_vegetation.WISP_SHAPE:
                x = (head + dx) % span - 6
                y = wy + dy
                if 0 <= x < _limits.W and 0 <= y < _limits.H:
                    px[x, y] = tuple(min(255, c + 26) for c in px[x, y])

    # The murmuration returns at dusk, playing its original eight shapes
    # once per loop, dark or pale per-dot for contrast against the gradient
    if (
        -8 < elev < 3
        and local.hour >= 16
        and (seed % 5) < 2
        and not (wx.rain or wx.stormy)
    ):
        shape = _render_art.FLOCK_FRAMES[int(phase * 8) % 8]
        for fx, fy in shape:
            base = px[fx, fy]
            lum = 0.3 * base[0] + 0.59 * base[1] + 0.11 * base[2]
            dot = (30, 26, 34) if lum > 90 else (172, 172, 186)
            px[fx, fy] = tuple(int(o * 0.35 + c * 0.65) for o, c in zip(base, dot))

    amb = _render_primitives._ambient(elev, cloud, wx)
    if scene == "skyline":
        _render_city._draw_skyline(
            px,
            local,
            elev,
            daylight,
            seed,
            phase,
            horizon,
            wx,
            amb,
            storm_day=wx.stormy and elev >= 0,
        )
        # Moonlight on the city: silvered rooflines, no pool on the street
        if moon_pos is not None:
            _render_astronomy._apply_moonlight_skyline(
                px, *moon_pos, moon_ill, cloud, phase
            )
    elif scene == "lakefront":
        # The lake makes its own moonlight: the glitter path is the pool
        _render_lakefront._draw_lakefront(
            px,
            local,
            elev,
            daylight,
            seed,
            phase,
            wx,
            horizon,
            sun_pos,
            moon_pos,
            moon_ill,
            cloud,
        )
    elif scene == "forest":
        _render_forest._draw_forest(
            px, local, elev, daylight, seed, phase, wx, amb, moon_pos, moon_ill, cloud
        )
    elif scene == "grove":
        _render_grove._draw_grove(
            px, local, elev, daylight, seed, phase, wx, amb, moon_pos, moon_ill, cloud
        )
    elif scene == "backroads":
        _render_backroads._draw_backroads(
            px, local, elev, daylight, seed, phase, wx, amb, moon_ill, cloud, lane=lane
        )
    else:
        # The house, exactly as drawn in August 2026; lamplight breathes in
        # the window itself and in a glow centered on it, with some flicker.
        # By day the night palette lifts to sunlit walls and sky-glass.
        window_lit = elev < 2 or wx.stormy  # lights on when it's dark out
        # one full breath per 8s loop, with a whisper of flicker over it
        pulse_norm = (
            0.5
            + 0.5 * math.sin(math.tau * phase)
            + 0.12 * math.sin(math.tau * phase * 5 + 1.7)
        )
        house_day = {
            (26, 24, 32): (118, 104, 96),  # walls in sun
            (38, 30, 40): (140, 116, 104),  # roof
            (18, 16, 22): (92, 82, 78),  # trim/shadow
        }
        for hx, hy, color in _render_art.HOUSE_SPRITE:
            if color == _render_art.WINDOW_COLOR:
                if window_lit:
                    color = _render_primitives._rgb_int(
                        c * (0.72 + 0.28 * pulse_norm) for c in _render_art.WINDOW_COLOR
                    )
                else:
                    color = _render_primitives._rgb_int(
                        _render_primitives._lerp_rgb(
                            (30, 28, 36),
                            (150, 180, 205),
                            daylight,
                        )
                    )
            elif color in house_day:
                color = _render_primitives._shade(
                    _render_primitives._lerp_rgb(color, house_day[color], daylight), amb
                )
            px[hx, hy] = color
        if window_lit:
            _render_primitives._add_glow(
                px,
                *_render_art.WINDOW_CENTER,
                _render_art.WINDOW_COLOR,
                3.2,
                0.08 + 0.10 * pulse_norm,
            )

        # Christmas lights along the roofline, roof drawn but not yet
        # occluded by anything foreground (trees, snow, moonlight below).
        if _render_season.is_christmas(local):
            eaves = [
                (x, _render_art.HOUSE_TOP[x]) for x in sorted(_render_art.HOUSE_TOP)
            ]
            _render_season.string_lights(px, eaves, phase, amb)

        # Two trees in the yard, seasonal: green, autumn orange, winter bare
        mm = local.month
        rustle = wx.wind_kmh >= 8
        # Downwind lean: screen-right is east. N/S winds rustle in place.
        wind_lean = 0
        if wx.wind_dir is not None and wx.wind_kmh >= 5:
            comp = math.sin(math.radians(wx.wind_dir + 180))
            wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
        tree_rng = random.Random(37)
        trunk_c = _render_primitives._shade(
            _render_primitives._lerp_rgb(
                _render_vegetation.TRUNK_NIGHT, _render_vegetation.TRUNK_DAY, daylight
            ),
            amb,
        )
        for tx, size in _render_vegetation.TREES:
            top = 13 - size * 2  # big tree canopy starts at y9, small at y11
            for ty in range(top + size + 2, 15):  # trunk below the canopy
                px[tx, ty] = trunk_c
            if _render_grove.is_winter(local):  # winter: bare limbs
                for lx, ly in (
                    (tx - 1, top + 1),
                    (tx + 1, top + 1),
                    (tx, top),
                    (tx - size, top + 2),
                    (tx + size, top + 2),
                ):
                    if 0 <= lx < _limits.W:
                        px[lx, ly] = trunk_c
                continue
            for ci, cy2 in enumerate(range(top, top + size + 2)):
                half = (1, size + 1, size + 1, size)[min(ci, 3)]
                sway = 0
                if rustle and ci == 0:  # the crown gusts downwind, relaxes back
                    gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
                    lean = wind_lean if wind_lean else 1
                    sway = lean if gust > 0.25 else 0
                for cx2 in range(tx - half + sway, tx + half + 1 + sway):
                    if not (0 <= cx2 < _limits.W):
                        continue
                    if mm in (9, 10, 11):
                        base = _render_vegetation.CANOPY_FALL[tree_rng.randrange(3)]
                    else:
                        base = (
                            _render_vegetation.CANOPY_NIGHT
                            if tree_rng.random() < 0.35
                            else _render_primitives._lerp_rgb(
                                _render_vegetation.CANOPY_NIGHT,
                                _render_vegetation.CANOPY_DAY,
                                1.0,
                            )
                        )
                    px[cx2, cy2] = _render_primitives._shade(
                        _render_primitives._lerp_rgb(
                            (22, 34, 20), base, max(0.25, daylight)
                        ),
                        amb,
                    )

        # Grass with real height variance; a wind wave rolls through it
        wave_on = wx.wind_kmh >= 5
        g1 = _render_primitives._shade(
            _render_primitives._lerp_rgb(
                _render_vegetation.GRASS_COLOR, (70, 100, 48), daylight
            ),
            amb,
        )
        g2 = _render_primitives._shade(
            _render_primitives._lerp_rgb(
                _render_vegetation.GRASS_COLOR_2, (58, 86, 40), daylight
            ),
            amb,
        )
        tier = _render_precipitation.snow_tier(wx.snow_depth_m)
        if tier >= 3:
            # Buried: the tufts are gone, and that absence IS the depth cue.
            _render_precipitation.settle_snow(
                px, {x: 14 for x in range(_limits.W)}, tier, amb
            )
        else:
            # settle_snow's contract: tops[x] is the topmost pixel actually
            # on the ground in that column, and snow lands ON it -- the same
            # question surface_tops answers for every other scene. A wind
            # wave can shift a tall blade's row-13 pixel sideways, so the
            # real top per column can only be read AFTER the fringe is
            # drawn, from what actually landed -- not guessed from height
            # alone (that guess used to land one row above the tuft, and two
            # rows above it once the wave moved the blade out from under the
            # guess entirely).
            fringe_x = range(2, 47)  # GRASS_FRINGE's own span, clear of the house
            sky_before = {px[x, y] for x in fringe_x for y in (13, 14)}
            for gx, gh in _render_art.GRASS_FRINGE:
                if gh == 0:
                    continue
                color = g1 if gx % 2 else g2
                px[gx, 14] = color
                if gh == 2:
                    xoff = 0
                    if wave_on and math.sin(math.tau * 2 * phase + gx * 0.55) > 0.2:
                        xoff = wind_lean if wind_lean else 1
                    if 0 <= gx + xoff < _limits.W:
                        px[gx + xoff, 13] = color
            if tier > 0:
                # Row 15 is in range on purpose: 16 of the 45 fringe columns
                # have gh == 0, so rows 13-14 are still bare sky there and
                # surface_tops would skip them entirely -- losing ~40% of the
                # ground snow on exactly the columns with no tuft to catch it.
                # Bare lawn takes snow too. sky_before deliberately stays at
                # rows 13-14: it is snapshotted before the grass is drawn, so
                # adding row 15 would file the lawn itself as sky and exclude
                # the very pixels this is here to include.
                tops = _render_precipitation.surface_tops(
                    px, fringe_x, (13, 14, 15), sky_before
                )
                _render_precipitation.settle_snow(px, tops, tier, amb)

        # Moonlight falls on the scene, sliding along with the moon itself
        if moon_pos is not None:
            _render_astronomy._apply_moonlight(px, *moon_pos, moon_ill, cloud, phase)

    # Precipitation falls in front of every scene's foreground (it used to
    # draw before the scene branch and vanished behind water, walls, towers)
    if _render_precipitation.is_raining(wx):
        _render_precipitation.draw_rain(px, wx, seed, phase)
    if wx.snow:
        _render_precipitation.draw_snow(px, seed, phase)

    month = local.month

    # Fireflies: warm summer nights over the grass, each blinking on its own
    if (
        scene in ("house", "forest", "grove")
        and month in (6, 7, 8)
        and elev < -6
        and wx.temp_c > 15
        and not (wx.rain or wx.stormy)
    ):
        fly_rng = random.Random(seed * 7 + 2)
        for i in range(3):
            if scene == "forest":  # they gather over the clearing
                fx = fly_rng.randrange(35, 49)
                fy = fly_rng.randrange(9, 13)
            else:
                fx = fly_rng.randrange(3, 46)
                fy = fly_rng.randrange(9, 14)
            blink = math.sin(math.tau * phase + i * 2.4)  # one cycle per loop
            if blink > 0.35:
                bright = (blink - 0.35) / 0.65
                drift = int(2 * math.sin(math.tau * 2 * phase + i))
                x = min(_limits.W - 1, max(0, fx + drift))
                px[x, fy] = tuple(
                    int(c * (0.4 + 0.6 * bright))
                    for c in _render_vegetation.FIREFLY_COLOR
                )

    # Chimney smoke on cold days: a small chimney appears, puffs drift upwind
    if scene == "house" and wx.temp_c < 5:
        ch_x, ch_y = _render_vegetation.CHIMNEY
        px[ch_x, ch_y] = (26, 24, 32)
        px[ch_x, ch_y + 1] = (26, 24, 32)
        for i in range(4):
            prog = (phase + i / 4) % 1.0  # puffs spaced along the plume
            sy = ch_y - 1 - int(prog * 5)
            sx = ch_x - int(prog * (2 + wx.wind_kmh / 12))
            if 0 <= sx < _limits.W and 0 <= sy < _limits.H:
                fade = (1.0 - prog) * 0.55
                px[sx, sy] = tuple(int(o * (1 - fade) + 126 * fade) for o in px[sx, sy])

    # A morning bird, some minutes, crossing once per loop with a lazy flap
    if (
        -4 < elev < 10
        and local.hour < 12
        and (seed % 7) < 2
        and not (wx.rain or wx.stormy)
    ):
        prog = phase
        bx = int(prog * (_limits.W + 10)) - 5
        by = 4 + int(1.5 * math.sin(math.tau * 2 * prog))
        flap = int(phase * _limits.ANIM_FRAMES) % 4 < 2
        if 1 <= bx < _limits.W - 1 and 0 < by < _limits.H - 1:
            px[bx, by] = _render_vegetation.BIRD_COLOR
            wing_y = by - 1 if flap else by
            px[bx - 1, wing_y] = _render_vegetation.BIRD_COLOR
            px[bx + 1, wing_y] = _render_vegetation.BIRD_COLOR

    # Autumn leaves tumbling on the wind
    if month in (9, 10, 11) and wx.wind_kmh >= 5 and not wx.snow:
        leaf_rng = random.Random(seed * 5 + 1)
        span_x = _limits.W + 20
        span_y = _limits.H - 3
        for i in range(3):
            x0 = leaf_rng.randrange(0, span_x)
            y0 = leaf_rng.randrange(0, span_y)
            prog_x = (x0 - int(phase * span_x)) % span_x - 10
            prog_y = (y0 + int(phase * 2 * span_y)) % span_y
            sway = round(math.sin(math.tau * 3 * phase + i * 2.1))
            lx, ly = prog_x + sway, prog_y
            if 0 <= lx < _limits.W and 0 <= ly < _limits.H:
                px[lx, ly] = _render_vegetation.LEAF_COLORS[
                    i % len(_render_vegetation.LEAF_COLORS)
                ]

    # Ground fog on humid mornings or in genuinely low visibility, two
    # counter-drifting sine layers hugging the grass and the house's feet
    # The air column, before the ground-hugging fog layer and before the
    # status ink — the clock is a readout, not part of the weather, and
    # must stay legible through a smoke day.
    if wx.obscuration:
        _render_atmosphere._apply_obscuration(px, wx.obscuration, daylight_now)

    fog_d = 0.0
    if wx.visibility_m < 5000:
        fog_d = min(0.65, 0.3 + (5000 - wx.visibility_m) / 5000 * 0.35)
    elif wx.humidity >= 88 and elev < 12:
        fog_d = 0.25 + 0.30 * min(1.0, (wx.humidity - 88) / 10)
    if wx.fog:
        # A source saying "fog" outright is stronger evidence than either
        # number above, both of which are inferences. Patchy fog that leaves
        # the official visibility reading healthy is still fog to anyone
        # standing in it, so it gets a floor rather than a threshold; low
        # visibility still deepens it past this.
        fog_d = max(fog_d, 0.40)
    if fog_d > 0:
        fog_col = _render_primitives._lerp_rgb((52, 56, 66), (198, 203, 209), daylight)
        row_weight = {10: 0.35, 11: 0.6, 12: 0.85, 13: 1.0, 14: 1.0, 15: 0.5}
        for x in range(_limits.W):
            wave = (
                0.5
                + 0.3 * math.sin(x / 9 - math.tau * phase)
                + 0.2 * math.sin(x / 17 + math.tau * phase + 1.3)
            )
            for y, wgt in row_weight.items():
                a = fog_d * wave * wgt
                px[x, y] = tuple(
                    int(o * (1 - a) + c * a) for o, c in zip(px[x, y], fog_col)
                )

    _render_status._bake_status(px, now, wx, phase, scene, scrubbed)
    return img


def render_loop_frames(
    now: datetime,
    wx: _weather.WeatherState,
    seed: int,
    scene: str = "house",
    scrubbed: bool = False,
    n_frames: int | None = None,
) -> list[Image.Image]:
    # backroads runs a double-rate loop: full-width traffic needs
    # ~1px/frame to look driven rather than dragged. Precipitation needs it
    # for the same reason -- at 40 frames a downpour jumps 6 of 16 rows per
    # frame and strobes instead of falling. `fps` is derived from the frame
    # count downstream, so the loop stays 8 seconds either way and no other
    # motion in the scene changes rate.
    # Snow is left at 40: it drifts at 0.75 rows/frame already.
    n = n_frames or (
        80
        if scene == "backroads" or _render_precipitation.is_raining(wx)
        else _limits.ANIM_FRAMES
    )
    return [
        render_scene(now, wx, seed, phase=i / n, scene=scene, scrubbed=scrubbed)
        for i in range(n)
    ]


def render_sky(now: datetime, wx: _weather.WeatherState, seed: int) -> Image.Image:
    """Back-compat alias for earlier scripts."""
    return render_scene(now, wx, seed)

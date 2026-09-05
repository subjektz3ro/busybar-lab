"""Skystrip render / city."""

from __future__ import annotations

import math
import random

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import precipitation as _render_precipitation
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import season as _render_season


def _building_row_span(
    x0: int, x1: int, h: int, kind: int, y: int
) -> tuple[int, int] | None:
    """Horizontal extent of a building at row y, honoring setbacks/taper.

    Buildings stand on the street row (15), so bodies span top..14.
    """
    top = _limits.H - 1 - h
    if y < top or y > 14:
        return None
    if kind == 1 and y <= top + 1:  # stepped crown
        return (x0 + 2, x1 - 2)
    if kind == 2:  # taper
        if y == top:
            return (x0 + 2, x1 - 2)
        if y == top + 1:
            return (x0 + 1, x1 - 1)
    return (x0, x1)


def _draw_skyline(
    px,
    local,
    elev: float,
    daylight: float,
    seed: int,
    phase: float,
    horizon: tuple,
    wx: _weather.WeatherState,
    amb: tuple = (1, 1, 1),
    storm_day: bool = False,
) -> None:
    # Snapshot the living sky (and the plain ground row under it) before
    # this scene paints towers over it -- same reasoning as _draw_grove's
    # sky_before: the gradient carries per-pixel noise, so there's no
    # fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(_limits.W) for y in range(_limits.H)}
    front_col = _render_primitives._shade(
        _render_primitives._lerp_rgb((24, 26, 34), (126, 130, 138), daylight), amb
    )
    back_col = tuple(
        int(c) for c in _render_primitives._lerp_rgb(front_col, horizon, 0.5)
    )
    night = elev < 0

    for x0, x1, h in _render_art.SKYLINE_BACK:
        for x in range(x0, x1 + 1):
            for y in range(_limits.H - 1 - h, _limits.H - 1):
                px[x, y] = back_col

    hour = local.hour
    if not night:
        lit_frac = 0.25 if storm_day else 0.0  # storm gloom: lights on
    elif 17 <= hour <= 22:
        lit_frac = 0.55  # the city is home from work
    elif hour >= 23 or hour < 5:
        lit_frac = 0.18  # night owls
    else:
        lit_frac = 0.35  # early risers / late dusk

    # Every lit window this frame lands here, in the exact order drawn --
    # the Christmas recolour below reads this list rather than re-deriving
    # "which pixels are windows" by matching WINDOW_WARM/WINDOW_COOL, since
    # that colour also appears on the house sprite and the lakefront lamps.
    lit_windows: list[tuple[int, int]] = []

    for bi, (x0, x1, h, kind) in enumerate(_render_art.SKYLINE_FRONT):
        top = _limits.H - 1 - h
        # Two facade languages, alternating: curtain-wall glass towers (the
        # whole face is glass, darker floor-slab lines every other row) and
        # punched masonry (lighter wall, clearly darker window grid — by
        # day real windows read DARKER than the facade around them)
        curtain = bi % 2 == 0
        if curtain:
            body = _render_primitives._shade(
                _render_primitives._lerp_rgb((14, 17, 26), (104, 120, 142), daylight),
                amb,
            )
            slab = _render_primitives._shade(
                _render_primitives._lerp_rgb((10, 12, 18), (76, 88, 104), daylight), amb
            )
        else:
            body = _render_primitives._shade(
                _render_primitives._lerp_rgb((22, 22, 30), (132, 128, 118), daylight),
                amb,
            )
            wind_c = _render_primitives._shade(
                _render_primitives._lerp_rgb((34, 36, 48), (86, 98, 120), daylight), amb
            )
        for y in range(top, _limits.H - 1):
            span = _building_row_span(x0, x1, h, kind, y)
            if span:
                row = slab if curtain and (y - top) % 2 == 0 else body
                for x in range(span[0], span[1] + 1):
                    px[x, y] = row
        # Which windows are lit shifts every ten minutes, and some
        # buildings keep a whole office floor burning
        win_rng = random.Random(seed * 13 + bi * 7)
        office_floor = None
        if win_rng.random() < 0.3:
            office_floor = top + 1 + 2 * win_rng.randrange(max(1, (13 - top) // 2))
        for wy in range(top + 1, 14, 2):
            span = _building_row_span(x0, x1, h, kind, wy)
            if not span:
                continue
            for wxx in range(span[0] + 1, span[1], 2):
                if lit_frac > 0 and (wy == office_floor or win_rng.random() < lit_frac):
                    color = (
                        _render_art.WINDOW_WARM
                        if win_rng.random() < 0.7
                        else _render_art.WINDOW_COOL
                    )
                    px[wxx, wy] = color
                    lit_windows.append((wxx, wy))
                elif not curtain:
                    px[wxx, wy] = wind_c
        # Rooftop furniture on the flat roofs: antennas and water tanks
        if kind == 0:
            if bi % 3 == 0:
                ax = (x0 + x1) // 2
                for ay in (top - 1, top - 2):
                    if ay >= 0:
                        px[ax, ay] = front_col
            elif bi % 3 == 1:
                for ty in (top - 1, top - 2):
                    if ty >= 0:
                        px[x0 + 1, ty] = front_col
                        px[x0 + 2, ty] = front_col
        # Twin masts with aviation beacons on the tall towers
        if kind in (1, 2):
            for mi, mx in enumerate((x0 + 2, x1 - 2)):
                for my in range(max(0, top - 3), top):
                    px[mx, my] = front_col
                # ~one blink every two seconds, like the real ones
                if math.sin(math.tau * 4 * phase + bi * 1.7 + mi) > 0.7:
                    px[mx, max(0, top - 3)] = _render_art.BEACON_RED
            if kind == 2 and night:  # lit crown on the tapered tower
                span = _building_row_span(x0, x1, h, kind, top)
                if span:
                    for x in range(span[0], span[1] + 1):
                        px[x, top] = (150, 140, 110)

    if _render_season.is_christmas(local):
        # Recolour, never add: extra lit windows would change the tower's
        # apparent occupancy, and more lit pixels is this panel's haze
        # direction (LEDs lit on a mostly-dark pitch -- see the app skill).
        # Only a minority turn festive, so the skyline still reads as an
        # office tower first and a decoration second.
        #
        # Seeded independently of `seed`/`phase` so the choice of WHICH lit
        # windows go red or green is fixed across frames and across
        # weather -- a window that flips colour every frame reads as a
        # fault, not decoration. lit_windows is itself already deterministic
        # for a given seed/hour (built above from win_rng), so replaying the
        # same festive draws over it in the same order reproduces the same
        # festive windows every time.
        festive = random.Random(1225)
        for fx, fy in lit_windows:
            if festive.random() < 0.35:
                # Raw, not _shade()'d: lit windows are emissive, same as the
                # warm/cool windows beside them (see the raw write above at
                # the base window loop) -- they are the window's own light,
                # not a surface reflecting the ambient. Shading it agrees
                # with the raw write at night (amb is the identity there),
                # but diverges in the storm_day path (lit_frac=0.25,
                # amb != identity): a shaded festive window there lands
                # ~30% dimmer than its unshaded warm neighbour, right at
                # the panel's contrast floor, so it reads as dirty rather
                # than festive.
                px[fx, fy] = _render_season.XMAS_BULBS[festive.randrange(2)]

    # The street: two streams of traffic at different speeds for parallax —
    # headlights flowing one way, taillights the other
    street = _render_primitives._shade(
        _render_primitives._lerp_rgb((15, 15, 19), (112, 110, 108), daylight), amb
    )
    for x in range(_limits.W):
        px[x, 15] = street
    span_r = _limits.W + 10
    car_rng = random.Random(41)
    for _ in range(4):  # the fast stream, two crossings per loop
        x0 = car_rng.randrange(span_r)
        pos = (x0 + int(phase * 2 * span_r)) % span_r - 5
        if 0 <= pos < _limits.W:
            px[pos, 15] = _render_art.HEADLIGHT if night else _render_art.CAR_DARK
            if pos > 0:
                px[pos - 1, 15] = (
                    tuple(c // 2 for c in _render_art.HEADLIGHT)
                    if night
                    else _render_art.CAR_DARK
                )
    for _ in range(3):  # the slow stream, one crossing per loop
        x0 = car_rng.randrange(span_r)
        pos = (x0 - int(phase * span_r)) % span_r - 5
        if 0 <= pos < _limits.W:
            px[pos, 15] = _render_art.TAILLIGHT if night else _render_art.CAR_DARK
            if pos < _limits.W - 1:
                px[pos + 1, 15] = (
                    tuple(c // 2 for c in _render_art.TAILLIGHT)
                    if night
                    else _render_art.CAR_DARK
                )

    # Settled snow: rooftops, not ground. There's no single "roof" row --
    # towers step to all sorts of heights -- so the answer is the same one
    # every scene asks: the topmost non-sky pixel per column, searched
    # over the whole frame rather than a fixed band near the floor.
    tier = _render_precipitation.snow_tier(wx.snow_depth_m)
    if tier:
        tops = _render_precipitation.surface_tops(
            px, range(_limits.W), range(_limits.H), sky_before
        )
        _render_precipitation.settle_snow(px, tops, tier, amb)

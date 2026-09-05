"""Skystrip render / astronomy."""

from __future__ import annotations

import math
from datetime import datetime

from astral import moon

from apps.skystrip_app import eclipse as _eclipse
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import city as _render_city
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import vegetation as _render_vegetation


def _sun_screen_pos(
    now: datetime, elev: float, wx: _weather.WeatherState, cloud: float
) -> tuple[int, int] | None:
    """Where the sun disc sits on screen, or None when it is not drawn.

    Shared by the renderer and the status ink choice: the morning arc
    starts at x=4, inside the status corner, and the ink beside a ~250
    luminance disc must be dark no matter what the weather estimate says.
    """
    if elev <= 0 or wx.stormy or cloud >= 0.9:
        return None
    local = now.astimezone(_settings.TZ)
    day_frac = (local.hour * 60 + local.minute) / 1440
    f = min(max((day_frac - 0.23) / 0.58, 0.0), 1.0)  # ~5:30-19:30 arc
    return int(4 + f * 40), int(6 - 4 * math.sin(math.pi * f)) + 1


def _draw_sun(px, cx: int, cy: int, strength: float, breath: float) -> None:
    """The sun with the same care as the moon: the classic round 7px disc
    (sun and moon subtend the same angle, so same size), a near-white
    blinding core inside the gold body, a soft limb, and a wide halo that
    breathes. `strength` fades it all behind real cloud."""
    r = 3.4  # the 3,5,7,7,7,5,3 raster circle, same as the moon
    core_r = 1.8
    _render_primitives._add_glow(
        px, cx, cy, _render_art.SUN_COLOR, 5.5, (0.30 + 0.30 * strength + 0.04 * breath)
    )
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            d2 = dx * dx + dy * dy
            if d2 > r * r:
                continue
            x, y = cx + dx, cy + dy
            if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                continue
            col = (255, 251, 230) if d2 <= core_r * core_r else _render_art.SUN_COLOR
            alpha = strength
            if d2 > (r - 0.7) * (r - 0.7):
                alpha *= 0.55  # soft limb into the sky
            px[x, y] = tuple(
                int(b * (1 - alpha) + c * alpha) for b, c in zip(px[x, y], col)
            )


def _draw_moon(
    px, cx: int, cy: int, phase_days: float, breath: float, eclipse=None
) -> None:
    """The moon as it actually looks tonight: correct phase and orientation,
    earthshine on the shadowed part, a soft terminator, and its own face.

    Waxing = lit on the right, waning = lit on the left.

    `eclipse` is an `EclipseState` when Earth's shadow is on the disc. It
    arrives already converted to Moon-radii on screen axes, so the geometry
    here is one circle test per pixel and nothing more. Only the umbra is
    drawn: a penumbral eclipse is a few percent of dimming that nobody sees
    without instruments, and painting one would invent a spectacle.
    """
    # r chosen for the classic round 7px raster circle (row widths
    # 3,5,7,7,7,5,3) — r ~3.1 degenerates into a square with four nubs
    r = 3.4
    synodic = 29.53
    ill = (1.0 - math.cos(math.tau * phase_days / synodic)) / 2  # lit fraction
    waxing = phase_days <= synodic / 2
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            if dx * dx + dy * dy > r * r:
                continue
            half_w = math.sqrt(max(r * r - dy * dy, 1e-4))
            t = half_w * (1.0 - 2.0 * ill)  # terminator x for this row
            metric = (dx - t) if waxing else (-dx - t)  # >=0 means sunlit
            x, y = cx + dx, cy + dy
            if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                continue
            if metric >= 0.6 or ill >= 0.98:
                color = _render_art.MOON_COLOR
                factor = _render_primitives.MOON_MARIA.get((dx, dy))
                if factor is not None:
                    color = _render_primitives._rgb_int(c * factor for c in color)
            elif metric >= -0.6 and ill > 0.02:
                color = _render_primitives.MOON_TERMINATOR
            else:
                color = _render_primitives.MOON_EARTHSHINE
            if eclipse is not None and eclipse.in_umbra:
                # Position of this pixel, and of the umbra's centre, both in
                # Moon radii — the only units the shadow geometry speaks.
                depth = eclipse.umbra_r - math.hypot(
                    dx / r - eclipse.umbra_dx, dy / r - eclipse.umbra_dy
                )
                if depth > 0:
                    # Maria are deliberately dropped inside the shadow: a
                    # 30%-darkened patch of an already-dim ember is a delta
                    # the panel cannot show, so it would only muddy the one
                    # edge that matters, which is the shadow's own.
                    color = (
                        _render_primitives.MOON_UMBRA_EMBER
                        if depth >= _render_primitives.MOON_UMBRA_EMBER_DEPTH
                        else _render_primitives.MOON_UMBRA_RIM
                    )
            px[x, y] = color
    # The halo answers to how much Moon is still in sunlight. A 93%-covered
    # disc lighting the sky as brightly as a full one is the same lie as
    # drawing the disc uncovered.
    lit = ill * (1.0 - eclipse.obscuration) if eclipse is not None else ill
    if lit > 0.15:
        _render_primitives._add_glow(
            px, cx, cy, _render_art.MOON_COLOR, 4.8, lit * (0.09 + 0.03 * breath)
        )


def _moon_age_days(on_date) -> float:
    """Convert Astral's 0..28 phase index to a synodic-month age."""
    return moon.phase(on_date) / 28.0 * 29.530588


_ECLIPSE_CACHE: tuple[int, object] | None = None


def _eclipse_now(now: datetime):
    """Earth's shadow on the Moon right now, or None — never fatal.

    Cached to the minute because one ambient loop renders 40 frames of the
    same instant, and because the whole point of local math is that it costs
    nothing. An exception here must lose the shadow, not the sky: this runs
    inside the frame renderer, and a night with no moon at all would be a
    far worse failure than a night with an unmarked eclipse.
    """
    global _ECLIPSE_CACHE
    minute = int(now.timestamp()) // 60
    if _ECLIPSE_CACHE is not None and _ECLIPSE_CACHE[0] == minute:
        return _ECLIPSE_CACHE[1]
    try:
        state = _eclipse.visible_state(now, _settings.OBSERVER)
    except Exception:  # noqa: BLE001 - ambient detail, never fatal
        state = None
    _ECLIPSE_CACHE = (minute, state)
    return state


MOONLIGHT = (168, 178, 196)  # the cool silver the moon paints with


def _apply_moonlight(
    px, mx: int, my: int, ill: float, cloud: float, phase: float
) -> None:
    """Strong moonlight on the scene: a silver pool on the ground sliding
    with the moon, a kiss on the grass tufts, and rim light along the
    house's top silhouette. Scales with the real lit fraction, dies under
    cloud, and breathes with the loop like everything else here."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return
    for x in range(_limits.W):
        mix = 0.85 * strength * math.exp(-(((x - mx) / 13.0) ** 2))
        if mix < 0.02:
            continue
        px[x, 15] = tuple(
            int(c) for c in _render_primitives._lerp_rgb(px[x, 15], MOONLIGHT, mix)
        )
        if px[x, 14] == _render_vegetation.GRASS_COLOR:
            px[x, 14] = tuple(
                int(c)
                for c in _render_primitives._lerp_rgb(
                    _render_vegetation.GRASS_COLOR, MOONLIGHT, 0.9 * mix
                )
            )
    for hx, hy in _render_art.HOUSE_TOP.items():
        d = math.hypot(hx - mx, hy - my)
        mix = 0.75 * strength * math.exp(-((d / 26.0) ** 2))
        if mix >= 0.02:
            px[hx, hy] = tuple(
                int(c) for c in _render_primitives._lerp_rgb(px[hx, hy], MOONLIGHT, mix)
            )


def _apply_moonlight_forest(
    px, mx: int, my: int, ill: float, cloud: float, phase: float, fire_on: bool
) -> None:
    """Backlight, not floodlight: the moon rides BEHIND the treeline
    here, so no pool reaches the floor in front of the silhouettes.
    What backlight does paint: rim light down the moon-side edges of
    the pines, a glow along the back-ridge crest where light bleeds
    through the woods, a kiss on the aspen crowns, and the faintest
    line on the tent's ridge."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return
    # Glow along the back-ridge crest, bleeding through under the moon
    for x in range(_limits.W):
        mix = 0.30 * strength * math.exp(-(((x - mx) / 11.0) ** 2))
        if mix < 0.02:
            continue
        crest = 11 + int(1.0 + 1.4 * math.sin(x * 0.55 + 1.3))
        if 0 <= crest < _limits.H:
            px[x, crest] = tuple(
                int(c)
                for c in _render_primitives._lerp_rgb(px[x, crest], MOONLIGHT, mix)
            )
    # Rim light down the moon-side edge of each pine's upper tiers
    for tx, top in _render_art.FOREST_PINES:
        side = -1 if mx < tx else 1  # each tree rims toward its own moon
        for i in range(0, 6):
            cy = top + i
            if cy >= 13:
                break
            edge_x = tx + side * min(2, i // 2)
            if not (0 <= edge_x < _limits.W):
                continue
            d = math.hypot(edge_x - mx, cy - my)
            mix = 0.60 * strength * math.exp(-((d / 20.0) ** 2))
            if mix >= 0.02:
                px[edge_x, cy] = tuple(
                    int(c)
                    for c in _render_primitives._lerp_rgb(
                        px[edge_x, cy], MOONLIGHT, mix
                    )
                )
    # Aspen crowns catch a kiss
    for ax in _render_art.FOREST_ASPENS:
        d = math.hypot(ax - mx, 9 - my)
        mix = 0.4 * strength * math.exp(-((d / 20.0) ** 2))
        if mix >= 0.02:
            px[ax, 9] = tuple(
                int(c) for c in _render_primitives._lerp_rgb(px[ax, 9], MOONLIGHT, mix)
            )
    # The tent is foreground: backlight only grazes its ridge line
    d = math.hypot(_render_art.TENT_APEX - mx, 10 - my)
    mix = 0.30 * strength * math.exp(-((d / 22.0) ** 2))
    if mix >= 0.02:
        px[_render_art.TENT_APEX, 10] = tuple(
            int(c)
            for c in _render_primitives._lerp_rgb(
                px[_render_art.TENT_APEX, 10], MOONLIGHT, mix
            )
        )


def _skyline_col_top(x0: int, x1: int, h: int, kind: int, x: int) -> int:
    """Highest row a building occupies at column x, honoring crowns/tapers."""
    top = _limits.H - 1 - h
    if kind == 0 or x0 + 2 <= x <= x1 - 2:
        return top
    if kind == 2 and x in (x0 + 1, x1 - 1):
        return top + 1
    return top + 2


def _apply_moonlight_skyline(
    px, mx: int, my: int, ill: float, cloud: float, phase: float
) -> None:
    """City moonlight: no pool on the streets — the moon silvers what faces
    it. Rooflines and setback ledges catch it with distance falloff, the
    back row gets a hazy version, and each tower's moon-side glass corner
    takes a faint sheen. Windows are interior pixels and stay untouched."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return

    def kiss(x, y, amt):
        if 0 <= x < _limits.W and 0 <= y < _limits.H and amt >= 0.02:
            px[x, y] = tuple(
                int(c) for c in _render_primitives._lerp_rgb(px[x, y], MOONLIGHT, amt)
            )

    front_cover: dict[int, int] = {}
    for x0, x1, h, kind in _render_art.SKYLINE_FRONT:
        for x in range(x0, x1 + 1):
            t = _skyline_col_top(x0, x1, h, kind, x)
            front_cover[x] = min(front_cover.get(x, _limits.H), t)

    # Back-row rooftops: fainter, hazier, and only where the front row
    # doesn't hide them
    for x0, x1, h in _render_art.SKYLINE_BACK:
        top = _limits.H - 1 - h
        for x in range(x0, x1 + 1):
            if front_cover.get(x, _limits.H) <= top:
                continue
            d = math.hypot(x - mx, top - my)
            kiss(x, top, 0.35 * strength * math.exp(-((d / 24.0) ** 2)))

    for x0, x1, h, kind in _render_art.SKYLINE_FRONT:
        top = _limits.H - 1 - h
        # Rooflines plus the ledges where crowns and tapers step back
        rows = [top] + (
            [top + 2] if kind == 1 else [top + 1, top + 2] if kind == 2 else []
        )
        for y in rows:
            span = _render_city._building_row_span(x0, x1, h, kind, y)
            if not span:
                continue
            above = (
                _render_city._building_row_span(x0, x1, h, kind, y - 1)
                if y > top
                else None
            )
            for x in range(span[0], span[1] + 1):
                if above and above[0] <= x <= above[1]:
                    continue  # a storey above covers this pixel: no sky here
                d = math.hypot(x - mx, y - my)
                kiss(x, y, 0.7 * strength * math.exp(-((d / 26.0) ** 2)))
        # The moon-side glass corner, a sheen running down the exposed edge
        if mx < x0 or mx > x1:
            for y in range(top, 14):
                span = _render_city._building_row_span(x0, x1, h, kind, y)
                if not span:
                    continue
                ex = span[0] if mx < x0 else span[1]
                d = math.hypot(ex - mx, y - my)
                kiss(ex, y, 0.4 * strength * math.exp(-((d / 20.0) ** 2)))

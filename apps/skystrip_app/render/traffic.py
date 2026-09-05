"""Skystrip render / traffic."""

from __future__ import annotations

import random

from PIL import Image

from apps.skystrip_app.render import primitives as _render_primitives

# --- traffic ---------------------------------------------------------------
#
# Cars are NOT part of the scene loop. Baked into an 8-second loop they
# repeated ~7.5 times a minute and, because the texture seed only turns over
# every ten minutes, the SAME two cars made the same trip about 75 times
# before anything changed — "it looks like it's looping", from the panel
# (2026-08-15). Traffic is now a one-shot overlay on the three rows it
# actually occupies: each episode gets its own entropy, its own vehicles,
# and its own speeds, so no two crossings are alike. A non-looping overlay
# also frees the vehicles from completing whole journeys per cycle, which is
# what lets them have individual speeds at all.
TRAFFIC_PAINTS = (
    (176, 46, 40),
    (208, 204, 198),
    (76, 106, 172),
    (218, 162, 46),
    (60, 130, 90),
)

TRAFFIC_BAND_TOP = 9  # scene rows 9-11: glass, body, wheels

TRAFFIC_BAND_ROWS = 3

TRAFFIC_FPS = 10


def traffic_density(hour: int) -> tuple[float, float]:
    """(mean seconds between episodes, mean vehicles per episode).

    A country road, by the clock: commuters bunch morning and evening,
    midday ambles, and the small hours are a lone pair of headlights.
    """
    if 6 <= hour < 9 or 15 <= hour < 18:
        return 26.0, 2.6  # rush
    if 9 <= hour < 15:
        return 48.0, 1.7  # midday
    if 18 <= hour < 23:
        return 65.0, 1.4  # evening
    return 150.0, 1.0  # the small hours


def _vehicle_kind(rng: random.Random, hour: int) -> str:
    roll = rng.random()
    if hour < 6 or hour >= 23:  # freight owns the night road
        return "semi" if roll < 0.45 else "sedan" if roll < 0.8 else "pickup"
    return (
        "sedan"
        if roll < 0.5
        else "pickup"
        if roll < 0.72
        else "semi"
        if roll < 0.88
        else "police"
    )


def _draw_vehicle(
    put,
    x_nose: int,
    row: int,
    kind: str,
    paint,
    trailer,
    lights_on: bool,
    amb: tuple,
    far: bool,
    blink: bool,
) -> None:
    """One vehicle, nose at `x_nose`, wheels on `row`.

    `put(x, y, colour)` clips for its own surface, so this draws the same
    whether the target is a full frame or the three-row traffic band.
    """
    if far:
        # The far lane runs leftbound and small: three pixels, dimmer, with
        # its lights on the opposite ends.
        body = _render_primitives._shade(
            _render_primitives._rgb_int(v * 0.6 for v in paint), amb
        )
        for dx in range(3):
            put(x_nose + dx, row - 1, body)
        if lights_on:
            put(x_nose - 1, row - 1, (235, 215, 150))
            put(x_nose + 3, row - 1, (150, 24, 16))
        return
    body = _render_primitives._shade(paint, amb)
    glass = _render_primitives._shade((70, 84, 104), amb)
    wheel = (16, 16, 18)
    if kind == "semi":
        trl = _render_primitives._shade(trailer, amb)
        for dx in range(2):
            put(x_nose - dx, row - 1, body)
        put(x_nose - 1, row - 2, body)
        put(x_nose, row - 2, glass)
        for dx in range(3, 11):
            put(x_nose - dx, row - 1, trl)
            put(x_nose - dx, row - 2, trl)
        for wdx in (0, 4, 9, 10):
            put(x_nose - wdx, row, wheel)
        if lights_on:
            put(x_nose + 1, row - 1, (255, 240, 170))
            put(x_nose + 2, row - 1, (170, 150, 95))
            put(x_nose - 11, row - 1, (200, 30, 18))
            put(x_nose - 11, row - 2, (200, 30, 18))
        return
    for dx in range(4):
        put(x_nose - dx, row - 1, body)
    put(x_nose - 1, row - 2, glass)
    if kind == "pickup":
        put(x_nose - 3, row - 1, tuple(int(v * 0.6) for v in body))
    else:
        put(x_nose - 2, row - 2, body)
    if kind == "police":
        put(x_nose - 2, row - 2, (255, 40, 40) if blink else (60, 90, 255))
    put(x_nose, row, wheel)
    put(x_nose - 3, row, wheel)
    if lights_on:
        put(x_nose + 1, row - 1, (255, 240, 170))
        put(x_nose + 2, row - 1, (170, 150, 95))
        put(x_nose - 4, row - 1, (200, 30, 18))


def plan_traffic(
    rng: random.Random, hour: int, lights_on: bool, n_vehicles: int
) -> list[dict]:
    """Lay out one episode's vehicles: when each enters, how fast, which way.

    Arrivals are spread by random gaps rather than an even cadence, and each
    vehicle carries its own speed, so nothing about an episode is periodic.
    """
    plan: list[dict] = []
    entry = rng.uniform(4.0, 14.0)
    for _ in range(max(1, n_vehicles)):
        far = rng.random() < 0.45
        kind = "sedan" if far else _vehicle_kind(rng, hour)
        speed = rng.uniform(0.9, 1.5)
        if kind == "semi":
            speed *= 0.8  # loaded, and slower for it
        paint = TRAFFIC_PAINTS[rng.randrange(len(TRAFFIC_PAINTS))]
        if kind == "police":
            paint = (225, 225, 230)
        trailer = (222, 222, 218) if rng.random() < 0.6 else (176, 120, 60)
        if lights_on:
            paint = _render_primitives._rgb_int(v * 0.22 for v in paint)
            trailer = _render_primitives._rgb_int(v * 0.28 for v in trailer)
        plan.append(
            {
                "far": far,
                "kind": kind,
                "speed": speed,
                "paint": paint,
                "trailer": trailer,
                "entry_s": entry,
            }
        )
        # A believable gap: usually a real pause, occasionally a tailgater.
        # Kept short enough that an episode stays an event — a 45-second
        # overlay would be on screen almost continuously, which is the
        # looping problem wearing a different hat.
        entry += rng.uniform(1.0, 2.5) if rng.random() < 0.3 else rng.uniform(3.0, 7.5)
    return plan


def traffic_episode_frames(
    band: Image.Image,
    plan: list[dict],
    lights_on: bool,
    amb: tuple,
    foreground=frozenset(),
) -> list[Image.Image]:
    """Render one traffic episode across the three-row band.

    Nothing here loops, so a vehicle only has to enter off one edge and
    leave off the other; it never has to arrive back where it started.
    """
    row = TRAFFIC_BAND_ROWS - 1  # wheels sit on the band's last row
    bw = band.width
    lead = 16  # longest sprite plus its lights
    span = bw + 2 * lead
    last_exit = max(
        (v["entry_s"] + span / (v["speed"] * TRAFFIC_FPS) for v in plan), default=1.0
    )
    n = int((last_exit + 1.5) * TRAFFIC_FPS)
    n = max(TRAFFIC_FPS, min(n, 400))  # 40s ceiling per episode
    base_px = _render_primitives._rgb_pixels(band)
    frames: list[Image.Image] = []
    for f in range(n):
        im = band.copy()
        pxb = _render_primitives._rgb_pixels(im)

        def put(x, y, c, _p=pxb):
            if 0 <= x < bw and 0 <= y < TRAFFIC_BAND_ROWS:
                _p[x, y] = c

        for v in plan:
            travelled = (f - v["entry_s"] * TRAFFIC_FPS) * v["speed"]
            if travelled < 0:
                continue
            if v["far"]:  # leftbound
                x_nose = int(round(bw + lead - travelled))
            else:  # rightbound
                x_nose = int(round(-lead + travelled))
            if x_nose < -lead or x_nose > bw + lead:
                continue
            _draw_vehicle(
                put,
                x_nose,
                row,
                v["kind"],
                v["paint"],
                v["trailer"],
                lights_on,
                amb,
                v["far"],
                blink=(f // 2) % 2 == 0,
            )
        # Trunks stand in front of the road: repaint them over the traffic
        # so a car passes behind a poplar instead of through it.
        for fx, fy in foreground:
            if 0 <= fx < bw and 0 <= fy < TRAFFIC_BAND_ROWS:
                pxb[fx, fy] = base_px[fx, fy]
        frames.append(im)
    return frames

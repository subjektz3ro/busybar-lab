"""DSN render / timing."""

from __future__ import annotations

import math

from apps.dsn_app import limits as _limits
from apps.dsn_app.render import palette as _render_palette

TIME_COMPRESSION = 600.0  # one second on the strip = ten minutes of real flight

CROSS_KNEE_S = 1200.0  # 20 min of light time: above this the law is linear

CROSS_KNEE_CROSS = CROSS_KNEE_S / TIME_COMPRESSION  # ...and 2.0s there

CROSS_MIN_S = 0.7  # the Moon. Three frames at 5 fps, still motion

CROSS_MAX_S = 180.0


def crossing_seconds(light_s: float | None) -> float:
    """How long ONE message takes to cross the strip.

    Above the knee this is exactly 1/600 of the estimated light time, so the
    range ratios survive: Jupiter is roughly twice Mars, Voyager roughly twenty-three
    times Jupiter.

    Below it the law has to bend, and pretending otherwise was the old bug.
    A flat 2-second floor made the Moon, the Lagrange points and Mars at
    closest approach render IDENTICALLY - 1.3 seconds of light time and 190
    seconds of it drawn at the same speed - while the docstring claimed true
    ratios. The real span is 55,000:1 and the strip has about 200:1, so no
    linear map can hold it.

    So: linear above 20 minutes of light time, and a log squeeze below, which
    keeps the Moon visibly quicker than L1, and L1 quicker than Mars, instead
    of collapsing all three onto the floor. The number on the panel is always
    the published or ephemeris-estimated light time either way.
    """
    # A missing range has no defensible crossing speed. Return one neutral loop
    # only as a planning bound; render_frames() holds the active carrier still
    # instead of turning this placeholder into apparent distance or velocity.
    if not light_s:
        return _limits.LOOP_S
    if light_s >= CROSS_KNEE_S:
        return min(CROSS_MAX_S, light_s / TIME_COMPRESSION)
    # continuous at the knee: both branches give CROSS_KNEE_CROSS there
    frac = math.log1p(light_s) / math.log1p(CROSS_KNEE_S)
    return CROSS_MIN_S + (CROSS_KNEE_CROSS - CROSS_MIN_S) * frac


DOWN_Y = _render_palette.TRACK_Y  # the spacecraft's half, on the original row


def realtime_progress(light_s: float, ts: float, since: float) -> float:
    """How far a light-speed traversal has got: 0 at the craft, 1 at Earth.

    A chain of evenly spaced marks was the first attempt and it failed as a
    picture. Twelve identical dots strung across the strip say "signal exists"
    but never say how far along anything is -- and at these distances they do
    not move either, so there is nothing to read. One bright head and the
    ground already covered behind it answer the question actually being asked:
    how far does light get across this distance after this much real time.

    Measured from the moment you locked on, so the head starts AT the craft
    and you watch it go. It used to be anchored to absolute time, which meant
    locking on dropped you into the middle of a crossing already in progress
    with no way to tell why -- and worse, the clock label was reporting a
    different time reference entirely. The head and countdown now share one
    lock instant and one arrival deadline.

    This is an honest distance/time ruler, not decoded telemetry. A DSN
    downSignal says a carrier is arriving at Earth now; it cannot prove that a
    particular bit left the spacecraft at the moment the user clicked.
    """
    if not light_s or light_s <= 0:
        return 0.0
    return min(1.0, max(0.0, (ts - since) / light_s))


def _mark(px, x: float, y: int, color: tuple[int, int, int]) -> None:
    """Draw a mark at a FRACTIONAL x by splitting it across two LEDs.

    The panel has no spatial subpixels -- one pixel is one RGB package on a
    2.2 mm pitch -- but apparent size tracks brightness, so splitting intensity
    between the two LEDs a mark straddles reads as a position between them.
    Gamma crushes fine steps (deltas under ~30% are invisible on the panel),
    so this quantises to about three usable positions per pixel rather than a
    smooth ramp. That is what makes a 27-minute-per-pixel creep perceptible.
    """
    base = int(math.floor(x))
    frac = x - base
    for xi, weight in ((base, 1.0 - frac), (base + 1, frac)):
        if not (_limits.TRACK0 <= xi <= _limits.TRACK1) or weight < 0.25:
            continue
        step = 1.0 if weight > 0.8 else (0.65 if weight > 0.5 else 0.4)
        lit = tuple(int(c * step) for c in color)
        px[xi, y] = tuple(max(a, b) for a, b in zip(lit, px[xi, y]))


def _scale_rgb(colour: tuple[int, int, int], scale: float) -> tuple[int, int, int]:
    """Scale an RGB triple without widening its static type to tuple[int, ...]."""
    return (
        int(colour[0] * scale),
        int(colour[1] * scale),
        int(colour[2] * scale),
    )


def countdown_label(light_s: float | None, since: float, now_ts: float) -> str:
    """Time left in the real-time light-crossing watch.

    This replaced a clock showing when the signal DEPARTED, which answered a
    question nobody asked and needed a paragraph to explain. A countdown needs
    none: it and the travelling pulse tell the same story, and the number is
    the point of the whole app -- how long it takes to cross that distance.

    It runs at real speed and it really does finish. Voyager's is 19 hours 48
    minutes, which completes tomorrow morning on a device that sits on a desk
    all day; completing the watch is a real elapsed-time event rather than a
    compressed imitation of one.
    """
    if not light_s:
        return "?"
    left = max(0.0, light_s - (now_ts - since))
    if left >= 3600:
        hours, rest = divmod(int(left), 3600)
        return f"{hours}:{rest // 60:02d}"
    minutes, secs = divmod(int(left), 60)
    return f"{minutes}:{secs:02d}"


def arrived(light_s: float | None, since: float, now_ts: float) -> bool:
    """Has one full light time elapsed? That ends the watch."""
    if not light_s:
        return False
    return (now_ts - since) >= light_s


def realtime_redraw_s(light_s: float | None) -> float:
    """How often a locked scene must be re-pushed for the chain to creep.

    Roughly the time one mark takes to cross a single pixel — pushing faster
    just re-uploads an identical frame. Bounded so a Mars pass updates often
    enough to watch and Voyager doesn't spend the day uploading.
    """
    if not light_s or light_s <= _limits.RT_SEAMLESS_MAX_S:
        return float(_limits.REDRAW_S)  # the device is animating it itself
    return max(10.0, min(300.0, light_s / (_limits.TRACK1 - _limits.TRACK0)))

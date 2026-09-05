"""DSN render / carriers."""

from __future__ import annotations

import math

from apps.dsn_app import limits as _limits
from apps.dsn_app.render import timing as _render_timing


def _link_row(
    px,
    y: int,
    track0: int,
    track1: int,
    phase: float,
    spacing: float,
    outward: bool,
    span: int,
    colour: tuple[int, int, int],
    tether: tuple[int, int, int],
) -> list[float]:
    """One direction of the conversation, on its own row.

    Returns the pulse positions so the caller can tell when one has landed.
    `outward` sends the signal from Earth to the craft; the wake always
    trails behind whichever way it is going.
    """
    for x in range(track0, track1 + 1):
        px[x, y] = tether
    positions = []
    pos = track1 - phase * spacing
    while pos >= track0 - spacing:
        positions.append(pos)
        pos -= spacing
    if outward:
        positions = [track0 + track1 - p for p in positions]
    behind = -1 if outward else 1
    for pos in positions:
        base = int(round(pos))
        for step in range(span):
            xs = base + step * behind
            if track0 <= xs <= track1:
                px[xs, y] = colour
        if spacing > 6:  # room for a tail
            for fade, dx in ((0.5, 1), (0.22, 2)):
                xs = base + (span - 1 + dx) * behind
                if track0 <= xs <= track1:
                    px[xs, y] = tuple(
                        max(a, int(c * fade)) for a, c in zip(tether, colour)
                    )
    return positions


def _static_link_row(
    px,
    y: int,
    track0: int,
    track1: int,
    *,
    span: int,
    colour: tuple[int, int, int],
    tether: tuple[int, int, int],
) -> None:
    """An active direction whose range is unknown: presence, never velocity.

    A centred, stationary mark carries direction and (for receive) the rate
    bucket without assigning it an invented crossing time, spacing or endpoint.
    """
    for x in range(track0, track1 + 1):
        px[x, y] = tether
    centre = (track0 + track1) // 2
    first = centre - (span - 1) // 2
    for x in range(first, first + span):
        if track0 <= x <= track1:
            px[x, y] = colour


def pulse_span(bps: float | None) -> int:
    """How long a pulse is drawn, from the data rate.

    The chain already carries DISTANCE in its spacing. This puts the other
    half of the link on screen: a 2 Mbit downlink from Mars arrives as a fat
    dash, Voyager's 160 bits per second as a bare flicker. Three steps, not a
    ramp — the panel's gamma erases anything finer.
    """
    if bps is not None and math.isfinite(bps) and bps >= 1e6:
        return 3
    if bps is not None and math.isfinite(bps) and bps >= 1e3:
        return 2
    return 1


def packet_spacing(cross_s: float, track_px: int) -> float:
    """Pixels between packets in the chain, for a FIXED-duration loop.

    This is what decouples everything. The loop used to be as long as the
    crossing, so text scrolled at whatever speed the spacecraft's distance
    dictated — a name crawling for two minutes on Voyager. Now the loop is
    always LOOP_S and distance shows up as SPACING instead: each packet
    still takes the represented crossing time to cross (speed = spacing / LOOP_S =
    track / cross_s), but a distant craft simply has more of them in flight.

    That count is physically real — it is how many LOOP_S-long chunks of
    signal are in transit at once. Voyager carries about fifteen; Mars has
    one packet and a pause.
    """
    return max(2.0, track_px * _limits.LOOP_S / max(cross_s, 0.1))


DISTANCE_SPACING_QUANTA = 16

DISTANCE_PROGRESS_QUANTA = 3


def represented_packet_spacing(light_s: float | None, live: bool) -> float | None:
    """Canonical spacing shared by the Distance cache key and renderer."""
    if not light_s:
        return None
    crossing = light_s if live else _render_timing.crossing_seconds(light_s)
    raw = packet_spacing(crossing, _limits.TRACK1 - _limits.TRACK0)
    return round(raw * DISTANCE_SPACING_QUANTA) / DISTANCE_SPACING_QUANTA


def represented_progress(
    light_s: float,
    now_ts: float,
    since: float,
) -> tuple[int, float]:
    """Physical-panel progress bucket and its exact canonical fraction."""
    width = _limits.TRACK1 - _limits.TRACK0
    units = int(
        round(
            _render_timing.realtime_progress(light_s, now_ts, since)
            * width
            * DISTANCE_PROGRESS_QUANTA
        )
    )
    return units, units / (width * DISTANCE_PROGRESS_QUANTA)

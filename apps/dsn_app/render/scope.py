"""DSN render / scope."""

from __future__ import annotations

import math

from apps.dsn_app import limits as _limits
from apps.dsn_app import source as _source
from apps.dsn_app.render import palette as _render_palette


def pointing_pixel(azimuth: float, elevation: float) -> tuple[int, int]:
    """Project DSN az/el into the 15x15 polar scope.

    North is up, east is right, zenith is the centre and the horizon is the
    ring.  This is actual antenna geometry, not a decorative orbit.
    """
    az = math.radians(azimuth % 360.0)
    radius = (90.0 - min(90.0, max(0.0, elevation))) / 90.0 * _render_palette.SCOPE_R
    return (
        int(round(_render_palette.SCOPE_CX + math.sin(az) * radius)),
        int(round(_render_palette.SCOPE_CY - math.cos(az) * radius)),
    )


def link_pointing_pixel(link: _source.Link) -> tuple[int, int] | None:
    return pointing_pixel(link.azimuth, link.elevation) if link.pointing_valid else None


def _ring_points() -> set[tuple[int, int]]:
    points = set()
    for degrees in range(0, 360, 15):
        angle = math.radians(degrees)
        points.add(
            (
                int(
                    round(
                        _render_palette.SCOPE_CX
                        + math.sin(angle) * _render_palette.SCOPE_R
                    )
                ),
                int(
                    round(
                        _render_palette.SCOPE_CY
                        - math.cos(angle) * _render_palette.SCOPE_R
                    )
                ),
            )
        )
    return points


SCOPE_POINTS = _ring_points()


def _scope_points(cx: int, cy: int, radius: int) -> set[tuple[int, int]]:
    """Sparse physical-panel-safe ring points for one local sky."""
    step = 45 if radius < 5 else 15
    points = set()
    for degrees in range(0, 360, step):
        angle = math.radians(degrees)
        points.add(
            (
                int(round(cx + math.sin(angle) * radius)),
                int(round(cy - math.cos(angle) * radius)),
            )
        )
    return points


def _draw_scope(px, cx: int, cy: int, radius: int) -> None:
    for point in _scope_points(cx, cy, radius):
        px[point] = _render_palette.SCOPE_RING
    px[cx, cy] = _render_palette.SCOPE_RING
    # One neutral cardinal fiducial makes north explicit without a fake moving
    # sweep or another RF-coloured point.
    px[cx, cy - radius] = _render_palette.THREE_SKIES_NORTH


def _project_angles(
    azimuth: float, elevation: float, cx: int, cy: int, radius: int
) -> tuple[int, int]:
    """Literal local alt-az projection at an arbitrary scope radius."""
    az = math.radians(azimuth % 360.0)
    distance = (90.0 - min(90.0, max(0.0, elevation))) / 90.0 * radius
    return (
        int(round(cx + math.sin(az) * distance)),
        int(round(cy - math.cos(az) * distance)),
    )


def _draw_freshness_frame(px, freshness: str, index: int) -> None:
    # Fresh is a separate native element lease; baking it would let a dead
    # host continue looking live.  Delayed/stale are last-known-source states.
    if freshness == "delayed" and (index // 5) % 2 == 0:
        for y in (0, _limits.H // 2, _limits.H - 1):
            px[_render_palette.FRESH_X, y] = _render_palette.DELAYED
    elif freshness in {"stale", "offline"}:
        for y in (0, _limits.H // 2, _limits.H - 1):
            px[_render_palette.FRESH_X, y] = _render_palette.STALE

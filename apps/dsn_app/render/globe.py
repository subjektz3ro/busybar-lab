"""DSN render / globe."""

from __future__ import annotations

import math
from datetime import datetime

from apps.dsn_app import limits as _limits
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import text as _render_text


def terrain(lat: float, lon: float, is_land: bool, coastal: bool):
    """Four surfaces instead of two.

    A globe of one green and one blue is legible but flat, and at 13 pixels
    across the ice caps are what make it read as EARTH rather than as a
    green-and-blue ball: Antarctica along the bottom and Greenland at the top
    are the two shapes anyone recognises instantly. Deserts break up the green
    across Africa and Australia. All four are far enough apart to survive the
    panel's gamma, which erases anything under about a 30% step.
    """
    # +/-55 rather than the true polar circles: at 13 pixels across, the
    # projection compresses latitude hard (the row below the pole is already
    # only 56 degrees), so stricter thresholds put ice on exactly one pixel
    # and the caps vanish. These are the top and bottom rows of the disc, and
    # showing them as ice is what makes the ball read as EARTH.
    # The caps are cut at 55 because that is the only threshold that renders
    # ANY ice. The -6 in the disc's radius test drops the cardinal pixels, so
    # the outermost drawn row is dy = +/-5 and asin(5/6) = 56.44 degrees is
    # all the latitude this projection ever reaches - eleven rows, and the
    # top and bottom ones ARE the polar regions as far as this globe is
    # concerned. (A Greenland-specific rule used to sit further down at
    # lat > 58. It could never fire at any ordering, and is gone.)
    if lat > 55.0 or lat < -55.0:
        return _render_palette.ICE
    if not is_land:
        return _render_palette.SHELF if coastal else _render_palette.OCEAN
    # The Sahara stops at the Sahel. Running it to 30S swallowed the Congo
    # basin, which is the wettest place on the continent.
    if 12.0 < lat < 33.0 and -18.0 < lon < 52.0:  # Sahara + Arabia
        return _render_palette.DESERT
    if -30.0 < lat < -18.0 and 12.0 < lon < 25.0:  # Kalahari + Namib
        return _render_palette.DESERT
    if -32.0 < lat < -18.0 and 118.0 < lon < 142.0:  # Australian interior
        return _render_palette.DESERT
    # ...and the Gobi is not the Ganges plain or the North China Plain.
    if 30.0 < lat < 48.0 and 55.0 < lon < 108.0:  # Iranian plateau, Gobi
        return _render_palette.DESERT
    return _render_palette.LAND


# The world at 10-degree cells: 36 longitudes x 18 latitudes, row 0 = 85N,
# column 0 = 180W. Coarse, but sampled through a real spherical projection
# it turns like Earth — Africa and Eurasia swing past, then a wide empty
# Pacific, then the Americas. A longitude-only mask (which this replaced)
# renders as vertical stripes, which is not a planet.
WORLD = (
    "....................................",  # 85N
    "...####......###.....##########.....",  # 75N  N.Canada, Greenland, Siberia
    "..#######....##...############......",  # 65N
    "..#######.........###########.......",  # 55N
    "...######.......############........",  # 45N
    "....####........###########.........",  # 35N
    ".....###.......############.........",  # 25N  Mexico, Sahara, Arabia, India
    "......##......##########...###......",  # 15N
    ".........#....########....####......",  # 5N
    ".........##...#######.....###.......",  # 5S
    ".........###..######.......####.....",  # 15S  Brazil, Africa, Australia
    ".........###..#####........#####....",  # 25S
    "..........##...###.........####.....",  # 35S
    "..........#.................##......",  # 45S  Argentina tip, New Zealand
    "....................................",  # 55S
    "....................................",  # 65S
    "####################################",  # 75S  Antarctica
    "####################################",  # 85S
)

NIGHT_DIM = 0.28  # a 72% drop: gamma erases anything subtler


def subsolar(when: datetime) -> tuple[float, float]:
    """Where the Sun is directly overhead: (latitude, longitude) in degrees.

    Low-precision solar position, good to about 0.01 deg, which is far past
    what 13 pixels of Earth can show. It is here because the LATITUDE is not
    optional: the terminator only tilts with the season if you know the
    Sun's declination, and without it the globe sits at a permanent equinox
    with no midnight sun and no polar night.

    The equation of time comes out of the same arithmetic - it is just the
    mean longitude minus the apparent right ascension - so the longitude is
    the true subsolar meridian rather than the mean-time one. That is worth
    perhaps four degrees, under half a pixel at the centre of the disc and
    less at the limbs, so it is invisible. It is in anyway, because a real
    declination paired with a mean-time hour angle is a hybrid nobody can
    reason about, and the correction costs two lines once the rest exists.
    """
    n = when.timestamp() / 86400.0 - 10957.5  # days since J2000.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic = math.radians(
        mean_lon + 1.915 * math.sin(anomaly) + 0.020 * math.sin(2 * anomaly)
    )
    obliquity = math.radians(23.439 - 4e-7 * n)
    decl = math.degrees(math.asin(math.sin(obliquity) * math.sin(ecliptic)))
    ra = math.degrees(
        math.atan2(math.cos(obliquity) * math.sin(ecliptic), math.cos(ecliptic))
    )
    eot = ((mean_lon - ra + 180.0) % 360.0) - 180.0  # degrees
    hours = (when.timestamp() % 86400.0) / 3600.0
    lon = ((180.0 - 15.0 * hours - eot) + 180.0) % 360.0 - 180.0
    return decl, lon


def _globe(px, spin: float, sun_lat: float, sun_lon: float) -> None:
    """Earth, lit where the sun is.

    Each pixel of the disc is projected back to a latitude and longitude, so
    continents foreshorten toward the limbs the way a sphere's do.

    Day and night are decided per LONGITUDE — `away` is measured from the same
    `lon` the land is looked up with — so the shading is a property of the
    geography and travels with it as the globe turns. A version that pinned
    the terminator to screen space instead was wrong twice over: the shadow
    sat there while the planet moved under it, and at the hours when the
    centred complex was in full day or full night the disc showed no
    terminator at all for the whole loop.
    """
    for dy in range(-_render_palette.GLOBE_R, _render_palette.GLOBE_R + 1):
        for dx in range(-_render_palette.GLOBE_R, _render_palette.GLOBE_R + 1):
            # -6 rather than a bare R^2: the plain test leaves a single
            # protruding pixel at each cardinal point and the disc reads as
            # a diamond rather than a sphere.
            if (
                dx * dx + dy * dy
                > _render_palette.GLOBE_R * _render_palette.GLOBE_R - 6
            ):
                continue
            x, y = _render_palette.GLOBE_CX + dx, _render_palette.GLOBE_CY + dy
            if not (0 <= x < _limits.W and 0 <= y < _limits.H):
                continue
            lat = math.degrees(
                math.asin(max(-1.0, min(1.0, -dy / _render_palette.GLOBE_R)))
            )
            cos_lat = math.cos(math.radians(lat))
            if cos_lat < 0.08:  # a pole: no useful longitude
                lon_off = 0.0
            else:
                s = max(-1.0, min(1.0, dx / (_render_palette.GLOBE_R * cos_lat)))
                lon_off = math.degrees(math.asin(s))
            lon = (spin * 360.0 + lon_off + 180.0) % 360.0 - 180.0

            row = min(len(WORLD) - 1, max(0, int((90.0 - lat) / 10.0)))
            col = int((lon + 180.0) / 10.0) % 36
            is_land = WORLD[row][col] == "#"
            # Shallow water reads as a coast: check whether any neighbouring
            # cell is land. It costs nothing and gives the oceans some depth
            # instead of one flat blue.
            coastal = not is_land and any(
                WORLD[min(len(WORLD) - 1, max(0, row + dr))][(col + dc) % 36] == "#"
                for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0))
            )
            color = terrain(lat, lon, is_land, coastal)

            # Day or night: the true angular distance from the subsolar
            # POINT, not from its meridian. Longitude alone put the
            # terminator on a great circle through both poles all year, so
            # the globe sat at a permanent equinox - no midnight sun over
            # the Arctic in July, no dark Antarctic winter, which is the
            # most recognisable thing about how Earth is lit.
            phi, dec = math.radians(lat), math.radians(sun_lat)
            away = math.degrees(
                math.acos(
                    max(
                        -1.0,
                        min(
                            1.0,
                            math.sin(phi) * math.sin(dec)
                            + math.cos(phi)
                            * math.cos(dec)
                            * math.cos(math.radians(lon - sun_lon)),
                        ),
                    )
                )
            )
            if away > 95.0:
                color = tuple(int(c * NIGHT_DIM) for c in color)
            elif away > 80.0:  # the terminator itself
                color = tuple(int(c * 0.6) for c in color)
            px[x, y] = color


def _unknown_globe(px) -> None:
    """An Earth-shaped unknown, without invented site-centred daylight."""
    for dy in range(-_render_palette.GLOBE_R, _render_palette.GLOBE_R + 1):
        for dx in range(-_render_palette.GLOBE_R, _render_palette.GLOBE_R + 1):
            radius2 = dx * dx + dy * dy
            if (
                _render_palette.GLOBE_R * _render_palette.GLOBE_R - 15
                <= radius2
                <= _render_palette.GLOBE_R * _render_palette.GLOBE_R
            ):
                x, y = _render_palette.GLOBE_CX + dx, _render_palette.GLOBE_CY + dy
                if 0 <= x < _limits.W and 0 <= y < _limits.H:
                    px[x, y] = _render_palette.SCOPE_RING
    _render_text._text(
        px,
        _render_palette.GLOBE_CX - 2,
        _render_palette.GLOBE_CY - 2,
        "?",
        _render_palette.DISH_NO,
        clip=(_render_palette.GLOBE_CX - 2, _render_palette.GLOBE_CX + 2),
    )

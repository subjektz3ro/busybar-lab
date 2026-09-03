"""Radar-truth precipitation: slippy-tile math, RainViewer decode, and the
rain source resolver (radar > Open-Meteo nowcast > fresh NWS station).

Pure functions only — skystrip owns the polling; these are unit-testable
without a network.

PROBE EVIDENCE (2026-08-05, live RainViewer frames): the free tile cache serves
radar tiles only up to ZOOM 7. Higher zooms return a "Zoom Level Not Supported"
image whose anti-aliased text looks like radar unless the size/zoom contract is
enforced. Tiles use RainViewer's Universal Blue palette. The decoder anchors
below come directly from RainViewer's published CSV, sampled at 5-dBZ steps:
https://www.rainviewer.com/files/rainviewer_api_colors_table.csv

Low-alpha tan shades are the palette's own sub-15 dBZ mist colors and remain
below RAIN_DBZ. Interpolated shades from server-side smoothing land on a nearby
anchor within the distance cap.
"""

from __future__ import annotations

import io
import math

from PIL import Image, UnidentifiedImageError

RADAR_MAX_ZOOM = 7      # free tile cache ceiling (verified live 2026-08-05)
RADAR_FRESH_S = 900.0   # radar frame younger than this speaks for the sky
RADAR_FUTURE_SKEW_S = 300.0  # tolerate small provider/host clock disagreement
OM_FRESH_S = 1800.0     # Open-Meteo current younger than this is trustworthy
STATION_FRESH_S = 2 * 3600.0  # NWS latest observation / overall weather lease
MODEL_FALLBACK_S = 2 * 3600.0  # bounded cold-start evidence, never a nowcast
RAIN_DBZ = 20.0         # below this: mist/virga/clutter, not rain on the ground
RAINVIEWER_TILE_SIZE = 256
WEB_MERCATOR_MAX_LAT = 85.0511287798066
_MAX_SLIPPY_ZOOM = 30
_MAX_TILE_SIZE = 4096
_COVERAGE_MASK_MAX_BYTES = 1 << 20
_RADAR_TILE_MAX_BYTES = 1 << 20
_COVERAGE_MASK_SIZES = frozenset((256, 512))

# RainViewer Universal Blue rain palette: (dBZ, r, g, b), sampled from the
# vendor's published 1-dBZ CSV at 5-dBZ steps. Colors beyond ~35 RGB distance
# from every entry are treated as not-radar artifacts.
_UB_PALETTE = (
    (-10, 99, 97, 89), (-5, 114, 110, 97), (0, 130, 123, 105),
    (5, 146, 136, 113), (10, 206, 192, 135), (15, 136, 221, 238),
    (20, 0, 163, 224), (25, 0, 119, 170), (30, 0, 85, 136),
    (35, 255, 238, 0), (40, 255, 170, 0), (45, 255, 68, 0),
    (50, 193, 0, 0), (55, 255, 170, 255), (60, 255, 119, 255),
    (65, 255, 255, 255), (75, 0, 255, 0),
)
_MAX_DIST_SQ = 35 * 35


def tile_pixel(lat: float, lon: float, zoom: int,
               tile_size: int = 256) -> tuple[int, int, int, int]:
    """Return one bounded slippy-map tile and in-tile pixel.

    Web Mercator cannot represent the poles.  Clamp latitude to its exact
    finite limit and wrap longitude into ``[-180, 180)`` so the otherwise
    valid configuration edges (90 degrees and +180 degrees) never produce a
    negative/out-of-range RainViewer URL.  Callers that need to distinguish a
    clamped polar coordinate from a genuinely representable one should use
    :func:`web_mercator_contains` before treating a tile as local evidence.
    """
    if (isinstance(zoom, bool) or not isinstance(zoom, int)
            or not 0 <= zoom <= _MAX_SLIPPY_ZOOM):
        raise ValueError(f"zoom must be an integer from 0 to {_MAX_SLIPPY_ZOOM}")
    if (isinstance(tile_size, bool) or not isinstance(tile_size, int)
            or not 1 <= tile_size <= _MAX_TILE_SIZE):
        raise ValueError(
            f"tile_size must be an integer from 1 to {_MAX_TILE_SIZE}")
    try:
        latitude = float(lat)
        longitude = float(lon)
    except (TypeError, ValueError) as exc:
        raise ValueError("latitude and longitude must be finite numbers") from exc
    if not math.isfinite(latitude) or not math.isfinite(longitude):
        raise ValueError("latitude and longitude must be finite numbers")

    latitude = min(max(latitude, -WEB_MERCATOR_MAX_LAT), WEB_MERCATOR_MAX_LAT)
    longitude = (longitude + 180.0) % 360.0 - 180.0
    n = 1 << zoom
    xf = (longitude + 180.0) / 360.0 * n
    yf = (
        1.0 - math.asinh(math.tan(math.radians(latitude))) / math.pi
    ) / 2.0 * n

    # Floating point at the south Mercator limit can round to exactly ``n``.
    # Keep the world coordinate inside the final tile instead of emitting an
    # invalid tile index one beyond the pyramid.
    world_max = math.nextafter(float(n), -math.inf)
    xf = min(max(xf, 0.0), world_max)
    yf = min(max(yf, 0.0), world_max)
    tx, ty = math.floor(xf), math.floor(yf)
    px = min(tile_size - 1, math.floor((xf - tx) * tile_size))
    py = min(tile_size - 1, math.floor((yf - ty) * tile_size))
    return tx, ty, px, py


def web_mercator_contains(lat: float) -> bool:
    """Whether a latitude is honestly represented by a slippy-map tile."""
    try:
        latitude = float(lat)
    except (TypeError, ValueError):
        return False
    return math.isfinite(latitude) and abs(latitude) <= WEB_MERCATOR_MAX_LAT


def rainviewer_frame_age(timestamp: object, *, now_unix: float) -> float:
    """Validate a RainViewer frame timestamp and return its source age.

    RainViewer documents ``frame["time"]`` as Unix seconds.  A cached index
    can still be served successfully after that frame has aged out, so HTTP
    receipt time is not evidence freshness.  Only a finite non-negative number
    within the same strict freshness window used by :func:`resolve_rain` is
    accepted.  Small future skew is tolerated and maps to age zero; a larger
    skew is not plausible evidence.
    """
    if (isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))):
        raise ValueError("RainViewer frame time must be a Unix timestamp")
    frame_time = float(timestamp)
    try:
        wall_now = float(now_unix)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            "current Unix time must be finite and non-negative") from exc
    if (not math.isfinite(frame_time) or frame_time < 0
            or not math.isfinite(wall_now) or wall_now < 0):
        raise ValueError("RainViewer frame time must be finite and non-negative")

    age = wall_now - frame_time
    if age < -RADAR_FUTURE_SKEW_S:
        raise ValueError(
            "RainViewer frame time is implausibly far in the future")
    # resolve_rain uses a strict `< RADAR_FRESH_S` comparison. Reject the exact
    # boundary too: accepting a frame that cannot win resolution would let its
    # successful HTTP receipt disguise a stale cache.
    if age >= RADAR_FRESH_S:
        raise ValueError("RainViewer frame is stale")
    return max(0.0, age)


def decode_coverage_mask(
    png: bytes | bytearray | memoryview,
    px: int,
    py: int,
    *,
    tile_size: int = RAINVIEWER_TILE_SIZE,
) -> bool:
    """Return whether RainViewer says radar covers one tile pixel.

    RainViewer's official mask is transparent where radar evidence exists and
    black where it does not.  Only a *fully* transparent target pixel grants
    radar authority; an anti-aliased edge or unexpected opaque error image
    safely falls through to the model/station chain.

    The remote payload is capped before decode, must be a PNG of the exact
    requested size, and only one pixel is inspected.  Those bounds make this a
    deterministic parser rather than an invitation for a remote image to drive
    unbounded allocation or work.
    """
    if tile_size not in _COVERAGE_MASK_SIZES:
        raise ValueError("coverage tile_size must be 256 or 512")
    if (isinstance(px, bool) or not isinstance(px, int)
            or isinstance(py, bool) or not isinstance(py, int)
            or not 0 <= px < tile_size or not 0 <= py < tile_size):
        raise ValueError("coverage sample must lie inside the requested tile")
    if not isinstance(png, (bytes, bytearray, memoryview)):
        raise ValueError("coverage mask must be a PNG byte buffer")
    if not 0 < len(png) <= _COVERAGE_MASK_MAX_BYTES:
        raise ValueError("coverage mask payload is empty or exceeds 1 MiB")

    try:
        with Image.open(io.BytesIO(bytes(png))) as mask:
            if mask.format != "PNG":
                raise ValueError("coverage mask is not a PNG")
            if mask.size != (tile_size, tile_size):
                raise ValueError(
                    f"coverage mask must be exactly {tile_size}x{tile_size}")
            pixel = mask.convert("RGBA").getpixel((px, py))
            if not isinstance(pixel, tuple) or len(pixel) < 4:
                raise ValueError("coverage mask has no alpha channel")
            alpha = pixel[3]
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("coverage mask is not a decodable bounded PNG") from exc
    return alpha == 0


def decode_radar_tile(
    png: bytes | bytearray | memoryview,
    *,
    tile_size: int = RAINVIEWER_TILE_SIZE,
) -> Image.Image:
    """Decode one bounded, exact-size RainViewer echo tile into RGBA."""
    if tile_size not in _COVERAGE_MASK_SIZES:
        raise ValueError("radar tile_size must be 256 or 512")
    if not isinstance(png, (bytes, bytearray, memoryview)):
        raise ValueError("radar tile must be a PNG byte buffer")
    if not 0 < len(png) <= _RADAR_TILE_MAX_BYTES:
        raise ValueError("radar tile payload is empty or exceeds 1 MiB")

    try:
        with Image.open(io.BytesIO(bytes(png))) as tile:
            if tile.format != "PNG":
                raise ValueError("radar tile is not a PNG")
            if tile.size != (tile_size, tile_size):
                raise ValueError(
                    f"radar tile must be exactly {tile_size}x{tile_size}")
            # Convert while the bounded source is open so the returned image is
            # fully decoded and detached from the remote byte stream.
            rgba = tile.convert("RGBA")
            rgba.load()
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError) as exc:
        raise ValueError("radar tile is not a decodable bounded PNG") from exc
    return rgba


def dbz_from_rgb(rgb: tuple[int, int, int]) -> float | None:
    r, g, b = rgb
    best_dbz, best_d = None, _MAX_DIST_SQ + 1
    for dbz, pr, pg, pb in _UB_PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_d:
            best_dbz, best_d = float(dbz), d
    return best_dbz


def sample_dbz(img, px: int, py: int, radius: int = 2) -> float | None:
    rgba = img.convert("RGBA")
    best: float | None = None
    for dx in range(-radius, radius + 1):
        for dy in range(-radius, radius + 1):
            x = min(max(px + dx, 0), img.width - 1)
            y = min(max(py + dy, 0), img.height - 1)
            r, g, b, a = rgba.getpixel((x, y))
            if a == 0:
                continue                     # transparent = no echo
            dbz = dbz_from_rgb((r, g, b))
            if dbz is not None and (best is None or dbz > best):
                best = dbz
    return best


def resolve_rain(
    radar_dbz: float | None,
    radar_age_s: float,
    om_rain: bool | None,
    om_age_s: float,
    station_rain: bool | None,
    station_age_s: float,
    last_rain: bool,
    last_tier: int,
    last_known: bool,
    last_age_s: float,
    snowing: bool,
) -> tuple[bool, int, str]:
    """Choose the freshest precipitation evidence without inventing clear.

    ``None`` means a source has never supplied station/model evidence.  Once
    every available source ages out, retain the already-resolved last-good
    value until its own source-aged weather lease expires; a partial station
    refresh must never renew that precipitation lease or manufacture dry
    evidence.  ``unavailable`` suppresses stale rain visually without claiming
    that any provider observed clear conditions.
    """
    if snowing:
        return False, 1, "snow"
    if radar_age_s < RADAR_FRESH_S:
        # Radar is speaking — None just means "no echo over the house".
        if radar_dbz is None or radar_dbz < RAIN_DBZ:
            return False, 1, "radar"
        tier = 0 if radar_dbz < 30 else (1 if radar_dbz < 40 else 2)
        return True, tier, "radar"
    if om_rain is not None and om_age_s < OM_FRESH_S:
        return bool(om_rain), 1, "nowcast"
    if station_rain is not None and station_age_s < STATION_FRESH_S:
        return bool(station_rain), 1, "station"
    # A cold start (or an expired last-good) has no current precipitation to
    # preserve. Open-Meteo's complete current row can still be usable for the
    # two-hour base-weather lease after it stops qualifying as a 30-minute
    # nowcast. Prefer a still-live last-good value, but keep process restarts
    # from changing how the same bounded model row resolves.
    if ((not last_known or last_age_s > MODEL_FALLBACK_S)
            and om_rain is not None
            and om_age_s < MODEL_FALLBACK_S):
        return bool(om_rain), 1, "model-aged"
    if last_known and last_age_s <= MODEL_FALLBACK_S:
        return bool(last_rain), last_tier, "last-good"
    return False, 1, "unavailable"

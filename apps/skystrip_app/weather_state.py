"""Skystrip weather state."""

from __future__ import annotations

import asyncio
import math
from datetime import datetime, timezone

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import weather as _weather
from busybar_dev.radar import resolve_rain


def _source_datetime(
    value,
    *,
    now: datetime | None = None,
    allow_naive_utc: bool = False,
    max_age_s: float | None = None,
) -> datetime | None:
    return _weather._source_datetime(
        value,
        now=now or datetime.now(timezone.utc),
        allow_naive_utc=allow_naive_utc,
        max_age_s=_weather.WEATHER_LEASE_S if max_age_s is None else max_age_s,
    )


def _source_monotonic_at(
    source_at: datetime,
    *,
    wall_now: datetime | None = None,
    monotonic_now: float | None = None,
) -> float:
    """Map one validated wall-clock source instant onto the monotonic axis."""
    current = (wall_now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_utc = source_at.astimezone(timezone.utc)
    age = max(0.0, (current - source_utc).total_seconds())
    if monotonic_now is None:
        monotonic_now = asyncio.get_running_loop().time()
    return monotonic_now - age


def _mark_weather_fresh(state: _model.SkyState, source_at: datetime) -> None:
    """Set the live-weather lease from the committed snapshot's real age.

    Receipt time is not observation time.  Giving a nearly two-hour-old
    observation a fresh two-hour lease would keep it on screen for almost four
    hours.  Map the wall-clock source age onto the monotonic clock used by the
    runtime gate.  This assignment is deliberate: if a newly committed fused
    state has an older limiting source than its predecessor, its lease must
    shorten with it.
    """
    state.weather_updated_at = _source_monotonic_at(source_at)
    state.weather_ready.set()


def weather_is_fresh(state: _model.SkyState) -> bool:
    if not state.weather_ready.is_set() or state.weather_updated_at is None:
        return False
    return (
        asyncio.get_running_loop().time() - state.weather_updated_at
        <= _weather.WEATHER_LEASE_S
    )


RADAR_INTERVAL_S = 300  # RainViewer refreshes its mosaic every ~5 minutes


# zoom comes from busybar_dev.radar.RADAR_MAX_ZOOM (7, ~900 m/px at 42°N —
# the free tile cache rejects anything deeper; verified live 2026-08-05)
def apply_rain(state: _model.SkyState) -> None:
    """Re-resolve the rain flag from the freshest honest source. Called by
    every feed that lands new evidence (same pattern as the temp nowcast)."""
    now = asyncio.get_event_loop().time()
    radar_age = now - state.radar_at if state.radar_at != 0.0 else 1e9
    om_age = now - state.om_at if state.om_at is not None else 1e9
    station_age = now - state.station_at if state.station_at is not None else 1e9
    last_age = now - state.rain_at if state.rain_at is not None else 1e9
    snow_fresh = (
        state.weather.snow
        and state.snow_at is not None
        and now - state.snow_at <= _weather.WEATHER_LEASE_S
    )
    rain, tier, src = resolve_rain(
        state.radar_dbz,
        radar_age,
        state.om_rain,
        om_age,
        state.station_rain,
        station_age,
        state.weather.rain,
        state.weather.rain_tier,
        state.rain_known,
        last_age,
        snow_fresh,
    )
    source_at = {
        "radar": state.radar_at,
        "nowcast": state.om_at,
        "model-aged": state.om_at,
        "station": state.station_at,
        "snow": state.snow_at,
    }.get(src)
    if source_at is not None:
        state.rain_known = True
        state.rain_at = source_at
    state.weather.rain = rain
    state.weather.rain_tier = tier
    if src != state.rain_src:
        _limits.logger.info(
            "rain source -> %s (rain=%s tier=%d dbz=%s)",
            src,
            rain,
            tier,
            state.radar_dbz,
        )
        state.rain_src = src


def _expire_stale_phenomena(state: _model.SkyState) -> None:
    """Suppress weather effects whose own evidence lease has expired.

    Cloud, wind, and temperature can be refreshed by a station row whose
    optional phenomena fields are missing.  Their successful refresh must not
    make an older snowstorm, thunder report, or modeled ground-snow depth live
    forever.
    """
    now = asyncio.get_running_loop().time()
    for field_name, timestamp_name in (
        ("snow", "snow_at"),
        ("thunder", "thunder_at"),
    ):
        observed_at = getattr(state, timestamp_name)
        if observed_at is None or now - observed_at > _weather.WEATHER_LEASE_S:
            setattr(state.weather, field_name, False)
    if (
        state.snow_depth_at is not None
        and now - state.snow_depth_at > _weather.WEATHER_LEASE_S
    ):
        state.weather.snow_depth_m = 0.0


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (
        0.5
        - math.cos((lat2 - lat1) * p) / 2
        + math.cos(lat1 * p)
        * math.cos(lat2 * p)
        * (1 - math.cos((lon2 - lon1) * p))
        / 2
    )
    return 12742 * math.asin(math.sqrt(a))

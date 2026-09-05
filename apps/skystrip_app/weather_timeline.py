"""Skystrip weather timeline."""

from __future__ import annotations

from datetime import datetime, timezone

from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather


def parse_hourly(payload, *, tz=None) -> list:
    return _weather.parse_hourly(payload, tz=_settings.TZ if tz is None else tz)


def _parse_obs_history(payload, start: datetime, now: datetime | None = None) -> list:
    return _weather._parse_obs_history(payload, start, now=now, tz=_settings.TZ)


def wx_at(state: _model.SkyState, target: datetime) -> _weather.WeatherState:
    """Weather for a scrubbed moment, from the hourly table (real history
    backward, real forecast forward). Falls back to live conditions."""
    if not state.hourly:
        return state.weather
    # Compare as instants, not as wall clocks. Python subtracts two aware
    # datetimes in the SAME zone naively — it ignores fold — so at the autumn
    # fall-back both 01:00 rows read as zero seconds from a 01:00 target and
    # min() silently took the first. Converting to UTC forces real interval
    # arithmetic, which is the half of the fold fix that lives here rather
    # than at ingestion.
    goal = target.astimezone(timezone.utc)

    def distance(row) -> float:
        return abs((row[0].astimezone(timezone.utc) - goal).total_seconds())

    row = min(state.hourly, key=distance)
    if distance(row) > 5400:
        return state.weather
    d = row[1]
    live = state.weather
    # Precipitation is the one thing the hourly table cannot be trusted for in
    # both directions, so it is resolved separately. Everything else -- cloud,
    # temperature, wind, humidity, visibility -- comes from the model, which is
    # good at those and denser than the station record.
    if goal <= datetime.now(timezone.utc):
        fall = _weather.observed_precip_at(state.obs_history, target)
    else:
        fall = _weather.forecast_precip(d)
    return _weather.WeatherState(
        cloud_frac=(d["cloud"] or 0) / 100.0,
        # No observation covering a past moment means unknown, not dry. Either
        # way nothing is drawn -- but it must never fall through to the hourly
        # table, which is the model's guess about the past and was wrong by a
        # whole thunderstorm on the day this was written.
        rain=bool(fall and fall["rain"]),
        rain_tier=fall["tier"] if fall else 1,
        snow=bool(fall and fall["snow"]),
        thunder=bool(fall and fall["thunder"]),
        severe=False,  # alarms live in the present only
        # `temp` and `cloud` are guaranteed by parse_hourly — a row without
        # them is dropped there rather than defaulted here. For the rest, the
        # live snapshot is the honest fallback: 20 degrees, 50% humidity and
        # 16 km of visibility were invented constants that rendered as
        # confidently as measurements. Falling back to what is actually
        # outside is at least a real observation.
        wind_kmh=d["wind"] if d["wind"] is not None else live.wind_kmh,
        wind_dir=d["wdir"],
        temp_c=d["temp"],
        humidity=d["rh"] if d["rh"] is not None else live.humidity,
        visibility_m=(d["vis"] if d["vis"] is not None else live.visibility_m),
        snow_depth_m=(
            d["snow_depth"] if d.get("snow_depth") is not None else live.snow_depth_m
        ),
    )

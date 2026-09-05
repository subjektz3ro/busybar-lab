"""Skystrip providers / weather."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import cast

import httpx

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app import weather_state as _weather_state
from apps.skystrip_app import weather_timeline as _weather_timeline
from busybar_dev.config import describe_exception


async def poll_nws(state: _model.SkyState) -> None:
    station = _settings.NWS_STATION
    forecast_url = None
    nws_ok = True  # health of this observation/forecast discovery pipeline
    nws_retry_at = 0.0
    async with httpx.AsyncClient(headers=_settings.NWS_UA, timeout=20) as client:
        while True:
            t0 = asyncio.get_event_loop().time()
            nws_observed = False
            nws_snapshot: _weather.WeatherUpdates | None = None
            nws_source_time: datetime | None = None
            if (not station or forecast_url is None) and t0 >= nws_retry_at:
                points_resolved = False
                try:
                    r = await client.get(
                        f"https://api.weather.gov/points/{_settings.LAT:.4f},{_settings.LON:.4f}"
                    )
                    r.raise_for_status()
                    # Only the status of `/points` defines the geographic
                    # boundary. A later station-list 404 means the covered
                    # point lacks usable station discovery, not that CAP and
                    # every other NWS point product are unavailable there.
                    points_resolved = True
                    props = r.json()["properties"]
                    state.nws_point_covered = True
                    state.nws_point_checked.set()
                    forecast_url = props["forecast"]
                    if not station:
                        rs = await client.get(props["observationStations"])
                        rs.raise_for_status()
                        station = rs.json()["features"][0]["properties"][
                            "stationIdentifier"
                        ]
                    _limits.logger.info("NWS: station %s, forecast discovered", station)
                    nws_ok = True
                except Exception as exc:  # noqa: BLE001
                    # A 404 from /points genuinely means "outside NWS coverage"
                    # and deserves a slow retry; anything else (DNS cold at
                    # boot, a 503, a timeout) is transient. This used to latch
                    # off forever, silently taking observations, the forecast
                    # AND the severe-weather alarm with it for the life of
                    # the process.
                    status = getattr(
                        getattr(exc, "response", None), "status_code", None
                    )
                    points_unsupported = status == 404 and not points_resolved
                    if points_unsupported:
                        state.nws_point_covered = False
                        _alerts._stand_down_nws_alerts(state)
                    if not state.nws_point_checked.is_set():
                        state.nws_point_checked.set()
                    wait = 6 * 3600 if points_unsupported else 900
                    nws_retry_at = t0 + wait
                    if nws_ok:  # log only on the transition, not every cycle
                        if points_unsupported:
                            _limits.logger.info(
                                "NWS point unsupported (%s) — global feeds "
                                "only; retrying in %.0fs",
                                describe_exception(exc),
                                wait,
                            )
                        else:
                            _limits.logger.info(
                                "NWS observation/forecast discovery failed "
                                "(%s); retrying in %.0fs",
                                describe_exception(exc),
                                wait,
                            )
                    nws_ok = False
                    if points_unsupported:
                        # Point coverage is the locality contract. A pinned
                        # station must not smuggle unrelated US observation
                        # history into a location the point API does not cover.
                        state.obs_history = []
            try:
                if not (nws_ok and station):
                    raise RuntimeError("no NWS station")
                r = await client.get(
                    f"https://api.weather.gov/stations/{station}/observations/latest"
                )
                r.raise_for_status()
                obs_props = r.json()["properties"]
                obs_time = _weather_state._source_datetime(obs_props.get("timestamp"))
                if obs_time is None:
                    raise ValueError("NWS observation timestamp is missing or stale")
                obs = _weather._parse_obs(obs_props)
                nws_observed = {
                    "cloud_frac",
                    "wind_kmh",
                    "temp_c",
                    "humidity",
                    "visibility_m",
                }.issubset(obs)
                if not nws_observed:
                    raise ValueError("NWS observation is incomplete")
                # Stage the station snapshot across the Open-Meteo await.
                # Rendering must continue to see the previous fused snapshot,
                # not temporarily regress to airport values mid-cycle.
                nws_snapshot = obs
                nws_source_time = obs_time
                _limits.logger.info(
                    "obs: cloud=%.0f%% rain=%s snow=%s thunder=%s "
                    "wind=%.0fkm/h temp=%.0fC rh=%.0f%% vis=%.0fm",
                    obs["cloud_frac"] * 100,
                    obs.get("rain"),
                    obs.get("snow"),
                    obs.get("thunder"),
                    obs["wind_kmh"],
                    obs["temp_c"],
                    obs["humidity"],
                    obs["visibility_m"],
                )
            except Exception as exc:  # noqa: BLE001 - feed is best-effort
                if nws_ok:
                    _limits.logger.warning(
                        "obs fetch failed: %s", describe_exception(exc)
                    )
            # The NWS station is truth for phenomena but reads at the airport,
            # roughly hourly. Open-Meteo nowcasts temperature at our exact
            # coordinates (what the watch on your wrist does) — use it for
            # the number people compare against.
            try:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    headers=_limits.NEUTRAL_UA,  # no contact needed here
                    params={
                        "latitude": _settings.LAT,
                        "longitude": _settings.LON,
                        "current": "temperature_2m,precipitation,rain,showers,"
                        "snow_depth,cloud_cover,weather_code,"
                        "wind_speed_10m,wind_direction_10m,"
                        "relative_humidity_2m,visibility",
                        "temperature_unit": "celsius",
                        "wind_speed_unit": "kmh",
                        "timezone": "UTC",
                    },
                )
                r.raise_for_status()
                cur = r.json()["current"]
                if not isinstance(cur, dict):
                    raise ValueError("Open-Meteo current snapshot is not an object")
                current_time = _weather_state._source_datetime(
                    cur.get("time"), allow_naive_utc=True
                )
                required = {
                    key: _weather._finite_number(cur.get(key), low, high)
                    for key, low, high in (
                        ("temperature_2m", -100, 70),
                        ("precipitation", 0, 1000),
                        ("rain", 0, 1000),
                        ("showers", 0, 1000),
                        ("snow_depth", 0, 20),
                        ("cloud_cover", 0, 100),
                        ("wind_speed_10m", 0, 500),
                        ("wind_direction_10m", 0, 360),
                        ("relative_humidity_2m", 0, 100),
                        ("visibility", 0, 200_000),
                        ("weather_code", 0, 99),
                    )
                }
                if current_time is None or any(
                    value is None for value in required.values()
                ):
                    raise ValueError(
                        "Open-Meteo current snapshot is stale or incomplete"
                    )
                complete = cast(dict[str, float], required)

                # Build the entire candidate first.  Nothing below this point
                # reads provider data that can invalidate the envelope, so a
                # malformed/stale response can never half-overwrite the live
                # last-known-good WeatherState.
                model_t = complete["temperature_2m"]
                cloud_cover = complete["cloud_cover"]
                code_value = complete["weather_code"]
                code_rain, code_snow, code_thunder, code_fog = _weather._wmo_phenomena(
                    code_value
                )
                # This is the ONLY feed of settled snow depth to the live
                # scene: wx_at() (the Time Machine) reads its own copy from
                # the hourly table, but the bar's actual live scene renders
                # from state.weather, which only poll_nws ever writes.
                # Without this, snow_tier(wx.snow_depth_m) is 0 forever on
                # the device no matter what really fell.
                snow_depth = complete["snow_depth"]
                precip = complete["precipitation"]
                rain_mm = complete["rain"]
                showers = complete["showers"]
                om_rain = precip > 0.05 or rain_mm > 0 or showers > 0 or code_rain
                modeled: _weather.WeatherUpdates = {
                    "temp_c": model_t,
                    "snow_depth_m": snow_depth,
                }
                # A station snapshot can be complete for cloud/wind/temp while
                # its optional phenomena envelope is absent. In that case the
                # valid global model must refresh snow/thunder instead of
                # letting an old station storm state survive indefinitely.
                if nws_snapshot is None or "snow" not in nws_snapshot:
                    modeled["snow"] = code_snow
                if nws_snapshot is None or "thunder" not in nws_snapshot:
                    modeled["thunder"] = code_thunder
                if nws_snapshot is None or "fog" not in nws_snapshot:
                    modeled["fog"] = code_fog
                if not nws_observed:
                    modeled.update(
                        {
                            "cloud_frac": cloud_cover / 100.0,
                            "wind_kmh": complete["wind_speed_10m"],
                            "wind_dir": complete["wind_direction_10m"],
                            "humidity": complete["relative_humidity_2m"],
                            "visibility_m": complete["visibility"],
                        }
                    )

                prior_temp = state.weather.temp_c
                # Build on the latest live object *after* the network await so
                # a CAP task update to severe/severe_event cannot be lost.
                candidate = state.weather
                if nws_snapshot is not None:
                    # Rain is committed as separately timestamped evidence
                    # below. Letting replace() write it here would allow a
                    # station sample that aged out during the Open-Meteo await
                    # to silently become the last-good fallback.
                    candidate = replace(
                        candidate,
                        **_weather._without_rain(nws_snapshot),
                    )
                candidate = replace(candidate, **modeled)
                state.weather = candidate
                station_source_at = (
                    _weather_state._source_monotonic_at(nws_source_time)
                    if nws_source_time is not None
                    else None
                )
                model_source_at = _weather_state._source_monotonic_at(current_time)
                state.snow_depth_at = model_source_at
                if (
                    nws_snapshot is not None
                    and nws_source_time is not None
                    and "rain" in nws_snapshot
                ):
                    state.station_rain = nws_snapshot["rain"]
                    state.station_at = station_source_at
                if nws_snapshot is not None and "snow" in nws_snapshot:
                    state.snow_at = station_source_at
                elif "snow" in modeled:
                    state.snow_at = model_source_at
                if nws_snapshot is not None and "thunder" in nws_snapshot:
                    state.thunder_at = station_source_at
                elif "thunder" in modeled:
                    state.thunder_at = model_source_at
                state.om_rain = om_rain
                state.om_at = model_source_at
                # The fused WeatherState still carries NWS cloud, wind, and
                # observed phenomena.  Its lease is therefore limited by the
                # oldest contributing source, not extended by the newer one.
                fused_source_time = current_time
                if nws_source_time is not None:
                    fused_source_time = min(nws_source_time, current_time)
                _weather_state._mark_weather_fresh(state, fused_source_time)
                _limits.logger.info(
                    "temp: prior %.0fC -> nowcast %.1fC", prior_temp, model_t
                )
            except Exception as exc:  # noqa: BLE001
                _limits.logger.warning(
                    "nowcast temp failed (%s); station stands", describe_exception(exc)
                )
                if nws_snapshot is not None and nws_source_time is not None:
                    # The global nowcast failed, but the already validated
                    # station observation is still an honest complete fallback.
                    # Commit it only now, once, preserving any concurrent CAP
                    # fields that landed while Open-Meteo was awaited.
                    state.weather = replace(
                        state.weather,
                        **_weather._without_rain(nws_snapshot),
                    )
                    station_source_at = _weather_state._source_monotonic_at(
                        nws_source_time
                    )
                    if "rain" in nws_snapshot:
                        state.station_rain = nws_snapshot["rain"]
                        state.station_at = station_source_at
                    if "snow" in nws_snapshot:
                        state.snow_at = station_source_at
                    if "thunder" in nws_snapshot:
                        state.thunder_at = station_source_at
                    _weather_state._mark_weather_fresh(state, nws_source_time)
            # Outside the try on purpose: this must also run when the fetch
            # failed, so a previously-fresh nowcast can age out of the
            # precedence chain instead of freezing the rain flag.
            _weather_state._expire_stale_phenomena(state)
            _weather_state.apply_rain(state)

            now = asyncio.get_event_loop().time()
            if now >= getattr(poll_nws, "_hourly_due", 0.0):
                poll_nws.__dict__["_hourly_due"] = now + _limits.FORECAST_INTERVAL_S
                try:
                    r = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        headers=_limits.NEUTRAL_UA,
                        params={
                            "latitude": _settings.LAT,
                            "longitude": _settings.LON,
                            "hourly": "temperature_2m,cloud_cover,"
                            "precipitation,weather_code,"
                            "precipitation_probability,"
                            "wind_speed_10m,wind_direction_10m,"
                            "relative_humidity_2m,visibility,"
                            "snow_depth",
                            "past_days": 1,
                            "forecast_days": 2,
                            # UTC, then convert. Asking for local time and
                            # attaching the zone afterwards collapses the two
                            # repeated hours at the autumn fall-back.
                            "timezone": "UTC",
                        },
                    )
                    r.raise_for_status()
                    state.hourly = _weather_timeline.parse_hourly(r.json()["hourly"])
                    _limits.logger.info(
                        "hourly: %d rows for the Time Machine", len(state.hourly)
                    )
                except Exception as exc:  # noqa: BLE001
                    _limits.logger.warning(
                        "hourly fetch failed: %s", describe_exception(exc)
                    )
            if nws_ok and station and now >= getattr(poll_nws, "_obs_history_due", 0.0):
                poll_nws.__dict__["_obs_history_due"] = (
                    now + _limits.FORECAST_INTERVAL_S
                )
                try:
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(hours=_weather.OBS_HISTORY_HOURS)
                    r = await client.get(
                        f"https://api.weather.gov/stations/{station}/observations",
                        headers=_limits.NEUTRAL_UA,
                        params={
                            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "limit": _weather.OBS_HISTORY_MAX,
                        },
                    )
                    r.raise_for_status()
                    state.obs_history = _weather_timeline._parse_obs_history(
                        r.json(), start
                    )
                    _limits.logger.info(
                        "observations: %d rows of confirmed history",
                        len(state.obs_history),
                    )
                except Exception as exc:  # noqa: BLE001
                    # Leave the last good history in place. A failed fetch
                    # must not turn a rained-on afternoon into a dry one.
                    _limits.logger.warning(
                        "observation history fetch failed: %s", describe_exception(exc)
                    )
            if now >= getattr(poll_nws, "_forecast_due", 0.0):
                poll_nws.__dict__["_forecast_due"] = now + _limits.FORECAST_INTERVAL_S
                try:
                    if not forecast_url:
                        raise RuntimeError("no forecast endpoint")
                    r = await client.get(forecast_url)
                    r.raise_for_status()
                    state.forecast = r.json()["properties"]["periods"][:2]
                    _limits.logger.info(
                        "forecast: %s", state.forecast[0]["shortForecast"]
                    )
                except Exception as exc:  # noqa: BLE001
                    if nws_ok:
                        _limits.logger.warning(
                            "forecast fetch failed: %s", describe_exception(exc)
                        )
            await asyncio.sleep(_limits.OBS_INTERVAL_S)

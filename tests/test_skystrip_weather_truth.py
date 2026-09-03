"""Host-only contracts for Skystrip's weather truth boundary.

These tests deliberately exercise malformed, incomplete, and stale provider
payloads.  A plausible scene built from defaults or half of a response is
worse than a scene that honestly expires.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from busybar_dev.radar import OM_FRESH_S

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
skystrip = pytest.importorskip("skystrip")


NOW = datetime(2026, 8, 9, 18, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("value", "allow_naive", "expected"),
    [
        ("2026-08-09T17:59:00Z", False, NOW - timedelta(minutes=1)),
        ("2026-08-09T12:59:00-05:00", False, NOW - timedelta(minutes=1)),
        ("2026-08-09T17:59:00z", False, NOW - timedelta(minutes=1)),
        ("2026-08-09T17:59:00", True, NOW - timedelta(minutes=1)),
        ("2026-08-09T17:59:00", False, None),
        ("not-a-time", False, None),
        (None, False, None),
    ],
)
def test_source_datetime_accepts_only_bounded_unambiguous_instants(
    value, allow_naive, expected
):
    got = skystrip._source_datetime(
        value, now=NOW, allow_naive_utc=allow_naive
    )
    assert got == expected


def test_source_datetime_rejects_expired_and_implausibly_future_values():
    stale = NOW - timedelta(seconds=skystrip.WEATHER_LEASE_S + 1)
    future = NOW + timedelta(seconds=skystrip.SOURCE_FUTURE_SKEW_S + 1)

    assert skystrip._source_datetime(stale.isoformat(), now=NOW) is None
    assert skystrip._source_datetime(future.isoformat(), now=NOW) is None
    assert skystrip._source_datetime("2" * 65, now=NOW) is None


def test_source_monotonic_mapping_preserves_age_even_before_host_uptime():
    source_at = NOW - timedelta(minutes=90)

    mapped = skystrip._source_monotonic_at(
        source_at, wall_now=NOW, monotonic_now=120.0)

    assert mapped == 120.0 - 90 * 60


def test_parse_obs_keeps_missing_measurements_unknown():
    parsed = skystrip._parse_obs(
        {
            "textDescription": "Fair",
            "presentWeather": [],
            "cloudLayers": [],
            "windSpeed": {"value": None},
            "temperature": {"value": float("nan")},
            "relativeHumidity": {"value": 101},
            "visibility": {"value": -1},
        }
    )

    assert parsed == {
        "rain": False,
        "snow": False,
        "thunder": False,
        "fog": False,
        "obscuration": "",
        "cloud_frac": 0.0,
    }
    assert "temp_c" not in parsed
    assert "humidity" not in parsed
    assert "visibility_m" not in parsed


def test_parse_obs_treats_schema_wrong_optional_fields_as_incomplete():
    parsed = skystrip._parse_obs(
        {
            "textDescription": 42,
            "presentWeather": [],
            "cloudLayers": "BKN",
            "windSpeed": "fast",
            "windDirection": [270],
            "temperature": True,
            "relativeHumidity": {"value": "unknown"},
            "visibility": {"value": {}},
        }
    )

    assert parsed == {"rain": False, "snow": False, "thunder": False,
                      "fog": False, "obscuration": ""}


def test_parse_obs_does_not_turn_absent_phenomena_fields_into_dry_evidence():
    parsed = skystrip._parse_obs(
        {
            "textDescription": None,
            "presentWeather": {"unexpected": "object"},
            "cloudLayers": [{"amount": "BKN"}],
            "windSpeed": {"value": 10.0},
            "temperature": {"value": 12.0},
            "relativeHumidity": {"value": 70.0},
            "visibility": {"value": 12000.0},
        }
    )

    assert "rain" not in parsed
    assert "snow" not in parsed
    assert "thunder" not in parsed
    assert "fog" not in parsed
    assert "obscuration" not in parsed
    assert parsed["cloud_frac"] == pytest.approx(0.8)


def test_parse_obs_validates_and_normalizes_a_complete_snapshot():
    parsed = skystrip._parse_obs(
        {
            "textDescription": "Thunderstorms and rain",
            "presentWeather": [{"weather": "TSRA"}],
            "cloudLayers": [
                {"amount": "SCT"},
                {"amount": "OVC"},
                {"amount": "NOT-A-CODE"},
            ],
            "windSpeed": {"value": 31.5},
            "windDirection": {"value": 270},
            "temperature": {"value": 19.25},
            "relativeHumidity": {"value": 87},
            "visibility": {"value": 6400},
        }
    )

    assert parsed == {
        "rain": True,
        "snow": False,
        "thunder": True,
        "fog": False,
        "obscuration": "",
        "cloud_frac": 1.0,
        "wind_kmh": 31.5,
        "wind_dir": 270.0,
        "temp_c": 19.25,
        "humidity": 87.0,
        "visibility_m": 6400.0,
    }


@pytest.mark.asyncio
async def test_weather_gate_starts_closed_and_expires_on_monotonic_age():
    state = skystrip.SkyState()
    loop = asyncio.get_running_loop()

    assert not skystrip.weather_is_fresh(state)
    state.weather_ready.set()
    assert not skystrip.weather_is_fresh(state)

    state.weather_updated_at = loop.time() - skystrip.WEATHER_LEASE_S + 1
    assert skystrip.weather_is_fresh(state)
    state.weather_updated_at = loop.time() - skystrip.WEATHER_LEASE_S - 1
    assert not skystrip.weather_is_fresh(state)


@pytest.mark.asyncio
async def test_freshness_lease_includes_source_age_not_just_receipt_age():
    state = skystrip.SkyState()
    source_at = datetime.now(timezone.utc) - timedelta(
        seconds=skystrip.WEATHER_LEASE_S - 30
    )

    skystrip._mark_weather_fresh(state, source_at)
    assert skystrip.weather_is_fresh(state)

    # Advancing the monotonic source-age estimate by the remaining margin
    # expires the observation.  A receipt-time lease would incorrectly leave
    # almost the full two hours here.
    state.weather_updated_at -= 31
    assert not skystrip.weather_is_fresh(state)


@pytest.mark.asyncio
async def test_committing_an_older_snapshot_shortens_the_existing_lease():
    state = skystrip.SkyState()
    now = datetime.now(timezone.utc)
    skystrip._mark_weather_fresh(state, now)
    newest = state.weather_updated_at

    skystrip._mark_weather_fresh(state, now - timedelta(hours=1))

    assert state.weather_updated_at < newest - 3599


@pytest.mark.asyncio
async def test_alert_restore_does_not_renew_an_expired_live_scene():
    state = skystrip.SkyState()
    state.current_scene_file = "expired-sky.anim"
    state.weather_ready.set()
    state.weather_updated_at = (
        asyncio.get_running_loop().time() - skystrip.WEATHER_LEASE_S - 1
    )

    payload, restored_reveal, restored_readout = skystrip._restore_payload(state)

    # The expired sky must not come back — but the restore follows a
    # display_clear, so "draw nothing" means "go black and stay black". It
    # says why instead.
    element_ids = [element.id for element in payload.elements]
    assert "sky" not in element_ids
    assert element_ids == ["wxstaleb", "wxstalet"]
    assert restored_reveal is None
    assert restored_readout is None


@pytest.mark.asyncio
async def test_time_machine_reveal_restores_without_stale_live_sky_under_it():
    state = skystrip.SkyState()
    state.current_scene_file = "expired-sky.anim"
    state.weather_ready.set()
    state.weather_updated_at = (
        asyncio.get_running_loop().time() - skystrip.WEATHER_LEASE_S - 1
    )
    state.scrub_slot = 12
    state.revealed = True
    state.last_reveal = {
        "eid": "rv4",
        "slot": 12,
        "fname": "timeline.anim",
        "section": "s12",
    }

    payload, restored_reveal, _ = skystrip._restore_payload(state)

    assert payload is not None
    assert [element.id for element in payload.elements] == ["rv4"]
    assert restored_reveal == state.last_reveal


def _complete_current(**overrides):
    current = {
        "time": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "temperature_2m": -3.5,
        "precipitation": 0.0,
        "rain": 0.0,
        "showers": 0.0,
        "snow_depth": 0.22,
        "cloud_cover": 88.0,
        "weather_code": 71,
        "wind_speed_10m": 14.0,
        "wind_direction_10m": 315.0,
        "relative_humidity_2m": 90.0,
        "visibility": 5000.0,
    }
    current.update(overrides)
    return current


CURRENT_REQUIRED_FIELDS = (
    "time",
    "temperature_2m",
    "precipitation",
    "rain",
    "showers",
    "snow_depth",
    "cloud_cover",
    "weather_code",
    "wind_speed_10m",
    "wind_direction_10m",
    "relative_humidity_2m",
    "visibility",
)


async def _poll_one_open_meteo_iteration(monkeypatch, state, current):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            return httpx.Response(404, json={"detail": "outside coverage"})
        if request.url.host == "api.open-meteo.com":
            if "current" in request.url.params:
                return httpx.Response(200, json={"current": current})
            return httpx.Response(200, json={"hourly": {"time": []}})
        raise AssertionError(f"unexpected request: {request.url}")

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(skystrip.httpx, "AsyncClient", MockClient)
    monkeypatch.setattr(skystrip, "NWS_STATION", "")
    monkeypatch.delattr(skystrip.poll_nws, "_hourly_due", raising=False)
    monkeypatch.delattr(skystrip.poll_nws, "_forecast_due", raising=False)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(skystrip.poll_nws(state), timeout=0.3)


@pytest.mark.asyncio
async def test_stale_open_meteo_snapshot_is_rejected_before_any_mutation(
    monkeypatch,
):
    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(
        temp_c=17.0,
        cloud_frac=0.25,
        wind_kmh=5.0,
        humidity=45.0,
        visibility_m=15000.0,
        snow_depth_m=0.01,
    )
    stale_time = datetime.now(timezone.utc) - timedelta(
        seconds=skystrip.WEATHER_LEASE_S + 60
    )

    await _poll_one_open_meteo_iteration(
        monkeypatch,
        state,
        _complete_current(time=stale_time.isoformat()),
    )

    assert state.weather.temp_c == 17.0
    assert state.weather.cloud_frac == 0.25
    assert state.weather.snow_depth_m == 0.01
    assert state.om_at is None
    assert state.om_rain is None
    assert not state.weather_ready.is_set()


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", CURRENT_REQUIRED_FIELDS)
async def test_incomplete_open_meteo_snapshot_is_atomic_and_keeps_last_good(
    monkeypatch, missing,
):
    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(
        temp_c=17.0,
        cloud_frac=0.25,
        wind_kmh=5.0,
        humidity=45.0,
        visibility_m=15000.0,
        snow_depth_m=0.01,
    )
    incomplete = _complete_current()
    incomplete.pop(missing)

    await _poll_one_open_meteo_iteration(monkeypatch, state, incomplete)

    assert state.weather.temp_c == 17.0
    assert state.weather.cloud_frac == 0.25
    assert state.weather.snow_depth_m == 0.01
    assert state.om_at is None
    assert state.om_rain is None
    assert not state.weather_ready.is_set()


@pytest.mark.asyncio
async def test_complete_fresh_open_meteo_snapshot_commits_once_and_opens_gate(
    monkeypatch,
):
    state = skystrip.SkyState()

    await _poll_one_open_meteo_iteration(
        monkeypatch, state, _complete_current()
    )

    assert state.weather_ready.is_set()
    assert skystrip.weather_is_fresh(state)
    assert state.weather.temp_c == -3.5
    assert state.weather.cloud_frac == pytest.approx(0.88)
    assert state.weather.snow is True
    assert state.weather.snow_depth_m == pytest.approx(0.22)
    assert state.weather.wind_kmh == 14.0
    assert state.weather.wind_dir == 315.0
    assert state.weather.humidity == 90.0
    assert state.weather.visibility_m == 5000.0
    assert state.om_at is not None
    assert state.snow_depth_at is not None
    assert state.station_rain is None
    assert state.station_at is None


@pytest.mark.asyncio
async def test_cached_open_meteo_uses_source_age_and_cannot_outrank_station(
    monkeypatch,
):
    state = skystrip.SkyState()
    now_mono = asyncio.get_running_loop().time()
    state.weather = skystrip.WeatherState(rain=True, rain_tier=2)
    state.weather_ready.set()
    state.weather_updated_at = now_mono
    state.station_rain = True
    state.station_at = now_mono
    cached_at = datetime.now(timezone.utc) - timedelta(
        seconds=OM_FRESH_S + 60
    )

    await _poll_one_open_meteo_iteration(
        monkeypatch,
        state,
        _complete_current(
            time=cached_at.isoformat(),
            temperature_2m=20.0,
            snow_depth=0.0,
            weather_code=0,
        ),
    )

    assert state.om_at is not None
    assert asyncio.get_running_loop().time() - state.om_at > OM_FRESH_S
    assert state.rain_src == "station"
    assert state.weather.rain is True


@pytest.mark.asyncio
async def test_outside_nws_cold_start_uses_aged_model_not_default_dry(
    monkeypatch,
):
    state = skystrip.SkyState()
    cached_at = datetime.now(timezone.utc) - timedelta(
        seconds=OM_FRESH_S + 60
    )

    await _poll_one_open_meteo_iteration(
        monkeypatch,
        state,
        _complete_current(
            time=cached_at.isoformat(),
            precipitation=1.0,
            rain=1.0,
            snow_depth=0.0,
            weather_code=61,
        ),
    )

    assert state.station_rain is None
    assert state.weather_ready.is_set()
    assert state.weather.rain is True
    assert state.rain_known is True
    assert state.rain_src == "model-aged"


@pytest.mark.asyncio
async def test_outside_nws_recovery_matches_cold_start_after_rain_lease_expires(
    monkeypatch,
):
    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(rain=False, rain_tier=2)
    state.rain_known = True
    state.rain_at = (
        asyncio.get_running_loop().time() - skystrip.WEATHER_LEASE_S - 1.0
    )
    cached_at = datetime.now(timezone.utc) - timedelta(
        seconds=OM_FRESH_S + 60
    )

    await _poll_one_open_meteo_iteration(
        monkeypatch,
        state,
        _complete_current(
            time=cached_at.isoformat(),
            precipitation=1.0,
            rain=1.0,
            snow_depth=0.0,
            weather_code=61,
        ),
    )

    assert state.weather.rain is True
    assert state.rain_src == "model-aged"
    assert state.rain_at == state.om_at


@pytest.mark.asyncio
async def test_model_refreshes_phenomena_missing_from_complete_station_snapshot(
    monkeypatch,
):
    observation = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "textDescription": None,
        "presentWeather": {"unexpected": "object"},
        "cloudLayers": [{"amount": "BKN"}],
        "windSpeed": {"value": 12.0},
        "windDirection": {"value": 180.0},
        "temperature": {"value": 15.0},
        "relativeHumidity": {"value": 70.0},
        "visibility": {"value": 12000.0},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            if "/points/" in request.url.path:
                return httpx.Response(200, json={"properties": {
                    "forecast": "https://api.weather.gov/gridpoints/TST/1,1/forecast",
                }})
            if "/observations/latest" in request.url.path:
                return httpx.Response(200, json={"properties": observation})
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(200, json={"current": _complete_current(
                temperature_2m=15.0,
                snow_depth=0.0,
                weather_code=0,
            )})
        raise AssertionError(f"unexpected request: {request.url}")

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(skystrip.httpx, "AsyncClient", MockClient)
    monkeypatch.setattr(skystrip, "NWS_STATION", "KTST")
    monkeypatch.setattr(skystrip.poll_nws, "_hourly_due", float("inf"),
                        raising=False)
    monkeypatch.setattr(skystrip.poll_nws, "_obs_history_due", float("inf"),
                        raising=False)
    monkeypatch.setattr(skystrip.poll_nws, "_forecast_due", float("inf"),
                        raising=False)

    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(snow=True, thunder=True)

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(skystrip.poll_nws(state), timeout=0.3)

    assert state.station_rain is None
    assert state.station_at is None
    assert state.weather.snow is False
    assert state.weather.thunder is False


@pytest.mark.asyncio
async def test_partial_station_cannot_renew_stale_phenomena_when_model_fails(
    monkeypatch,
):
    observation = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "textDescription": None,
        "presentWeather": {"unexpected": "object"},
        "cloudLayers": [{"amount": "BKN"}],
        "windSpeed": {"value": 12.0},
        "windDirection": {"value": 180.0},
        "temperature": {"value": 15.0},
        "relativeHumidity": {"value": 70.0},
        "visibility": {"value": 12000.0},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            if "/points/" in request.url.path:
                return httpx.Response(200, json={"properties": {
                    "forecast": "https://api.weather.gov/gridpoints/TST/1,1/forecast",
                }})
            if "/observations/latest" in request.url.path:
                return httpx.Response(200, json={"properties": observation})
        if request.url.host == "api.open-meteo.com":
            return httpx.Response(503, json={"error": "unavailable"})
        raise AssertionError(f"unexpected request: {request.url}")

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(skystrip.httpx, "AsyncClient", MockClient)
    monkeypatch.setattr(skystrip, "NWS_STATION", "KTST")
    monkeypatch.setattr(skystrip.poll_nws, "_hourly_due", float("inf"),
                        raising=False)
    monkeypatch.setattr(skystrip.poll_nws, "_obs_history_due", float("inf"),
                        raising=False)
    monkeypatch.setattr(skystrip.poll_nws, "_forecast_due", float("inf"),
                        raising=False)

    now = asyncio.get_running_loop().time()
    stale_at = now - skystrip.WEATHER_LEASE_S - 1.0
    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(
        temp_c=25.0,
        rain=True,
        rain_tier=2,
        snow=True,
        thunder=True,
        snow_depth_m=0.22,
    )
    state.weather_ready.set()
    state.weather_updated_at = stale_at
    state.station_rain = True
    state.station_at = stale_at
    state.rain_known = True
    state.rain_at = stale_at
    state.snow_at = stale_at
    state.thunder_at = stale_at
    state.snow_depth_at = stale_at

    with pytest.raises(asyncio.TimeoutError):
        await asyncio.wait_for(skystrip.poll_nws(state), timeout=0.3)

    # Numeric station truth is still useful and refreshes the base-weather
    # lease, but its absent phenomena envelope cannot renew old storm effects.
    assert state.weather.temp_c == 15.0
    assert skystrip.weather_is_fresh(state)
    assert state.weather.rain is False
    assert state.weather.rain_tier == 1
    assert state.rain_src == "unavailable"
    assert state.weather.snow is False
    assert state.weather.thunder is False
    assert state.weather.snow_depth_m == 0.0
    assert state.station_at == stale_at
    assert state.rain_at == stale_at
    assert state.snow_at == stale_at
    assert state.thunder_at == stale_at
    assert state.snow_depth_at == stale_at


@pytest.mark.asyncio
@pytest.mark.parametrize("open_meteo_succeeds", [True, False])
async def test_cross_feed_await_keeps_last_fused_snapshot_until_one_commit(
    monkeypatch, open_meteo_succeeds,
):
    om_entered = asyncio.Event()
    release_om = asyncio.Event()
    station_source_time = datetime.now(timezone.utc) - timedelta(
        seconds=skystrip.WEATHER_LEASE_S - 60
    )
    observation = {
        "timestamp": station_source_time.isoformat(),
        "textDescription": "Mostly cloudy",
        "presentWeather": [],
        "cloudLayers": [{"amount": "BKN"}],
        "windSpeed": {"value": 22.0},
        "windDirection": {"value": 180.0},
        "temperature": {"value": 10.0},
        "relativeHumidity": {"value": 72.0},
        "visibility": {"value": 12000.0},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "api.weather.gov":
            if "/points/" in request.url.path:
                return httpx.Response(200, json={"properties": {
                    "forecast": "https://api.weather.gov/gridpoints/TST/1,1/forecast",
                    "observationStations": "https://api.weather.gov/stations",
                }})
            if "/observations/latest" in request.url.path:
                return httpx.Response(200, json={"properties": observation})
            if "/gridpoints/" in request.url.path:
                return httpx.Response(200, json={"properties": {"periods": [
                    {"shortForecast": "Mostly cloudy"}
                ]}})
        if request.url.host == "api.open-meteo.com":
            if "current" in request.url.params:
                om_entered.set()
                await release_om.wait()
                if not open_meteo_succeeds:
                    return httpx.Response(503, json={"error": "unavailable"})
                return httpx.Response(200, json={"current": _complete_current()})
            return httpx.Response(200, json={"hourly": {"time": []}})
        raise AssertionError(f"unexpected request: {request.url}")

    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(skystrip.httpx, "AsyncClient", MockClient)
    monkeypatch.setattr(skystrip, "NWS_STATION", "KTST")
    monkeypatch.delattr(skystrip.poll_nws, "_hourly_due", raising=False)
    monkeypatch.delattr(skystrip.poll_nws, "_forecast_due", raising=False)

    state = skystrip.SkyState()
    state.weather = skystrip.WeatherState(
        temp_c=25.0,
        cloud_frac=0.10,
        wind_kmh=4.0,
        humidity=40.0,
        visibility_m=18000.0,
        severe=True,
        severe_event="Tornado Warning",
    )
    state.weather_ready.set()
    state.weather_updated_at = asyncio.get_running_loop().time()
    original_mark = state.weather_updated_at

    poller = asyncio.create_task(skystrip.poll_nws(state))
    try:
        await asyncio.wait_for(om_entered.wait(), 0.3)
        # NWS is validated and staged, but Open-Meteo is still in flight.  No
        # consumer can observe the older station snapshot in this window.
        assert state.weather.temp_c == 25.0
        assert state.weather.cloud_frac == 0.10
        assert state.weather_updated_at == original_mark

        release_om.set()
        expected_temp = -3.5 if open_meteo_succeeds else 10.0

        async def committed():
            while state.weather.temp_c != expected_temp:
                await asyncio.sleep(0)

        await asyncio.wait_for(committed(), 0.3)
        assert state.weather.cloud_frac == pytest.approx(0.80)
        assert state.weather.wind_kmh == 22.0
        assert state.weather.severe is True
        assert state.weather.severe_event == "Tornado Warning"
        assert state.station_rain is False
        assert state.station_at is not None
        station_age = (
            asyncio.get_running_loop().time() - state.station_at
        )
        expected_station_age = (
            datetime.now(timezone.utc) - station_source_time
        ).total_seconds()
        assert station_age == pytest.approx(expected_station_age, abs=1.0)
        # Both the successful fusion and the NWS-only fallback contain these
        # nearly-expired station fields.  A newer OM timestamp must not grant
        # the fused state another full lease.
        source_age = (
            asyncio.get_running_loop().time() - state.weather_updated_at
        )
        remaining_lease = skystrip.WEATHER_LEASE_S - source_age
        assert 0 < remaining_lease <= 61
        if open_meteo_succeeds:
            assert state.weather.snow_depth_m == pytest.approx(0.22)
            assert state.om_rain is False
            assert state.om_at is not None
        else:
            assert state.weather.snow_depth_m == 0.0
            assert state.om_rain is None
            assert state.om_at is None
    finally:
        release_om.set()
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)


def test_moon_age_converts_astrals_28_day_index_to_a_synodic_month(
    monkeypatch,
):
    monkeypatch.setattr(skystrip.moon, "phase", lambda _day: 14.0)

    assert skystrip._moon_age_days(date(2026, 8, 9)) == pytest.approx(
        29.530588 / 2
    )


@pytest.mark.parametrize(
    ("display_unit", "source_unit", "source_value", "expected"),
    [
        ("f", "F", 68, 68),
        ("f", "C", 20, 68),
        ("c", "C", 20, 20),
        ("c", "F", 68, 20),
        ("c", "f", 32, 0),
    ],
)
def test_forecast_temperature_converts_the_declared_source_unit(
    monkeypatch, display_unit, source_unit, source_value, expected
):
    monkeypatch.setattr(skystrip, "UNITS", display_unit)

    assert skystrip._forecast_temperature(
        {"temperature": source_value, "temperatureUnit": source_unit}
    ) == expected


@pytest.mark.parametrize(
    "period",
    [
        {},
        {"temperature": None, "temperatureUnit": "F"},
        {"temperature": True, "temperatureUnit": "F"},
        {"temperature": 72, "temperatureUnit": "K"},
        {"temperature": float("inf"), "temperatureUnit": "F"},
    ],
)
def test_forecast_temperature_rejects_missing_or_unsupported_values(period):
    assert skystrip._forecast_temperature(period) is None

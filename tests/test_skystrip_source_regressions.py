"""Malformed station rows and DST folds must not change weather truth."""

import sys
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))
from apps.skystrip_app import model as sky_model
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app import weather_state as sky_weather_state
from apps.skystrip_app import weather_timeline as sky_weather_timeline


def test_history_does_not_borrow_precipitation_from_the_other_repeated_hour():
    zone = ZoneInfo("America/New_York")
    first = datetime(2026, 11, 1, 1, 30, tzinfo=zone, fold=0)
    second = first.replace(fold=1)
    rain = {"presentWeather": [{"weather": "rain", "intensity": "heavy"}]}
    assert sky_weather.observed_precip_at([(first, rain)], second) is None
    assert sky_weather.observed_precip_at([(second, rain)], first) is None
    assert sky_weather.observed_precip_at([(first, rain)], first)["tier"] == 2


def test_hourly_rows_stay_in_instant_order_through_a_clock_fold():
    times = ["2026-11-01T05:45Z", "2026-11-01T06:15Z"]
    rows = sky_weather_timeline.parse_hourly({
        "time": times, "temperature_2m": [10, 11], "cloud_cover": [20, 30],
    }, tz=ZoneInfo("America/New_York"))
    assert [when.astimezone(timezone.utc).isoformat() for when, _ in rows] == [
        "2026-11-01T05:45:00+00:00", "2026-11-01T06:15:00+00:00",
    ]


@pytest.mark.parametrize("weather", [[], {}, 1, None])
def test_malformed_phenomenon_does_not_discard_valid_temperature(weather):
    parsed = sky_weather._parse_obs({
        "presentWeather": [{"weather": weather}],
        "temperature": {"value": 12},
    })
    assert parsed["temp_c"] == 12
    assert "rain" not in parsed


@pytest.mark.parametrize("present", [42, {"weather": "rain"}, "rain"])
def test_history_ignores_malformed_weather_envelopes(present):
    assert sky_weather.obs_precipitation({"presentWeather": present}) is None


def test_history_ignores_malformed_intensity():
    assert sky_weather.obs_precipitation({
        "presentWeather": [{"weather": "rain", "intensity": []}],
    })["tier"] == 1


def test_history_window_uses_elapsed_time_across_the_spring_clock_change():
    zone = ZoneInfo("America/New_York")
    observed = datetime(2026, 3, 8, 1, 55, tzinfo=zone)
    target = datetime(2026, 3, 8, 3, 5, tzinfo=zone)
    rain = {"presentWeather": [{"weather": "rain", "intensity": "heavy"}]}
    assert sky_weather.observed_precip_at([(observed, rain)], target)["tier"] == 2


def test_a_future_fold_uses_forecast_instead_of_history(monkeypatch):
    zone = ZoneInfo("America/New_York")
    now = datetime(2026, 11, 1, 1, 45, tzinfo=zone, fold=0)
    target = datetime(2026, 11, 1, 1, 15, tzinfo=zone, fold=1)

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz)

    monkeypatch.setattr(sky_weather_timeline, "datetime", Clock)
    monkeypatch.setattr(sky_settings, "TZ", zone)
    state = sky_model.SkyState()
    state.hourly = sky_weather_timeline.parse_hourly({
        "time": [target.isoformat()], "temperature_2m": [12],
        "cloud_cover": [80], "precipitation_probability": [90],
        "precipitation": [5], "weather_code": [63],
    }, tz=zone)
    assert sky_weather_timeline.wx_at(state, target).rain


def test_extreme_numeric_input_does_not_discard_other_observation_fields():
    parsed = sky_weather._parse_obs({
        "temperature": {"value": 10 ** 400},
        "windSpeed": {"value": 12},
    })
    assert parsed == {"wind_kmh": 12}


@pytest.mark.parametrize("timestamp", [
    "0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00",
])
def test_timezone_overflow_is_an_invalid_source_timestamp(timestamp):
    assert sky_weather_state._source_datetime(timestamp) is None
    rows = sky_weather_timeline.parse_hourly({
        "time": [timestamp, "2026-09-01T12:00Z"],
        "temperature_2m": [10, 12], "cloud_cover": [20, 30],
    }, tz=timezone.utc)
    assert len(rows) == 1
    assert rows[0][1]["temp"] == 12

"""The hourly table is validated and swapped atomically.

Every other source in this app goes through `_finite_number` and an atomic
last-good swap. `state.hourly` was built by direct index into nine parallel
arrays, and `wx_at` papered over the gaps with invented constants — 20 degrees,
50% humidity, 16 km of visibility — which render exactly as confidently as
measurements do.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import skystrip  # noqa: E402

EASTERN = ZoneInfo("America/New_York")


def payload(times, **columns):
    body = {"time": list(times)}
    n = len(body["time"])
    defaults = {
        "temperature_2m": [20.0] * n, "cloud_cover": [10.0] * n,
        "precipitation": [0.0] * n, "weather_code": [0.0] * n,
        "precipitation_probability": [0.0] * n, "wind_speed_10m": [5.0] * n,
        "wind_direction_10m": [90.0] * n, "relative_humidity_2m": [50.0] * n,
        "visibility": [16000.0] * n, "snow_depth": [0.0] * n,
    }
    defaults.update(columns)
    body.update(defaults)
    return body


# --- the fold --------------------------------------------------------------


def test_the_autumn_fall_back_keeps_two_distinct_hours():
    """1 a.m. happens twice on the US fall-back date. Asking Open-Meteo for
    local time and attaching the zone afterwards gave both the same key with
    fold=0, so the Time Machine's nearest-row search returned the wrong hour
    for an hour a year. Asking for UTC and converting keeps them apart."""
    # 2026-11-01: EDT (UTC-4) becomes EST (UTC-5) at 06:00 UTC.
    times = ["2026-11-01T04:00", "2026-11-01T05:00",
             "2026-11-01T06:00", "2026-11-01T07:00"]
    rows = skystrip.parse_hourly(
        payload(times, temperature_2m=[10.0, 11.0, 12.0, 13.0]), tz=EASTERN)
    local_hours = [when.hour for when, _ in rows]
    assert local_hours == [0, 1, 1, 2], local_hours
    # The two 1 a.m. rows are one real hour apart and carry different data.
    first, second = rows[1], rows[2]
    # Same-zone datetime subtraction is wall-clock arithmetic and ignores
    # fold, so compare as instants — which is exactly why wx_at has to do the
    # same thing.
    assert (second[0].astimezone(timezone.utc)
            - first[0].astimezone(timezone.utc)) == timedelta(hours=1)
    assert first[1]["temp"] != second[1]["temp"]
    assert first[0].utcoffset() != second[0].utcoffset()


def test_the_scrubber_can_tell_the_two_repeated_hours_apart():
    """The other half of the fold fix. Storing distinct instants is not
    enough: `min(hourly, key=lambda r: abs(r[0] - target))` compared two
    same-zone aware datetimes, which Python does naively, so both 01:00 rows
    measured zero seconds from a 01:00 target and the first always won."""
    times = ["2026-11-01T04:00", "2026-11-01T05:00",
             "2026-11-01T06:00", "2026-11-01T07:00"]
    rows = skystrip.parse_hourly(
        payload(times, temperature_2m=[10.0, 11.0, 12.0, 13.0]), tz=EASTERN)
    state = _state_with(rows)

    # Production targets are same-zone and carry fold: the timeline start is
    # `_half_hour_floor(datetime.now(TZ)) - 24h`. A UTC target would not
    # exercise this — cross-zone subtraction already uses instants.
    early = datetime(2026, 11, 1, 1, 0, tzinfo=EASTERN)            # EDT
    late = datetime(2026, 11, 1, 1, 0, fold=1, tzinfo=EASTERN)     # EST
    assert early.utcoffset() != late.utcoffset(), "fixture must straddle the fold"
    assert skystrip.wx_at(state, early).temp_c == 11.0
    assert skystrip.wx_at(state, late).temp_c == 12.0


def test_rows_are_returned_in_time_order():
    times = ["2026-06-15T12:00", "2026-06-15T10:00", "2026-06-15T11:00"]
    rows = skystrip.parse_hourly(payload(times), tz=timezone.utc)
    assert [w.hour for w, _ in rows] == [10, 11, 12]


# --- unknown is dropped, not defaulted -------------------------------------


def test_an_hour_with_no_temperature_is_dropped():
    """Open-Meteo returns null past its range. Such an hour is absent, not
    weak — wx_at should fall back to live rather than show a number."""
    times = ["2026-06-15T10:00", "2026-06-15T11:00"]
    rows = skystrip.parse_hourly(
        payload(times, temperature_2m=[20.0, None]), tz=timezone.utc)
    assert len(rows) == 1 and rows[0][0].hour == 10


def test_an_hour_with_no_cloud_cover_is_dropped():
    times = ["2026-06-15T10:00", "2026-06-15T11:00"]
    rows = skystrip.parse_hourly(
        payload(times, cloud_cover=[10.0, None]), tz=timezone.utc)
    assert len(rows) == 1


def test_out_of_range_values_are_refused_not_clamped():
    times = ["2026-06-15T10:00"]
    rows = skystrip.parse_hourly(
        payload(times, temperature_2m=[500.0]), tz=timezone.utc)
    assert rows == []


def test_a_string_where_a_number_belongs_is_refused():
    times = ["2026-06-15T10:00", "2026-06-15T11:00"]
    rows = skystrip.parse_hourly(
        payload(times, temperature_2m=["warm", 20.0]), tz=timezone.utc)
    assert len(rows) == 1 and rows[0][0].hour == 11


def test_a_secondary_column_may_be_unknown_without_losing_the_hour():
    times = ["2026-06-15T10:00"]
    rows = skystrip.parse_hourly(
        payload(times, visibility=[None], relative_humidity_2m=[None]),
        tz=timezone.utc)
    assert len(rows) == 1
    assert rows[0][1]["vis"] is None and rows[0][1]["rh"] is None


def test_a_missing_column_entirely_is_tolerated():
    """An older cached response has no snow_depth column at all."""
    body = payload(["2026-06-15T10:00"])
    del body["snow_depth"]
    rows = skystrip.parse_hourly(body, tz=timezone.utc)
    assert len(rows) == 1 and rows[0][1]["snow_depth"] is None


def test_an_unparseable_timestamp_drops_only_its_own_row():
    times = ["not-a-time", "2026-06-15T11:00"]
    rows = skystrip.parse_hourly(payload(times), tz=timezone.utc)
    assert len(rows) == 1


def test_the_row_count_is_bounded():
    times = [f"2026-06-15T{h:02d}:00" for h in range(24)] * 20
    rows = skystrip.parse_hourly(payload(times), tz=timezone.utc)
    assert len(rows) <= skystrip.HOURLY_MAX_ROWS


# --- a malformed envelope leaves last-good alone ---------------------------


@pytest.mark.parametrize("bad", [None, [], "hourly", {"time": "not-a-list"}, {}])
def test_a_malformed_envelope_raises_rather_than_returning_a_partial(bad):
    with pytest.raises(ValueError):
        skystrip.parse_hourly(bad, tz=timezone.utc)


# --- wx_at no longer invents ------------------------------------------------


def _state_with(rows, live=None):
    state = skystrip.SkyState()
    state.hourly = rows
    if live is not None:
        state.weather = live
    return state


def test_wx_at_falls_back_to_live_for_an_hour_it_has_no_row_for():
    live = skystrip.WeatherState(temp_c=31.0, humidity=77.0)
    target = datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc)
    state = _state_with([], live)
    assert skystrip.wx_at(state, target) is live


def test_wx_at_uses_live_values_for_columns_the_model_lacks():
    """Not 50% humidity and 16 km — those were constants that rendered as
    confidently as a measurement."""
    rows = skystrip.parse_hourly(
        payload(["2026-06-15T10:00"], relative_humidity_2m=[None],
                visibility=[None], wind_speed_10m=[None]),
        tz=timezone.utc)
    live = skystrip.WeatherState(humidity=88.0, visibility_m=1200.0,
                                 wind_kmh=42.0)
    state = _state_with(rows, live)
    got = skystrip.wx_at(state, datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc))
    assert got.humidity == 88.0
    assert got.visibility_m == 1200.0
    assert got.wind_kmh == 42.0
    assert got.temp_c == 20.0            # the model's own value, not live


def test_wx_at_prefers_the_model_over_live_when_it_has_data():
    rows = skystrip.parse_hourly(
        payload(["2026-06-15T10:00"], temperature_2m=[3.0]), tz=timezone.utc)
    state = _state_with(rows, skystrip.WeatherState(temp_c=31.0))
    got = skystrip.wx_at(state, datetime(2026, 6, 15, 10, 0, tzinfo=timezone.utc))
    assert got.temp_c == 3.0

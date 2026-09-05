"""The words the bar says out loud remain truthful and bounded.

`_compose_report` was 133 of 135 statements uncovered. Every existing test
that touches the report monkeypatches it away — `test_skystrip_report_ux.py`
replaces it in three places — so roughly two hundred lines of branching text
generation had never been executed by a test, despite being spoken aloud and
hashed into a device filename.

These tests assert the produced sentences, not that a function was called.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from apps.skystrip_app import eclipse as sky_eclipse
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.audio import report_facts as sky_audio_report_facts
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain
from datetime import timezone


def local(hour: int, minute: int = 0, day: int = 15, month: int = 6):
    return datetime(2026, month, day, hour, minute, tzinfo=sky_settings.TZ)


def wx(**kwargs):
    return sky_weather.WeatherState(**kwargs)


def report(wx_state, forecast=None, when=None, hourly=None):
    return sky_audio_report_plain._compose_report(
        wx_state, forecast, when or local(14), hourly)


@pytest.fixture(autouse=True)
def plain_fahrenheit(monkeypatch):
    """Both style knobs are module constants read at import."""
    monkeypatch.setattr(sky_settings, "STYLE", "plain")
    monkeypatch.setattr(sky_settings, "UNITS", "f")


# --- greeting --------------------------------------------------------------


@pytest.mark.parametrize("hour,greeting", [
    (5, "Good morning"), (11, "Good morning"),
    (12, "Good afternoon"), (16, "Good afternoon"),
    (17, "Good evening"), (21, "Good evening"),
    (22, "Still up"), (3, "Still up"), (4, "Still up"),
])
def test_the_greeting_follows_the_local_clock(hour, greeting):
    assert report(wx(), when=local(hour)).startswith(greeting)


# --- conditions, in precedence order ---------------------------------------


@pytest.mark.parametrize("state,phrase", [
    (dict(severe=True, thunder=True, rain=True), "severe weather in the area"),
    (dict(thunder=True, rain=True), "thunderstorms"),
    (dict(snow=True, rain=True), "snow coming down"),
    (dict(rain=True), "rain"),
    (dict(cloud_frac=0.9), "overcast skies"),
    (dict(cloud_frac=0.6), "mostly cloudy skies"),
    (dict(cloud_frac=0.3), "partly cloudy skies"),
    (dict(cloud_frac=0.1), "clear skies"),
])
def test_conditions_are_reported_in_precedence_order(state, phrase):
    """Each row sets everything below it too, so this pins the ordering, not
    just that each phrase can be produced."""
    assert phrase in report(wx(**state))


def test_severe_outranks_every_other_condition():
    text = report(wx(severe=True, thunder=True, snow=True, rain=True,
                     cloud_frac=1.0))
    assert "severe weather in the area" in text
    assert "thunderstorms" not in text


# --- temperature and units -------------------------------------------------


def test_fahrenheit_is_the_default_conversion():
    assert "68 degrees" in report(wx(temp_c=20.0))


def test_celsius_is_reported_unconverted(monkeypatch):
    monkeypatch.setattr(sky_settings, "UNITS", "c")
    assert "20 degrees" in report(wx(temp_c=20.0))


def test_a_negative_temperature_survives_the_conversion():
    assert "-4 degrees" in report(wx(temp_c=-20.0))


# --- wind ------------------------------------------------------------------


@pytest.mark.parametrize("kmh,expected", [
    (60.0, "Properly windy"),
    (35.0, "Breezy"),
    (20.0, "A light breeze"),
])
def test_wind_tiers(kmh, expected):
    assert expected in report(wx(wind_kmh=kmh, wind_dir=90.0))


def test_a_calm_wind_is_not_mentioned():
    text = report(wx(wind_kmh=2.0, wind_dir=90.0))
    assert "breeze" not in text and "windy" not in text.lower()


def test_the_wind_direction_is_named_when_known():
    assert "out of the east" in report(wx(wind_kmh=60.0, wind_dir=90.0))


def test_an_unknown_wind_direction_is_omitted_not_invented():
    """`wind_dir` is None whenever no source published one. The skill's rule:
    unknown is a product state, not permission to invent a default — and due
    north is a plausible-looking invention."""
    text = report(wx(wind_kmh=60.0, wind_dir=None))
    assert "Properly windy" in text
    assert "out of the" not in text
    assert "north" not in text


def test_a_gentle_breeze_needs_a_direction_to_be_worth_saying():
    assert "breeze" not in report(wx(wind_kmh=20.0, wind_dir=None))


# --- humidity --------------------------------------------------------------


def test_muggy_needs_heat_and_humidity_and_no_rain():
    assert "Muggy" in report(wx(humidity=80.0, temp_c=26.0))
    assert "Muggy" not in report(wx(humidity=80.0, temp_c=15.0))
    assert "Muggy" not in report(wx(humidity=50.0, temp_c=26.0))
    assert "Muggy" not in report(wx(humidity=80.0, temp_c=26.0, rain=True))


# --- the NWS forecast period ----------------------------------------------


def _period(**kwargs):
    base = {"name": "Tonight", "temperature": 55, "temperatureUnit": "F",
            "isDaytime": False, "shortForecast": "Partly Cloudy",
            "probabilityOfPrecipitation": {"value": None}}
    base.update(kwargs)
    return base


def test_a_daytime_period_reports_a_high():
    text = report(wx(), forecast=[_period(name="Today", isDaytime=True,
                                          temperature=75)])
    assert "Heading for a high of 75 today" in text


def test_a_night_period_reports_a_low():
    assert "Down to 55 tonight" in report(wx(), forecast=[_period()])


def test_a_missing_forecast_temperature_does_not_invent_one():
    text = report(wx(), forecast=[_period(temperature=None)])
    assert "Looking ahead to tonight" in text
    assert "None" not in text


def test_an_unusable_temperature_unit_is_refused():
    """_forecast_temperature requires F or C; anything else is unknown."""
    text = report(wx(), forecast=[_period(temperatureUnit="K")])
    assert "Looking ahead to tonight" in text


@pytest.mark.parametrize("short,kind", [
    ("Chance Thunderstorms", "storms"),
    ("Snow Likely", "snow"),
    ("Rain Showers", "rain"),
])
def test_the_precipitation_kind_comes_from_the_short_forecast(short, kind):
    text = report(wx(), forecast=[_period(
        shortForecast=short, probabilityOfPrecipitation={"value": 60})])
    assert f"chance of {kind}" in text


def test_a_low_precipitation_probability_is_not_mentioned():
    text = report(wx(), forecast=[_period(
        probabilityOfPrecipitation={"value": 10})])
    assert "chance of" not in text


def test_a_null_precipitation_probability_is_treated_as_none():
    text = report(wx(), forecast=[_period(
        probabilityOfPrecipitation={"value": None})])
    assert "chance of" not in text


def test_an_empty_forecast_list_is_simply_omitted():
    text = report(wx(), forecast=[])
    assert "Heading for" not in text and "Down to" not in text


# --- the hourly outlook ----------------------------------------------------


def hourly_rows(start, count=8, **row):
    base = {"temp": 20.0, "cloud": 10, "precip": 0.0, "prob": 0,
            "code": 0, "wind": 5.0, "wdir": 90.0, "rh": 50.0,
            "vis": 16000.0, "snow_depth": 0.0}
    base.update(row)
    return [(start + timedelta(hours=i), dict(base)) for i in range(count)]


def test_a_likely_soaking_names_the_hour_and_the_tool():
    rows = hourly_rows(local(10), prob=70, code=61)
    text = report(wx(), when=local(10), hourly=rows)
    assert "70 percent chance of rain" in text
    assert "Keep the umbrella handy." not in text     # plain style stays terse


def test_snow_gets_a_shovel_in_the_chicago_style(monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", "chicago")
    rows = hourly_rows(local(10), prob=70, code=73)
    text = report(wx(), when=local(10), hourly=rows)
    assert "chance of snow" in text
    assert "Keep the shovel handy." in text


def test_a_slight_chance_is_hedged():
    rows = hourly_rows(local(10), prob=25, code=61)
    assert "slight chance of rain" in report(wx(), when=local(10), hourly=rows)


def test_a_clear_rest_of_day_says_so():
    rows = hourly_rows(local(10), prob=0, cloud=5)
    assert "Staying clear the rest of the day" in report(
        wx(), when=local(10), hourly=rows)


def test_a_cloudy_but_dry_rest_of_day_is_distinguished():
    rows = hourly_rows(local(10), prob=0, cloud=80)
    assert "Dry the rest of the day" in report(
        wx(), when=local(10), hourly=rows)


def test_late_evening_talks_about_the_rest_of_the_evening():
    rows = hourly_rows(local(18), count=5, prob=0, cloud=5)
    assert "rest of the evening" in report(wx(), when=local(18), hourly=rows)


def test_after_the_day_runs_out_it_talks_about_tomorrow():
    """Fewer than three hours left today, so the report pivots."""
    rows = hourly_rows(local(7, day=16), count=12, prob=50, code=61, temp=25.0)
    text = report(wx(), when=local(23, minute=30), hourly=rows)
    assert "Tomorrow heads for" in text
    assert "50 percent chance of rain" in text


def test_tomorrow_can_be_dry():
    rows = hourly_rows(local(7, day=16), count=12, prob=0, temp=25.0)
    text = report(wx(), when=local(23, minute=30), hourly=rows)
    assert "Tomorrow heads for 77 and looks dry" in text


def test_too_few_hours_and_no_tomorrow_is_simply_silent():
    text = report(wx(), when=local(23, minute=30), hourly=[])
    assert "Tomorrow" not in text and "rest of the" not in text


def test_the_hourly_outlook_suppresses_the_forecast_umbrella():
    """One umbrella mention per broadcast: if the hourly table is going to
    name precipitation odds, the NWS period line stays out of it."""
    rows = hourly_rows(local(10), prob=70, code=61)
    text = report(wx(), forecast=[_period(
        probabilityOfPrecipitation={"value": 80})],
        when=local(10), hourly=rows)
    assert text.count("percent chance") == 1


# --- style -----------------------------------------------------------------


def test_the_chicago_style_signs_off(monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", "chicago")
    assert report(wx()).endswith("And that's the picture, folks.")


def test_the_plain_style_does_not(monkeypatch):
    assert "folks" not in report(wx())


def test_the_chicago_lake_breeze_needs_an_easterly(monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", "chicago")
    assert "Cooler by the lake" in report(wx(wind_kmh=25.0, wind_dir=90.0))
    assert "Cooler by the lake" not in report(wx(wind_kmh=25.0, wind_dir=270.0))


# --- the whole string ------------------------------------------------------


def test_the_report_is_speakable_ascii():
    """It goes to a TTS engine and its hash becomes a device filename."""
    rows = hourly_rows(local(10), prob=70, code=95)
    text = report(wx(temp_c=28.0, humidity=85.0, wind_kmh=60.0, wind_dir=45.0,
                     thunder=True),
                  forecast=[_period(isDaytime=True, temperature=90)],
                  when=local(10), hourly=rows)
    assert text.isascii(), text
    assert text.strip() == text
    assert "  " not in text
    assert "None" not in text and "{" not in text


def test_no_condition_leaves_an_empty_report():
    for hour in range(0, 24, 3):
        for state in ({}, {"rain": True}, {"snow": True}, {"severe": True}):
            text = report(wx(**state), when=local(hour))
            assert len(text) > 20, (hour, state)
            assert text.endswith(".")


# --- eclipse heads-up facts ------------------------------------------------


def test_a_heads_up_candidate_without_peak_geometry_is_omitted(monkeypatch):
    """One incomplete candidate must not abort the rest of the search."""
    now = local(22)
    now_utc = now.astimezone(timezone.utc)
    begin = now_utc + timedelta(hours=1)

    class Candidate:
        def __init__(self, kind, greatest):
            self.kind = kind
            self.greatest = greatest

        def contact(self, phase):
            assert phase == "partial"
            return begin, begin + timedelta(hours=2)

    incomplete = Candidate("partial", begin + timedelta(minutes=30))
    complete = Candidate("total", begin + timedelta(minutes=45))
    candidates = [incomplete, complete]
    state_calls = []

    monkeypatch.setattr(
        sky_eclipse, "eclipses_near", lambda _when: candidates)
    monkeypatch.setattr(sky_eclipse, "visible_state",
        lambda when, _observer: None if when == now_utc else object(),
    )

    class Peak:
        obscuration = 0.42

    def state_at(_when, *, eclipse):
        state_calls.append(eclipse)
        return None if eclipse is incomplete else Peak()

    monkeypatch.setattr(sky_eclipse, "state_at", state_at)

    facts = sky_audio_report_facts._eclipse_report_facts(now)

    assert state_calls == candidates
    assert facts is not None
    assert facts["phase"] == "total"
    assert facts["pct"] == 42


# --- which forecast period the report is allowed to talk about -------------


def _periods(day: int = 15):
    """An NWS pair shaped like the real feed: the afternoon ends at six.

    Times are built from the same clock the test reads, so the fixture
    cannot drift from `sky_settings.TZ`.
    """
    return [
        {"name": "This Afternoon", "isDaytime": True, "temperature": 81,
         "temperatureUnit": "F", "shortForecast": "Sunny",
         "startTime": local(12, day=day).isoformat(),
         "endTime": local(18, day=day).isoformat(),
         "probabilityOfPrecipitation": {"value": 0}},
        {"name": "Tonight", "isDaytime": False, "temperature": 62,
         "temperatureUnit": "F", "shortForecast": "Clear",
         "startTime": local(18, day=day).isoformat(),
         "endTime": local(6, day=day + 1).isoformat(),
         "probabilityOfPrecipitation": {"value": 0}},
    ]


@pytest.mark.parametrize("style", ["plain", "chicago", "genz"])
def test_the_report_stops_naming_a_period_that_is_nearly_over(
    style, monkeypatch,
):
    """At half five it said "evening" and then "this Afternoon".

    NWS's periods[0] is the CURRENT period, and the report phrased it as
    something still ahead ("Later we're going for 81 this afternoon").
    The greeting flips to evening at five; the NWS afternoon runs to six.
    In that hour the bar greeted you with one and forecast the other, for
    a high that had already happened.
    """
    monkeypatch.setattr(sky_settings, "STYLE", style)
    text = report(wx(temp_c=25.0), forecast=_periods(), when=local(17, 30))
    assert "fternoon" not in text, (
        f"still forecasting an afternoon that ends in 30 minutes: {text}")
    assert "onight" in text, f"never moved on to the next period: {text}"
    assert "62" in text, f"lost the temperature it should be naming: {text}"


@pytest.mark.parametrize("style", ["plain", "chicago", "genz"])
def test_the_current_period_is_still_used_while_it_has_time_left(
    style, monkeypatch,
):
    """The fix must not skip ahead all day — at one o'clock the afternoon
    is exactly what the report should be talking about."""
    monkeypatch.setattr(sky_settings, "STYLE", style)
    text = report(wx(temp_c=25.0), forecast=_periods(), when=local(13, 0))
    assert "fternoon" in text, f"skipped the live period: {text}"
    assert "81" in text, text


@pytest.mark.parametrize("stamp", [None, "", "not-a-timestamp", 12345])
def test_an_unusable_period_time_falls_back_instead_of_crashing(stamp):
    """A missing or malformed endTime must not take the report down."""
    periods = _periods()
    periods[0]["endTime"] = stamp
    text = report(wx(temp_c=25.0), forecast=periods, when=local(17, 30))
    assert "81" in text or "62" in text, text

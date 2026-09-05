"""Guards for weather-aware time scrubbing.

The fixtures are not invented. They are the real 2026-08-09 event at the
configured station: a two-hour storm that Open-Meteo's past_days rows recorded
as overcast with 0.00mm. Scrubbing backward over that afternoon showed a dry
sky, which is the bug these tests exist to keep fixed.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import model as sky_model
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app import weather_state as sky_weather_state
from apps.skystrip_app import weather_timeline as sky_weather_timeline
from apps.skystrip_app.render import precipitation as sky_render_precipitation

TZ = sky_settings.TZ


def _obs(when, description, present, **props):
    """One station observation, shaped like the NWS feed."""
    return {
        "timestamp": when.astimezone(timezone.utc).isoformat(),
        "textDescription": description,
        "presentWeather": present,
        **props,
    }


def _rain(intensity):
    return [{"intensity": intensity, "modifier": None, "weather": "rain",
             "rawString": "RA"}]


def _hourly_row(**kw):
    """An Open-Meteo hourly row. Defaults are the overcast-and-dry reading
    the model gave during the real storm."""
    row = {"temp": 21.0, "cloud": 100, "precip": 0.0, "prob": 27, "code": 3,
           "wind": 8.0, "wdir": 180.0, "rh": 90.0, "vis": 16000.0,
           "snow_depth": 0.0}
    row.update(kw)
    return row


# --- observation parsing ----------------------------------------------------

def test_metar_intensity_maps_onto_the_rain_tiers():
    cases = [
        ("Light Rain", _rain("light"), 0),
        ("Rain", _rain(None), 1),
        ("Heavy Rain", _rain("heavy"), 2),
    ]
    for description, present, tier in cases:
        got = sky_weather.obs_precipitation(
            {"textDescription": description, "presentWeather": present})
        assert got["tier"] == tier, description
        assert got["rain"] and not got["snow"]


def test_the_heavy_thunderstorm_reads_as_a_downpour():
    """The actual 14:35 observation from the day this was written."""
    got = sky_weather.obs_precipitation({
        "textDescription": "Heavy Thunderstorms and Heavy Rain and Fog/Mist",
        "presentWeather": [
            {"intensity": "heavy", "weather": "rain", "rawString": "+RA"},
            {"intensity": None, "weather": "fog_mist", "rawString": "BR"},
        ]})
    assert got["tier"] == 2
    assert got["thunder"] is True
    assert got["rain"] is True


def test_fog_alone_is_not_precipitation():
    assert sky_weather.obs_precipitation({
        "textDescription": "Fog/Mist",
        "presentWeather": [{"intensity": None, "weather": "fog_mist"}]}) is None
    assert sky_weather.obs_precipitation(
        {"textDescription": "Mostly Cloudy", "presentWeather": []}) is None


def test_snow_is_told_apart_from_rain():
    got = sky_weather.obs_precipitation({
        "textDescription": "Light Snow",
        "presentWeather": [{"intensity": "light", "weather": "snow"}]})
    assert got["snow"] and not got["rain"]


# --- slot aggregation -------------------------------------------------------

def test_the_heaviest_observation_in_a_slot_wins():
    """A three-minute downpour inside a half-hour slot is the thing a person
    remembers. Nearest-in-time would average it down to light rain."""
    slot = datetime(2026, 8, 9, 14, 30, tzinfo=TZ)
    history = [
        (slot - timedelta(minutes=1), _obs(slot, "Light Rain", _rain("light"))),
        (slot + timedelta(minutes=5), _obs(slot, "Heavy Rain", _rain("heavy"))),
        (slot + timedelta(minutes=9), _obs(slot, "Rain", _rain(None))),
    ]
    got = sky_weather.observed_precip_at(history, slot)
    assert got["tier"] == 2, "the nearest reading won instead of the heaviest"


def test_observations_outside_the_window_are_ignored():
    slot = datetime(2026, 8, 9, 14, 30, tzinfo=TZ)
    far = slot + timedelta(minutes=40)
    history = [(far, _obs(far, "Heavy Rain", _rain("heavy")))]
    assert sky_weather.observed_precip_at(history, slot) is None


def test_no_observation_means_unknown_not_dry():
    """None is the signal for 'nothing covers this moment'. The caller draws
    nothing either way, but it must never fall through to the hourly table."""
    slot = datetime(2026, 8, 9, 14, 30, tzinfo=TZ)
    assert sky_weather.observed_precip_at([], slot) is None
    assert sky_weather.observed_precip_at(None, slot) is None


# --- forecast gate ----------------------------------------------------------

def test_likelihood_gates_precipitation():
    below = sky_weather.forecast_precip(_hourly_row(prob=39, code=3))
    at = sky_weather.forecast_precip(_hourly_row(prob=40, code=3))
    assert below is None
    assert at is not None and at["rain"]


def test_likelihood_does_not_decide_intensity():
    """A 90% chance of drizzle must draw drizzle, not a downpour. This is the
    whole reason probability gates and accumulation sizes."""
    drizzle = sky_weather.forecast_precip(_hourly_row(prob=90, precip=0.3))
    downpour = sky_weather.forecast_precip(_hourly_row(prob=45, precip=8.0))
    assert drizzle["tier"] == 0
    assert downpour["tier"] == 2


def test_a_gate_passed_on_likelihood_alone_draws_the_lightest_thing():
    """prob >= 40 with no expected accumulation is the common case, and
    overstating it would put a downpour on a maybe."""
    got = sky_weather.forecast_precip(_hourly_row(prob=53, precip=0.0))
    assert got["tier"] == 0


def test_a_precipitation_code_wins_regardless_of_probability():
    got = sky_weather.forecast_precip(_hourly_row(prob=0, code=65, precip=7.9))
    assert got is not None and got["rain"] and got["tier"] == 2


def test_freezing_temperatures_turn_a_bare_gate_into_snow():
    """Probability carries no precipitation type, so temperature decides."""
    got = sky_weather.forecast_precip(_hourly_row(prob=60, code=3, temp=-4.0))
    assert got["snow"] and not got["rain"]


# --- wx_at end to end -------------------------------------------------------

def _state(hourly=None, history=None):
    state = sky_model.SkyState()
    state.hourly = hourly
    state.obs_history = history
    return state


def test_a_past_storm_scrubs_as_a_storm():
    """The regression this whole change exists for."""
    now = datetime.now(TZ)
    past = now - timedelta(hours=3)
    state = _state(
        hourly=[(past, _hourly_row())],       # model said overcast, 0.00mm
        history=[(past, _obs(
            past, "Heavy Thunderstorms and Heavy Rain",
            [{"intensity": "heavy", "weather": "rain"}]))])
    wx = sky_weather_timeline.wx_at(state, past)
    assert wx.rain, "a confirmed storm scrubbed as a dry sky"
    assert wx.rain_tier == 2
    assert wx.thunder


def test_the_model_never_speaks_for_the_past():
    """Open-Meteo's past rows reported 27% and 0.00mm through a real storm.
    With no observation covering the moment, nothing may be drawn -- and the
    hourly row must not be consulted to fill the gap either way."""
    now = datetime.now(TZ)
    past = now - timedelta(hours=3)
    state = _state(hourly=[(past, _hourly_row(prob=50, precip=5.0, code=65))],
                   history=[])
    wx = sky_weather_timeline.wx_at(state, past)
    assert not wx.rain and not wx.snow and not wx.thunder


def test_a_future_hour_uses_the_forecast():
    now = datetime.now(TZ)
    ahead = now + timedelta(hours=3)
    state = _state(hourly=[(ahead, _hourly_row(prob=45, precip=0.0))],
                   history=[])
    assert sky_weather_timeline.wx_at(state, ahead).rain


def test_a_future_hour_below_the_threshold_stays_dry():
    now = datetime.now(TZ)
    ahead = now + timedelta(hours=3)
    state = _state(hourly=[(ahead, _hourly_row(prob=25, precip=0.0))],
                   history=[])
    assert not sky_weather_timeline.wx_at(state, ahead).rain


def test_non_precipitation_fields_still_come_from_the_model():
    """Only precipitation changed source. Cloud and temperature are the
    model's job in both directions, and the station record is sparser."""
    now = datetime.now(TZ)
    past = now - timedelta(hours=3)
    state = _state(hourly=[(past, _hourly_row(cloud=88, temp=17.5))],
                   history=[])
    wx = sky_weather_timeline.wx_at(state, past)
    assert wx.cloud_frac == pytest.approx(0.88)
    assert wx.temp_c == pytest.approx(17.5)


# --- ingestion is bounded and validated -------------------------------------

def test_history_ingestion_rejects_malformed_payloads():
    start = datetime.now(timezone.utc) - timedelta(hours=26)
    for payload in (None, [], {}, {"features": "nope"}, {"features": None}):
        with pytest.raises(ValueError):
            sky_weather_timeline._parse_obs_history(payload, start)


def test_history_ingestion_is_bounded():
    """Record count is remote input and it drives a scan for all 97 slots."""
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=26)
    feats = [{"properties": _obs(now - timedelta(minutes=i % 600),
                                 "Light Rain", _rain("light"))}
             for i in range(sky_weather.OBS_HISTORY_MAX + 250)]
    rows = sky_weather_timeline._parse_obs_history({"features": feats}, start)
    assert len(rows) <= sky_weather.OBS_HISTORY_MAX


def test_history_ingestion_drops_junk_rows_without_failing():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=26)
    good = {"properties": _obs(now - timedelta(hours=1), "Rain", _rain(None))}
    payload = {"features": [
        good, None, {}, {"properties": None},
        {"properties": {"timestamp": "not-a-date"}},
        {"properties": {"timestamp": None}},
        # older than the window, and implausibly far in the future
        {"properties": _obs(now - timedelta(days=9), "Rain", _rain(None))},
        {"properties": _obs(now + timedelta(hours=6), "Rain", _rain(None))},
    ]}
    rows = sky_weather_timeline._parse_obs_history(payload, start)
    assert len(rows) == 1


def test_history_rows_come_back_in_time_order():
    now = datetime.now(timezone.utc)
    start = now - timedelta(hours=26)
    times = [now - timedelta(hours=h) for h in (2, 20, 9, 1)]
    payload = {"features": [
        {"properties": _obs(t, "Rain", _rain(None))} for t in times]}
    rows = sky_weather_timeline._parse_obs_history(payload, start)
    assert [r[0] for r in rows] == sorted(r[0] for r in rows)


def test_the_live_path_still_rejects_stale_timestamps():
    """max_age_s defaults to the weather lease. Widening it for history must
    not quietly widen it for current conditions."""
    now = datetime.now(timezone.utc)
    old = (now - timedelta(hours=5)).isoformat()
    assert sky_weather_state._source_datetime(old, now=now) is None
    assert sky_weather_state._source_datetime(old, now=now, max_age_s=26 * 3600)


def test_a_thunderstorm_keeps_its_observed_intensity():
    """The scrubber's whole point is replaying what happened. Pinning every
    storm to tier 2 threw away the station's own intensity reading."""
    now = datetime.now(TZ)
    past = now - timedelta(hours=3)
    for intensity, want in (("light", 1), (None, 1), ("heavy", 2)):
        state = _state(
            hourly=[(past, _hourly_row())],
            history=[(past, _obs(past, "Thunderstorms and Rain", [
                {"intensity": intensity, "weather": "rain"}]))])
        wx = sky_weather_timeline.wx_at(state, past)
        assert wx.thunder
        assert sky_render_precipitation._rain_tier(wx) == want, intensity

"""Skystrip weather models and pure provider parsing.

Inputs, timezone and reference times are explicit; this module does not read
configuration, contact providers, draw, or import the app's runtime. Keep
source vocabularies and their validation here so live and historical weather
use the same interpretation. Runtime wrappers live in weather_state.py and
weather_timeline.py; they supply explicit clocks and timezone to this leaf.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timezone, tzinfo
from typing import TypedDict

WEATHER_LEASE_S = 2 * 3600
SOURCE_FUTURE_SKEW_S = 5 * 60
CLOUD_AMOUNT = {
    "CLR": 0.0,
    "SKC": 0.0,
    "FEW": 0.2,
    "SCT": 0.45,
    "BKN": 0.8,
    "OVC": 1.0,
    "VV": 1.0,
}

# Scrubbing forward is a forecast, so it is gated on likelihood rather than on
# a single deterministic run: Open-Meteo's hourly `precipitation` is 0.00mm for
# most hours even when the chance is above half, which is why 1 hour of 72 drew
# rain. 40% sits above the narration's 30% mention threshold, so the voice
# calling something "a slight chance" can never accompany a scene drawing rain.
PRECIP_LIKELY_PCT = 40
# Expected accumulation picks the tier. Likelihood decides WHETHER, amount
# decides HOW HARD -- a 90% chance of drizzle must draw drizzle.
PRECIP_TIER_MM = ((1.0, 0), (4.0, 1))  # above the last bound -> tier 2
# Scrubbing backward is history, and history must come from what was observed.
# Open-Meteo's past_days rows are model reanalysis: on 2026-08-09 they reported
# overcast with 0.00mm straight through a two-hour thunderstorm the station
# recorded as Heavy Thunderstorms and Heavy Rain. They are never consulted for
# precipitation in the past.
OBS_HISTORY_HOURS = 26  # the 24h scrub window, plus slack for gaps
# ~5-minute cadence over 26h is ~335 records, so this is headroom, not a
# squeeze. It is also the API's own ceiling: NWS 400s on limit > 500, and a
# 400 here would log a warning and quietly leave the past dry.
OBS_HISTORY_MAX = 500
OBS_SLOT_WINDOW_S = 900  # +-15 min: one half-hour timeline slot
# NWS observation intensity is structured, and maps 1:1 onto RAIN_TIERS.
OBS_INTENSITY_TIER = {"light": 0, None: 1, "heavy": 2}
# Every value below is from api.weather.gov's published `presentWeather`
# enum (36 values), not from what a mapping happened to expect. Audited
# 2026-08-27; `tests/test_skystrip_obs_vocabulary.py` pins the enum so a
# feed value we do not classify fails loudly instead of falling through to
# a silent default. That is how `ice_pellets` sat unmatched behind a search
# for "ice pellets" with a space in it.
OBS_SNOW_WORDS = {
    "snow",
    "snow_grains",
    "snow_pellets",
    "ice_pellets",
    "snow_showers",
    "sleet",
    "blowing_snow",
}
# `hail` rides with the liquid set on purpose. The scene has no distinct
# hail treatment, and the honest choice between "draw it as the heavy
# precipitation it is" and "draw nothing" is the former — drawing a clear
# sky through a hailstorm is the worse lie. It is also what the past-slot
# path already concluded, since hail is in OBS_PRECIP_WORDS but not in
# OBS_SNOW_WORDS; before this the two paths silently disagreed.
OBS_RAIN_WORDS = {
    "rain",
    "rain_showers",
    "drizzle",
    "freezing_rain",
    "freezing_drizzle",
    "hail",
}
# Falling precipitation, for the past Time Machine slots. Blowing snow is
# snow in the air rather than snow arriving, so it colours the live scene
# without counting as an observation that precipitation fell.
OBS_PRECIP_WORDS = (OBS_RAIN_WORDS | OBS_SNOW_WORDS) - {"blowing_snow"}
OBS_THUNDER_WORDS = {"thunderstorms"}
# Fog proper. The obscurations below are a separate axis: fog is condensed
# water and hugs the ground, while haze/smoke/dust/ash fill the whole air
# column and kill distance instead of pooling.
OBS_FOG_WORDS = {"fog", "fog_mist", "freezing_fog", "ice_fog"}

# Obscurations, grouped by the tint each one gets. Dust and sand share a
# tan on purpose: their real colours sit about 12% apart in red, and the
# panel cannot resolve under ~30% per channel, so shipping two would be a
# claim of distinguishability the hardware cannot honour.
OBS_OBSCURATION_WORDS = {
    "haze": {"haze"},
    "smoke": {"smoke"},
    "dust": {
        "dust",
        "blowing_dust",
        "dust_whirls",
        "dust_storm",
        "sand",
        "blowing_sand",
        "sand_storm",
    },
    "ash": {"volcanic_ash"},
}
# Most-obscuring first: a report of both smoke and haze is a smoke day.
OBS_OBSCURATION_ORDER = ("ash", "smoke", "dust", "haze")


@dataclass
class WeatherState:
    cloud_frac: float = 0.0
    rain: bool = False
    rain_tier: int = 1  # 0 drizzle / 1 rain / 2 downpour (radar dBZ tiers)
    snow: bool = False
    thunder: bool = False
    severe: bool = False  # active severe thunderstorm / tornado warning
    wind_kmh: float = 0.0
    temp_c: float = 20.0
    humidity: float = 50.0
    visibility_m: float = 16000.0
    snow_depth_m: float = 0.0  # settled snow, not falling snow (wx.snow)
    fog: bool = False  # reported fog, independent of the visibility number
    # "", "haze", "smoke", "dust" or "ash" — selects an airlight tint.
    obscuration: str = ""
    wind_dir: float | None = None  # meteorological degrees, FROM which it blows

    severe_event: str = ""  # the warning's name, for the display

    @property
    def stormy(self) -> bool:
        # Visuals follow OBSERVED station weather, never warnings —
        # a warned-but-still-sunny sky stays sunny; the alarm layer screams.
        return self.thunder


class WeatherUpdates(TypedDict, total=False):
    cloud_frac: float
    rain: bool
    rain_tier: int
    snow: bool
    thunder: bool
    severe: bool
    wind_kmh: float
    temp_c: float
    humidity: float
    visibility_m: float
    snow_depth_m: float
    fog: bool
    obscuration: str
    wind_dir: float | None
    severe_event: str


def _without_rain(updates: WeatherUpdates) -> WeatherUpdates:
    """Copy a typed weather update while leaving rain to its leased sources."""
    filtered = updates.copy()
    filtered.pop("rain", None)
    return filtered


def _finite_number(value, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _source_datetime(
    value,
    *,
    now: datetime | None = None,
    allow_naive_utc: bool = False,
    max_age_s: float | None = None,
) -> datetime | None:
    """Parse and bound a timestamp from a remote feed.

    `max_age_s` defaults to WEATHER_LEASE_S, which is what the live path
    wants: an observation older than the lease is not current weather. The
    history path passes its own window instead -- a record from twenty hours
    ago is exactly what it is asking for, not a stale reading.
    """
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not allow_naive_utc:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        parsed = parsed.astimezone(timezone.utc)
    except (ValueError, OverflowError):
        return None
    age = (current - parsed).total_seconds()
    limit = WEATHER_LEASE_S if max_age_s is None else max_age_s
    if age < -SOURCE_FUTURE_SKEW_S or age > limit:
        return None
    return parsed


def _weather_entries(value: object) -> list[dict] | None:
    """Keep usable phenomena; distinguish a dry list from an invalid one."""
    if not isinstance(value, list):
        return None
    entries = [
        entry
        for entry in value
        if isinstance(entry, dict)
        and isinstance(entry.get("weather"), str)
        and entry["weather"]
    ]
    # An explicitly empty list is affirmative dry evidence. A nonempty list
    # whose records are all malformed provides no evidence about rain/snow.
    return entries if entries or not value else None


def _parse_obs(props: object) -> WeatherUpdates:
    if not isinstance(props, dict):
        return {}
    description = props.get("textDescription")
    if isinstance(description, str):
        description_known = bool(description.strip())
        text = description.lower() if description_known else ""
    else:
        description_known = False
        text = ""
    present_value = _weather_entries(props.get("presentWeather"))
    present_known = present_value is not None
    try:
        present_raw = json.dumps(present_value).lower() if present_known else ""
    except (TypeError, ValueError):
        present_known = False
        present_raw = ""
    # The structured `weather` values are the authority; textDescription is
    # prose and only fills in when the envelope is absent. Substring-matching
    # the raw JSON was how `snow_showers` set rain (it contains "shower") and
    # how `ice_pellets` set nothing (the search string had a space in it).
    reported = (
        {
            entry.get("weather")
            for entry in (present_value or [])
            if isinstance(entry, dict)
        }
        if present_known
        else set()
    )

    out: WeatherUpdates = {}
    if description_known or present_known:
        # A valid empty presentWeather list is affirmative dry evidence. If
        # both phenomenon fields are missing/malformed, omit these keys instead
        # of manufacturing a station report of clear weather.
        text_snow = any(w in text for w in ("snow", "sleet", "ice pellets"))
        # "shower" only implies rain when it is not a snow shower — the whole
        # reason the old substring test double-counted.
        text_rain = (
            "rain" in text or "drizzle" in text or ("shower" in text and not text_snow)
        )
        out.update(
            {
                "rain": bool(reported & OBS_RAIN_WORDS) or text_rain,
                "snow": bool(reported & OBS_SNOW_WORDS) or text_snow,
                # api.weather.gov publishes a 36-value `weather` enum and that
                # is the contract; the METAR check behind it is a deliberate
                # belt-and-braces for thunder, which is the one phenomenon whose
                # absence changes the whole sky. Every populated observation
                # reachable during this audit was empty, so recall here was not
                # something the feed could be made to demonstrate.
                "thunder": (
                    bool(reported & OBS_THUNDER_WORDS)
                    or "thunder" in text
                    or '"ts' in present_raw
                ),
                "fog": bool(reported & OBS_FOG_WORDS) or "fog" in text,
                "obscuration": _obscuration_kind(reported, text),
            }
        )
    layers = props.get("cloudLayers")
    if isinstance(layers, list):
        amounts: list[float] = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            amount = layer.get("amount")
            if isinstance(amount, str) and amount in CLOUD_AMOUNT:
                amounts.append(CLOUD_AMOUNT[amount])
        if amounts:
            out["cloud_frac"] = max(amounts)
        elif any(word in text for word in ("clear", "fair", "sunny")):
            out["cloud_frac"] = 0.0
    fields = (
        ("windSpeed", "wind_kmh", 0.0, 500.0),
        ("windDirection", "wind_dir", 0.0, 360.0),
        ("temperature", "temp_c", -100.0, 70.0),
        ("relativeHumidity", "humidity", 0.0, 100.0),
        ("visibility", "visibility_m", 0.0, 200_000.0),
    )
    for source, target, low, high in fields:
        field = props.get(source)
        raw = field.get("value") if isinstance(field, dict) else None
        value = _finite_number(raw, low, high)
        if value is None:
            continue
        if target == "wind_kmh":
            out["wind_kmh"] = value
        elif target == "wind_dir":
            out["wind_dir"] = value
        elif target == "temp_c":
            out["temp_c"] = value
        elif target == "humidity":
            out["humidity"] = value
        else:
            out["visibility_m"] = value
    return out


def _obscuration_kind(reported: set, text: str) -> str:
    """Which obscuration is in the air, most-obscuring first.

    Open-Meteo cannot answer this: its `weather_code` is the WMO subset
    that omits 04-09 (smoke, haze, dust, sand), confirmed against the live
    endpoint on 2026-08-30. So this is NWS-only, and outside NWS point
    coverage the scene simply never shows an obscuration rather than
    inferring one from a visibility number that fog would also explain.
    """
    for kind in OBS_OBSCURATION_ORDER:
        if reported & OBS_OBSCURATION_WORDS[kind]:
            return kind
    # Prose fallback, for a station sending textDescription without the
    # structured envelope. Deliberately narrow: "dust" and "sand" are too
    # common as ordinary English to match on safely.
    for kind, word in (("ash", "volcanic ash"), ("smoke", "smoke"), ("haze", "haze")):
        if word in text:
            return kind
    return ""


def _wmo_phenomena(code) -> tuple[bool, bool, bool, bool]:
    """Rain, snow, thunder and fog from one Open-Meteo WMO weather code.

    45 and 48 are fog and depositing rime fog. They were absent here, so the
    only way fog could reach the scene was by inference from the visibility
    or humidity numbers — and a model reporting fog outright while visibility
    stayed above the threshold drew a clear morning.
    """
    parsed = _finite_number(code, 0, 99)
    if parsed is None:
        return False, False, False, False
    code_i = int(parsed)
    rain = 51 <= code_i <= 67 or 80 <= code_i <= 82
    snow = 71 <= code_i <= 77 or code_i in (85, 86)
    thunder = code_i in (95, 96, 99)
    fog = code_i in (45, 48)
    return rain, snow, thunder, fog


HOURLY_MAX_ROWS = 96 + 48  # past_days=1 + forecast_days=2, with slack


def parse_hourly(payload, *, tz: tzinfo) -> list:
    """Validate an Open-Meteo hourly table into (local time, row) pairs.

    Built complete, then returned for an atomic swap: a malformed table must
    leave the last good one untouched rather than half-replace it.

    Two things this fixes beyond the obvious.

    **The fold.** The request now asks for UTC and converts, instead of asking
    for local time and calling `.replace(tzinfo=TZ)` on a naive string. At the
    autumn fall-back that `.replace` gave both 01:00 CDT and 01:00 CST the same
    key with fold=0, so the Time Machine's nearest-row search would return the
    wrong hour for an hour a year. `_parse_obs_history` already converts from
    UTC; this now matches it.

    **Unknown rows are dropped, not defaulted.** Open-Meteo returns null for
    hours outside its range. A row without a usable time, temperature and cloud
    cover is not a weaker row, it is an absent one — and `wx_at` falls back to
    live conditions for a moment it has no model data for. That is the skill's
    rule: unknown is a product state, not permission to invent a default.
    """
    zone = tz
    if not isinstance(payload, dict):
        raise ValueError("hourly payload is not an object")
    times = payload.get("time")
    if not isinstance(times, list):
        raise ValueError("hourly payload has no time column")
    if len(times) > HOURLY_MAX_ROWS:
        times = times[:HOURLY_MAX_ROWS]

    def column(name):
        values = payload.get(name)
        return values if isinstance(values, list) else []

    columns = {
        name: column(name)
        for name in (
            "temperature_2m",
            "cloud_cover",
            "precipitation",
            "weather_code",
            "precipitation_probability",
            "wind_speed_10m",
            "wind_direction_10m",
            "relative_humidity_2m",
            "visibility",
            "snow_depth",
        )
    }

    def at(name, index, low, high):
        values = columns[name]
        if index >= len(values):
            return None
        return _finite_number(values[index], low, high)

    rows = []
    for i, raw_time in enumerate(times):
        if not isinstance(raw_time, str) or len(raw_time) > 64:
            continue
        try:
            when = datetime.fromisoformat(
                raw_time[:-1] + "+00:00" if raw_time.endswith(("Z", "z")) else raw_time
            )
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        try:
            local_when = when.astimezone(zone)
            local_when.astimezone(
                timezone.utc
            )  # ensure instant sorting is representable
        except (ValueError, OverflowError):
            continue
        temp = at("temperature_2m", i, -100.0, 70.0)
        cloud = at("cloud_cover", i, 0.0, 100.0)
        if temp is None or cloud is None:
            continue  # no model data for this hour; not a weak row
        rows.append(
            (
                local_when,
                {
                    "temp": temp,
                    "cloud": cloud,
                    "precip": at("precipitation", i, 0.0, 1000.0),
                    "prob": at("precipitation_probability", i, 0.0, 100.0),
                    "code": at("weather_code", i, 0.0, 99.0),
                    "wind": at("wind_speed_10m", i, 0.0, 500.0),
                    "wdir": at("wind_direction_10m", i, 0.0, 360.0),
                    "rh": at("relative_humidity_2m", i, 0.0, 100.0),
                    "vis": at("visibility", i, 0.0, 200_000.0),
                    "snow_depth": at("snow_depth", i, 0.0, 20.0),
                },
            )
        )
    rows.sort(key=lambda row: row[0].astimezone(timezone.utc))
    return rows


def _parse_obs_history(
    payload, start: datetime, now: datetime | None = None, *, tz: tzinfo
) -> list:
    """Validate a station observation feed into (local time, props) rows.

    Built complete, then returned for an atomic swap: a feed that turns out to
    be malformed must leave the last good history untouched rather than
    half-replace it. Bounded at ingestion -- record count is remote input, and
    it drives a per-slot scan for all 97 timeline frames.
    """
    if not isinstance(payload, dict):
        raise ValueError("observation payload is not an object")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("observation payload has no feature list")
    if len(features) > OBS_HISTORY_MAX:
        features = features[:OBS_HISTORY_MAX]
    rows = []
    for feature in features:
        props = (feature or {}).get("properties") if isinstance(feature, dict) else None
        if not isinstance(props, dict):
            continue
        when = _source_datetime(
            props.get("timestamp"), now=now, max_age_s=OBS_HISTORY_HOURS * 3600
        )
        if when is None or when < start:
            continue
        rows.append((when.astimezone(tz), props))
    rows.sort(key=lambda r: r[0].astimezone(timezone.utc))
    return rows


def obs_precipitation(props: dict) -> dict | None:
    """What a single station observation says was falling, and how hard.

    Separate from _parse_obs on purpose: that feeds `replace(WeatherState,
    **obs)` on the live path, so every key it returns must be a field there.
    This returns a tier the live path has no business inheriting -- live
    intensity belongs to resolve_rain and the radar.
    """
    if not isinstance(props, dict):
        return None
    present = _weather_entries(props.get("presentWeather"))
    entries = [
        entry for entry in (present or []) if entry["weather"] in OBS_PRECIP_WORDS
    ]
    if not entries:
        return None
    words = {e.get("weather") for e in entries}
    text = props.get("textDescription")
    text = text.lower() if isinstance(text, str) else ""
    try:
        raw = json.dumps(present or []).lower()
    except (TypeError, ValueError):
        raw = ""
    snow = bool(words & OBS_SNOW_WORDS)
    return {
        "rain": not snow,
        "snow": snow,
        "thunder": "thunder" in text or "thunder" in raw,
        # Heaviest wins within one observation too: "Heavy Rain and Fog/Mist"
        # is a downpour that happens to also be misty.
        "tier": max(
            OBS_INTENSITY_TIER.get(e.get("intensity"), 1)
            if isinstance(e.get("intensity"), str)
            else 1
            for e in entries
        ),
    }


def observed_precip_at(
    history, target: datetime, window_s: int = OBS_SLOT_WINDOW_S
) -> dict | None:
    """The most significant precipitation observed near `target`, or None.

    None means "no observation covers this moment", which is NOT the same as
    "it was dry" -- the caller must draw nothing rather than invent a default.

    Heaviest in the window rather than nearest in time: a three-minute
    downpour inside a half-hour slot is the thing a person remembers, and
    averaging it down to light rain would understate what actually happened.
    """
    if not history:
        return None
    # Same-zone datetime subtraction ignores fold. The two 01:30 readings
    # during autumn's clock change are an hour apart, not interchangeable.
    goal = target.astimezone(timezone.utc)
    best = None
    for when, props in history:
        if abs((when.astimezone(timezone.utc) - goal).total_seconds()) > window_s:
            continue
        found = obs_precipitation(props)
        if found is None:
            continue
        if best is None or (found["tier"], found["thunder"]) > (
            best["tier"],
            best["thunder"],
        ):
            best = found
    return best


def forecast_precip(row: dict, threshold: int = PRECIP_LIKELY_PCT) -> dict | None:
    """What the forecast says will fall at an hour, or None if it likely won't.

    Gated on probability, sized by expected accumulation. The two are
    different questions and conflating them would draw a downpour for a 90%
    chance of drizzle.
    """
    prob = _finite_number(row.get("prob"), 0.0, 100.0) or 0.0
    code = row.get("code") or 0
    code_precip = 51 <= code <= 67 or 71 <= code <= 77 or 80 <= code <= 86 or code >= 95
    if prob < threshold and not code_precip:
        return None
    mm = _finite_number(row.get("precip"), 0.0, 1000.0) or 0.0
    tier = 2
    for bound, value in PRECIP_TIER_MM:
        if mm < bound:
            tier = value
            break
    snow = 71 <= code <= 77 or code in (85, 86)
    if not snow and not (51 <= code <= 67 or 80 <= code <= 82):
        # The gate passed on likelihood alone, so the code carries no
        # precipitation type. Temperature is the only honest discriminator.
        temp = _finite_number(row.get("temp"), -100.0, 70.0)
        snow = temp is not None and temp <= 0.5
    return {"rain": not snow, "snow": snow, "thunder": code >= 95, "tier": tier}

"""The `genz` report style: a stereotype that still tells the truth.

A voice is only a way of SAYING the same facts. Every number this style
speaks has to match what the plain style would have said, the severe-alert
line has to stay something a person can act on, and the whole thing still
has to survive the TTS normalizer — which is a real constraint on the
register, because the initialisms this slang is famous for ("ngl", "tbh",
"fr", "rn") are unpronounceable to a neural voice and the normalizer does
not expand them.
"""

from __future__ import annotations

import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain
from busybar_dev.tts import speakable  # noqa: E402


def _hourly(base, peak_prob=70.0, peak_at=3, code=61, cloud=40):
    return [(base + timedelta(hours=i),
             {"prob": peak_prob if i == peak_at else 5.0, "code": code,
              "temp": 22.0, "cloud": cloud}) for i in range(14)]


FORECAST = [{"name": "This Afternoon", "isDaytime": True, "temperature": 81,
             "temperatureUnit": "F", "shortForecast": "Rain Likely",
             "probabilityOfPrecipitation": {"value": 64.0}}]


@pytest.fixture(autouse=True)
def fahrenheit(monkeypatch):
    monkeypatch.setattr(sky_settings, "UNITS", "f")


def _report(style, monkeypatch, wx=None, when=None, forecast=None,
            hourly=None):
    monkeypatch.setattr(sky_settings, "STYLE", style)
    base = when or datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    return sky_audio_report_plain._compose_report(
        wx or sky_weather.WeatherState(temp_c=25.0), forecast, base, hourly)


def test_the_genz_style_does_not_just_fall_back_to_plain(monkeypatch):
    """An unknown SKYSTRIP_STYLE silently reads as plain, so a style that
    was never implemented would pass every other test in this file."""
    base = datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    plain = _report("plain", monkeypatch, hourly=_hourly(base))
    genz = _report("genz", monkeypatch, hourly=_hourly(base))
    assert genz != plain, "genz produced the plain report verbatim"


# The register itself. Word-based slang only — see the module docstring.
GENZ_MARKERS = (
    "lowkey", "no cap", "deadass", "it's giving", "bestie", "chat",
    "aura", "delulu", "cooked", "crashing out", "standing on business",
    "ate", "mid", "touch grass", "main character", "we're so back",
    "respectfully", "we move", "allegations", "big yikes", "actually insane",
)


def test_the_genz_report_actually_sounds_like_the_style(monkeypatch):
    text = _report("genz", monkeypatch,
                   hourly=_hourly(datetime(2026, 6, 15, 14, 30,
                                           tzinfo=sky_settings.TZ)))
    hits = [m for m in GENZ_MARKERS if m in text.lower()]
    assert len(hits) >= 3, f"barely in register ({hits}): {text}"


BANNED_INITIALISMS = ("ngl", "tbh", "fr fr", " fr ", " rn ", "istg", "iykyk",
                      "omg", "idk", "smh", "afaik")


@pytest.mark.parametrize("hour", [3, 9, 14, 19, 23])
def test_no_initialism_the_voice_cannot_pronounce(hour, monkeypatch):
    """Kokoro reads these as letter mush and `speakable` does not expand
    them, so the stereotype has to be carried by whole words."""
    base = datetime(2026, 6, 15, hour, 30, tzinfo=sky_settings.TZ)
    text = _report("genz", monkeypatch, when=base, forecast=FORECAST,
                   hourly=_hourly(base)).lower()
    for bad in BANNED_INITIALISMS:
        assert bad not in f" {text} ", f"{bad!r} is unspeakable: {text}"


def test_a_severe_alert_is_still_named_and_still_serious(monkeypatch):
    """Slang must not bury something a person has to act on."""
    wx = sky_weather.WeatherState(severe=True, severe_event="Tornado Warning")
    text = _report("genz", monkeypatch, wx=wx)
    assert "Tornado Warning" in text, text
    # The sentence that carries the warning, not merely the first one —
    # the first sentence is the greeting and would pass regardless.
    warned = next(s for s in text.split(".") if "Tornado Warning" in s)
    assert "take that seriously" in text.lower(), warned
    # The bit is off for the WHOLE report, not just this sentence. Telling
    # someone to go touch grass under a warning is the joke landing in the
    # worst possible place.
    for marker in GENZ_MARKERS:
        assert marker not in text.lower(), (
            f"{marker!r} has no business in a Tornado Warning report: {text}")
    assert "touch grass" not in text.lower(), text


@pytest.mark.parametrize(("with_hourly", "facts"), [
    # The hourly peak owns the precip mention when there is one; the
    # forecast period only names its own odds when hourly stays quiet.
    (True, ("77 degrees", "70 percent")),
    (False, ("77 degrees", "64 percent")),
])
def test_the_numbers_match_what_plain_would_have_said(
    with_hourly, facts, monkeypatch,
):
    """A voice changes the wording, never the facts."""
    base = datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    wx = sky_weather.WeatherState(temp_c=25.0, wind_kmh=40.0, wind_dir=270.0)
    kw = dict(wx=wx, when=base, forecast=FORECAST,
              hourly=_hourly(base) if with_hourly else None)
    plain = _report("plain", monkeypatch, **kw)
    genz = _report("genz", monkeypatch, **kw)
    for fact in facts:
        assert fact in plain, f"fixture drifted: {fact} not in {plain}"
        assert fact in genz, f"genz lost {fact}: {genz}"


def test_no_decimal_number_reaches_the_genz_report(monkeypatch):
    base = datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    text = _report("genz", monkeypatch, forecast=FORECAST,
                   hourly=_hourly(base, peak_prob=56.5))
    assert not re.search(r"\d+\.\d", text), text


@pytest.mark.parametrize("hour", [3, 9, 14, 19, 23])
def test_the_genz_report_survives_voice_normalization(hour, monkeypatch):
    base = datetime(2026, 6, 15, hour, 30, tzinfo=sky_settings.TZ)
    text = _report("genz", monkeypatch, when=base, forecast=FORECAST,
                   hourly=_hourly(base))
    spoken = speakable(text)
    assert not re.search(r"\d", spoken), spoken
    assert not re.search(r"[a-z]\.[a-z]", spoken), spoken


@pytest.mark.parametrize("state", [
    dict(rain=True, temp_c=12.0),
    dict(snow=True, temp_c=-3.0),
    dict(thunder=True, temp_c=27.0),
    dict(cloud_frac=0.9), dict(cloud_frac=0.6),
    dict(cloud_frac=0.3), dict(cloud_frac=0.05),
])
def test_every_condition_has_something_to_say(state, monkeypatch):
    """No weather state may fall through to an empty or bare report."""
    text = _report("genz", monkeypatch, wx=sky_weather.WeatherState(**state))
    assert len(text) > 40, text
    assert text.strip().endswith("."), text


def test_the_same_hour_always_says_it_the_same_way(monkeypatch):
    """Determinism is not a nicety here — it protects device flash.

    The report's text is hashed into the asset filename and the firmware
    caches assets by path forever, so wording that rerolled per render
    would bake and upload a fresh sound file every minute. Two renders of
    the same hour must be byte-identical.
    """
    base = datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    kw = dict(when=base, forecast=FORECAST, hourly=_hourly(base))
    first = _report("genz", monkeypatch, **kw)
    second = _report("genz", monkeypatch, **kw)
    assert first == second, "the wording rerolled between two renders"

    # Same hour, a later minute: the phrasing must not move either.
    later = datetime(2026, 6, 15, 14, 58, tzinfo=sky_settings.TZ)
    same_hour = _report("genz", monkeypatch, when=later, forecast=FORECAST,
                        hourly=_hourly(base))
    assert same_hour.split(".")[0] == first.split(".")[0], (
        "the greeting changed inside a single hour")


def test_the_wording_actually_changes_through_the_day(monkeypatch):
    """A stereotype that says one fixed sentence is a catchphrase."""
    greetings, signoffs = set(), set()
    for day in (14, 15, 16):
        for hour in range(24):
            base = datetime(2026, 6, day, hour, 15, tzinfo=sky_settings.TZ)
            text = _report("genz", monkeypatch, when=base,
                           hourly=_hourly(base))
            greetings.add(text.split(".")[0])
            signoffs.add(text.strip().rsplit(".", 2)[-2].strip())
    assert len(greetings) >= 6, f"too few openings: {sorted(greetings)}"
    assert len(signoffs) >= 3, f"too few sign-offs: {sorted(signoffs)}"


def test_the_slang_is_current_rather_than_remembered(monkeypatch):
    """This vocabulary ages in months, so the dated tier is banned by name.

    These read as a parody of the register rather than the register.
    """
    stale = ("yeet", "on fleek", "bussin", "sus", "rizzler", "cheugy",
             "sheesh", "bruh moment", "slaps")
    for day in (14, 15):
        for hour in range(24):
            base = datetime(2026, 6, day, hour, 15, tzinfo=sky_settings.TZ)
            text = _report("genz", monkeypatch, when=base, forecast=FORECAST,
                           hourly=_hourly(base)).lower()
            for word in stale:
                assert word not in text, f"{word!r} is from a previous era"


# --- number gags -----------------------------------------------------------


def _at_temp_f(f_value, monkeypatch, **kw):
    """A report whose CURRENT temperature reads as `f_value` Fahrenheit."""
    return _report("genz", monkeypatch,
                   wx=sky_weather.WeatherState(temp_c=(f_value - 32) * 5 / 9,
                                            cloud_frac=0.1),
                   **kw)


def test_sixty_nine_degrees_gets_the_obvious_response(monkeypatch):
    text = _at_temp_f(69, monkeypatch)
    assert "69 degrees" in text, f"lost the actual temperature: {text}"
    assert "nice" in text.lower(), text


def test_sixty_seven_degrees_says_it_the_way_it_has_to_be_said(monkeypatch):
    text = _at_temp_f(67, monkeypatch)
    # The number itself still has to be spoken — the bit is an addition,
    # not a replacement. A listener still needs the temperature.
    assert "67 degrees" in text, f"lost the actual temperature: {text}"
    assert "six" in text.lower() and "seven" in text.lower(), text
    # Written as words on purpose: `speakable` turns "6-7" into
    # "sixnegative seven", which is why the digits cannot carry this one.
    assert "6-7" not in text, text


@pytest.mark.parametrize("f_value", [66, 68, 70, 77, 32])
def test_an_ordinary_temperature_gets_no_bit(f_value, monkeypatch):
    text = _at_temp_f(f_value, monkeypatch)
    assert "nice." not in text.lower(), text
    assert "six... seven" not in text.lower(), text


def test_a_warning_gets_no_jokes_about_the_number(monkeypatch):
    """The bit is off under a warning, and that includes this."""
    wx = sky_weather.WeatherState(severe=True, severe_event="Tornado Warning",
                               temp_c=(69 - 32) * 5 / 9)
    text = _report("genz", monkeypatch, wx=wx)
    assert "69 degrees" in text, text
    assert "nice" not in text.lower(), text


def test_at_most_one_bit_per_report(monkeypatch):
    """Two gags in one broadcast is a comedy routine, not a forecast."""
    base = datetime(2026, 6, 15, 14, 30, tzinfo=sky_settings.TZ)
    forecast = [{"name": "This Afternoon", "isDaytime": True,
                 "temperature": 69, "temperatureUnit": "F",
                 "shortForecast": "Sunny",
                 "probabilityOfPrecipitation": {"value": 0}}]
    text = _at_temp_f(67, monkeypatch, when=base, forecast=forecast)
    gags = sum(text.lower().count(w) for w in ("nice.", "six... seven"))
    assert gags == 1, f"{gags} bits in one report: {text}"


def test_the_gag_survives_voice_normalization(monkeypatch):
    for f_value in (67, 69):
        spoken = speakable(_at_temp_f(f_value, monkeypatch))
        assert not re.search(r"\d", spoken), spoken
        assert not re.search(r"[a-z]\.[a-z]", spoken), spoken

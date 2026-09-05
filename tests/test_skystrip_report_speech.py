"""Skystrip's generated report must remain natural spoken prose."""

from __future__ import annotations

import re
import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from busybar_dev.tts import speakable

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.audio import report_facts as sky_audio_report_facts
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain


def _rows(peak_hour: int, probability: float, *, day: int = 11):
    base = datetime(2026, 8, day, 0, 0, tzinfo=sky_settings.TZ)
    return [
        (
            base.replace(hour=hour),
            {
                "temp": 25.0,
                "cloud": 90.0,
                "prob": probability if hour == peak_hour else 5.0,
                "code": 61.0,
                "wind": 10.0,
                "rh": 60.0,
            },
        )
        for hour in range(24)
    ]


def _report(rows, *, hour: int = 10, style: str = "chicago", monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", style)
    monkeypatch.setattr(sky_settings, "UNITS", "f")
    now = datetime(2026, 8, 11, hour, 43, tzinfo=sky_settings.TZ)
    weather = replace(sky_weather.WeatherState(), temp_c=25.0, rain=True)
    return sky_audio_report_plain._compose_report(weather, None, now, rows)


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
@pytest.mark.parametrize("probability", [56.0, 45.0, 99.9, 30.5, 20.0])
def test_no_decimal_number_reaches_the_report(
    probability, style, monkeypatch,
):
    text = _report(
        _rows(12, probability), style=style, monkeypatch=monkeypatch,
    )
    assert not re.search(r"\d+\.\d", text), text


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_peak_chance_is_spoken_as_a_whole_percent(style, monkeypatch):
    text = _report(_rows(12, 56.0), style=style, monkeypatch=monkeypatch)
    assert "56 percent" in text
    assert "fifty six percent" in speakable(text)


@pytest.mark.parametrize(
    ("hour", "expected"),
    [
        (12, "noon"),
        (0, "midnight"),
        (13, "1 this afternoon"),
        (9, "9 this morning"),
        (17, "5 in the evening"),
        (23, "11 in the evening"),
    ],
)
def test_peak_hour_is_named_naturally(hour, expected):
    assert sky_audio_report_facts._peak_hour_words(hour) == expected


@pytest.mark.parametrize("hour", range(24))
def test_no_hour_is_announced_as_twelve_of_the_wrong_half(hour):
    words = sky_audio_report_facts._peak_hour_words(hour)
    assert "12 this" not in words
    assert not words.startswith("0 ")


@pytest.mark.parametrize(
    ("peak_hour", "phrase"),
    [
        (12, "around noon"),
        (13, "around 1 this afternoon"),
        (19, "around 7 in the evening"),
    ],
)
def test_named_hour_reaches_report(peak_hour, phrase, monkeypatch):
    text = _report(
        _rows(peak_hour, 80.0), hour=0, monkeypatch=monkeypatch,
    )
    assert phrase in text


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_tomorrows_chance_is_a_whole_percent(style, monkeypatch):
    text = _report(
        _rows(14, 72.0, day=12), hour=23, style=style, monkeypatch=monkeypatch,
    )
    assert not re.search(r"\d+\.\d", text), text


def test_float_forecast_probability_is_rounded(monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", "chicago")
    monkeypatch.setattr(sky_settings, "UNITS", "f")
    forecast = [
        {
            "name": "this afternoon",
            "isDaytime": True,
            "temperature": 81,
            "temperatureUnit": "F",
            "shortForecast": "Rain Likely",
            "probabilityOfPrecipitation": {"value": 64.0},
        }
    ]
    now = datetime(2026, 8, 11, 10, 43, tzinfo=sky_settings.TZ)
    text = sky_audio_report_plain._compose_report(
        replace(sky_weather.WeatherState(), temp_c=25.0), forecast, now, None,
    )
    assert not re.search(r"\d+\.\d", text), text
    assert "64 percent" in text


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_whole_report_survives_voice_normalization(style, monkeypatch):
    text = _report(_rows(12, 56.0), style=style, monkeypatch=monkeypatch)
    spoken = speakable(text)
    assert not re.search(r"\d", spoken), spoken
    assert not re.search(r"[a-z]\.[a-z]", spoken), spoken

"""The spoken report must name the warning, not just admit one exists.

Observed on 2026-08-11: a Tornado Warning was active, the card and the siren
both fired correctly, and the report said "we have severe weather in the area".
That sentence is equally true of a Frost Advisory. The one fact you can act on
— which warning — was the one fact the speech left out.

Host-only: no speech engine, device, or network participates.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.audio import report_facts as sky_audio_report_facts
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain


def _report(event: str, *, style: str, monkeypatch) -> str:
    monkeypatch.setattr(sky_settings, "STYLE", style)
    monkeypatch.setattr(sky_settings, "UNITS", "f")
    wx = replace(
        sky_weather.WeatherState(), severe=True, severe_event=event, temp_c=25.0)
    return sky_audio_report_plain._compose_report(wx, None, datetime(2026, 8, 11, 10, 43))


# The CAP events that actually matter here, plus one vowel-initial name to pin
# the article. These are the NWS's own strings, spoken verbatim.
NAMED = [
    "Tornado Warning",
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
    "Extreme Wind Warning",
]


@pytest.mark.parametrize("event", NAMED)
@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_the_report_speaks_the_warning_by_name(event, style, monkeypatch):
    text = _report(event, style=style, monkeypatch=monkeypatch)

    assert event in text, f"{style} style never said {event!r}"
    assert "severe weather in the area" not in text, (
        "the vague phrase replaced the specific one"
    )


@pytest.mark.parametrize("event,article", [
    ("Tornado Warning", "a Tornado Warning"),
    ("Extreme Wind Warning", "an Extreme Wind Warning"),
    ("Excessive Heat Warning", "an Excessive Heat Warning"),
    ("Ice Storm Warning", "an Ice Storm Warning"),
    ("Winter Storm Warning", "a Winter Storm Warning"),
])
def test_the_article_matches_the_warning_it_introduces(
    event, article, monkeypatch,
):
    """It is read aloud. "a Extreme Wind Warning" is a stumble every time."""
    assert article in _report(event, style="chicago", monkeypatch=monkeypatch)


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_an_unnamed_alert_stays_vague_rather_than_inventing_a_name(
    style, monkeypatch,
):
    """Severe with no CAP event is the pre-existing fallback. Vague is bad;
    naming a warning the NWS did not issue would be far worse."""
    text = _report("", style=style, monkeypatch=monkeypatch)

    assert "severe weather in the area" in text
    assert "in effect" not in text


def test_a_blank_or_padded_event_is_treated_as_unnamed(monkeypatch):
    assert "severe weather in the area" in _report(
        "   ", style="chicago", monkeypatch=monkeypatch)


@pytest.mark.parametrize("style", ["chicago", "plain", "genz"])
def test_the_warning_leads_the_report(style, monkeypatch):
    """It opens the report, ahead of the breeze and the sunset.

    "plain" spends its first sentence on the greeting alone and states
    conditions in the second, so the opening is the first two sentences.
    """
    text = _report("Tornado Warning", style=style, monkeypatch=monkeypatch)
    opening = ".".join(text.split(".")[:2])

    assert "Tornado Warning" in opening
    for later in ("breeze", "Sunset", "chance of rain"):
        assert later not in opening


def test_the_named_warning_still_outranks_every_other_condition(monkeypatch):
    monkeypatch.setattr(sky_settings, "STYLE", "plain")
    monkeypatch.setattr(sky_settings, "UNITS", "f")
    wx = replace(
        sky_weather.WeatherState(),
        severe=True, severe_event="Tornado Warning",
        thunder=True, snow=True, rain=True, cloud_frac=1.0, temp_c=25.0,
    )

    text = sky_audio_report_plain._compose_report(wx, None, datetime(2026, 8, 11, 10, 43))

    assert "Tornado Warning" in text
    assert "thunderstorms" not in text


def test_the_phrase_comes_from_the_committed_alert_state(monkeypatch):
    """``severe_event`` is set by ``_commit_alerts`` from the CAP event, so
    the speech and the display card cannot disagree about which warning."""
    wx = replace(
        sky_weather.WeatherState(), severe=True, severe_event="Tornado Warning")

    assert sky_audio_report_facts._alert_phrase(wx) == "a Tornado Warning in effect"
    assert sky_audio_report_facts._alert_phrase(sky_weather.WeatherState()) == (
        "severe weather in the area")

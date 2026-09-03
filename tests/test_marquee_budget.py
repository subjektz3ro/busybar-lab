"""The alert marquee's two budgets have to compose.

weather_alerts caps a CAP event name at MAX_EVENT_CHARS. pixel_text caps a
marquee at `maximum` frames, at a declared `speed_px_s`. Those numbers were
chosen independently, and past a certain width the frame cap binds and the
label silently scrolls faster than the declared readability ceiling — on the
severe-weather card, which is the one life-safety marquee in this repo.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from busybar_dev.pixel_text import (
    marquee_frame_count,
    marquee_speed_px_s,
    max_text_width,
    text_width,
)
from busybar_dev.weather_alerts import MAX_EVENT_CHARS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import skystrip  # noqa: E402

BOX = (2, skystrip.W - 3)
BOX_WIDTH = BOX[1] - BOX[0] + 1
SPEED = skystrip.ALERT_SCROLL_SPEED_PX_S
FPS = skystrip.ALERT_ANIM_FPS


def test_the_budgets_are_documented_as_a_single_number():
    limit = max_text_width(fps=FPS, speed_px_s=SPEED)
    assert limit > BOX_WIDTH
    # Below the limit the declared speed is honoured exactly.
    text = "A" * 20
    assert text_width(text) < limit
    assert marquee_speed_px_s(text, BOX_WIDTH, fps=FPS,
                              speed_px_s=SPEED) <= SPEED + 1e-6


def test_beyond_the_limit_the_frame_cap_used_to_win_silently():
    """The failure this item is about: the realised speed exceeds the declared
    one, and nothing reports it."""
    limit = max_text_width(fps=FPS, speed_px_s=SPEED)
    long_text = "W" * 400
    assert text_width(long_text) > limit
    realised = marquee_speed_px_s(long_text, BOX_WIDTH, fps=FPS,
                                  speed_px_s=SPEED)
    assert realised > SPEED, "fixture no longer exercises the cap"


def _alert(event: str):
    from datetime import UTC, datetime, timedelta
    from busybar_dev.weather_alerts import Alert
    now = datetime.now(UTC)
    return Alert(identifier="x", references=(), event=event, headline="",
                 status="Actual", message_type="Alert", severity="Extreme",
                 urgency="Immediate", certainty="Observed", effective=now,
                 onset=None, expires=now + timedelta(hours=1), ends=None)


def test_every_event_name_the_ingestion_bound_allows_stays_readable():
    """The composition assertion. MAX_EVENT_CHARS is what CAP parsing lets
    through; whatever the card ends up showing must scroll at or under the
    declared speed."""
    worst = "W" * MAX_EVENT_CHARS          # W is the widest glyph, 5 columns
    shown = skystrip.presentable_event(_alert(worst + " Warning"))
    realised = marquee_speed_px_s(shown, BOX_WIDTH, fps=FPS, speed_px_s=SPEED)
    assert realised <= SPEED + 1e-6, (
        f"the card scrolls {shown!r} at {realised:.1f} px/s against a "
        f"declared ceiling of {SPEED} px/s")


def test_an_unpresentable_name_falls_back_to_its_product_class():
    """Not a slice of the original — visual_eligible has already established
    that the event ends in 'warning' or 'emergency', so the generic is derived
    from the CAP data rather than invented."""
    assert skystrip.presentable_event(
        _alert("W" * 300 + " Warning")) == "WEATHER WARNING"
    assert skystrip.presentable_event(
        _alert("W" * 300 + " Emergency")) == "WEATHER EMERGENCY"


def test_a_real_event_name_is_shown_verbatim():
    assert skystrip.presentable_event(
        _alert("Tornado Warning")) == "TORNADO WARNING"


def test_the_alert_card_renders_within_a_sane_frame_budget():
    frames = skystrip.alert_animation_frames(_alert("W" * 300 + " Warning"))
    assert len(frames) <= 240, f"{len(frames)} frames is not a bounded asset"


def test_real_nws_event_names_are_nowhere_near_the_limit():
    """Context for the above: this is a bound-composition defect, not a live
    bug. No real product name comes close."""
    longest_real = "Special Marine Warning Until Further Notice"
    assert marquee_speed_px_s(longest_real, BOX_WIDTH, fps=FPS,
                              speed_px_s=SPEED) <= SPEED + 1e-6


def test_a_short_label_is_centred_not_scrolled():
    assert marquee_speed_px_s("TORNADO", BOX_WIDTH, fps=FPS,
                              speed_px_s=SPEED) == 0.0
    assert marquee_frame_count("TORNADO", BOX_WIDTH) == 40


@pytest.mark.parametrize("chars", [1, 12, 40, 114, 200, MAX_EVENT_CHARS])
def test_the_alert_card_never_exceeds_its_declared_speed(chars):
    shown = skystrip.presentable_event(_alert("M" * chars + " Warning"))
    realised = marquee_speed_px_s(shown, BOX_WIDTH, fps=FPS, speed_px_s=SPEED)
    assert realised <= SPEED + 1e-6, chars

"""Narration normalization must not turn decimal points into sentence stops."""

from __future__ import annotations

import re

import pytest

from busybar_dev.tts import speakable


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("56.0 percent", "fifty six percent"),
        ("0.0 degrees", "zero degrees"),
        ("7.50 inches", "seven point five inches"),
        ("56.4 percent", "fifty six point four percent"),
        ("1.25 inches", "one point two five inches"),
        ("-3.5 degrees", "minus three point five degrees"),
    ],
)
def test_a_decimal_is_spoken_not_split_into_two_sentences(text, expected):
    assert speakable(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "56.0 percent chance of rain",
        "a high of 79.0 today",
        "0.0 inches of snow",
    ],
)
def test_no_period_survives_between_two_digits_worth_of_words(text):
    spoken = speakable(text)
    assert not re.search(r"\d", spoken), f"a digit survived: {spoken}"
    assert ".zero" not in spoken
    assert not re.search(r"[a-z]\.[a-z]", spoken), spoken


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("74 degrees", "seventy four degrees"),
        ("100 percent", "one hundred percent"),
        ("5:48", "five forty eight"),
        ("7:56 tonight", "seven fifty six tonight"),
        ("8:05", "eight oh five"),
        ("9:00", "nine o'clock"),
        ("minus 12 out", "minus twelve out"),
    ],
)
def test_existing_number_and_clock_rewrites_are_unchanged(text, expected):
    assert speakable(text) == expected


@pytest.mark.parametrize("dash", ["—", "–"])
def test_dashes_still_become_commas(dash):
    assert speakable(f"rain{dash}keep an eye out") == "rain, keep an eye out"


def test_trailing_decimal_zero_is_dropped_rather_than_spoken():
    assert speakable("56.0") == "fifty six"
    assert speakable("56.00") == "fifty six"
    assert speakable("56.10") == "fifty six point one"

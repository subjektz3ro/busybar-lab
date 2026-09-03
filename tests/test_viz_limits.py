"""Resource limits are strict before renderer work begins."""

from __future__ import annotations

import pytest

from busybar_viz.limits import (
    MAX_FPS,
    MAX_FRAMES,
    MAX_EVENTS,
    MAX_PARAMETER_LENGTH,
    LimitError,
    validate_parameters,
    validate_signals,
    validate_timing,
)
from busybar_viz.models import Confidence, SignalEvent


def test_timing_accepts_the_declared_integer_boundaries():
    validate_timing(frame_count=1, fps=1)
    validate_timing(frame_count=MAX_FRAMES, fps=MAX_FPS)


@pytest.mark.parametrize(
    ("frame_count", "fps"),
    ((0, 1), (MAX_FRAMES + 1, 1), (1, 0), (1, MAX_FPS + 1)),
)
def test_timing_rejects_values_outside_the_work_budget(frame_count, fps):
    with pytest.raises(LimitError):
        validate_timing(frame_count=frame_count, fps=fps)


@pytest.mark.parametrize(
    ("frame_count", "fps"),
    ((True, 1), (1, False), (1.0, 1), (1, 5.0)),
)
def test_timing_does_not_treat_booleans_or_floats_as_counts(frame_count, fps):
    with pytest.raises(LimitError, match="integers"):
        validate_timing(frame_count=frame_count, fps=fps)


def test_parameters_accept_bounded_finite_json_values():
    validate_parameters({
        "count": 3,
        "enabled": True,
        "label": "rain",
        "nested": {"values": [1, None, 2.5]},
    })


@pytest.mark.parametrize("value", (float("nan"), float("inf"), object()))
def test_parameters_reject_values_that_cannot_enter_evidence_json(value):
    with pytest.raises(LimitError, match="finite JSON"):
        validate_parameters({"value": value})


def test_parameters_reject_duplicate_scale_and_value_bombs_early():
    with pytest.raises(LimitError, match="too many"):
        validate_parameters({f"p{index}": index for index in range(65)})
    with pytest.raises(LimitError, match="value budget"):
        validate_parameters({"label": "x" * (MAX_PARAMETER_LENGTH + 1)})


@pytest.mark.parametrize("key", ("", "x" * 65, 123))
def test_parameter_names_are_nonempty_bounded_strings(key):
    with pytest.raises(LimitError, match="parameter names"):
        validate_parameters({key: "value"})  # type: ignore[dict-item]


def test_signal_timeline_accepts_bounded_finite_semantic_values():
    validate_signals((
        SignalEvent(0, "top_led.pulse", "#FFFFFFFF"),
        SignalEvent(
            100,
            "input.applied",
            {"control": "mode", "value": True},
            Confidence.LOGICAL_ONLY,
        ),
    ), duration_us=100)


@pytest.mark.parametrize(
    ("signal", "message"),
    (
        (SignalEvent(True, "pulse", True), "integer microseconds"),
        (SignalEvent(-1, "pulse", True), "rendered segment"),
        (SignalEvent(101, "pulse", True), "rendered segment"),
        (SignalEvent(0, "", True), "1-64"),
        (SignalEvent(0, "pulse", float("nan")), "finite JSON"),
        (SignalEvent(0, "pulse", True, "logical_only"), "known confidence"),
    ),
)
def test_signal_timeline_rejects_malformed_or_unserializable_values(signal, message):
    with pytest.raises(LimitError, match=message):
        validate_signals((signal,), duration_us=100)


def test_signal_timeline_has_a_hard_event_count_budget():
    signals = tuple(
        SignalEvent(0, "metric", index) for index in range(MAX_EVENTS + 1)
    )
    with pytest.raises(LimitError, match="event budget"):
        validate_signals(signals, duration_us=100)

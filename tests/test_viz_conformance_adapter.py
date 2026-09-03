"""The protocol fixture proves general tracks and semantic input replay."""

from __future__ import annotations

import pytest

from busybar_viz.adapters.conformance import (
    DURATION_US,
    SCENARIO_ID,
    ConformanceAdapter,
)
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.limits import LimitError
from busybar_viz.models import Confidence, InputEvent, RenderRequest
from busybar_viz.registry import adapter_for_scenario, scenarios


def _render(*, parameters=None, inputs=()):
    return ConformanceAdapter().render(
        RenderRequest.from_values(SCENARIO_ID, parameters, inputs)
    )


def test_conformance_scenario_is_explicitly_registered_and_app_neutral():
    spec = next(spec for spec in scenarios() if spec.id == SCENARIO_ID)

    assert isinstance(adapter_for_scenario(SCENARIO_ID), ConformanceAdapter)
    assert spec.adapter == "conformance"
    assert spec.expected_displays == ("front", "back")
    assert [(item.id, item.kind) for item in spec.inputs] == [
        ("level", "encoder.delta"),
        ("mode", "button.press"),
    ]
    assert "skystrip" not in spec.description.lower()
    assert "dsn" not in spec.description.lower()


def test_fixture_emits_both_named_device_profiles_on_one_clock():
    segment = _render()

    assert segment.evidence_level is None
    assert [
        (track.id, track.size, len(track.frames), track.fps, track.duration_us)
        for track in segment.displays
    ] == [
        ("front", (72, 16), 8, 4, 2_000_000),
        ("back", (160, 80), 8, 4, 2_000_000),
    ]
    assert all(
        track.confidence is Confidence.EMULATED_CONFORMANT
        for track in segment.displays
    )
    assert required_checks_pass(analyze(segment))
    assert "not a production app" in segment.notes[0]


def test_event_on_frame_boundary_affects_the_frame_beginning_there():
    baseline = _render()
    changed = _render(inputs=(
        InputEvent(500_000, "encoder.delta", "level", 2),
    ))

    for baseline_track, changed_track in zip(baseline.displays, changed.displays):
        assert [frame.tobytes() for frame in changed_track.frames[:2]] == [
            frame.tobytes() for frame in baseline_track.frames[:2]
        ]
        assert changed_track.frames[2].tobytes() != baseline_track.frames[2].tobytes()
    assert changed.signals[0].t_us == 500_000
    assert changed.signals[0].kind == "input.applied"
    assert changed.signals[0].value == {
        "kind": "encoder.delta",
        "control": "level",
        "value": 2,
        "level": 4,
        "mode": "cyan",
    }


def test_event_before_boundary_affects_the_preceding_half_open_interval():
    baseline = _render()
    changed = _render(inputs=(
        InputEvent(499_999, "button.press", "mode"),
    ))

    for baseline_track, changed_track in zip(baseline.displays, changed.displays):
        assert changed_track.frames[0].tobytes() == baseline_track.frames[0].tobytes()
        assert changed_track.frames[1].tobytes() != baseline_track.frames[1].tobytes()


def test_same_timestamp_events_replay_in_stable_request_order_and_repeat_exactly():
    events = (
        InputEvent(0, "encoder.delta", "level", 3),
        InputEvent(0, "encoder.delta", "level", -1),
        InputEvent(0, "button.press", "mode"),
    )

    first = _render(inputs=events)
    second = _render(inputs=events)

    assert [signal.value for signal in first.signals] == [
        {
            "kind": "encoder.delta", "control": "level", "value": 3,
            "level": 5, "mode": "cyan",
        },
        {
            "kind": "encoder.delta", "control": "level", "value": -1,
            "level": 4, "mode": "cyan",
        },
        {
            "kind": "button.press", "control": "mode", "value": True,
            "level": 4, "mode": "amber",
        },
    ]
    assert [
        [frame.tobytes() for frame in track.frames] for track in first.displays
    ] == [
        [frame.tobytes() for frame in track.frames] for track in second.displays
    ]


@pytest.mark.parametrize(
    ("parameters", "message"),
    [
        ({"initial_level": True}, "initial_level"),
        ({"initial_level": 9}, "initial_level"),
        ({"initial_mode": "other"}, "initial_mode"),
        ({"app_specific_weather": "rain"}, "unknown conformance controls"),
    ],
)
def test_controls_fail_closed(parameters, message):
    with pytest.raises(ValueError, match=message):
        _render(parameters=parameters)


@pytest.mark.parametrize(
    ("event", "message"),
    [
        (InputEvent(0, "encoder.delta", "level", True), "encoder deltas"),
        (InputEvent(0, "button.release", "mode"), "unsupported conformance input"),
        (InputEvent(0, "button.press", "mode", False), "value true"),
        (InputEvent(DURATION_US, "button.press", "mode"), "must occur before"),
    ],
)
def test_inputs_fail_closed(event, message):
    with pytest.raises(ValueError, match=message):
        _render(inputs=(event,))


def test_input_budget_and_timestamp_order_are_enforced():
    with pytest.raises(LimitError, match="ordered"):
        _render(inputs=(
            InputEvent(10, "button.press", "mode"),
            InputEvent(9, "button.press", "mode"),
        ))

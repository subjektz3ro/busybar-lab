"""App-neutral fixture for exercising the visualizer adapter contract.

This is intentionally not an example BUSY Bar application.  It gives the
toolchain a small, deterministic source for both display profiles and for
timed semantic inputs, without making a network request or importing an app.
"""

from __future__ import annotations

from dataclasses import dataclass

from PIL import Image

from busybar_viz.limits import validate_inputs
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    ControlSpec,
    DisplayTrack,
    InputEvent,
    InputSpec,
    RenderedSegment,
    RenderRequest,
    ScenarioSpec,
    SignalEvent,
)
from busybar_viz.profiles import BACK, FRONT

SCENARIO_ID = "conformance/dual-display-input-replay"
FPS = 4
FRAME_COUNT = 8
DURATION_US = FRAME_COUNT * 1_000_000 // FPS

_MODE_NAMES = ("cyan", "amber", "violet")
_MODE_COLORS = (
    (25, 190, 235),
    (245, 115, 20),
    (175, 55, 235),
)


@dataclass(frozen=True, slots=True)
class _State:
    level: int
    mode: int
    input_count: int = 0


def _validated_initial_state(parameters: dict[str, object]) -> _State:
    unknown = sorted(set(parameters) - {"initial_level", "initial_mode"})
    if unknown:
        raise ValueError(f"unknown conformance controls: {', '.join(unknown)}")

    level = parameters.get("initial_level", 2)
    if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= 8:
        raise ValueError("initial_level must be an integer from 0 through 8")

    mode_name = parameters.get("initial_mode", _MODE_NAMES[0])
    if mode_name not in _MODE_NAMES:
        raise ValueError(
            "initial_mode must be one of " + ", ".join(repr(name) for name in _MODE_NAMES)
        )
    return _State(level, _MODE_NAMES.index(mode_name))


def _apply_event(state: _State, event: InputEvent) -> _State:
    if event.kind == "encoder.delta" and event.control == "level":
        delta = event.value
        if isinstance(delta, bool) or not isinstance(delta, int) or not -8 <= delta <= 8:
            raise ValueError("level encoder deltas must be integers from -8 through 8")
        return _State(
            max(0, min(8, state.level + delta)),
            state.mode,
            state.input_count + 1,
        )
    if event.kind == "button.press" and event.control == "mode":
        if event.value is not True:
            raise ValueError("mode button presses must use value true")
        return _State(state.level, (state.mode + 1) % len(_MODE_NAMES), state.input_count + 1)
    raise ValueError(
        f"unsupported conformance input: {event.kind!r} on {event.control!r}"
    )


def _replay(
    initial: _State,
    inputs: tuple[InputEvent, ...],
) -> tuple[tuple[_State, ...], tuple[SignalEvent, ...]]:
    """Apply events to half-open frame intervals in stable request order.

    An event exactly on a frame boundary affects the frame beginning at that
    boundary.  An event at ``DURATION_US`` is outside the finite segment.
    """

    validate_inputs(inputs)
    if inputs and inputs[-1].t_us >= DURATION_US:
        raise ValueError(
            f"conformance inputs must occur before {DURATION_US} microseconds"
        )

    states: list[_State] = []
    signals: list[SignalEvent] = []
    state = initial
    event_index = 0
    for frame_index in range(FRAME_COUNT):
        frame_end_us = (frame_index + 1) * 1_000_000 // FPS
        while event_index < len(inputs) and inputs[event_index].t_us < frame_end_us:
            event = inputs[event_index]
            state = _apply_event(state, event)
            signals.append(SignalEvent(
                event.t_us,
                "input.applied",
                {
                    "kind": event.kind,
                    "control": event.control,
                    "value": event.value,
                    "level": state.level,
                    "mode": _MODE_NAMES[state.mode],
                },
                Confidence.LOGICAL_ONLY,
            ))
            event_index += 1
        states.append(state)
    return tuple(states), tuple(signals)


def _front_frame(state: _State, frame_index: int) -> Image.Image:
    frame = Image.new("RGB", FRONT.size, (0, 0, 0))
    color = _MODE_COLORS[state.mode]

    # Mode, bounded level, applied-input count, and scenario-clock cursor are
    # separate visual channels so tests can identify which contract changed.
    frame.paste(color, (2, 2, 8, 8))
    frame.paste((35, 35, 35), (11, 8, 70, 13))
    if state.level:
        frame.paste(color, (12, 9, 12 + state.level * 7, 12))
    for dot in range(min(state.input_count, 8)):
        x = 12 + dot * 7
        frame.paste((100, 210, 80), (x, 14, x + 3, 16))
    cursor_x = 2 + frame_index * 9
    frame.paste((110, 110, 110), (cursor_x, 0, cursor_x + 3, 1))
    return frame


def _back_frame(state: _State, frame_index: int) -> Image.Image:
    frame = Image.new("RGB", BACK.size, (0, 0, 0))
    color = _MODE_COLORS[state.mode]

    frame.paste(color, (8, 8, 48, 48))
    frame.paste((35, 35, 35), (56, 16, 152, 48))
    for step in range(state.level):
        x = 60 + step * 11
        frame.paste(color, (x, 20, x + 8, 44))
    for dot in range(min(state.input_count, 12)):
        x = 12 + dot * 12
        frame.paste((100, 210, 80), (x, 58, x + 7, 65))
    cursor_x = 8 + frame_index * 20
    frame.paste((110, 110, 110), (cursor_x, 72, cursor_x + 8, 76))
    return frame


class ConformanceAdapter:
    """Closed, deterministic fixture for adapter and client conformance."""

    id = "conformance"

    def scenarios(self) -> tuple[ScenarioSpec, ...]:
        return (ScenarioSpec(
            SCENARIO_ID,
            "Visualizer protocol: dual-display input replay",
            "Exercises named front/back tracks and deterministic semantic inputs.",
            self.id,
            controls=(
                ControlSpec(
                    "initial_level", "Initial level", "number", 2,
                    minimum=0, maximum=8,
                ),
                ControlSpec(
                    "initial_mode", "Initial mode", "choice", _MODE_NAMES[0],
                    choices=_MODE_NAMES,
                ),
            ),
            inputs=(
                InputSpec("level", "Level encoder", "encoder.delta"),
                InputSpec("mode", "Mode button", "button.press", (True,)),
            ),
            expected_displays=("front", "back"),
        ),)

    def render(self, request: RenderRequest) -> RenderedSegment:
        if request.scenario_id != SCENARIO_ID:
            raise KeyError(f"unknown conformance scenario: {request.scenario_id}")
        initial = _validated_initial_state(dict(request.parameters))
        states, signals = _replay(initial, request.inputs)
        return RenderedSegment(
            displays=(
                DisplayTrack(
                    "front",
                    tuple(_front_frame(state, index) for index, state in enumerate(states)),
                    FPS,
                    Confidence.EMULATED_CONFORMANT,
                ),
                DisplayTrack(
                    "back",
                    tuple(_back_frame(state, index) for index, state in enumerate(states)),
                    FPS,
                    Confidence.EMULATED_CONFORMANT,
                ),
            ),
            signals=signals,
            checks=(
                CheckSpec.create(
                    "front-dimensions", "frame.dimensions",
                    display="front", size=FRONT.size,
                ),
                CheckSpec.create(
                    "back-dimensions", "frame.dimensions",
                    display="back", size=BACK.size,
                ),
                CheckSpec.create(
                    "front-clock", "animation.unique_frames",
                    display="front", minimum=FRAME_COUNT,
                ),
                CheckSpec.create(
                    "back-clock", "animation.unique_frames",
                    display="back", minimum=FRAME_COUNT,
                ),
            ),
            notes=(
                "Protocol fixture only; these pixels are not a production app design.",
                "Tracks are emulated-conformant, not framebuffer or hardware evidence.",
                "Inputs are applied in request order to half-open frame intervals.",
            ),
            source_paths=("busybar_viz/adapters/conformance.py",),
        )

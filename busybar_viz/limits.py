"""Hard work budgets shared by the CLI, server, and render workers."""

from __future__ import annotations

import json
from collections.abc import Sequence

from .models import Confidence, InputEvent, SignalEvent

MAX_DISPLAYS = 2
MAX_FRAMES = 240
MAX_FPS = 20
MAX_DURATION_US = 60_000_000
MAX_EVENTS = 1_000
MAX_JSON_BYTES = 256 * 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_PARAMETER_LENGTH = 256


class LimitError(ValueError):
    """A request or result exceeded a declared visual-debugging budget."""


def _validate_json(value: object, *, label: str) -> None:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise LimitError(f"{label} must contain finite JSON values") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise LimitError(f"{label} exceed the JSON request budget")


def validate_timing(*, frame_count: int, fps: int) -> None:
    if (isinstance(frame_count, bool) or not isinstance(frame_count, int)
            or isinstance(fps, bool) or not isinstance(fps, int)):
        raise LimitError("frame count and fps must be integers")
    if not 1 <= frame_count <= MAX_FRAMES:
        raise LimitError(f"frame count must be between 1 and {MAX_FRAMES}")
    if not 1 <= fps <= MAX_FPS:
        raise LimitError(f"fps must be between 1 and {MAX_FPS}")
    duration_us = (frame_count * 1_000_000 + fps - 1) // fps
    if duration_us > MAX_DURATION_US:
        raise LimitError("animation exceeds the 60 second render budget")


def _validate_parameter_value(key: str, value: object) -> None:
    """Bound every leaf, not the str() of a container.

    The budget is a per-value identity limit; a list of 240 frame hashes is
    a legitimate parameter whose total size the JSON budget already bounds.
    Stringifying whole containers capped `view` directories at three frames.
    """
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_parameter_value(key, item)
    elif isinstance(value, dict):
        for item in value.values():
            _validate_parameter_value(key, item)
    elif len(str(value)) > MAX_PARAMETER_LENGTH:
        raise LimitError(f"parameter {key!r} exceeds the value budget")


def validate_parameters(parameters: dict[str, object]) -> None:
    if len(parameters) > 64:
        raise LimitError("too many scenario parameters")
    for key, value in parameters.items():
        if not isinstance(key, str) or not key or len(key) > 64:
            raise LimitError("parameter names must be 1-64 character strings")
        _validate_parameter_value(key, value)
    _validate_json(parameters, label="scenario parameters")


def validate_inputs(inputs: Sequence[InputEvent]) -> None:
    if len(inputs) > MAX_EVENTS:
        raise LimitError(f"input timeline exceeds the {MAX_EVENTS} event budget")
    previous = -1
    for event in inputs:
        if isinstance(event.t_us, bool) or not isinstance(event.t_us, int):
            raise LimitError("input timestamps must be integer microseconds")
        if not 0 <= event.t_us <= MAX_DURATION_US:
            raise LimitError("input timestamps must fall within 0-60 seconds")
        if event.t_us < previous:
            raise LimitError("input events must be ordered by timestamp")
        previous = event.t_us
        if not isinstance(event.kind, str) or not event.kind or len(event.kind) > 64:
            raise LimitError("input kinds must be 1-64 characters")
        if (not isinstance(event.control, str) or not event.control
                or len(event.control) > 64):
            raise LimitError("input controls must be 1-64 characters")
        if len(str(event.value)) > MAX_PARAMETER_LENGTH:
            raise LimitError("input event value exceeds the value budget")
    _validate_json(
        [event.as_dict() for event in inputs],
        label="input events",
    )


def validate_signals(signals: Sequence[SignalEvent], *, duration_us: int) -> None:
    if len(signals) > MAX_EVENTS:
        raise LimitError(f"signal timeline exceeds the {MAX_EVENTS} event budget")
    for signal in signals:
        if isinstance(signal.t_us, bool) or not isinstance(signal.t_us, int):
            raise LimitError("signal timestamps must be integer microseconds")
        if not 0 <= signal.t_us <= duration_us:
            raise LimitError("signal timestamps must fall within the rendered segment")
        if (not isinstance(signal.kind, str) or not signal.kind
                or len(signal.kind) > 64):
            raise LimitError("signal kinds must be 1-64 character strings")
        if not isinstance(signal.confidence, Confidence):
            raise LimitError("signal confidence must use a known confidence level")
        if len(str(signal.value)) > MAX_PARAMETER_LENGTH:
            raise LimitError("signal value exceeds the value budget")
    _validate_json(
        [{
            "t_us": signal.t_us,
            "kind": signal.kind,
            "value": signal.value,
            "confidence": signal.confidence.value,
        } for signal in signals],
        label="logical signals",
    )

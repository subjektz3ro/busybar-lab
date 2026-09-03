"""Small, versioned domain objects shared across every viz surface."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import math
import re
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from PIL import Image

SCENARIO_SCHEMA = "busybar.scenario/v1"
RENDER_REQUEST_SCHEMA = "busybar.render-request/v1"
TRACE_SCHEMA = "busybar.trace/v1"
EVIDENCE_SCHEMA = "busybar.evidence/v1"
SESSION_SCHEMA = "busybar.session/v1"
SESSION_EVENT_SCHEMA = "busybar.session-event/v1"

_SAFE_CHECK_ID = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_CHECK_SEVERITIES = frozenset({"error", "warning", "info"})


class _FrozenDict(dict):
    """A JSON-serializable mapping that rejects ordinary mutation."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("render request JSON values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable


class _FrozenList(list):
    """A JSON-serializable array that rejects ordinary mutation."""

    @staticmethod
    def _immutable(*_args, **_kwargs):
        raise TypeError("render request JSON values are immutable")

    __setitem__ = _immutable
    __delitem__ = _immutable
    append = _immutable
    clear = _immutable
    extend = _immutable
    insert = _immutable
    pop = _immutable
    remove = _immutable
    reverse = _immutable
    sort = _immutable
    __iadd__ = _immutable
    __imul__ = _immutable


def _freeze_json(value: object, *, label: str) -> object:
    """Return a detached, deeply immutable copy of one finite JSON value."""

    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain finite JSON values")
        return value
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError(f"{label} object keys must be strings")
        return _FrozenDict(
            (key, _freeze_json(item, label=f"{label}.{key}"))
            for key, item in sorted(value.items())
        )
    if isinstance(value, (list, tuple)):
        return _FrozenList(
            _freeze_json(item, label=f"{label}[{index}]")
            for index, item in enumerate(value)
        )
    raise ValueError(f"{label} must contain only JSON values")


def _plain_json(value: object) -> object:
    """Return an independent ordinary-dict/list copy of a frozen JSON value."""

    if isinstance(value, Mapping):
        return {key: _plain_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item) for item in value]
    return value


class Confidence(StrEnum):
    SOURCE_EXACT = "source_exact"
    EMULATED_CONFORMANT = "emulated_conformant"
    FRAMEBUFFER_OBSERVED = "framebuffer_observed"
    PHYSICAL_OBSERVED = "physical_observed"
    LOGICAL_ONLY = "logical_only"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class CheckStatus(StrEnum):
    PASS = "pass"
    FAIL = "fail"
    UNKNOWN = "unknown"
    SKIPPED = "skipped"


class EvidenceLevel(StrEnum):
    RENDERER_VERIFIED = "renderer-verified"
    GAP_PREVIEWED = "gap-previewed"
    FRAMEBUFFER_CAPTURED = "framebuffer-captured"
    HARDWARE_OBSERVED = "hardware-observed"


@dataclass(frozen=True, slots=True)
class ControlSpec:
    id: str
    label: str
    kind: str
    default: object
    choices: tuple[object, ...] = ()
    minimum: float | None = None
    maximum: float | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "default": self.default,
            "choices": list(self.choices),
            "minimum": self.minimum,
            "maximum": self.maximum,
        }


@dataclass(frozen=True, slots=True)
class InputSpec:
    id: str
    label: str
    kind: str
    values: tuple[object, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "label": self.label,
            "kind": self.kind,
            "values": list(self.values),
        }


@dataclass(frozen=True, slots=True)
class ScenarioSpec:
    id: str
    title: str
    description: str
    adapter: str
    controls: tuple[ControlSpec, ...] = ()
    inputs: tuple[InputSpec, ...] = ()
    expected_displays: tuple[str, ...] = ("front",)

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SCENARIO_SCHEMA,
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "adapter": self.adapter,
            "controls": [control.as_dict() for control in self.controls],
            "inputs": [item.as_dict() for item in self.inputs],
            "expected_displays": list(self.expected_displays),
        }


@dataclass(frozen=True, slots=True)
class RenderRequest:
    scenario_id: str
    parameters: Mapping[str, object] = field(
        default_factory=lambda: MappingProxyType({})
    )
    inputs: tuple["InputEvent", ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.scenario_id, str) or not self.scenario_id:
            raise ValueError("scenario_id must be a non-empty string")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("parameters must be an object")
        frozen = _freeze_json(self.parameters, label="scenario parameters")
        assert isinstance(frozen, dict)
        object.__setattr__(self, "parameters", MappingProxyType(frozen))
        object.__setattr__(self, "inputs", tuple(self.inputs))

    @classmethod
    def from_values(
        cls,
        scenario_id: str,
        parameters: Mapping[str, object] | None = None,
        inputs: Sequence["InputEvent"] = (),
    ) -> "RenderRequest":
        return cls(
            scenario_id,
            parameters or {},
            tuple(inputs),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "RenderRequest":
        allowed = {"schema", "scenario_id", "parameters", "inputs"}
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown render request fields: {', '.join(sorted(unknown))}")
        if value.get("schema", RENDER_REQUEST_SCHEMA) != RENDER_REQUEST_SCHEMA:
            raise ValueError("unsupported render request schema")
        scenario_id = value.get("scenario_id")
        parameters = value.get("parameters", {})
        inputs = value.get("inputs", [])
        if not isinstance(scenario_id, str) or not scenario_id:
            raise ValueError("scenario_id must be a non-empty string")
        if not isinstance(parameters, Mapping):
            raise ValueError("parameters must be an object")
        if not isinstance(inputs, Sequence) or isinstance(inputs, (str, bytes)):
            raise ValueError("inputs must be an array")
        return cls.from_values(
            scenario_id,
            parameters,
            [InputEvent.from_dict(item) for item in inputs],
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": RENDER_REQUEST_SCHEMA,
            "scenario_id": self.scenario_id,
            "parameters": _plain_json(self.parameters),
            "inputs": [event.as_dict() for event in self.inputs],
        }


@dataclass(frozen=True, slots=True)
class InputEvent:
    """One semantic input at a deterministic point on the scenario clock."""

    t_us: int
    kind: str
    control: str
    value: object = True

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "value",
            _freeze_json(self.value, label="input event values"),
        )

    @classmethod
    def from_dict(cls, value: object) -> "InputEvent":
        if not isinstance(value, Mapping):
            raise ValueError("each input event must be an object")
        unknown = set(value) - {"t_us", "kind", "control", "value"}
        if unknown:
            raise ValueError(f"unknown input fields: {', '.join(sorted(unknown))}")
        try:
            t_us = value["t_us"]
            kind = value["kind"]
            control = value["control"]
        except KeyError as exc:
            raise ValueError(f"missing input field: {exc.args[0]}") from exc
        if isinstance(t_us, bool) or not isinstance(t_us, int):
            raise ValueError("input t_us must be an integer")
        if not isinstance(kind, str) or not isinstance(control, str):
            raise ValueError("input kind and control must be strings")
        return cls(t_us, kind, control, value.get("value", True))

    def as_dict(self) -> dict[str, object]:
        return {
            "t_us": self.t_us,
            "kind": self.kind,
            "control": self.control,
            "value": _plain_json(self.value),
        }


@dataclass(frozen=True, slots=True)
class RegionSpec:
    id: str
    display: str = "front"
    rect: tuple[int, int, int, int] | None = None
    points: tuple[tuple[int, int], ...] = ()

    def coordinates(self, width: int, height: int) -> tuple[tuple[int, int], ...]:
        coords = set(self.points)
        if self.rect is not None:
            x0, y0, x1, y1 = self.rect
            coords.update((x, y) for y in range(y0, y1) for x in range(x0, x1))
        return tuple(sorted((x, y) for x, y in coords
                            if 0 <= x < width and 0 <= y < height))


@dataclass(frozen=True, slots=True)
class InkReference:
    """Independent full-ink samples for proving composed text completeness.

    Coordinates are intentionally not clipped.  An expected sample outside
    the named display is a failed fit proof, not a pixel to silently discard.
    """

    id: str
    label: str
    display: str
    pixels: tuple[tuple[int, int, tuple[int, int, int]], ...]
    frame_indices: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class SignalEvent:
    t_us: int
    kind: str
    value: object
    confidence: Confidence = Confidence.LOGICAL_ONLY


@dataclass(frozen=True, slots=True)
class DisplayTrack:
    """Exact raster frames for one named BUSY Bar display."""

    id: str
    frames: tuple[Image.Image, ...]
    fps: int
    confidence: Confidence
    baselines: tuple[Image.Image, ...] = ()

    @property
    def size(self) -> tuple[int, int]:
        if not self.frames:
            raise ValueError(f"display track {self.id!r} has no frames")
        return self.frames[0].size

    @property
    def duration_us(self) -> int:
        return round(len(self.frames) * 1_000_000 / self.fps)


@dataclass(frozen=True, slots=True)
class CheckSpec:
    id: str
    kind: str
    severity: str = "error"
    parameters: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _SAFE_CHECK_ID.fullmatch(self.id):
            raise ValueError("check ids must be safe 1-64 character names")
        if not isinstance(self.kind, str) or not self.kind:
            raise ValueError("check kinds must be non-empty strings")
        if self.severity not in _CHECK_SEVERITIES:
            raise ValueError("check severity must be error, warning, or info")
        if not isinstance(self.parameters, Mapping):
            raise ValueError("check parameters must be an object")
        object.__setattr__(
            self,
            "parameters",
            MappingProxyType(dict(self.parameters)),
        )

    @classmethod
    def create(
        cls,
        check_id: str,
        kind: str,
        *,
        severity: str = "error",
        **parameters: Any,
    ) -> "CheckSpec":
        return cls(check_id, kind, severity, MappingProxyType(parameters))


@dataclass(frozen=True, slots=True)
class CheckResult:
    id: str
    kind: str
    status: CheckStatus
    severity: str
    confidence: Confidence
    message: str
    observed: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    expected: Mapping[str, Any] = field(
        default_factory=lambda: MappingProxyType({})
    )
    frame_indices: tuple[int, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "status": self.status.value,
            "severity": self.severity,
            "confidence": self.confidence.value,
            "message": self.message,
            "observed": dict(self.observed),
            "expected": dict(self.expected),
            "frame_indices": list(self.frame_indices),
        }


@dataclass(frozen=True, slots=True)
class RenderedSegment:
    displays: tuple[DisplayTrack, ...]
    evidence_level: EvidenceLevel | None = None
    signals: tuple[SignalEvent, ...] = ()
    regions: tuple[RegionSpec, ...] = ()
    ink_references: tuple[InkReference, ...] = ()
    checks: tuple[CheckSpec, ...] = ()
    notes: tuple[str, ...] = ()
    source_paths: tuple[str, ...] = ()

    def region(self, region_id: str) -> RegionSpec | None:
        return next((region for region in self.regions if region.id == region_id), None)

    def display(self, display_id: str = "front") -> DisplayTrack | None:
        return next((track for track in self.displays if track.id == display_id), None)

    def ink_reference(self, reference_id: str) -> InkReference | None:
        return next(
            (reference for reference in self.ink_references
             if reference.id == reference_id),
            None,
        )

    @property
    def duration_us(self) -> int:
        return max((track.duration_us for track in self.displays), default=0)


def frozen_mapping(value: Mapping[str, Any]) -> Mapping[str, Any]:
    return MappingProxyType(dict(value))


def ensure_rgb_frames(frames: Sequence[Image.Image]) -> tuple[Image.Image, ...]:
    return tuple(frame.convert("RGB") for frame in frames)

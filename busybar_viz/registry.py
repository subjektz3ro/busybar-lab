"""Closed adapter registry used by every CLI and HTTP entry point."""

from __future__ import annotations

from functools import lru_cache

from .adapters.base import ScenarioAdapter
from .models import RenderedSegment, RenderRequest, ScenarioSpec


_DISPLAY_IDS = frozenset({"front", "back"})


@lru_cache(maxsize=1)
def adapters() -> tuple[ScenarioAdapter, ...]:
    # Keep imports lazy: application modules are large and may read environment
    # at import time.  Merely asking for CLI help must stay side-effect free.
    from .adapters.conformance import ConformanceAdapter
    from .adapters.skystrip import SkystripAdapter
    from .declared import DeclaredAdapter

    # Registration is deliberately explicit.  Neither the CLI nor a future
    # HTTP surface may turn a request value into a module path or entrypoint.
    # DeclaredAdapter serves `[<app>.viz]` tables from apps.toml — checked-in
    # data naming a seam inside the apps package, still never request data.
    return (ConformanceAdapter(), SkystripAdapter(), DeclaredAdapter())


def _registrations() -> tuple[tuple[ScenarioSpec, ScenarioAdapter], ...]:
    """Validate and pair every declared scenario with its owning adapter."""

    registrations: list[tuple[ScenarioSpec, ScenarioAdapter]] = []
    adapter_ids: set[str] = set()
    scenario_ids: set[str] = set()
    for adapter in adapters():
        adapter_id = getattr(adapter, "id", None)
        if not isinstance(adapter_id, str) or not adapter_id:
            raise RuntimeError("registered adapters must have a non-empty string id")
        if adapter_id in adapter_ids:
            raise RuntimeError(f"duplicate registered adapter: {adapter_id}")
        adapter_ids.add(adapter_id)
        declared = adapter.scenarios()
        if not isinstance(declared, tuple):
            raise RuntimeError(
                f"adapter {adapter_id!r} scenarios must be returned as a tuple"
            )
        for spec in declared:
            if not isinstance(spec, ScenarioSpec):
                raise RuntimeError(
                    f"adapter {adapter_id!r} returned an invalid scenario declaration"
                )
            if spec.adapter != adapter_id:
                raise RuntimeError(
                    f"scenario {spec.id!r} names adapter {spec.adapter!r}, "
                    f"but is declared by {adapter_id!r}"
                )
            if spec.id in scenario_ids:
                raise RuntimeError(f"duplicate registered scenario: {spec.id}")
            scenario_ids.add(spec.id)
            expected = spec.expected_displays
            if (
                not isinstance(expected, tuple)
                or not expected
                or len(expected) != len(set(expected))
                or not set(expected) <= _DISPLAY_IDS
            ):
                raise RuntimeError(
                    f"scenario {spec.id!r} must declare a non-empty unique "
                    "subset of front/back displays"
                )
            registrations.append((spec, adapter))
    return tuple(registrations)


def scenarios() -> tuple[ScenarioSpec, ...]:
    return tuple(spec for spec, _adapter in _registrations())


def _registration_for(
    scenario_id: str,
) -> tuple[ScenarioSpec, ScenarioAdapter]:
    matches = tuple(
        registration for registration in _registrations()
        if registration[0].id == scenario_id
    )
    if not matches:
        raise KeyError(f"unknown scenario: {scenario_id}")
    # _registrations() rejects duplicates before returning, but retain this
    # assertion at the lookup boundary so future refactors stay fail closed.
    if len(matches) != 1:
        raise RuntimeError(f"duplicate registered scenario: {scenario_id}")
    return matches[0]


def scenario_for(scenario_id: str) -> ScenarioSpec:
    return _registration_for(scenario_id)[0]


def adapter_for_scenario(scenario_id: str) -> ScenarioAdapter:
    return _registration_for(scenario_id)[1]


def render_registered(request: RenderRequest) -> RenderedSegment:
    """Render through the closed registry and enforce its display contract."""

    spec, adapter = _registration_for(request.scenario_id)
    segment = adapter.render(request)
    actual = tuple(track.id for track in segment.displays)
    if len(actual) != len(set(actual)) or set(actual) != set(spec.expected_displays):
        raise ValueError(
            f"scenario {spec.id!r} expected displays "
            f"{spec.expected_displays!r}, got {actual!r}"
        )
    return segment

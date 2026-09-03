"""The closed registry enforces scenario declarations at every render entry."""

from __future__ import annotations

import pytest
from PIL import Image

from busybar_viz.models import (
    Confidence,
    DisplayTrack,
    RenderedSegment,
    RenderRequest,
    ScenarioSpec,
)
from busybar_viz import registry


class _Adapter:
    id = "fixture"

    def __init__(self, displays: tuple[str, ...]) -> None:
        self.displays = displays

    def scenarios(self) -> tuple[ScenarioSpec, ...]:
        return (ScenarioSpec(
            "fixture/default",
            "Fixture",
            "Registry contract fixture.",
            self.id,
            expected_displays=("front",),
        ),)

    def render(self, request: RenderRequest) -> RenderedSegment:
        return RenderedSegment(displays=tuple(
            DisplayTrack(
                display,
                (Image.new("RGB", (72, 16)),),
                1,
                Confidence.SOURCE_EXACT,
            )
            for display in self.displays
        ))


def test_registered_render_accepts_the_declared_display_set(monkeypatch):
    adapter = _Adapter(("front",))
    monkeypatch.setattr(registry, "adapters", lambda: (adapter,))

    segment = registry.render_registered(
        RenderRequest.from_values("fixture/default")
    )

    assert tuple(track.id for track in segment.displays) == ("front",)


@pytest.mark.parametrize("displays", (("back",), ("front", "front")))
def test_registered_render_rejects_missing_extra_or_duplicate_displays(
    monkeypatch, displays,
):
    adapter = _Adapter(displays)
    monkeypatch.setattr(registry, "adapters", lambda: (adapter,))

    with pytest.raises(ValueError, match="expected displays"):
        registry.render_registered(RenderRequest.from_values("fixture/default"))


def test_registry_rejects_duplicate_expected_displays_before_render(monkeypatch):
    adapter = _Adapter(("front",))
    adapter.scenarios = lambda: (ScenarioSpec(  # type: ignore[method-assign]
        "fixture/default",
        "Fixture",
        "Invalid registry declaration.",
        adapter.id,
        expected_displays=("front", "front"),
    ),)
    monkeypatch.setattr(registry, "adapters", lambda: (adapter,))

    with pytest.raises(RuntimeError, match="non-empty unique subset"):
        registry.render_registered(RenderRequest.from_values("fixture/default"))


def test_registry_rejects_a_scenario_claimed_by_another_adapter(monkeypatch):
    declaring = _Adapter(("front",))
    declaring.id = "declaring"
    declaring.scenarios = lambda: (ScenarioSpec(  # type: ignore[method-assign]
        "fixture/default",
        "Fixture",
        "Invalid registry declaration.",
        "executing",
        expected_displays=("front",),
    ),)
    executing = _Adapter(("front",))
    executing.id = "executing"
    executing.scenarios = lambda: ()  # type: ignore[method-assign]
    monkeypatch.setattr(registry, "adapters", lambda: (declaring, executing))

    with pytest.raises(RuntimeError, match="declared by 'declaring'"):
        registry.render_registered(RenderRequest.from_values("fixture/default"))


def test_registry_rejects_duplicate_scenario_and_adapter_ids(monkeypatch):
    first = _Adapter(("front",))
    second = _Adapter(("front",))
    monkeypatch.setattr(registry, "adapters", lambda: (first, second))

    with pytest.raises(RuntimeError, match="duplicate registered adapter"):
        registry.scenarios()

    second.id = "second"
    second.scenarios = lambda: (ScenarioSpec(  # type: ignore[method-assign]
        "fixture/default",
        "Fixture again",
        "Duplicate scenario.",
        second.id,
        expected_displays=("front",),
    ),)
    with pytest.raises(RuntimeError, match="duplicate registered scenario"):
        registry.scenarios()

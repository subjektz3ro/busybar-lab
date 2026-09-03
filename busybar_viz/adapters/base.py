"""Adapter protocol. HTTP callers can select IDs, never Python entrypoints."""

from __future__ import annotations

from typing import Protocol

from busybar_viz.models import RenderedSegment, RenderRequest, ScenarioSpec


class ScenarioAdapter(Protocol):
    id: str

    def scenarios(self) -> tuple[ScenarioSpec, ...]: ...

    def render(self, request: RenderRequest) -> RenderedSegment: ...

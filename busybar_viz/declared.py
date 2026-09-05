"""Default scenarios declared in apps.toml — registration as data, not code.

An app that exposes one pure zero-argument renderer should not need a
hand-written adapter module, a test file, and registry edits before an agent
can see it. A `[<app>.viz]` table in `apps.toml` declares the seam and what
must read; this module materializes one `<app>/default` scenario per
declaration through a single generic adapter.

The registry stays closed. The declaration can only name a module inside the
repository's `apps` package, the parse fails loudly on anything it does not
recognise, and HTTP/CLI request data still selects scenario ids, never code.
Hand-written adapters remain the tier for typed controls, semantic input
replay, fault injection, and ink-reference proofs — design knowledge a
declaration cannot carry.
"""

from __future__ import annotations

import importlib
import re
import sys
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path

from PIL import Image

from .limits import validate_timing
from .models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    EvidenceLevel,
    RegionSpec,
    RenderedSegment,
    RenderRequest,
    ScenarioSpec,
)
from .profiles import DISPLAY_PROFILES, profile_for
from .sources import app_source_paths

_REGION_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_INK_COLOR = re.compile(r"#?[0-9A-Fa-f]{6}\Z")
_APP_NAME = re.compile(r"[a-z0-9_-]{1,32}\Z")
_MODULE_PART = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_VIZ_KEYS = frozenset({"renderer", "displays", "description", "regions", "scenarios"})
_SCENARIO_KEYS = frozenset({"renderer", "displays", "description", "regions"})
_REGION_KEYS = frozenset({
    "rect", "ink", "max_isolated", "display", "contrast_mode",
})
_SCENARIO_NAME = re.compile(r"[a-z0-9_-]{1,32}\Z")


@dataclass(frozen=True, slots=True)
class DeclaredRegion:
    name: str
    display: str
    rect: tuple[int, int, int, int]
    inks: tuple[str, ...]
    max_isolated: int
    contrast_mode: str


@dataclass(frozen=True, slots=True)
class DeclaredViz:
    app: str
    scenario: str
    module: str
    function: str
    displays: tuple[str, ...]
    description: str
    regions: tuple[DeclaredRegion, ...]

    @property
    def scenario_id(self) -> str:
        return f"{self.app}/{self.scenario}"

    @property
    def renderer_path(self) -> str:
        return str(Path(*self.module.split(".")).with_suffix(".py"))


def find_checkout(start: Path | None = None) -> Path:
    """Locate the active checkout independently of this package's install path."""

    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / "AGENTS.md").is_file() and (directory / "apps").is_dir():
            return directory
    raise ValueError("declared visualizer scenarios need a BUSY Bar Lab checkout")


def _error(app: str, message: str) -> ValueError:
    return ValueError(f"apps.toml [{app}.viz]: {message}")


def _parse_renderer(app: str, value: object) -> tuple[str, str]:
    if not isinstance(value, str):
        raise _error(app, "renderer must be a 'apps.module:function' string")
    module, separator, function = value.partition(":")
    parts = module.split(".")
    if (
        not separator
        or parts[0] != "apps"
        or len(parts) < 2
        or any(not _MODULE_PART.fullmatch(part) for part in parts[1:])
        or not _MODULE_PART.fullmatch(function)
    ):
        raise _error(
            app,
            f"renderer {value!r} must name a function inside the apps package, "
            "e.g. 'apps.myapp:render_visual'",
        )
    return module, function


def _parse_displays(app: str, value: object) -> tuple[str, ...]:
    if value is None:
        return ("front",)
    if (
        not isinstance(value, list)
        or not value
        or len(value) != len(set(value))
        or not set(value) <= set(DISPLAY_PROFILES)
    ):
        raise _error(app, "displays must be a non-empty unique subset of front/back")
    return tuple(value)


def _parse_region(
    app: str,
    name: object,
    value: object,
    displays: tuple[str, ...],
) -> DeclaredRegion:
    if not isinstance(name, str) or not _REGION_NAME.fullmatch(name):
        raise _error(app, f"region names must be 1-32 safe characters: {name!r}")
    if not isinstance(value, dict):
        raise _error(app, f"region {name} must be a table")
    unknown = set(value) - _REGION_KEYS
    if unknown:
        raise _error(app, f"region {name} has unknown keys: {', '.join(sorted(unknown))}")

    display = value.get("display", displays[0])
    if display not in displays:
        raise _error(app, f"region {name} names undeclared display {display!r}")
    profile = profile_for(display)

    rect_value = value.get("rect")
    if (
        not isinstance(rect_value, list)
        or len(rect_value) != 4
        or any(isinstance(item, bool) or not isinstance(item, int)
               for item in rect_value)
    ):
        raise _error(app, f"region {name} rect must be [x0, y0, x1, y1] integers")
    x0, y0, x1, y1 = rect_value
    if not (0 <= x0 < x1 <= profile.width and 0 <= y0 < y1 <= profile.height):
        raise _error(
            app,
            f"region {name} rect must be a half-open box inside the {display} "
            f"display's {profile.width}x{profile.height}",
        )

    ink_value = value.get("ink", [])
    if not isinstance(ink_value, list) or any(
        not isinstance(item, str) or not _INK_COLOR.fullmatch(item)
        for item in ink_value
    ):
        raise _error(app, f"region {name} ink must be a list of #RRGGBB colours")
    inks = tuple("#" + item.lstrip("#").upper() for item in ink_value)

    max_isolated = value.get("max_isolated", 0)
    if isinstance(max_isolated, bool) or not isinstance(max_isolated, int) \
            or max_isolated < 0:
        raise _error(app, f"region {name} max_isolated must be an integer >= 0")

    contrast_mode = value.get("contrast_mode", "luminance")
    if contrast_mode not in {"luminance", "luminance_or_channel"}:
        raise _error(
            app,
            f"region {name} contrast_mode must be 'luminance' or "
            "'luminance_or_channel'",
        )
    if contrast_mode == "luminance_or_channel" and not inks:
        raise _error(
            app,
            f"region {name} contrast_mode requires at least one declared ink",
        )

    return DeclaredRegion(
        name, display, (x0, y0, x1, y1), inks, max_isolated, contrast_mode,
    )


def _parse_scenario(
    app: str,
    scenario: str,
    table: dict,
    *,
    allowed_keys: frozenset[str],
) -> DeclaredViz:
    unknown = set(table) - allowed_keys
    if unknown:
        raise _error(app, f"unknown keys: {', '.join(sorted(unknown))}")
    if "renderer" not in table:
        raise _error(app, "renderer is required")
    module, function = _parse_renderer(app, table["renderer"])
    displays = _parse_displays(app, table.get("displays"))
    description = table.get(
        "description",
        f"Deterministic scenario declared by [{app}.viz] in apps.toml.",
    )
    if not isinstance(description, str) or not description:
        raise _error(app, "description must be a non-empty string")
    regions_value = table.get("regions", {})
    if not isinstance(regions_value, dict):
        raise _error(app, "regions must be a table of region tables")
    regions = tuple(
        _parse_region(app, name, value, displays)
        for name, value in sorted(regions_value.items())
    )
    return DeclaredViz(
        app, scenario, module, function, displays, description, regions,
    )


def load_declarations(repo_root: Path) -> tuple[DeclaredViz, ...]:
    """Parse every `[<app>.viz]` table, failing loudly on anything unrecognised.

    A top-level renderer declares the app's `default` scenario. Additional
    named scenarios — other views over their own fixed fixtures — live under
    `[<app>.viz.scenarios.<name>]` with the same keys.
    """

    manifest = repo_root / "apps.toml"
    if not manifest.is_file():
        return ()
    document = tomllib.loads(manifest.read_text(encoding="utf-8"))

    declarations: list[DeclaredViz] = []
    for app, table in document.items():
        if not isinstance(table, dict) or "viz" not in table:
            continue
        if not _APP_NAME.fullmatch(app):
            raise ValueError(f"apps.toml declares viz for an unsafe app name: {app!r}")
        viz = table["viz"]
        if not isinstance(viz, dict):
            raise _error(app, "must be a table")
        named = viz.get("scenarios", {})
        if not isinstance(named, dict):
            raise _error(app, "scenarios must be a table of scenario tables")
        if "renderer" in viz:
            declarations.append(_parse_scenario(
                app, "default",
                {key: value for key, value in viz.items() if key != "scenarios"},
                allowed_keys=_SCENARIO_KEYS,
            ))
        elif not named:
            raise _error(app, "renderer is required")
        else:
            unknown = set(viz) - {"scenarios"}
            if unknown:
                raise _error(
                    app,
                    "a viz table without a top-level renderer may only "
                    f"contain scenarios, not: {', '.join(sorted(unknown))}",
                )
        for name, value in sorted(named.items()):
            if not isinstance(name, str) or not _SCENARIO_NAME.fullmatch(name):
                raise _error(app, f"scenario names must be 1-32 safe characters: {name!r}")
            if name == "default" and "renderer" in viz:
                raise _error(app, "scenario 'default' collides with the top-level renderer")
            if not isinstance(value, dict):
                raise _error(app, f"scenario {name} must be a table")
            declarations.append(_parse_scenario(
                app, name, value, allowed_keys=_SCENARIO_KEYS,
            ))
    return tuple(declarations)


def _import_renderer(declaration: DeclaredViz, repo_root: Path):
    renderer_file = repo_root / declaration.renderer_path
    if not renderer_file.is_file():
        raise _error(
            declaration.app,
            f"renderer module file does not exist: {declaration.renderer_path}",
        )
    root = str(repo_root)
    if root not in sys.path:
        sys.path.insert(0, root)
    module = importlib.import_module(declaration.module)
    renderer = getattr(module, declaration.function, None)
    if not callable(renderer):
        raise _error(
            declaration.app,
            f"{declaration.module}:{declaration.function} is not a callable renderer",
        )
    return renderer


def _wrap_production(declaration: DeclaredViz, value: object) -> RenderedSegment:
    if not isinstance(value, Mapping):
        raise _error(declaration.app, "the renderer must return a display mapping")
    actual = tuple(value)
    if set(actual) != set(declaration.displays):
        raise _error(
            declaration.app,
            f"expected display tracks {declaration.displays!r}, got {actual!r}",
        )
    tracks: list[DisplayTrack] = []
    checks: list[CheckSpec] = []
    for display_id in declaration.displays:
        item = value[display_id]
        if (not isinstance(item, tuple) or len(item) != 2
                or not isinstance(item[0], Sequence)):
            raise _error(
                declaration.app,
                "each display value must be (sequence_of_frames, fps)",
            )
        raw_frames, fps = item
        frames = tuple(raw_frames)
        if not frames or any(not isinstance(frame, Image.Image) for frame in frames):
            raise _error(
                declaration.app, "display frames must be non-empty PIL sequences",
            )
        if any(frame.mode != "RGB" for frame in frames):
            raise _error(declaration.app, "display frames must use RGB mode")
        validate_timing(frame_count=len(frames), fps=fps)
        expected_size = profile_for(display_id).size
        if any(frame.size != expected_size for frame in frames):
            raise _error(
                declaration.app,
                f"display track {display_id!r} must contain "
                f"{expected_size[0]}x{expected_size[1]} frames",
            )
        tracks.append(DisplayTrack(
            display_id,
            tuple(frame.copy() for frame in frames),
            fps,
            Confidence.SOURCE_EXACT,
        ))
        checks.append(CheckSpec.create(
            f"{display_id}-dimensions",
            "frame.dimensions",
            display=display_id,
            size=expected_size,
        ))
        checks.append(CheckSpec.create(
            f"{display_id}-metrics",
            "frame.summary_metrics",
            severity="info",
            display=display_id,
        ))
        checks.append(CheckSpec.create(
            f"{display_id}-loop-seam",
            "animation.loop_seam",
            severity="info",
            display=display_id,
        ))

    region_specs: list[RegionSpec] = []
    for region in declaration.regions:
        region_specs.append(RegionSpec(
            region.name, display=region.display, rect=region.rect,
        ))
        checks.append(CheckSpec.create(
            f"{region.name}-body",
            "region.min_feature_size",
            display=region.display,
            region=region.name,
            max_isolated=region.max_isolated,
        ))
        if region.inks:
            checks.append(CheckSpec.create(
                f"{region.name}-contrast",
                "region.contrast_floor",
                display=region.display,
                region=region.name,
                ink=list(region.inks),
                contrast_mode=region.contrast_mode,
            ))

    return RenderedSegment(
        displays=tuple(tracks),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=tuple(region_specs),
        checks=tuple(checks),
        notes=(
            f"Rendered through the production seam declared by [{declaration.app}.viz] "
            "in apps.toml; the declaration is data, not adapter code.",
        ),
        source_paths=(
            declaration.renderer_path,
            "busybar_viz/declared.py",
        ),
    )


class DeclaredAdapter:
    """One generic adapter serving every `[<app>.viz]` declaration."""

    id = "declared"

    def scenarios(self) -> tuple[ScenarioSpec, ...]:
        repo_root = find_checkout()
        return tuple(
            ScenarioSpec(
                declaration.scenario_id,
                f"{declaration.app} {declaration.scenario}",
                declaration.description,
                self.id,
                expected_displays=declaration.displays,
            )
            for declaration in load_declarations(repo_root)
        )

    def render(self, request: RenderRequest) -> RenderedSegment:
        repo_root = find_checkout()
        declaration = next(
            (item for item in load_declarations(repo_root)
             if item.scenario_id == request.scenario_id),
            None,
        )
        if declaration is None:
            raise KeyError(f"unknown declared scenario: {request.scenario_id}")
        if request.parameters or request.inputs:
            raise ValueError(
                "declared default scenarios accept no controls or inputs; "
                "promote the app to a hand-written adapter for those"
            )
        renderer = _import_renderer(declaration, repo_root)
        segment = _wrap_production(declaration, renderer())
        return replace(segment, source_paths=tuple(sorted({
            *segment.source_paths, *app_source_paths(repo_root),
        })))

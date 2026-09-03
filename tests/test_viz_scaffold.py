"""A scaffold is safe source generation, not runtime module discovery."""

from __future__ import annotations

import ast
import sys
from types import ModuleType

import pytest
from PIL import Image

from busybar_viz.limits import LimitError
from busybar_viz.models import RenderRequest
from busybar_viz.scaffold import (
    AdapterScaffoldSpec,
    ScaffoldCollisionError,
    create_adapter_scaffold,
    plan_adapter_scaffold,
)


def _spec(**changes) -> AdapterScaffoldSpec:
    values = {
        "adapter_id": "status_board",
        "renderer_module": "apps.status_board",
        "renderer_name": "render_viz_segment",
        "scenario_name": "main-view",
        "title": "Status board: main view",
        "description": "Pinned production-renderer state.",
        "expected_displays": ("front", "back"),
    }
    values.update(changes)
    return AdapterScaffoldSpec(**values)


def test_plan_is_deterministic_static_source_and_does_not_touch_the_registry():
    plan = plan_adapter_scaffold(_spec())
    adapter_path, test_path = plan.files
    adapter_source = plan.files[adapter_path]

    assert adapter_path.as_posix() == "busybar_viz/adapters/status_board.py"
    assert test_path.as_posix() == "tests/test_viz_status_board_adapter.py"
    assert adapter_source == plan_adapter_scaffold(_spec()).files[adapter_path]
    assert "from apps.status_board import render_viz_segment as renderer" in adapter_source
    assert "importlib" not in adapter_source
    assert "http" not in adapter_source.lower()
    assert "validate_timing" in adapter_source
    generated_test = plan.files[test_path]
    assert "offline_render" in generated_test
    assert "with offline_render(REPO)" in generated_test
    assert "ArtifactStore" in generated_test
    assert "exact_tracks(repeated) == exact_tracks(segment)" in generated_test
    assert plan.registry_import == (
        "from .adapters.status_board import StatusBoardAdapter"
    )
    assert plan.registry_constructor == "StatusBoardAdapter()"
    compile(adapter_source, str(adapter_path), "exec")
    compile(plan.files[test_path], str(test_path), "exec")
    ast.parse(adapter_source)


def test_generated_adapter_calls_the_named_renderer_and_checks_both_tracks(monkeypatch):
    source = next(iter(plan_adapter_scaffold(_spec()).files.values()))
    app_package = ModuleType("apps")
    app_package.__path__ = []  # type: ignore[attr-defined]
    renderer_module = ModuleType("apps.status_board")
    seen = []

    def render_viz_segment():
        seen.append("called")
        return {
            "front": ((Image.new("RGB", (72, 16)),), 1),
            "back": ((Image.new("RGB", (160, 80)),), 1),
        }

    renderer_module.render_viz_segment = render_viz_segment  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "apps", app_package)
    monkeypatch.setitem(sys.modules, "apps.status_board", renderer_module)
    namespace = {"__name__": "generated_status_board"}
    exec(compile(source, "generated_status_board.py", "exec"), namespace)

    request = RenderRequest.from_values("status_board/main-view")
    segment = namespace["StatusBoardAdapter"]().render(request)

    assert seen == ["called"]
    assert tuple(track.id for track in segment.displays) == ("front", "back")
    assert "busybar_viz" not in render_viz_segment.__module__


def test_generated_adapter_rejects_invalid_timing_before_publication(monkeypatch):
    source = next(iter(plan_adapter_scaffold(_spec()).files.values()))
    app_package = ModuleType("apps")
    app_package.__path__ = []  # type: ignore[attr-defined]
    renderer_module = ModuleType("apps.status_board")
    renderer_module.render_viz_segment = lambda: {  # type: ignore[attr-defined]
        "front": ((Image.new("RGB", (72, 16)),), 0),
        "back": ((Image.new("RGB", (160, 80)),), 0),
    }
    monkeypatch.setitem(sys.modules, "apps", app_package)
    monkeypatch.setitem(sys.modules, "apps.status_board", renderer_module)
    namespace = {"__name__": "generated_status_board"}
    exec(compile(source, "generated_status_board.py", "exec"), namespace)

    with pytest.raises(LimitError, match="fps"):
        namespace["StatusBoardAdapter"]().render(
            RenderRequest.from_values("status_board/main-view")
        )


def test_generated_adapter_rejects_non_rgb_frames_instead_of_converting_them(
    monkeypatch,
):
    source = next(iter(plan_adapter_scaffold(_spec()).files.values()))
    app_package = ModuleType("apps")
    app_package.__path__ = []  # type: ignore[attr-defined]
    renderer_module = ModuleType("apps.status_board")
    renderer_module.render_viz_segment = lambda: {  # type: ignore[attr-defined]
        "front": ((Image.new("RGBA", (72, 16), (255, 0, 0, 0)),), 1),
        "back": ((Image.new("RGB", (160, 80)),), 1),
    }
    monkeypatch.setitem(sys.modules, "apps", app_package)
    monkeypatch.setitem(sys.modules, "apps.status_board", renderer_module)
    namespace = {"__name__": "generated_status_board"}
    exec(compile(source, "generated_status_board.py", "exec"), namespace)

    with pytest.raises(ValueError, match="RGB mode"):
        namespace["StatusBoardAdapter"]().render(
            RenderRequest.from_values("status_board/main-view")
        )


def test_create_writes_the_pair_exclusively_and_reports_reviewed_next_steps(tmp_path):
    root = tmp_path / "checkout"
    (root / "busybar_viz" / "adapters").mkdir(parents=True)
    (root / "tests").mkdir()
    spec = _spec()
    plan = plan_adapter_scaffold(spec)

    result = create_adapter_scaffold(root, spec)

    assert tuple(path.relative_to(root) for path in result.created) == tuple(plan.files)
    assert {
        path.relative_to(root): path.read_text(encoding="utf-8")
        for path in result.created
    } == dict(plan.files)
    assert "registry.py" in result.next_steps[0]
    assert "explicit adapters()" in result.next_steps[1]


def test_any_preexisting_target_aborts_before_writing_another_file(tmp_path):
    root = tmp_path / "checkout"
    adapter_dir = root / "busybar_viz" / "adapters"
    adapter_dir.mkdir(parents=True)
    (root / "tests").mkdir()
    owned = adapter_dir / "status_board.py"
    owned.write_text("user-owned\n", encoding="utf-8")

    with pytest.raises(ScaffoldCollisionError) as caught:
        create_adapter_scaffold(root, _spec())

    assert caught.value.paths == (owned,)
    assert owned.read_text(encoding="utf-8") == "user-owned\n"
    assert not (root / "tests" / "test_viz_status_board_adapter.py").exists()


def test_second_create_refuses_without_changing_either_file(tmp_path):
    root = tmp_path / "checkout"
    (root / "busybar_viz" / "adapters").mkdir(parents=True)
    (root / "tests").mkdir()
    spec = _spec()
    first = create_adapter_scaffold(root, spec)
    before = {path: path.read_bytes() for path in first.created}

    with pytest.raises(ScaffoldCollisionError):
        create_adapter_scaffold(root, spec)

    assert {path: path.read_bytes() for path in first.created} == before


@pytest.mark.parametrize(
    "changes",
    [
        {"adapter_id": "../escape"},
        {"adapter_id": "Capitalized"},
        {"adapter_id": "class"},
        {"renderer_module": "apps.status-board"},
        {"renderer_module": ".apps.status_board"},
        {"renderer_module": "apps.class"},
        {"renderer_name": "renderer()"},
        {"renderer_name": "class"},
        {"scenario_name": "not_a_slug"},
        {"expected_displays": ()},
        {"expected_displays": ["front"]},
        {"expected_displays": ("front", "front")},
        {"expected_displays": ("front", "side")},
    ],
)
def test_untrusted_names_and_display_profiles_are_rejected(changes):
    with pytest.raises(ValueError):
        _spec(**changes)


def test_checkout_and_parent_symlink_must_stay_inside_the_root(tmp_path):
    root = tmp_path / "checkout"
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "busybar_viz").mkdir(parents=True)
    (root / "busybar_viz" / "adapters").symlink_to(outside, target_is_directory=True)
    (root / "tests").mkdir()

    with pytest.raises(ValueError, match="unsafe scaffold target"):
        create_adapter_scaffold(root, _spec())

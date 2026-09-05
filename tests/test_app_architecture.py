"""Executable ownership rules for the first-party application packages."""

from __future__ import annotations

import ast
from graphlib import TopologicalSorter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def module_name(path: Path) -> str:
    parts = list(path.relative_to(ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def app_modules(app: str) -> dict[str, Path]:
    return {
        module_name(path): path for path in (ROOT / "apps" / f"{app}_app").rglob("*.py")
    }


def dependency_graph(modules: dict[str, Path]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for name, path in modules.items():
        dependencies: set[str] = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                dependencies.update(
                    alias.name for alias in node.names if alias.name in modules
                )
            elif isinstance(node, ast.ImportFrom):
                parent = node.module or ""
                if node.level:
                    package = (
                        name if path.name == "__init__.py" else name.rsplit(".", 1)[0]
                    )
                    base = package.split(".")[
                        : len(package.split(".")) - node.level + 1
                    ]
                    parent = ".".join([*base, *([parent] if parent else [])])
                if parent in modules:
                    dependencies.add(parent)
                dependencies.update(
                    f"{parent}.{alias.name}"
                    for alias in node.names
                    if f"{parent}.{alias.name}" in modules
                )
        graph[name] = dependencies - {name}
    return graph


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
def test_app_import_graph_is_acyclic(app):
    graph = dependency_graph(app_modules(app))
    assert set(TopologicalSorter(graph).static_order()) == set(graph)


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
def test_renderers_cannot_import_runtime_or_io_owners_transitively(app):
    graph = dependency_graph(app_modules(app))
    prefix = f"apps.{app}_app."
    forbidden = ("runtime", "input", "cli", "providers", "device", "audio")
    for renderer in (name for name in graph if name.startswith(prefix + "render.")):
        pending, reached = [renderer], set()
        while pending:
            current = pending.pop()
            if current in reached:
                continue
            reached.add(current)
            pending.extend(graph[current] - reached)
        assert not {
            name
            for name in reached
            if name.removeprefix(prefix).split(".")[0] in forbidden
        }, renderer


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
def test_packages_have_no_eager_import_facade_and_launchers_stay_small(app):
    for path in (ROOT / "apps" / f"{app}_app").rglob("__init__.py"):
        body = ast.parse(path.read_text()).body
        assert (
            len(body) == 1
            and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)
        ), path
    launcher = ROOT / "apps" / f"{app}.py"
    assert len(launcher.read_text().splitlines()) <= 40


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
def test_app_owners_do_not_grow_back_into_monoliths(app):
    oversized = {
        name: len(path.read_text().splitlines())
        for name, path in app_modules(app).items()
        if len(path.read_text().splitlines()) > 750
    }
    assert not oversized, (
        f"Split by responsibility before adding more code: {oversized}"
    )


@pytest.mark.parametrize(
    ("app", "module"),
    [
        ("dsn", "audio.words"),
        ("skystrip", "audio.report_plain"),
        ("skystrip", "audio.report_genz"),
    ],
)
def test_speech_composition_does_not_load_rendering_or_io(app, module):
    graph = dependency_graph(app_modules(app))
    prefix = f"apps.{app}_app."
    pending, reached = [prefix + module], set()
    while pending:
        current = pending.pop()
        if current in reached:
            continue
        reached.add(current)
        pending.extend(graph[current] - reached)
    forbidden = {"render", "device", "providers", "runtime", "cli", "input"}
    assert not {
        name for name in reached if name.removeprefix(prefix).split(".")[0] in forbidden
    }


def test_named_radio_bands_have_exactly_one_visual_mapping():
    from apps.dsn_app.formatting import NAMED_RF_BANDS
    from apps.dsn_app.render.palette import BAND_PULSE

    assert set(BAND_PULSE) == NAMED_RF_BANDS

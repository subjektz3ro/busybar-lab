"""Thin launchers must not hide the implementation behind visual evidence."""

from pathlib import Path

import pytest

from busybar_viz.adapters.skystrip import SkystripAdapter
from busybar_viz.declared import DeclaredAdapter
from busybar_viz.models import RenderRequest
from busybar_viz.sources import app_source_paths

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(
    "scenario",
    [
        "skystrip/status-clock",
        "skystrip/thunder-loop",
        "skystrip/lightning-near",
        "skystrip/lightning-distant",
    ],
)
def test_skystrip_provenance_includes_its_production_implementation(scenario):
    segment = SkystripAdapter().render(RenderRequest.from_values(scenario))
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "apps/skystrip_app").rglob("*.py")
    }
    expected.update(
        {
            "apps/skystrip.py",
            "busybar_viz/adapters/skystrip.py",
            "apps/assets/house.png",
        }
    )
    assert expected <= set(segment.source_paths)


def test_source_inventory_includes_helpers_but_not_owner_or_generated_data(tmp_path):
    included = [
        "apps/demo.py",
        "apps/demo_app/render/scene.py",
        "busybar_dev/pixels.py",
        "apps/assets/house.png",
        "apps.toml",
    ]
    excluded = [
        ".env",
        "config/demo.env",
        "state/session.json",
        "apps/__pycache__/demo.pyc",
        "apps/assets/private.env",
        "scratch/preview.png",
    ]
    for name in included + excluded:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture")
    assert app_source_paths(tmp_path) == tuple(
        sorted(
            [
                *included,
                "busybar_viz/sources.py",
            ]
        )
    )


@pytest.mark.parametrize("scenario", ["dsn/default", "dsn/distance", "dsn/instrument"])
def test_declared_provenance_includes_code_behind_the_public_renderer(scenario):
    segment = DeclaredAdapter().render(RenderRequest.from_values(scenario))
    expected = {
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "apps/dsn_app").rglob("*.py")
    }
    expected.update({"apps/dsn.py", "apps.toml", "busybar_viz/declared.py"})
    assert expected <= set(segment.source_paths)

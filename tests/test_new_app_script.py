"""scripts/new_app.py creates born-visible apps and never overwrites."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import new_app  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def checkout(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# fixture\n")
    (tmp_path / "apps").mkdir()
    template = (REPO / "apps" / "_template.py").read_text(encoding="utf-8")
    (tmp_path / "apps" / "_template.py").write_text(template)
    (tmp_path / "apps.toml").write_text('[existing]\nkind = "foreground"\n')
    return tmp_path


def test_creates_app_and_catalog_entry(checkout):
    lines = new_app.create_app(checkout, "pomodoro", "A focus timer")

    body = (checkout / "apps" / "pomodoro.py").read_text()
    assert 'default="pomodoro"' in body
    manifest = (checkout / "apps.toml").read_text()
    parsed = tomllib.loads(manifest)
    assert parsed["pomodoro"]["entrypoint"] == "apps/pomodoro.py"
    assert parsed["pomodoro"]["description"] == "A focus timer"
    assert "# [pomodoro.viz]" in manifest
    assert "viz" not in parsed["pomodoro"]  # stays commented until the seam exists
    assert any("busybar-viz view" in line for line in lines)
    assert any("busybar-viz run pomodoro/default" in line for line in lines)
    run_step = next(i for i, line in enumerate(lines) if "busybar-viz run" in line)
    baseline_step = next(
        i for i, line in enumerate(lines) if "baseline update" in line
    )
    assert run_step < baseline_step


def test_description_is_safely_serialized_as_toml(checkout):
    description = 'A "quoted" path like C:\\bar stays literal'
    new_app.create_app(checkout, "quoted", description)

    parsed = tomllib.loads((checkout / "apps.toml").read_text())
    assert parsed["quoted"]["description"] == description


def test_refuses_collisions_and_bad_names(checkout):
    new_app.create_app(checkout, "pomodoro", "A focus timer")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        new_app.create_app(checkout, "pomodoro", "again")
    (checkout / "apps" / "pomodoro.py").unlink()
    with pytest.raises(SystemExit, match="already declares"):
        new_app.create_app(checkout, "pomodoro", "again")

    (checkout / "apps" / "loose.py").write_text("# stray file\n")
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        new_app.create_app(checkout, "loose", "collides with a file")

    for bad in ("My-Idea", "1app", "has space", "app-dash"):
        with pytest.raises(SystemExit, match="lowercase module names"):
            new_app.create_app(checkout, bad, "bad name")

    for description in ("", "two\nlines", "carriage\rreturn", "tab\there"):
        with pytest.raises(SystemExit, match="single line"):
            new_app.create_app(checkout, "ok_name", description)


def test_refuses_invalid_manifest_and_changed_template_without_partial_app(checkout):
    (checkout / "apps.toml").write_text("[broken\n")
    with pytest.raises(SystemExit, match="invalid .*apps.toml"):
        new_app.create_app(checkout, "pomodoro", "A timer")
    assert not (checkout / "apps" / "pomodoro.py").exists()

    (checkout / "apps.toml").write_text('[existing]\nkind = "foreground"\n')
    (checkout / "apps" / "_template.py").write_text("# marker removed\n")
    with pytest.raises(SystemExit, match="default app-name marker"):
        new_app.create_app(checkout, "pomodoro", "A timer")
    assert not (checkout / "apps" / "pomodoro.py").exists()


def test_manifest_write_failure_rolls_back_new_module(checkout, monkeypatch):
    original_manifest = (checkout / "apps.toml").read_text()

    def fail_manifest(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(new_app, "_replace_manifest", fail_manifest)
    with pytest.raises(OSError, match="disk full"):
        new_app.create_app(checkout, "pomodoro", "A timer")

    assert not (checkout / "apps" / "pomodoro.py").exists()
    assert (checkout / "apps.toml").read_text() == original_manifest


def test_broken_symlink_is_an_existing_collision(checkout, tmp_path):
    outside = tmp_path / "does-not-exist.py"
    (checkout / "apps" / "linked.py").symlink_to(outside)
    with pytest.raises(SystemExit, match="refusing to overwrite"):
        new_app.create_app(checkout, "linked", "Do not follow links")
    assert not outside.exists()

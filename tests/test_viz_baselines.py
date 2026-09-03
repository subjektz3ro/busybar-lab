"""Committed pixel baselines fail when — and only when — the pixels change."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from busybar_viz import cli
from busybar_viz.baselines import (
    BASELINE_FILE,
    compare_baselines,
    load_baselines,
    pixel_digests,
    write_baselines,
)
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    RenderedSegment,
    ScenarioSpec,
)


def _segment(color=(10, 20, 30), fps=5) -> RenderedSegment:
    return RenderedSegment(
        displays=(DisplayTrack(
            "front",
            (Image.new("RGB", (72, 16), color),),
            fps,
            Confidence.SOURCE_EXACT,
        ),),
        checks=(CheckSpec.create(
            "front-dimensions", "frame.dimensions",
            display="front", size=(72, 16),
        ),),
    )


def test_pixel_digests_track_content_fps_and_nothing_else():
    base = pixel_digests(_segment())
    assert base == pixel_digests(_segment())
    assert pixel_digests(_segment(color=(10, 20, 31))) != base
    assert pixel_digests(_segment(fps=6)) != base
    assert list(base) == ["front"]


def test_baseline_file_round_trips_and_defaults_empty(tmp_path):
    assert load_baselines(tmp_path) == {}
    values = {
        "app/default": {"front": "a" * 64},
        "other/one": {"back": "b" * 64, "front": "c" * 64},
    }
    path = write_baselines(tmp_path, values)
    assert path.name == BASELINE_FILE
    assert load_baselines(tmp_path) == values
    assert path.read_text().startswith("# viz-baselines.toml")

    path.write_text('["bad"]\nfront = 3\n')
    with pytest.raises(ValueError, match="digest strings"):
        load_baselines(tmp_path)


def test_compare_classifies_changed_missing_and_stale():
    accepted = {"a/one": {"front": "x"}, "gone/old": {"front": "y"}}
    rendered = {"a/one": {"front": "z"}, "new/two": {"front": "w"}}
    result = compare_baselines(accepted, rendered)
    assert result["ok"] is False
    assert set(result["changed"]) == {"a/one"}
    assert result["missing_baselines"] == ["new/two"]
    assert result["stale_baselines"] == ["gone/old"]
    assert compare_baselines(rendered, rendered)["ok"] is True


class _FakeArtifact:
    artifact_id = "f" * 64
    path = Path("/fake/artifact")
    passed = True


class _FakeStore:
    def __init__(self, *_args, **_kwargs):
        pass

    def publish(self, _request, _segment):
        return _FakeArtifact()


@pytest.fixture
def baseline_cli(tmp_path, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("# fixture\n")
    (tmp_path / "apps").mkdir()
    spec = ScenarioSpec("fake/default", "Fake", "Fixture", "fake")
    state = {"segment": _segment()}
    monkeypatch.setattr(cli, "find_repo_root", lambda: tmp_path)
    monkeypatch.setattr(cli, "ArtifactStore", _FakeStore)
    monkeypatch.setattr(cli, "scenarios", lambda: (spec,))
    monkeypatch.setattr(
        cli, "render_registered", lambda _request: state["segment"],
    )
    return tmp_path, state


def test_baseline_update_then_check_then_drift(baseline_cli, capsys):
    tmp_path, state = baseline_cli

    assert cli.main(("baseline", "update", "--json")) == 0
    written = json.loads(capsys.readouterr().out)
    assert written["updated"] == ["fake/default"]
    assert load_baselines(tmp_path)["fake/default"]

    assert cli.main(("baseline", "check", "--json")) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True

    state["segment"] = _segment(color=(200, 0, 0))
    assert cli.main(("baseline", "check", "--json")) == 1
    result = json.loads(capsys.readouterr().out)
    assert set(result["changed"]) == {"fake/default"}
    assert result["drifted_artifacts"]["fake/default"]["artifact_id"] == "f" * 64

    assert cli.main(("baseline", "update", "--json")) == 0
    capsys.readouterr()
    assert cli.main(("baseline", "check", "--json")) == 0


def test_baseline_check_flags_missing_and_stale_entries(baseline_cli, capsys):
    tmp_path, _state = baseline_cli
    write_baselines(tmp_path, {"gone/old": {"front": "y" * 64}})

    assert cli.main(("baseline", "check", "--json")) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["missing_baselines"] == ["fake/default"]
    assert result["stale_baselines"] == ["gone/old"]


def test_baseline_update_rejects_unknown_scenarios(baseline_cli, capsys):
    assert cli.main(("baseline", "update", "nope/missing", "--json")) == 2
    error = json.loads(capsys.readouterr().out)
    assert "unknown scenarios" in error["error"]

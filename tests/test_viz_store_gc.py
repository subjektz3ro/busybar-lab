"""gc keeps everything cited or fresh, and dry-runs unless told otherwise."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from busybar_viz import cli
from busybar_viz import store_gc
from busybar_viz.journal import SessionJournal
from busybar_viz.store_gc import GcPlan, apply_gc, plan_gc, referenced_artifact_ids


def _make_entry(root: Path, content_id: str, *, age_s: float, body: bytes = b"x") -> Path:
    path = root / content_id[:2] / content_id
    path.mkdir(parents=True)
    (path / "manifest.json").write_bytes(body)
    old = os.stat(path / "manifest.json").st_mtime - age_s
    os.utime(path / "manifest.json", (old, old))
    os.utime(path, (old, old))
    return path


def _ids(prefix: str) -> str:
    return (prefix * 64)[:64]


def test_plan_keeps_referenced_and_recent_entries(tmp_path):
    data = tmp_path / "viz"
    referenced = _ids("a")
    stale = _ids("b")
    fresh = _ids("c")
    _make_entry(data / "artifacts", referenced, age_s=7 * 24 * 3600)
    _make_entry(data / "artifacts", stale, age_s=7 * 24 * 3600)
    _make_entry(data / "artifacts", fresh, age_s=60)

    journal = SessionJournal(data / "sessions.sqlite3")
    session, _event = journal.create_session("gc fixture")
    journal.append_event(
        session.id, expected_revision=session.revision, kind="agent.note",
        actor="agent", body={"message": "cited"}, artifact_id=referenced,
    )

    assert referenced in referenced_artifact_ids(data / "sessions.sqlite3")
    plan = plan_gc(data, now=os.stat(data / "artifacts").st_mtime + 1)
    assert plan.delete_artifacts == (stale,)
    assert set(plan.keep_artifacts) == {referenced, fresh}
    assert plan.bytes_reclaimable > 0


def test_comparisons_survive_only_with_intact_endpoints(tmp_path):
    data = tmp_path / "viz"
    kept = _ids("a")
    doomed = _ids("b")
    _make_entry(data / "artifacts", kept, age_s=60)
    _make_entry(data / "artifacts", doomed, age_s=7 * 24 * 3600)

    intact = _make_entry(data / "comparisons", _ids("d"), age_s=7 * 24 * 3600)
    (intact / "comparison.json").write_text(
        json.dumps({"before": kept, "after": kept}),
    )
    broken = _make_entry(data / "comparisons", _ids("e"), age_s=7 * 24 * 3600)
    (broken / "comparison.json").write_text(
        json.dumps({"before": kept, "after": doomed}),
    )
    for path in (intact, broken):
        old = os.stat(path / "manifest.json").st_mtime
        os.utime(path / "comparison.json", (old, old))
        os.utime(path, (old, old))

    plan = plan_gc(data, now=os.stat(data / "artifacts").st_mtime + 1)
    assert plan.delete_artifacts == (doomed,)
    assert plan.delete_comparisons == (_ids("e"),)

    apply_gc(data, plan)
    assert not (data / "artifacts" / doomed[:2]).exists()
    assert (data / "comparisons" / _ids("d")[:2] / _ids("d")).is_dir()


def test_recent_comparison_roots_its_old_endpoints(tmp_path):
    data = tmp_path / "viz"
    before = _ids("a")
    after = _ids("b")
    _make_entry(data / "artifacts", before, age_s=7 * 24 * 3600)
    _make_entry(data / "artifacts", after, age_s=7 * 24 * 3600)

    comparison_id = _ids("c")
    comparison = _make_entry(
        data / "comparisons", comparison_id, age_s=60,
    )
    (comparison / "comparison.json").write_text(json.dumps({
        "before": before,
        "after": after,
    }))

    plan = plan_gc(data, now=os.stat(comparison / "comparison.json").st_mtime + 1)
    assert plan.delete_artifacts == ()
    assert plan.delete_comparisons == ()
    assert set(plan.keep_artifacts) == {before, after}


def test_gc_ignores_malformed_or_missharded_store_entries(tmp_path):
    data = tmp_path / "viz"
    malformed = "g" * 64
    _make_entry(data / "artifacts", malformed, age_s=7 * 24 * 3600)

    valid_but_missharded = _ids("b")
    misplaced = data / "artifacts" / "aa" / valid_but_missharded
    misplaced.mkdir(parents=True)
    (misplaced / "manifest.json").write_text("fixture")

    plan = plan_gc(data, now=os.stat(misplaced).st_mtime + 7 * 24 * 3600)
    assert plan.delete_artifacts == ()

    unsafe = GcPlan((".." + "a" * 62,), (), (), 0)
    with pytest.raises(ValueError, match="unsafe gc content id"):
        apply_gc(data, unsafe)
    assert misplaced.is_dir()


def test_apply_gc_propagates_deletion_failures(tmp_path, monkeypatch):
    data = tmp_path / "viz"
    stale = _ids("b")
    target = _make_entry(
        data / "artifacts", stale, age_s=7 * 24 * 3600,
    )
    plan = GcPlan((stale,), (), (), 1)

    def fail_delete(_path):
        raise PermissionError("fixture refusal")

    monkeypatch.setattr(store_gc.shutil, "rmtree", fail_delete)
    with pytest.raises(PermissionError, match="fixture refusal"):
        apply_gc(data, plan)
    assert target.is_dir()


def test_gc_cli_is_dry_run_by_default(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "find_repo_root", lambda: tmp_path)
    data = tmp_path / "viz"
    stale = _ids("b")
    _make_entry(data / "artifacts", stale, age_s=7 * 24 * 3600)

    assert cli.main(("--data-dir", str(data), "gc", "--json")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["deleted"] is False
    assert result["artifacts"] == [stale]
    assert (data / "artifacts" / stale[:2] / stale).is_dir()

    assert cli.main(("--data-dir", str(data), "gc", "--delete", "--json")) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["deleted"] is True
    assert not (data / "artifacts" / stale[:2] / stale).exists()

    assert cli.main((
        "--data-dir", str(data), "gc", "--keep-recent-hours", "-1", "--json",
    )) == 2
    capsys.readouterr()
    assert cli.main((
        "--data-dir", str(data), "gc", "--keep-recent-hours", "nan", "--json",
    )) == 2

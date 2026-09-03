"""Reclaim unreferenced evidence without ever touching cited history.

Content-addressed stores never overwrite, so `scratch/busybar-viz` only
grows. Most artifacts are iteration debris within hours of being rendered;
the ones that matter are cited — by a session journal event, by a session's
current-artifact pointer, or by being recent enough that a loop may still be
mid-flight. The plan keeps every one of those and lists the rest.

Baselines are deliberately not a reference source: `viz-baselines.toml` pins
pixel digests, not artifact ids, and deterministic renders recreate their
artifacts on demand. Deleting an artifact never deletes acceptance.

The default is a dry run. Nothing is removed without `--delete`.
"""

from __future__ import annotations

import json
import math
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_ID_LENGTH = 64
_CONTENT_ID = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class GcPlan:
    delete_artifacts: tuple[str, ...]
    delete_comparisons: tuple[str, ...]
    keep_artifacts: tuple[str, ...]
    bytes_reclaimable: int


def referenced_artifact_ids(journal_path: Path) -> frozenset[str]:
    """Every artifact id cited by any session event or current pointer."""

    if not journal_path.is_file():
        return frozenset()
    from .journal import SessionJournal

    return SessionJournal(journal_path).referenced_artifact_ids()


def _entries(root: Path) -> dict[str, Path]:
    """Map content ids to their directories under a two-level store root."""

    entries: dict[str, Path] = {}
    if not root.is_dir():
        return entries
    for shard in sorted(root.iterdir()):
        if (
            shard.is_symlink()
            or not shard.is_dir()
            or not re.fullmatch(r"[0-9a-f]{2}", shard.name)
        ):
            continue
        for entry in sorted(shard.iterdir()):
            if (
                not entry.is_symlink()
                and entry.is_dir()
                and len(entry.name) == _ID_LENGTH
                and _CONTENT_ID.fullmatch(entry.name)
                and entry.name.startswith(shard.name)
            ):
                entries[entry.name] = entry
    return entries


def _tree_bytes(path: Path) -> int:
    return sum(
        item.stat().st_size for item in path.rglob("*") if item.is_file()
    )


def _newest_mtime(path: Path) -> float:
    newest = path.stat().st_mtime
    for item in path.rglob("*"):
        newest = max(newest, item.stat().st_mtime)
    return newest


def _comparison_endpoints(path: Path) -> tuple[str, ...]:
    try:
        summary = json.loads(
            (path / "comparison.json").read_text(encoding="utf-8"),
        )
        before, after = summary["before"], summary["after"]
        if (
            isinstance(before, str)
            and isinstance(after, str)
            and _CONTENT_ID.fullmatch(before)
            and _CONTENT_ID.fullmatch(after)
        ):
            return (before, after)
    except (OSError, ValueError, KeyError):
        pass
    return ()


def plan_gc(
    data_root: Path,
    *,
    now: float,
    keep_recent_s: float = 24 * 3600.0,
) -> GcPlan:
    """Decide what is reclaimable; deciding never deletes anything."""

    if not math.isfinite(now):
        raise ValueError("gc current time must be finite")
    if not math.isfinite(keep_recent_s) or keep_recent_s < 0:
        raise ValueError("gc recent-retention window must be finite and non-negative")

    referenced = referenced_artifact_ids(data_root / "sessions.sqlite3")
    artifacts = _entries(data_root / "artifacts")
    comparisons = _entries(data_root / "comparisons")

    keep = {
        artifact_id
        for artifact_id, path in artifacts.items()
        if artifact_id in referenced or now - _newest_mtime(path) < keep_recent_s
    }

    # A recent comparison is part of the active iteration window. Preserve
    # both of its existing endpoints with it so a dry-run can never propose a
    # plan that keeps a comparison while breaking the evidence it compares.
    comparison_state: dict[str, tuple[Path, tuple[str, ...], bool]] = {}
    for comparison_id, path in comparisons.items():
        endpoints = _comparison_endpoints(path)
        fresh = now - _newest_mtime(path) < keep_recent_s
        comparison_state[comparison_id] = (path, endpoints, fresh)
        if fresh and len(endpoints) == 2 and all(
            endpoint in artifacts for endpoint in endpoints
        ):
            keep.update(endpoints)

    delete_artifacts: list[str] = []
    reclaimable = 0
    for artifact_id, path in artifacts.items():
        if artifact_id not in keep:
            delete_artifacts.append(artifact_id)
            reclaimable += _tree_bytes(path)

    delete_comparisons: list[str] = []
    for comparison_id, (path, endpoints, _fresh) in comparison_state.items():
        intact = len(endpoints) == 2 and all(item in keep for item in endpoints)
        if intact:
            continue
        delete_comparisons.append(comparison_id)
        reclaimable += _tree_bytes(path)

    return GcPlan(
        tuple(delete_artifacts),
        tuple(delete_comparisons),
        tuple(sorted(keep)),
        reclaimable,
    )


def apply_gc(data_root: Path, plan: GcPlan) -> None:
    """Remove exactly what the plan lists, shard directories included."""

    for kind, ids in (
        ("artifacts", plan.delete_artifacts),
        ("comparisons", plan.delete_comparisons),
    ):
        root = data_root / kind
        for content_id in ids:
            if not _CONTENT_ID.fullmatch(content_id):
                raise ValueError(f"refusing unsafe gc content id: {content_id!r}")
            target = root / content_id[:2] / content_id
            shard = target.parent
            if not target.exists():
                continue
            if shard.is_symlink() or target.is_symlink() or not target.is_dir():
                raise ValueError(f"refusing unsafe gc target: {target}")
            try:
                target.resolve().relative_to(root.resolve())
            except ValueError as exc:
                raise ValueError(f"refusing gc target outside store: {target}") from exc
            # Propagate failures. The CLI must not report `deleted: true`
            # after an ignored permission or I/O error left evidence behind.
            shutil.rmtree(target)
            if shard.is_dir() and not any(shard.iterdir()):
                shard.rmdir()

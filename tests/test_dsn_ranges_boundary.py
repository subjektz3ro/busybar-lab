"""The DSN range service is cohesive, explicit and import-compatible."""

from __future__ import annotations

import ast
from dataclasses import replace
import inspect
from pathlib import Path
import subprocess
import sys
import time

import apps.dsn as dsn
import apps.dsn_ranges as ranges


ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_reexports_the_range_contract() -> None:
    """Existing callers retain one facade while the range owner is separate."""
    for name in (
        "RangeState",
        "HorizonsUnavailable",
        "horizons_au",
        "range_ttl_s",
        "range_cache_fresh",
    ):
        assert getattr(dsn, name) is getattr(ranges, name)

    for name in (
        "HORIZONS",
        "AU_LIGHT_S",
        "RANGE_NEAR_EARTH_TTL_S",
        "RANGE_INTERMEDIATE_TTL_S",
        "RANGE_TTL_S",
        "RANGE_RETRY_S",
        "RANGE_UNAVAILABLE_RETRY_S",
        "RANGE_CACHE_VERSION",
    ):
        assert getattr(dsn, name) == getattr(ranges, name)


def test_state_delegates_all_range_stores_to_one_owner() -> None:
    assert "range_state" in dsn.State.__dataclass_fields__
    assert {
        "ranges", "range_retry_at", "range_unavailable",
    }.isdisjoint(dsn.State.__dataclass_fields__)
    assert set(ranges.RangeState.__dataclass_fields__) == {
        "values", "retry_at", "unavailable",
    }


def test_range_worker_declares_every_runtime_dependency() -> None:
    assert set(inspect.signature(ranges.poll_ranges).parameters) == {
        "state",
        "links_getter",
        "wake",
        "client_factory",
        "headers",
        "endpoint",
        "clock",
        "sleep",
        "parser",
        "persist",
        "logger",
    }


def test_runtime_configured_cache_path_round_trips(tmp_path) -> None:
    cache_dir = tmp_path / "configured-range-cache"
    config = replace(
        dsn.DEFAULT_DSN_CONFIG,
        managed_cache_root=tmp_path,
        cache_dir=cache_dir,
    )
    previous = dsn.RUNTIME_CONFIG
    observed_at = time.time()
    try:
        dsn.apply_runtime_config(config)
        state = dsn.State(
            range_state=dsn.RangeState(
                values={-32: (21_000_000_000.0, observed_at)},
            ),
        )
        dsn.save_ranges(state)
        loaded = dsn.State()
        dsn.load_ranges(loaded)

        assert dsn.RANGE_CACHE == cache_dir / "dsn_ranges.json"
        assert loaded.range_state.values == state.range_state.values
    finally:
        dsn.apply_runtime_config(previous)


def test_range_module_never_imports_the_entrypoint() -> None:
    tree = ast.parse(Path(ranges.__file__).read_text())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "dsn" not in imported
    assert "apps.dsn" not in imported


def test_standalone_entrypoint_import_uses_the_same_range_objects() -> None:
    """``python apps/dsn.py`` and package import follow the same seam."""
    code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(ROOT / "apps")!r})
import dsn
import dsn_ranges
assert dsn.RangeState is dsn_ranges.RangeState
assert dsn.horizons_au is dsn_ranges.horizons_au
assert dsn.range_cache_fresh is dsn_ranges.range_cache_fresh
"""

    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

"""Filesystem configuration stays inside a managed cache boundary.

Every declared app key is settable over Barkeep's API, so a path-valued key
must not grant arbitrary directory creation or writes.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import dsn  # noqa: E402


def test_blank_uses_the_dedicated_repo_cache_directory(tmp_path):
    path, warning = dsn.resolve_cache_dir("", tmp_path)
    assert path == tmp_path / "cache" / "dsn"
    assert warning == ""
    assert dsn.resolve_cache_dir(None, tmp_path)[0] == tmp_path / "cache" / "dsn"


def test_a_path_inside_the_checkout_is_honoured(tmp_path):
    inside = tmp_path / "config" / "caches"
    path, warning = dsn.resolve_cache_dir(str(inside), tmp_path)
    assert path == inside.resolve()
    assert warning == ""


def test_an_existing_outside_directory_is_honoured(tmp_path):
    """An operator moving the cache off an SD card creates it once."""
    elsewhere = tmp_path.parent / f"{tmp_path.name}-external"
    elsewhere.mkdir()
    path, warning = dsn.resolve_cache_dir(str(elsewhere), tmp_path)
    assert path == elsewhere.resolve()
    assert warning == ""


def test_a_nonexistent_outside_path_is_refused_not_created(tmp_path):
    """The mkdir was the reachable part: a path the daemon's user can write
    would have been created on the next restart, and restart is an
    unauthenticated API call."""
    target = tmp_path.parent / f"{tmp_path.name}-attacker" / "deep" / "tree"
    path, warning = dsn.resolve_cache_dir(str(target), tmp_path)
    assert path == tmp_path / "cache" / "dsn", "must fall back, not obey"
    assert "does not exist" in warning
    assert not target.exists(), "resolving must never create the directory"


def test_the_fallback_is_reported_not_silent(tmp_path):
    _, warning = dsn.resolve_cache_dir("/nonexistent/somewhere", tmp_path)
    assert warning and "/nonexistent/somewhere" in warning


def test_a_malformed_value_falls_back(tmp_path):
    path, warning = dsn.resolve_cache_dir("\x00bad", tmp_path)
    assert path == tmp_path / "cache" / "dsn"
    assert warning


def test_malformed_managed_root_cannot_crash_module_startup(tmp_path):
    path, warning = dsn.resolve_managed_cache_root("\x00bad", tmp_path)
    assert path == tmp_path / "cache"
    assert "BUSYBAR_CACHE_DIR is unusable" in warning


def test_managed_child_accepts_only_the_installer_managed_cache_root(tmp_path):
    managed_root = tmp_path / "managed-cache"
    selected = managed_root / "dsn-custom"
    path, warning = dsn.resolve_cache_dir(
        str(selected),
        tmp_path,
        managed_cache_root=managed_root,
        managed=True,
    )
    assert path == selected.resolve()
    assert warning == ""


def test_managed_child_resolves_relative_subdirectories_below_managed_root(
        tmp_path):
    managed_root = tmp_path / "managed-cache"

    path, warning = dsn.resolve_cache_dir(
        "dsn-custom",
        tmp_path,
        managed_cache_root=managed_root,
        managed=True,
    )

    assert path == managed_root / "dsn-custom"
    assert warning == ""


def test_managed_child_falls_back_before_writing_an_unlisted_path(tmp_path):
    managed_root = tmp_path / "managed-cache"
    outside = tmp_path / "elsewhere"
    outside.mkdir()

    path, warning = dsn.resolve_cache_dir(
        str(outside),
        tmp_path,
        managed_cache_root=managed_root,
        managed=True,
    )

    assert path == managed_root / "dsn"
    assert "outside the service-managed cache root" in warning
    assert "BUSYBAR_CACHE_DIR" in warning

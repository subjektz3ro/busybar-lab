"""Reproducibility contracts for the redistributable documentation snapshot."""

from __future__ import annotations

import re
from pathlib import Path

from scripts import refresh_docs


ROOT = Path(__file__).resolve().parent.parent


def test_busylib_snapshot_is_pinned_and_recorded_in_notice():
    revision = refresh_docs.BUSYLIB_REVISION

    assert re.fullmatch(r"[0-9a-f]{40}", revision)
    assert revision in (ROOT / "NOTICE.md").read_text()

    source = (ROOT / "scripts" / "refresh_docs.py").read_text()
    fetch = source.index('"fetch", "--quiet", "--depth", "1"')
    checkout = source.index('"checkout", "--quiet", "--detach", "FETCH_HEAD"')
    assert fetch < checkout
    assert "BUSYLIB_REPO, BUSYLIB_REVISION" in source

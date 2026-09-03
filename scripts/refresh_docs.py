"""Refresh redistributable and owner-local reference documentation.

    uv run scripts/refresh_docs.py

Updates, under ``docs/``:

- ``api/openapi.yaml`` — the spec served by the owner's device. The file is
  gitignored because the vendor has not granted redistribution permission.
- ``busylib/`` — MIT-licensed docs, README, AGENTS.md, and examples from
  ``busy-app/busylib-py``. The upstream licence is copied with the material.

The script intentionally does not scrape or mirror docs.busy.app. Use the
vendor's published documentation at https://docs.busy.app instead.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
# Prefer the bar's deterministic USB address. When Wi-Fi is also configured,
# busybar.local resolves to both interfaces and can make this one-shot refresh
# hang against the API-disabled address.
DEVICE = "http://10.0.4.20"
BUSYLIB_REPO = "https://github.com/busy-app/busylib-py"
# Keep the redistributable snapshot reproducible and reviewable. Updating this
# revision is a deliberate dependency change that must be reflected in NOTICE.
BUSYLIB_REVISION = "23875e1c0201265365ab78ed9a1caa98d21de8ad"


def fetch(url: str, timeout: int = 20) -> bytes:
    return urllib.request.urlopen(url, timeout=timeout).read()


def fetch_device_spec() -> None:
    dest = DOCS / "api" / "openapi.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        dest.write_bytes(fetch(f"{DEVICE}/openapi.yaml", timeout=5))
        print("device spec: refreshed from the bar")
    except OSError as exc:
        print(f"device spec: kept existing copy (bar unreachable: {exc})")


def vendor_busylib() -> None:
    dest = DOCS / "busylib"
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(
            ["git", "init", "--quiet", tmp],
            check=True, capture_output=True,
        )
        subprocess.run(
            [
                "git", "-C", tmp, "fetch", "--quiet", "--depth", "1",
                BUSYLIB_REPO, BUSYLIB_REVISION,
            ],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "-C", tmp, "checkout", "--quiet", "--detach", "FETCH_HEAD"],
            check=True, capture_output=True,
        )
        src = Path(tmp)
        shutil.rmtree(dest, ignore_errors=True)
        dest.mkdir(parents=True)
        for sub in ("docs/api", "docs/guides", "examples"):
            shutil.copytree(src / sub, dest / Path(sub).name)
        # LICENSE is not optional. busylib is MIT, which permits this copy
        # only if the licence text and copyright notice travel with it. It
        # was missing for a long time, which made every vendored copy a
        # technical violation of the one term MIT asks for.
        for f in ("docs/index.md", "README.md", "AGENTS.md", "LICENSE"):
            shutil.copy(src / f, dest / Path(f).name)
        for css in dest.rglob("*.css"):
            css.unlink()
    print(
        "busylib: re-vendored docs, README, AGENTS.md, examples, LICENSE "
        f"at {BUSYLIB_REVISION[:12]}"
    )


if __name__ == "__main__":
    fetch_device_spec()
    vendor_busylib()

"""Skystrip build info."""

from __future__ import annotations

import subprocess

from apps.skystrip_app import config as _config


def _git_rev() -> str:
    try:
        return (
            subprocess.run(
                ["git", "rev-parse", "--short", "HEAD"],
                cwd=_config.REPO_ROOT,
                capture_output=True,
                text=True,
                timeout=5,
            ).stdout.strip()
            or "unknown"
        )
    except Exception:  # noqa: BLE001 - a missing git never blocks the sky
        return "unknown"

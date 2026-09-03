"""Desired state: which foreground is selected, which backgrounds are enabled.
Persisted so a reboot restores the same lineup."""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class DesiredState:
    foreground: str | None = None
    enabled_backgrounds: set[str] = field(default_factory=set)


def load_state(path: Path, default_foreground: str | None = None) -> DesiredState:
    if not path.is_file():
        return DesiredState(default_foreground, set())
    try:
        data = json.loads(path.read_text())
        fg = data["foreground"]
        backgrounds = data["enabled_backgrounds"]
        # Shape-check before trusting it: set("doorbell") would otherwise
        # silently become {'d','o','r','b','e','l'} and enable six ghosts.
        if fg is not None and not isinstance(fg, str):
            raise TypeError(f"foreground must be a string or null, got {fg!r}")
        if not isinstance(backgrounds, list):
            raise TypeError(
                f"enabled_backgrounds must be a list, got {type(backgrounds).__name__}")
        return DesiredState(fg, {str(b) for b in backgrounds})
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        log.warning("state file %s unreadable (%s); starting from defaults", path, exc)
        return DesiredState(default_foreground, set())


def save_state(path: Path, state: DesiredState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps({
        "foreground": state.foreground,
        "enabled_backgrounds": sorted(state.enabled_backgrounds),
    }, indent=2)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)

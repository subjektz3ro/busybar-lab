"""Skystrip selection."""

from __future__ import annotations

import contextlib
import os
import tempfile
from pathlib import Path

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings


def load_scene_idx() -> int:
    """Resume the saved scene, or start at the first enabled one.

    The file stores a NAME, so narrowing the enabled set needs no migration:
    a scene you just switched off simply isn't found, and the bar comes up on
    your first enabled scene rather than one you disabled.
    """
    try:
        return _settings.ENABLED_SCENES.index(_settings.SCENE_FILE.read_text().strip())
    except (OSError, ValueError):
        return 0


def save_scene_idx(idx: int) -> bool:
    """Atomically persist the selected scene in the managed state directory.

    A scene choice is user intent, but losing it must not take the live sky
    down. Report a persistence failure loudly and let the in-memory selection
    continue until the next restart.
    """
    temporary: Path | None = None
    try:
        _settings.SCENE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_temporary = tempfile.mkstemp(
            dir=_settings.SCENE_FILE.parent,
            prefix=f".{_settings.SCENE_FILE.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(_settings.ENABLED_SCENES[idx % len(_settings.ENABLED_SCENES)])
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, _settings.SCENE_FILE)
        temporary = None
        return True
    except OSError as exc:
        _limits.logger.warning(
            "scene state not persisted to %s: %s", _settings.SCENE_FILE, exc
        )
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _has_committed_start_view(state: _model.SkyState) -> bool:
    """Whether an accepted Skystrip view can support UNKNOWN START ownership.

    A successful scene draw records ``current_scene_file``.  Alert-first
    startup has no scene yet, so its accepted draw is represented by the
    matching alert generation.  Neither fact alone proves ownership: the Busy
    snapshot check below is still mandatory while the selector is unknown.
    """
    return state.current_scene_file is not None or (
        state.visual_alert is not None
        and not state.alert_acked
        and state.alert_drawn_generation == state.alert_generation
    )

"""Two config layers per app: shared .env (hand-managed) under config/<app>.env
(UI-managed). Merged into the child's environment at spawn; the UI edits only
the per-app file."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Mapping

from busybar_dev.config import parse_env_text as shared_parse_env_text
from busybar_dev.config import is_single_line

from .registry import APP_NAME_RE, AppSpec


def parse_env_text(text: str) -> dict[str, str]:
    """Parse a per-app override file.

    Deliberately NOT quote-stripping, unlike the hand-edited repo-root .env:
    this file is written verbatim by the web UI, so an operator who types
    `"hello"` into a field must see `"hello"` when the editor loads it back.
    The shared parser owns the line handling; only that one flag differs.

    An empty value round-trips: `KEY=` is how a per-app override says
    "explicitly blank" (anonymous NWS contact, auto-discovered station) as
    opposed to "not set, inherit the shared layer".
    """
    return shared_parse_env_text(text, strip_quotes=False)


def read_env_file(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text())


def write_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    for key, value in values.items():
        # Second line of defence behind the API's validation: one key per line
        # is the whole format, so a newline here would forge extra env vars.
        if "=" in str(key) or not is_single_line(str(key)) or not is_single_line(str(value)):
            raise ValueError(f"env key/value must be single-line: {key!r}")
    body = "".join(f"{k}={v}\n" for k, v in values.items())
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(body)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def app_env_path(config_dir: Path, spec: AppSpec) -> Path:
    """Return the override path for a trusted, canonical registry spec.

    HTTP route text must never reach this filesystem boundary directly.  The
    caller first resolves it through the registry and passes the resulting
    ``AppSpec``; the repeated name check protects non-registry/test callers.
    """
    if not APP_NAME_RE.fullmatch(spec.name):
        raise ValueError("app spec has an invalid name")
    return config_dir / f"{spec.name}.env"


def effective_config(
    spec: AppSpec, app_values: dict[str, str], shared: Mapping[str, str]
) -> list[dict]:
    rows = []
    for key in spec.config:
        if key.name in app_values:
            value, source = app_values[key.name], "app"
        elif key.name in shared:
            value, source = shared[key.name], "shared"
        else:
            value, source = key.default, "default"
        rows.append({
            "name": key.name,
            "description": key.description,
            "default": key.default,
            "value": value,
            "source": source,
            "type": key.type,
            "choices": list(key.choices),
            "blank_is_value": key.blank_is_value,
            "minimum": key.minimum,
            "maximum": key.maximum,
            "requires": list(key.requires),
            "format": key.format,
        })
    return rows


def normalize_multiselect(raw: str, choices: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """Split a multiselect value into (selected, unknown).

    Selected comes back deduped and in the order the app declared its
    choices, so the stored value is canonical no matter what order the
    operator clicked or hand-edited.
    """
    picked = [p.strip() for p in raw.split(",")]
    picked = [p for p in picked if p]
    unknown = sorted({p for p in picked if p not in choices})
    selected = [c for c in choices if c in set(picked)]
    return selected, unknown


def child_env(
    app_values: dict[str, str], base: Mapping[str, str], *,
    allowed_keys: set[str] | None = None,
) -> dict[str, str]:
    """Layer safe overrides; registry callers also restrict their key names."""
    env = dict(base)
    env.update({
        key: value for key, value in app_values.items()
        if (allowed_keys is None or key in allowed_keys)
        and is_single_line(key) and is_single_line(value)
    })
    return env

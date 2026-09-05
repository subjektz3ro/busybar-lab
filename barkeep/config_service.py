"""Configuration transactions independent of HTTP and process supervision.

The API supplies a resolved registry spec and translates ValueError into 422.
Normalization, layer validation and persistence stay here, so another caller
can use exactly the same rules. One service belongs to one Barkeep daemon.
"""

from collections.abc import Mapping
from pathlib import Path
from threading import RLock

from busybar_dev.config import is_single_line

from . import configstore
from .config_validation import validate_effective_config, validate_submitted_values
from .registry import AppSpec


def prepare_config_update(
    spec: AppSpec, values: object, current: Mapping[str, str],
    shared: Mapping[str, str],
) -> dict[str, str]:
    """Build a complete validated candidate without changing any input."""
    if not isinstance(values, dict):
        raise ValueError("values must be an object")
    declared = {key.name for key in spec.config}
    unknown = sorted(set(values) - declared)
    if unknown:
        raise ValueError(f"undeclared config keys: {', '.join(unknown)}")
    coerced = {key: str(value) for key, value in values.items()}
    bad = sorted(key for key, value in coerced.items() if not is_single_line(value))
    if bad:
        raise ValueError(f"values must be single-line: {', '.join(bad)}")

    for key in spec.config:
        if key.type != "multiselect" or key.name not in coerced:
            continue
        selected, unknown = configstore.normalize_multiselect(
            coerced[key.name], key.choices)
        if unknown:
            raise ValueError(f"{key.name}: not valid choices: {', '.join(unknown)}")
        if not selected:
            raise ValueError(f"{key.name}: select at least one")
        coerced[key.name] = ",".join(selected)

    validation_error = validate_submitted_values(spec, coerced)
    if validation_error:
        raise ValueError(validation_error)

    blankable = {key.name for key in spec.config if key.blank_is_value}
    merged = dict(current)
    for key, value in coerced.items():
        if value == "" and key not in blankable:
            merged.pop(key, None)
        else:
            merged[key] = value
    validation_error = validate_effective_config(spec, merged, shared)
    if validation_error:
        raise ValueError(validation_error)
    return merged


class ConfigService:
    def __init__(self, config_dir: Path, shared: Mapping[str, str]):
        self.config_dir = config_dir
        self.shared = shared
        # FastAPI runs sync routes on worker threads. Atomic replace protects
        # one write, but only a lock across read/merge/write prevents two
        # overlapping requests from silently discarding one another's edits.
        self._lock = RLock()

    def rows(self, spec: AppSpec) -> list[dict]:
        with self._lock:
            path = configstore.app_env_path(self.config_dir, spec)
            return configstore.effective_config(
                spec, configstore.read_env_file(path), self.shared)

    def update(self, spec: AppSpec, values: object) -> list[dict]:
        with self._lock:
            path = configstore.app_env_path(self.config_dir, spec)
            merged = prepare_config_update(
                spec, values, configstore.read_env_file(path), self.shared)
            configstore.write_env_file(path, merged)
            return self.rows(spec)

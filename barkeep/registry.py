"""apps.toml parsing — the checked-in catalog of bar apps barkeep can run."""

from __future__ import annotations

import math
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

# foreground: owns the display continuously (skystrip).
# background: dark until an event, then takes the display for a bounded window
# and hands it back. NOT an overlay — the device accepts or rejects a draw by
# priority, it does not composite two application_names, so the last writer
# wins outright. See CLAUDE.md "PRIORITY IS NOT Z-ORDER".
KINDS = ("foreground", "background")
# enum picks one of `choices`; multiselect picks a subset, stored as a
# comma-separated string because config values reach apps as env vars.
CONFIG_TYPES = ("text", "number", "email", "enum", "multiselect")
CHOICE_TYPES = ("enum", "multiselect")
CONFIG_FORMATS = ("timezone",)
# App names become Python module names, config filenames, and URL path
# segments.  Keep one deliberately narrow grammar for all three surfaces.
APP_NAME_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


class RegistryError(Exception):
    """apps.toml is missing or malformed — fail loud, never half-load."""


@dataclass(frozen=True)
class ConfigKey:
    name: str
    description: str
    default: str
    type: str = "text"
    choices: tuple[str, ...] = ()
    blank_is_value: bool = False
    minimum: float | None = None
    maximum: float | None = None
    requires: tuple[str, ...] = ()
    format: str | None = None


@dataclass(frozen=True)
class AppSpec:
    name: str
    kind: str
    entrypoint: str
    description: str
    config: tuple[ConfigKey, ...] = field(default=())


def load_registry(path: Path) -> dict[str, AppSpec]:
    if not path.is_file():
        raise RegistryError(f"registry not found: {path} (expected apps.toml)")
    try:
        data = tomllib.loads(path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{path}: {exc}") from exc

    registry: dict[str, AppSpec] = {}
    for name, entry in data.items():
        if not APP_NAME_RE.fullmatch(name):
            raise RegistryError(
                f"{name!r}: app names must match [a-z][a-z0-9_]* "
                "and be at most 32 characters"
            )
        if not isinstance(entry, dict):
            raise RegistryError(f"{name}: expected a table, got {type(entry).__name__}")
        for req in ("kind", "entrypoint", "description"):
            if not isinstance(entry.get(req), str) or not entry.get(req):
                raise RegistryError(f"{name}: missing or empty '{req}'")
        if entry["kind"] not in KINDS:
            raise RegistryError(f"{name}: kind must be one of {KINDS}, got {entry['kind']!r}")
        keys = []
        for key_name, key_entry in entry.get("config", {}).items():
            if not isinstance(key_entry, dict) or "description" not in key_entry:
                raise RegistryError(f"{name}: config key {key_name} needs a description")
            choices = tuple(str(c) for c in key_entry.get("choices", []))
            ktype = str(key_entry.get("type", "enum" if choices else "text"))
            if ktype not in CONFIG_TYPES:
                raise RegistryError(
                    f"{name}: config key {key_name} type must be one of "
                    f"{CONFIG_TYPES}, got {ktype!r}")
            if ktype in CHOICE_TYPES and not choices:
                raise RegistryError(
                    f"{name}: config key {key_name} is {ktype} but lists no choices")
            if choices and ktype not in CHOICE_TYPES:
                raise RegistryError(
                    f"{name}: config key {key_name} has choices but type {ktype!r}")
            blank_is_value = key_entry.get("blank_is_value", False)
            if not isinstance(blank_is_value, bool):
                raise RegistryError(
                    f"{name}: config key {key_name} blank_is_value must be boolean")
            minimum = key_entry.get("minimum")
            maximum = key_entry.get("maximum")
            if minimum is not None or maximum is not None:
                if ktype != "number":
                    raise RegistryError(
                        f"{name}: config key {key_name} bounds require type number")
                if any(
                    isinstance(bound, bool) or not isinstance(bound, (int, float))
                    for bound in (minimum, maximum)
                    if bound is not None
                ):
                    raise RegistryError(
                        f"{name}: config key {key_name} bounds must be numbers")
                minimum = None if minimum is None else float(minimum)
                maximum = None if maximum is None else float(maximum)
                if ((minimum is not None and not math.isfinite(minimum))
                        or (maximum is not None and not math.isfinite(maximum))):
                    raise RegistryError(
                        f"{name}: config key {key_name} bounds must be finite")
                if minimum is not None and maximum is not None and minimum > maximum:
                    raise RegistryError(
                        f"{name}: config key {key_name} minimum exceeds maximum")
            requires_raw = key_entry.get("requires", [])
            if (not isinstance(requires_raw, list)
                    or not all(isinstance(item, str) and item for item in requires_raw)):
                raise RegistryError(
                    f"{name}: config key {key_name} requires must be a list of keys")
            if len(set(requires_raw)) != len(requires_raw):
                raise RegistryError(
                    f"{name}: config key {key_name} requires contains duplicates")
            config_format = key_entry.get("format")
            if config_format is not None and config_format not in CONFIG_FORMATS:
                raise RegistryError(
                    f"{name}: config key {key_name} format must be one of "
                    f"{CONFIG_FORMATS}, got {config_format!r}")
            keys.append(ConfigKey(
                name=key_name,
                description=str(key_entry["description"]),
                default=str(key_entry.get("default", "")),
                type=ktype,
                choices=choices,
                blank_is_value=blank_is_value,
                minimum=minimum,
                maximum=maximum,
                requires=tuple(requires_raw),
                format=config_format,
            ))
        declared = {key.name for key in keys}
        for key in keys:
            unknown_requires = sorted(set(key.requires) - declared)
            if key.name in key.requires:
                raise RegistryError(
                    f"{name}: config key {key.name} cannot require itself")
            if unknown_requires:
                raise RegistryError(
                    f"{name}: config key {key.name} requires undeclared keys: "
                    f"{', '.join(unknown_requires)}")
        registry[name] = AppSpec(
            name=name,
            kind=entry["kind"],
            entrypoint=entry["entrypoint"],
            description=entry["description"],
            config=tuple(keys),
        )
    return registry

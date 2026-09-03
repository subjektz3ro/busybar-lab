"""Generic validation for a registry-declared effective app configuration."""

from __future__ import annotations

import math
from collections.abc import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .configstore import effective_config
from .registry import AppSpec, ConfigKey


def _limit_label(value: float) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else str(number)


def _validate_value(key: ConfigKey, value: str) -> str | None:
    if not value:
        return None
    if key.type == "enum" and value not in key.choices:
        return f"{key.name}: must be one of: {', '.join(key.choices)}"
    if key.type == "number":
        try:
            number = float(value)
        except ValueError:
            return f"{key.name}: must be a finite number"
        if not math.isfinite(number):
            return f"{key.name}: must be a finite number"
        if (key.minimum is not None and number < key.minimum) or (
            key.maximum is not None and number > key.maximum
        ):
            if key.minimum is not None and key.maximum is not None:
                return (
                    f"{key.name} must be between {_limit_label(key.minimum)} "
                    f"and {_limit_label(key.maximum)}"
                )
            if key.minimum is not None:
                return f"{key.name} must be at least {_limit_label(key.minimum)}"
            assert key.maximum is not None
            return f"{key.name} must be at most {_limit_label(key.maximum)}"
    if key.format == "timezone":
        if len(value) > 255:
            return f"{key.name}: unknown IANA timezone"
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError, OSError):
            return f"{key.name}: unknown IANA timezone: {value}"
    return None


def validate_submitted_values(spec: AppSpec, values: Mapping[str, str]) -> str | None:
    """Validate scalar values present in one API request."""
    for key in spec.config:
        if key.name in values:
            error = _validate_value(key, values[key.name])
            if error:
                return error
    return None


def validate_effective_config(
    spec: AppSpec,
    app_values: Mapping[str, str],
    shared: Mapping[str, str],
) -> str | None:
    """Validate the complete layered candidate before it is persisted."""
    rows = effective_config(spec, dict(app_values), shared)
    values = {row["name"]: row["value"].strip() for row in rows}
    declared_order = {key.name: index for index, key in enumerate(spec.config)}

    for key in spec.config:
        error = _validate_value(key, values[key.name])
        if error:
            return error
        if values[key.name]:
            for required_name in key.requires:
                if not values[required_name]:
                    # Use registry order rather than request/dict order, so a
                    # symmetric constraint always returns the same message.
                    pair = sorted(
                        (key.name, required_name),
                        key=declared_order.__getitem__,
                    )
                    return f"{pair[0]} and {pair[1]} must be configured together"
    return None

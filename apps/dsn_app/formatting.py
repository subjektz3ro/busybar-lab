"""Shared DSN fact formatting for speech and compact display labels."""

from __future__ import annotations

import re

# The bands the app can name and explain; unknown source values stay unknown.
NAMED_RF_BANDS = frozenset({"S", "X", "K", "KA"})


def activity_badge(activity: str) -> str:
    """A compact, source-native description of what the dish is doing."""
    words = (activity or "").upper()
    if "DEMO" in words:
        return "DEMO"
    if "UPGRADE" in words:
        return "UPGRADE"
    if "ENGINEER" in words:
        return "ENGINEER"
    if all(word in words for word in ("TELEMETRY", "TRACK", "COMMAND")):
        return "TTC"
    if "TELEMETRY" in words:
        return "TELEM"
    if "TRACK" in words:
        return "TRACK"
    if "COMMAND" in words:
        return "COMMAND"
    # A prefix of an unknown source phrase is not an abbreviation; it is an
    # unexplained fragment. Keep the fact that an unrecognised activity was
    # published without pretending a clipped stem is meaningful.
    return "OTHER" if words else ""


RATE_LABEL_MAX_GBPS = 999.0

NARRATION_RECORD_DETAIL_MAX = 4

POWER_LABEL_MAX = 999.0


def dish_metres(dish: str, dish_types: dict[str, str] | None = None) -> str | None:
    """Published dish diameter, or None rather than an invented default."""
    kind = (dish_types or {}).get(dish, "")
    found = re.match(r"(\d+)", kind)
    if found:
        return found.group(1)
    return None

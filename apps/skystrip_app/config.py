"""Pure configuration and state-path policy for the Skystrip app.

This module deliberately does not read ``os.environ`` or load ``.env``. It
turns an explicit mapping into one immutable value; ``settings.py`` owns
applying that value to its long-running process state.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

LIGHTNING_WS_URL_MAX_CHARS = 2048
REPO_ROOT = Path(__file__).resolve().parents[2]
SCENES = ("house", "skyline", "lakefront", "forest", "grove", "backroads")


@dataclass(frozen=True)
class SkystripConfig:
    """Validated process configuration, independent of ambient environment."""

    latitude: float
    longitude: float
    location_set: bool
    timezone: ZoneInfo
    units: str
    clock_ink: str
    style: str
    report_voice: str
    christmas_window: str
    nws_station: str
    nws_user_agent: str
    lightning_ws: str | None
    state_root: Path
    enabled_scenes: tuple[str, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


def _validate_lightning_ws_endpoint(raw: str | None) -> str | None:
    """Return one operator-supplied secure endpoint, or ``None`` when off.

    This key is deliberately .env-only because Barkeep's config API doesn't
    redact declared values. Relay credentials may therefore live in userinfo,
    the path, or query string, but validation errors never quote the URL.
    Requiring TLS keeps that endpoint and its credentials off clear text.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError("SKYSTRIP_LIGHTNING_WS must be a secure WebSocket URL")
    if not raw.strip():
        return None
    endpoint = raw.strip()
    if (
        endpoint != raw
        or len(endpoint) > LIGHTNING_WS_URL_MAX_CHARS
        or not endpoint.isascii()
        or any(ord(ch) < 0x21 or ord(ch) == 0x7F for ch in endpoint)
    ):
        raise ValueError("SKYSTRIP_LIGHTNING_WS must be a secure WebSocket URL")
    try:
        parsed = urlsplit(endpoint)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError(
            "SKYSTRIP_LIGHTNING_WS must be a secure WebSocket URL"
        ) from exc
    if (
        parsed.scheme.lower() != "wss"
        or not parsed.netloc
        or not hostname
        or parsed.fragment
        or port == 0
    ):
        raise ValueError("SKYSTRIP_LIGHTNING_WS must be a secure WebSocket URL")
    return endpoint


def _coordinate(
    raw: str | None,
    name: str,
    low: float,
    high: float,
) -> tuple[float, bool]:
    value_text = (raw or "").strip()
    if not value_text:
        return 0.0, False
    try:
        value = float(value_text)
    except ValueError as exc:
        raise ValueError(f"{name} must be a decimal coordinate") from exc
    if not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be finite and between {low:g} and {high:g}")
    return value, True


def resolve_state_root(raw: str | None, repo_root: Path) -> tuple[Path, str]:
    """Resolve owner-edited state configuration without making startup fail."""
    default = repo_root / "state"
    value = (raw or "").strip()
    if not value:
        return default.resolve(), ""
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        return candidate.resolve(), ""
    except (OSError, RuntimeError, ValueError):
        return default.resolve(), (
            f"BUSYBAR_STATE_DIR is unusable; using the repository state root {default}"
        )


def _enabled_scenes(raw: str | None) -> tuple[str, ...]:
    """Return selected scenes in declared order, with a nonempty fallback."""
    picked = {scene.strip() for scene in (raw or "").split(",")}
    return tuple(scene for scene in SCENES if scene in picked) or SCENES


def parse_runtime_config(
    values: Mapping[str, str],
    repo_root: Path = REPO_ROOT,
) -> SkystripConfig:
    """Validate a configuration mapping without reading process state."""
    latitude, latitude_set = _coordinate(
        values.get("SKYSTRIP_LAT"), "SKYSTRIP_LAT", -90.0, 90.0
    )
    longitude, longitude_set = _coordinate(
        values.get("SKYSTRIP_LON"), "SKYSTRIP_LON", -180.0, 180.0
    )
    if latitude_set != longitude_set:
        raise ValueError("SKYSTRIP_LAT and SKYSTRIP_LON must be configured together")

    timezone_name = (values.get("SKYSTRIP_TZ") or "UTC").strip()
    if len(timezone_name) > 255:
        raise ValueError("SKYSTRIP_TZ must be a valid IANA timezone name")
    try:
        configured_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError, OSError) as exc:
        raise ValueError("SKYSTRIP_TZ must be a valid IANA timezone name") from exc

    units = (values.get("SKYSTRIP_UNITS") or "f").strip().lower()
    if units not in {"f", "c"}:
        raise ValueError("SKYSTRIP_UNITS must be 'f' or 'c'")

    # A closed set: every offered ink is pre-proven readable against the
    # corner's measured backgrounds by hue separation (the contract tests
    # sweep all of them), so no unreadable clock is even configurable.
    clock_ink = (values.get("SKYSTRIP_CLOCK_INK") or "orange").strip().lower()
    if clock_ink not in STATUS_INKS:
        raise ValueError("SKYSTRIP_CLOCK_INK must be 'orange', 'pink', or 'red'")

    lightning_errors: tuple[str, ...] = ()
    try:
        lightning_ws = _validate_lightning_ws_endpoint(
            values.get("SKYSTRIP_LIGHTNING_WS")
        )
    except ValueError:
        # The value may contain credentials. Never retain it in a diagnostic.
        lightning_ws = None
        lightning_errors = (
            "SKYSTRIP_LIGHTNING_WS is invalid; live lightning is disabled",
        )

    state_root, state_warning = resolve_state_root(
        values.get("BUSYBAR_STATE_DIR"), repo_root
    )
    contact = (values.get("SKYSTRIP_CONTACT") or "").strip()
    return SkystripConfig(
        latitude=latitude,
        longitude=longitude,
        location_set=latitude_set and longitude_set,
        timezone=configured_timezone,
        units=units,
        clock_ink=clock_ink,
        style=(values.get("SKYSTRIP_STYLE") or "plain").lower(),
        report_voice=(values.get("SKYSTRIP_VOICE") or "am_michael").strip(),
        christmas_window=(values.get("SKYSTRIP_CHRISTMAS") or "dec24-26")
        .strip()
        .lower(),
        nws_station=values.get("SKYSTRIP_STATION", ""),
        nws_user_agent=(
            f"skystrip ({contact})" if contact else "skystrip (hobby project)"
        ),
        lightning_ws=lightning_ws,
        state_root=state_root,
        enabled_scenes=_enabled_scenes(values.get("SKYSTRIP_SCENES")),
        warnings=(state_warning,) if state_warning else (),
        errors=lightning_errors,
    )


# Constructed lexically so importing this module does not resolve owner paths.
DEFAULT_SKYSTRIP_CONFIG = SkystripConfig(
    latitude=0.0,
    longitude=0.0,
    location_set=False,
    timezone=ZoneInfo("UTC"),
    units="f",
    clock_ink="orange",
    style="plain",
    report_voice="am_michael",
    christmas_window="dec24-26",
    nws_station="",
    nws_user_agent="skystrip (hobby project)",
    lightning_ws=None,
    state_root=REPO_ROOT / "state",
    enabled_scenes=SCENES,
)

STATUS_INKS = {
    # Dominant channels at FULL scale, deliberately: brightness reads as
    # apparent size on this panel, so a 90% ink draws visibly thinner
    # digits than a 100% one for free. Teal was tried and panel-vetoed
    # ("doesn't work well", 2026-08-12); orange is the operator's pick and
    # matches the bar's own industrial design — white body, orange
    # accents. Its G sits at 130 so it stays >=30% from the alarm red in
    # G, and its B of zero is the separator against every sky.
    "orange": (255, 130, 0),  # the hardware's accent colour, on the panel
    "pink": (255, 64, 200),  # The Drake's neon family, dusk-flavoured
    "red": (255, 40, 28),  # reads hardest of all; also the alarm colour
}

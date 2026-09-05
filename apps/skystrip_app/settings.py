"""Process configuration, applied explicitly at startup; safe defaults on import.

Consumers retain this module, not copies of its mutable settings. The app has
one runtime per process; configure before starting its tasks.
"""

from __future__ import annotations

import os

from astral import Observer

from apps.skystrip_app import config as _config
from apps.skystrip_app import limits as _limits
from busybar_dev import load_env

# Plain imports use public-safe defaults. Owner dotenv values, custom state
# paths, and configuration diagnostics are handled only by configure_runtime()
# immediately before the CLI starts work.
RUNTIME_CONFIG = _config.DEFAULT_SKYSTRIP_CONFIG

LAT = _config.DEFAULT_SKYSTRIP_CONFIG.latitude

LON = _config.DEFAULT_SKYSTRIP_CONFIG.longitude

LOCATION_SET = _config.DEFAULT_SKYSTRIP_CONFIG.location_set

OBSERVER = Observer(latitude=LAT, longitude=LON)

TZ = _config.DEFAULT_SKYSTRIP_CONFIG.timezone

LIGHTNING_WS = _config.DEFAULT_SKYSTRIP_CONFIG.lightning_ws

UNITS = _config.DEFAULT_SKYSTRIP_CONFIG.units

STYLE = _config.DEFAULT_SKYSTRIP_CONFIG.style

REPORT_VOICE = _config.DEFAULT_SKYSTRIP_CONFIG.report_voice

CHRISTMAS_WINDOW = _config.DEFAULT_SKYSTRIP_CONFIG.christmas_window

NWS_STATION = _config.DEFAULT_SKYSTRIP_CONFIG.nws_station

NWS_UA = {"User-Agent": _config.DEFAULT_SKYSTRIP_CONFIG.nws_user_agent}

STATE_ROOT = _config.DEFAULT_SKYSTRIP_CONFIG.state_root

SCENE_FILE = STATE_ROOT / "skystrip-scene"

ENABLED_SCENES = _config.DEFAULT_SKYSTRIP_CONFIG.enabled_scenes


def apply_runtime_config(config: _config.SkystripConfig) -> None:
    """Apply one validated immutable configuration to runtime globals."""
    global CHRISTMAS_WINDOW, CLOCK_INK, ENABLED_SCENES, LAT, LIGHTNING_WS
    global LOCATION_SET, LON, NWS_STATION, NWS_UA, OBSERVER, REPORT_VOICE
    global RUNTIME_CONFIG, SCENE_FILE, STATE_ROOT, STYLE, TZ, UNITS

    RUNTIME_CONFIG = config
    LAT = config.latitude
    LON = config.longitude
    LOCATION_SET = config.location_set
    OBSERVER = Observer(latitude=LAT, longitude=LON)
    TZ = config.timezone
    LIGHTNING_WS = config.lightning_ws
    UNITS = config.units
    CLOCK_INK = _config.STATUS_INKS[config.clock_ink]
    STYLE = config.style
    REPORT_VOICE = config.report_voice
    CHRISTMAS_WINDOW = config.christmas_window
    NWS_STATION = config.nws_station
    NWS_UA = {"User-Agent": config.nws_user_agent}
    STATE_ROOT = config.state_root
    SCENE_FILE = STATE_ROOT / "skystrip-scene"
    ENABLED_SCENES = config.enabled_scenes
    for warning in config.warnings:
        _limits.logger.warning("%s", warning)
    for error in config.errors:
        _limits.logger.error("%s", error)


def configure_runtime() -> _config.SkystripConfig:
    """Load owner dotenv values, validate the environment, and apply it."""
    load_env()
    config = _config.parse_runtime_config(os.environ)
    apply_runtime_config(config)
    return config


def warn_if_unlocated() -> str:
    """The one message an unconfigured install must not fail to give.

    Weather apps degrade quietly: the sun still rises, the clock still ticks,
    the clouds still move. Everything looks fine and everything is about the
    wrong place.
    """
    if LOCATION_SET:
        return ""
    return (
        "SKYSTRIP_LAT/SKYSTRIP_LON are not set, so this is the sky at "
        "0,0 in the Gulf of Guinea — not yours. Set them in .env, or "
        "through barkeep's config editor, or run deploy/install.sh."
    )


# Named windows rather than a date pair: the choices ARE the options, so
# widening it later is a selection in the barkeep editor, not a code change.
# One key carries both the on/off and the extent, so they cannot disagree the
# way an "enabled" flag plus a separate window could.
CHRISTMAS_WINDOWS = {
    "off": (),
    "dec25": ((12, 25, 12, 25),),
    "dec24-26": ((12, 24, 12, 26),),
    "dec20-jan1": ((12, 20, 12, 31), (1, 1, 1, 1)),  # crosses the year
    "dec1-26": ((12, 1, 12, 26),),
}

# Preview-only override. None means "follow the date", which is what the
# device always does -- there is no way to set this outside --preview, on
# purpose: the decorations should be honest about the date on real hardware.
CHRISTMAS_FORCED: bool | None = None

CLOCK_INK = _config.STATUS_INKS["orange"]  # overwritten by apply_runtime_config

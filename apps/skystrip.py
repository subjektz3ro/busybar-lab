"""Live sky strip — the actual sky outside your window, on the bar.

Calculated solar elevation drives the base gradient (astral math, no API).
Weather observations mute it with cloud cover and shift it storm green-grey
under observed thunder; nearby reports from an optional, operator-authorized
lightning feed briefly illuminate the rendered sky backdrop and pulse the top
LEDs.
Over that sky sits the original house art (apps/assets/house.png), a local
lunar-phase cue or the sun with a soft glow, cloud-aware stars, falling rain or
snow when the weather feeds report it, and a tiny clock.

    uv run apps/skystrip.py --enable-network-providers
                                       # run the watcher (Ctrl+C clears the bar)
    uv run apps/skystrip.py --once     # local snapshot; no provider polling
    uv run apps/skystrip.py --report --enable-network-providers
                                       # fetch and speak one weather report
    uv run apps/skystrip.py --preview scratch/sky.png [--at 03:30] [--cloud 0.5]
                                       # render a frame to PNG only, no device

Elements carry a timeout so the bar self-clears if the watcher dies. Draws
yield politely (HTTP 409) while a BUSY/CUSTOM session owns the display.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import importlib
import os
import sys
import io
import re
import struct
import time
import json
import logging
import math
import random
import signal
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, TypedDict, cast

import httpx
from astral import Observer, moon
from astral.sun import elevation
from busylib import exceptions, types
from PIL import Image

from busybar_dev import aconnect, load_env
from busybar_dev.config import (
    CoordinateRedactingFilter,
    describe_exception,
)
from busybar_dev.device import (
    connect_with_retry,
    is_refusal,
    storage_file_matches,
)
from busybar_dev import anim
from busybar_dev.pixel_text import (
    device_text,
    draw_marquee,
    marquee_frame_count,
    max_text_width,
    # Re-exported deliberately: the layout-budget assertions reach it as
    # `skystrip.text_width`, because the question they ask is what THIS app
    # thinks a label measures. Unused inside this module, so an unused-import
    # autofix will take it and the failure lands in the tests, not here.
    text_width,  # noqa: F401
)
from busybar_dev.radar import (
    RADAR_MAX_ZOOM,
    RAINVIEWER_TILE_SIZE,
    decode_coverage_mask,
    decode_radar_tile,
    rainviewer_frame_age,
    resolve_rain,
    sample_dbz,
    tile_pixel,
    web_mercator_contains,
)
from busybar_dev.tts import synth_snd_async
from busybar_dev.weather_alerts import (
    Alert,
    parse_active_alerts,
    preserve_acknowledgement,
    select_siren_alert,
    select_visual_alert,
)

if TYPE_CHECKING:
    import skystrip_config as _skystrip_config
    import skystrip_eclipse as _skystrip_eclipse
    import skystrip_lightning as _skystrip_lightning
elif __package__:
    _skystrip_config = importlib.import_module(".skystrip_config", __package__)
    _skystrip_eclipse = importlib.import_module(".skystrip_eclipse", __package__)
    _skystrip_lightning = importlib.import_module(
        ".skystrip_lightning", __package__
    )
else:
    _skystrip_config = importlib.import_module("skystrip_config")
    _skystrip_eclipse = importlib.import_module("skystrip_eclipse")
    _skystrip_lightning = importlib.import_module("skystrip_lightning")

DEFAULT_SKYSTRIP_CONFIG = _skystrip_config.DEFAULT_SKYSTRIP_CONFIG
LIGHTNING_WS_URL_MAX_CHARS = _skystrip_config.LIGHTNING_WS_URL_MAX_CHARS
REPO_ROOT = _skystrip_config.REPO_ROOT
SCENES = _skystrip_config.SCENES
SkystripConfig = _skystrip_config.SkystripConfig
# Kept on the entry module because renderer adapters and callers use
# ``skystrip.ZoneInfo`` when pinning a deterministic timezone.
ZoneInfo = _skystrip_config.ZoneInfo
_coordinate = _skystrip_config._coordinate
_enabled_scenes = _skystrip_config._enabled_scenes
_validate_lightning_ws_endpoint = (
    _skystrip_config._validate_lightning_ws_endpoint
)
parse_runtime_config = _skystrip_config.parse_runtime_config
resolve_state_root = _skystrip_config.resolve_state_root
eclipse_state_at = _skystrip_eclipse.state_at
eclipse_visible_state = _skystrip_eclipse.visible_state

LIGHTNING_DECODED_MAX_CHARS = (
    _skystrip_lightning.LIGHTNING_DECODED_MAX_CHARS
)
LIGHTNING_FRAME_MAX_BYTES = _skystrip_lightning.LIGHTNING_FRAME_MAX_BYTES
LIGHTNING_LZW_MAX_ENTRIES = _skystrip_lightning.LIGHTNING_LZW_MAX_ENTRIES
LIGHTNING_SOURCE_FUTURE_SKEW_S = (
    _skystrip_lightning.LIGHTNING_SOURCE_FUTURE_SKEW_S
)
LIGHTNING_SOURCE_MAX_AGE_S = (
    _skystrip_lightning.LIGHTNING_SOURCE_MAX_AGE_S
)
_LightningStrike = _skystrip_lightning.LightningStrike
_decode_lightning_payload = _skystrip_lightning._decode_lightning_payload
_lzw_decode = _skystrip_lightning._lzw_decode
_parse_lightning_strike = _skystrip_lightning.parse_lightning_strike
_strict_json_coordinate = _skystrip_lightning._strict_json_coordinate

logger = logging.getLogger("skystrip")
# A configured lightning URL may carry relay credentials. The WebSocket
# library's debug records can include the request target, so give that one
# transport a disabled private logger and keep our value-free lifecycle logs
# on the normal app logger.
LIGHTNING_TRANSPORT_LOGGER = logging.Logger(
    "skystrip.lightning.transport", level=logging.CRITICAL + 1
)
LIGHTNING_TRANSPORT_LOGGER.disabled = True


RGB = tuple[int, int, int]
Color = tuple[float, float, float]


class RGBPixels(Protocol):
    """Typed view of ``Image.load()`` for images created or converted as RGB."""

    def __getitem__(self, position: tuple[int, int]) -> RGB: ...

    def __setitem__(self, position: tuple[int, int], value: object) -> None: ...


def _rgb_pixels(image: Image.Image) -> RGBPixels:
    """Narrow Pillow's mode-dependent pixel union at the explicit RGB boundary."""
    pixels = image.load()
    if pixels is None:  # Pillow RGB images expose storage; retain a clear guard.
        raise RuntimeError("RGB image has no pixel storage")
    return cast(RGBPixels, pixels)


def _rgb_int(values: Iterable[float]) -> RGB:
    """Materialize the app's three-channel color calculations as RGB integers."""
    red, green, blue = values
    return int(red), int(green), int(blue)


APP_NAME = "skystrip"
PRIORITY = 30  # above idle apps (~10); an active BUSY/CUSTOM session refuses us
W, H = 72, 16

# The status readout's reserved corner, cols 0..STATUS_CARD_W-1, rows 0-6.
# Measured, not chosen: the widest string is a 17px clock plus one pixel
# of margin each side. There is no card and no shadow — the ink's hue
# carries its own contrast (see STATUS_INKS) — but the span still
# matters twice: scene generators keep point noise (stars) out of it so
# nothing welds onto a letterform, and the backroads train band starts at
# this boundary.
STATUS_CARD_W = 19
# Plain imports use public-safe defaults. Owner dotenv values, custom state
# paths, and configuration diagnostics are handled only by configure_runtime()
# immediately before the CLI starts work.
RUNTIME_CONFIG = DEFAULT_SKYSTRIP_CONFIG
LAT = DEFAULT_SKYSTRIP_CONFIG.latitude
LON = DEFAULT_SKYSTRIP_CONFIG.longitude
LOCATION_SET = DEFAULT_SKYSTRIP_CONFIG.location_set
OBSERVER = Observer(latitude=LAT, longitude=LON)
TZ = DEFAULT_SKYSTRIP_CONFIG.timezone
LIGHTNING_WS = DEFAULT_SKYSTRIP_CONFIG.lightning_ws
UNITS = DEFAULT_SKYSTRIP_CONFIG.units
STYLE = DEFAULT_SKYSTRIP_CONFIG.style
REPORT_VOICE = DEFAULT_SKYSTRIP_CONFIG.report_voice
CHRISTMAS_WINDOW = DEFAULT_SKYSTRIP_CONFIG.christmas_window
NWS_STATION = DEFAULT_SKYSTRIP_CONFIG.nws_station
NWS_UA = {"User-Agent": DEFAULT_SKYSTRIP_CONFIG.nws_user_agent}
STATE_ROOT = DEFAULT_SKYSTRIP_CONFIG.state_root
SCENE_FILE = STATE_ROOT / "skystrip-scene"
ENABLED_SCENES = DEFAULT_SKYSTRIP_CONFIG.enabled_scenes


def apply_runtime_config(config: SkystripConfig) -> None:
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
    CLOCK_INK = STATUS_INKS[config.clock_ink]
    STYLE = config.style
    REPORT_VOICE = config.report_voice
    CHRISTMAS_WINDOW = config.christmas_window
    NWS_STATION = config.nws_station
    NWS_UA = {"User-Agent": config.nws_user_agent}
    STATE_ROOT = config.state_root
    SCENE_FILE = STATE_ROOT / "skystrip-scene"
    ENABLED_SCENES = config.enabled_scenes
    for warning in config.warnings:
        logger.warning("%s", warning)
    for error in config.errors:
        logger.error("%s", error)


def configure_runtime() -> SkystripConfig:
    """Load owner dotenv values, validate the environment, and apply it."""
    load_env()
    config = parse_runtime_config(os.environ)
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
    return ("SKYSTRIP_LAT/SKYSTRIP_LON are not set, so this is the sky at "
            "0,0 in the Gulf of Guinea — not yours. Set them in .env, or "
            "through barkeep's config editor, or run deploy/install.sh.")


# Named windows rather than a date pair: the choices ARE the options, so
# widening it later is a selection in the barkeep editor, not a code change.
# One key carries both the on/off and the extent, so they cannot disagree the
# way an "enabled" flag plus a separate window could.
CHRISTMAS_WINDOWS = {
    "off":        (),
    "dec25":      ((12, 25, 12, 25),),
    "dec24-26":   ((12, 24, 12, 26),),
    "dec20-jan1": ((12, 20, 12, 31), (1, 1, 1, 1)),   # crosses the year
    "dec1-26":    ((12, 1, 12, 26),),
}
# Preview-only override. None means "follow the date", which is what the
# device always does -- there is no way to set this outside --preview, on
# purpose: the decorations should be honest about the date on real hardware.
CHRISTMAS_FORCED: bool | None = None


def is_christmas(when: datetime) -> bool:
    """Is the decorative treatment showing on this date?

    Deliberately the only date-driven look in the app: everything else here is
    a measurement. Kept narrow so it stays a surprise rather than a mode, and
    switchable because somebody will not want it.

    An unknown window reads as off. A hand-edited config.env can hold
    anything, and a crash loop leaves the display dark.
    """
    if CHRISTMAS_FORCED is not None:
        return CHRISTMAS_FORCED
    # Normalise here, not at the call sites. Six scenes are about to call
    # this, and a UTC datetime would shift the window by the tz offset --
    # 19:00 on the 24th in Chicago is already the 25th in UTC. Making the
    # predicate own the conversion means no call site can get it wrong.
    local = when.astimezone(TZ)
    for m0, d0, m1, d1 in CHRISTMAS_WINDOWS.get(CHRISTMAS_WINDOW, ()):
        if (m0, d0) <= (local.month, local.day) <= (m1, d1):
            return True
    return False


# Optional: pin an NWS station; blank = auto-discover from coordinates.
# NWS enhancement is available only when /points resolves the coordinate;
# elsewhere Open-Meteo carries modeled weather and the NWS layers fail soft.
FORECAST_INTERVAL_S = 1800
# NWS asks callers to identify themselves; set SKYSTRIP_CONTACT to an
# email/URL you own, or leave blank for an anonymous-but-named UA.
NEUTRAL_UA = {"User-Agent": "skystrip (hobby project)"}
STRIKE_RADIUS_KM = 60   # beyond this you can't see it from a city
STRIKE_NEAR_KM = 25     # inside this the storm is genuinely overhead
FAR_FLASH_GAP_S = 25    # distant flicker is occasional, not a strobe
LIGHTNING_WS_MAX_QUEUE = 16
LIGHTNING_SUBSCRIPTION = '{"a": 111}'  # Blitz-compatible stream handshake
# A floor under reconnects whatever the close reason. 370 clean-close
# reconnects were observed in one host's log against a community-run service.
RECONNECT_FLOOR_S = 3.0
ELEMENT_TIMEOUT_S = 180  # self-clear if we stop redrawing
REDRAW_INTERVAL_S = 60
# The panel says so when the sky cannot be drawn truthfully. Refusing to
# render an expired observation is right; decaying to an unexplained black
# panel is not, and is indistinguishable from a crashed app. Kept well under
# ELEMENT_TIMEOUT_S so a recovered feed replaces it promptly.
STALE_ELEMENT_TIMEOUT_S = 90
STALE_REDRAW_S = 30
# Startup and a normal poll gap are both briefly "not fresh". Only a sustained
# outage earns the notice; announcing one every boot trains you to ignore it.
STALE_NOTICE_GRACE_S = 45
# ~12 characters is the condensed budget at 72px (see the busybar-app skill).
STALE_WEATHER_TEXT = "NO WX DATA"
KEEPALIVE_S = 120  # must beat ELEMENT_TIMEOUT_S
OBS_INTERVAL_S = 300
WEATHER_LEASE_S = 2 * 3600
SOURCE_FUTURE_SKEW_S = 5 * 60
ALERTS_INTERVAL_S = 180
ALERT_REDRAW_S = 5
ALERT_ELEMENT_TIMEOUT_S = 20
ALERT_ANIM_FPS = 5
ALERT_SCROLL_SPEED_PX_S = 12.0
ALERT_ASSET_KEEP = 3
ALERT_ASSET_RETRY_S = 2.0
SIREN_SECONDS = 10
SIREN_RETRIGGER_S = 9.0
SIREN_PROVISION_RETRY_S = 30.0
SIREN_RETIRE_GRACE_S = SIREN_SECONDS + 1.0
FLASH_MIN_GAP_S = 2.0
FLASH_QUEUE_MAX = 64
FLASH_EVENT_TTL_S = LIGHTNING_SOURCE_MAX_AGE_S
# The decoder and the eventual flash queue share this source-age contract, so
# a frame accepted at ingestion cannot outlive the runtime event lease.
FLASH_ANIM_FPS = 12
FLASH_ELEMENT_TIMEOUT_S = 2  # firmware leases are whole seconds
FLASH_ASSET_RETIRE_GRACE_S = 1.0  # let firmware release the expired file handle
AMBIENT_PERIOD_S = 20    # how often the top-strip mood re-evaluates
AMBIENT_LEVEL = 0.35     # ambient brightness scale (the strip is bright)
DOUBLE_PRESS_S = 0.45  # window for a second press to make a double
START_BOUNCE_S = 0.12  # reject only the switch's mechanical button bounce
START_OWNERSHIP_SETTLE_S = 0.12  # let a firmware-owned START update Busy state
START_OWNERSHIP_TIMEOUT_S = 1.0  # an uncertain ownership check fails closed
REPORT_PREPARING = "PREPARING..."
REPORT_READY = "START TWICE"
REPORT_AUDIO_BUSY = "AUDIO BUSY"
REPORT_AUDIO_ERROR = "AUDIO ERROR"
REPORT_STATUS_TIMEOUT_S = 3
REPORT_IO_TIMEOUT_S = 1.0
# Scrubbing forward is a forecast, so it is gated on likelihood rather than on
# a single deterministic run: Open-Meteo's hourly `precipitation` is 0.00mm for
# most hours even when the chance is above half, which is why 1 hour of 72 drew
# rain. 40% sits above the narration's 30% mention threshold, so the voice
# calling something "a slight chance" can never accompany a scene drawing rain.
PRECIP_LIKELY_PCT = 40
# Expected accumulation picks the tier. Likelihood decides WHETHER, amount
# decides HOW HARD -- a 90% chance of drizzle must draw drizzle.
PRECIP_TIER_MM = ((1.0, 0), (4.0, 1))   # above the last bound -> tier 2
# Scrubbing backward is history, and history must come from what was observed.
# Open-Meteo's past_days rows are model reanalysis: on 2026-08-09 they reported
# overcast with 0.00mm straight through a two-hour thunderstorm the station
# recorded as Heavy Thunderstorms and Heavy Rain. They are never consulted for
# precipitation in the past.
OBS_HISTORY_HOURS = 26       # the 24h scrub window, plus slack for gaps
# ~5-minute cadence over 26h is ~335 records, so this is headroom, not a
# squeeze. It is also the API's own ceiling: NWS 400s on limit > 500, and a
# 400 here would log a warning and quietly leave the past dry.
OBS_HISTORY_MAX = 500
OBS_SLOT_WINDOW_S = 900      # +-15 min: one half-hour timeline slot
# NWS observation intensity is structured, and maps 1:1 onto RAIN_TIERS.
OBS_INTENSITY_TIER = {"light": 0, None: 1, "heavy": 2}
# Every value below is from api.weather.gov's published `presentWeather`
# enum (36 values), not from what a mapping happened to expect. Audited
# 2026-08-27; `tests/test_skystrip_obs_vocabulary.py` pins the enum so a
# feed value we do not classify fails loudly instead of falling through to
# a silent default. That is how `ice_pellets` sat unmatched behind a search
# for "ice pellets" with a space in it.
OBS_SNOW_WORDS = {"snow", "snow_grains", "snow_pellets", "ice_pellets",
                  "snow_showers", "sleet", "blowing_snow"}
# `hail` rides with the liquid set on purpose. The scene has no distinct
# hail treatment, and the honest choice between "draw it as the heavy
# precipitation it is" and "draw nothing" is the former — drawing a clear
# sky through a hailstorm is the worse lie. It is also what the past-slot
# path already concluded, since hail is in OBS_PRECIP_WORDS but not in
# OBS_SNOW_WORDS; before this the two paths silently disagreed.
OBS_RAIN_WORDS = {"rain", "rain_showers", "drizzle", "freezing_rain",
                  "freezing_drizzle", "hail"}
# Falling precipitation, for the past Time Machine slots. Blowing snow is
# snow in the air rather than snow arriving, so it colours the live scene
# without counting as an observation that precipitation fell.
OBS_PRECIP_WORDS = (OBS_RAIN_WORDS | OBS_SNOW_WORDS) - {"blowing_snow"}
OBS_THUNDER_WORDS = {"thunderstorms"}
# Fog proper. The obscurations below are a separate axis: fog is condensed
# water and hugs the ground, while haze/smoke/dust/ash fill the whole air
# column and kill distance instead of pooling.
OBS_FOG_WORDS = {"fog", "fog_mist", "freezing_fog", "ice_fog"}

# Obscurations, grouped by the tint each one gets. Dust and sand share a
# tan on purpose: their real colours sit about 12% apart in red, and the
# panel cannot resolve under ~30% per channel, so shipping two would be a
# claim of distinguishability the hardware cannot honour.
OBS_OBSCURATION_WORDS = {
    "haze": {"haze"},
    "smoke": {"smoke"},
    "dust": {"dust", "blowing_dust", "dust_whirls", "dust_storm",
             "sand", "blowing_sand", "sand_storm"},
    "ash": {"volcanic_ash"},
}
# Most-obscuring first: a report of both smoke and haze is a smoke day.
OBS_OBSCURATION_ORDER = ("ash", "smoke", "dust", "haze")

SCRUB_STEP_S = 1800      # one wheel detent = 30 minutes
SCRUB_MAX_S = 86400      # the Time Machine reaches a day each way
SCRUB_SNAP_S = 45        # idle this long and it drifts back to now
REVEAL_REST_S = 1.5      # stillness before the scene commits — longer than
                         # a deliberate click cadence, shorter than patience
TIMELINE_SLOTS = 97      # 48h of half-hour marks, yesterday to tomorrow
TIMELINE_STEP_S = 1800
ENC_COUNTS_PER_DETENT = 1  # verified: one detent = one count

HOUSE_ART = Path(__file__).parent / "assets" / "house.png"

# (solar elevation°, horizon RGB, zenith RGB) — interpolated between rows
SKY_KEYFRAMES = [
    (-90.0, (6, 8, 20), (1, 2, 8)),          # deep night
    (-18.0, (8, 10, 26), (2, 3, 12)),        # astronomical twilight begins
    (-12.0, (38, 26, 62), (6, 9, 30)),       # nautical: mauve horizon
    (-6.0, (150, 70, 70), (18, 26, 66)),     # civil: ember horizon, indigo up
    (-2.0, (232, 116, 62), (44, 58, 110)),   # sunset ember
    (2.0, (244, 168, 88), (92, 124, 176)),   # golden hour
    (10.0, (196, 200, 190), (74, 128, 196)), # low sun, hazy horizon
    (30.0, (158, 200, 232), (52, 110, 186)), # midday
    (90.0, (170, 208, 236), (46, 104, 182)),
]

STORM_HORIZON, STORM_ZENITH = (74, 92, 82), (30, 38, 42)  # green-grey cell

GROUND_NIGHT = (24, 28, 22)  # the original art's moss row
MOON_COLOR = (226, 226, 206)  # the original art's moon cream
SUN_COLOR = (255, 214, 120)
WINDOW_COLOR = (255, 157, 69)
STAR_COLOR = (150, 150, 170)
# colors in the house region of the source art that are sky, not house
HOUSE_SKIP = {(150, 150, 170), (160, 160, 180), (226, 226, 206), (8, 10, 26)}

CLOUD_AMOUNT = {"CLR": 0.0, "SKC": 0.0, "FEW": 0.2, "SCT": 0.45, "BKN": 0.8, "OVC": 1.0, "VV": 1.0}

# Settled-snow depth thresholds, in metres. Deliberately constants and not
# config keys: they want a winter of watching before they are worth a knob.
SNOW_DUSTING_M = 0.01   # 1 cm: the grass still shows through
SNOW_COVERED_M = 0.08   # 8 cm: the ground is gone
SNOW_DEEP_M = 0.25      # 25 cm: tufts and low detail are buried

SNOW_LIT = (232, 238, 246)     # sunlit crust: cold white, faintly blue
SNOW_SHADE = (120, 134, 156)   # the shaded side of the same crust

# Which columns take snow at each tier. Fixed seed: previews and tests must
# not shimmer between runs, and the pattern reads as drift, not as noise.
_snow_rng = random.Random(1224)
_SNOW_ORDER = list(range(W))
_snow_rng.shuffle(_SNOW_ORDER)
# Never 1.0, even at the deepest tier. A fully lit row IS the haze failure --
# it is the thing the global constraint forbids and the thing
# test_settle_snow_never_fills_a_row asserts against. Depth is carried by the
# second row below, not by closing the last gaps in the first.
SNOW_FRACTION = {1: 0.40, 2: 0.70, 3: 0.90}

# Rain intensity: tier -> (drops, crossings per loop, streak length in px).
#
# Coverage is NOT one of the channels. Drops are dealt one per stratified
# column bucket (below), so every tier wets the whole panel; what changes is
# how much falls past a row per second, how fast, and how long the streak is.
# Drop count used to be the only channel, and at 5 drops for 72 columns a
# seeded draw put all five in the right half and stayed there for the whole
# ten-minute seed window -- it read as a broken panel, not as light rain.
#
# `crossings` MUST stay a whole number: fall returns to y0 at phase 1.0 only
# when it is, and that is what makes the device's loop seam invisible.
RAIN_TIERS = {0: (12, 4, 2), 1: (18, 8, 3), 2: (24, 16, 4)}
# Head is the streak colour outright; the rest fades toward whatever is behind
# it. Steps are coarse on purpose -- finer than ~30% is invisible on the panel.
RAIN_TAPER = (1.0, 0.65, 0.45, 0.35)
# Above this sky luminance rain reads as a DARK streak. sky+55 clamps to white
# on a bright sky and lands at +0..11% contrast, i.e. no rain at all.
RAIN_DARK_SKY_LUM = 110


def _streak_color(sky: RGB) -> RGB:
    """Rain against bright cloud is darker than it; against night, lighter.

    Either way the streak clears the panel's ~30%-per-channel floor. Picking
    one direction cannot: additive dies on a bright sky, subtractive on night.
    """
    lum = 0.3 * sky[0] + 0.59 * sky[1] + 0.11 * sky[2]
    if lum > RAIN_DARK_SKY_LUM:
        return _rgb_int(c * 0.62 for c in sky)
    return (
        min(255, sky[0] + 55),
        min(255, sky[1] + 55),
        min(255, sky[2] + 55),
    )


def _rain_tier(wx) -> int:
    """A storm is never a drizzle, but it does not erase a measured intensity.

    This used to pin every storm to tier 2. That was fine while the only
    source was radar dBZ, and wrong as soon as scrubbing began replaying
    observed history: the station recorded "Thunderstorms and Rain" -- plain
    moderate rain -- and pinning turned it into a downpour on the panel.
    Floor it instead, so an observed heavy stays heavy and an observed
    moderate stays moderate.
    """
    tier = min(max(wx.rain_tier, 0), 2)
    return max(tier, 1) if wx.stormy else tier


def is_raining(wx) -> bool:
    """Snow wins: the two never fall together, and snow draws its own flakes."""
    return (wx.rain or wx.stormy) and not wx.snow


SNOW_FLAKES = 12
SNOW_FLAKE_COLOR = (235, 235, 240)
SNOW_CROSSINGS = 2  # a drifting descent every four seconds


def draw_snow(px, seed: int, phase: float) -> None:
    """Drift flakes down the whole panel.

    Same two guarantees as draw_rain, for the same reasons: coverage comes
    from stratified buckets rather than from the seed being kind, and `fall`
    rounds rather than truncates. Neither was visibly broken here -- snow
    moves at 0.75 rows/frame, where truncation and rounding agree, and 12
    flakes clump onto one side in only ~0.7% of seeds. They are matched to
    the rain path so the next person to change SNOW_CROSSINGS does not
    rediscover both defects the hard way.
    """
    flake_rng = random.Random(seed * 17 + 3)
    span = H - 1
    fall = round(phase * SNOW_CROSSINGS * span)
    for i in range(SNOW_FLAKES):
        lo, hi = i * W // SNOW_FLAKES, (i + 1) * W // SNOW_FLAKES
        fx0 = flake_rng.randrange(lo, hi)
        fy0 = flake_rng.randrange(0, span)
        # Sway is +-1, so a flake can lean one column into its neighbour.
        # That is drift, not a coverage hole: the buckets are 6 wide.
        sway = round(math.sin(math.tau * 2 * phase + i * 1.3))
        px[(fx0 + sway) % W, (fy0 + fall) % span] = SNOW_FLAKE_COLOR


def draw_rain(px, wx, seed: int, phase: float) -> None:
    """Streak rain across the whole panel at the intensity `wx` implies.

    Split out of render_scene so the coverage and seam guarantees can be
    asserted on a blank canvas instead of inferred from a composed frame.
    """
    drops, crossings, length = RAIN_TIERS[_rain_tier(wx)]
    drop_rng = random.Random(seed * 13 + 5)
    streak = _streak_color(px[36, 4])
    span = H - 1
    # round, not int: phase is i/n, which is inexact in binary, so truncation
    # turns a steady 3 rows/frame into a 2-3-4 stutter. Rounding still lands
    # on crossings*span at phase 1.0, so the loop seam stays exact.
    fall = round(phase * crossings * span)
    slant = 1 if wx.wind_kmh >= 25 else 0
    for i in range(drops):
        # One drop per column bucket, jittered inside it. Sampling the full
        # width instead let all of them land on one side of the panel and
        # stay there for the whole ten-minute seed window.
        rx = drop_rng.randrange(i * W // drops, (i + 1) * W // drops)
        y0 = drop_rng.randrange(0, span)
        ry = (y0 + fall) % span
        for j in range(length):
            # The streak trails UP from the falling head, and is clipped at
            # the top rather than wrapped: a drop entering frame from above
            # is right, a streak split across both edges is not.
            y, x = ry - j, rx + j * slant
            if y < 0 or not 0 <= x < W:
                break
            # _lerp_rgb returns floats; the framebuffer wants ints.
            px[x, y] = tuple(
                int(c) for c in _lerp_rgb(px[x, y], streak, RAIN_TAPER[j]))


@dataclass
class WeatherState:
    cloud_frac: float = 0.0
    rain: bool = False
    rain_tier: int = 1  # 0 drizzle / 1 rain / 2 downpour (radar dBZ tiers)
    snow: bool = False
    thunder: bool = False
    severe: bool = False  # active severe thunderstorm / tornado warning
    wind_kmh: float = 0.0
    temp_c: float = 20.0
    humidity: float = 50.0
    visibility_m: float = 16000.0
    snow_depth_m: float = 0.0  # settled snow, not falling snow (wx.snow)
    fog: bool = False  # reported fog, independent of the visibility number
    # "", "haze", "smoke", "dust" or "ash" — selects an airlight tint.
    obscuration: str = ""
    wind_dir: float | None = None  # meteorological degrees, FROM which it blows

    severe_event: str = ""  # the warning's name, for the display

    @property
    def stormy(self) -> bool:
        # Visuals follow OBSERVED station weather, never warnings —
        # a warned-but-still-sunny sky stays sunny; the alarm layer screams.
        return self.thunder


class WeatherUpdates(TypedDict, total=False):
    cloud_frac: float
    rain: bool
    rain_tier: int
    snow: bool
    thunder: bool
    severe: bool
    wind_kmh: float
    temp_c: float
    humidity: float
    visibility_m: float
    snow_depth_m: float
    fog: bool
    obscuration: str
    wind_dir: float | None
    severe_event: str


def _without_rain(updates: WeatherUpdates) -> WeatherUpdates:
    """Copy a typed weather update while leaving rain to its leased sources."""
    filtered = updates.copy()
    filtered.pop("rain", None)
    return filtered


@dataclass(frozen=True)
class _FlashEvent:
    distance_km: float
    observed_at: float


@dataclass(frozen=True)
class ReportRequest:
    """One double-START intent, fenced from later views and alerts."""

    generation: int
    view_generation: int
    alert_generation: int
    text: str


@dataclass(frozen=True)
class ReportStatus:
    """One possibly committed native report card with a bounded lease."""

    request_generation: int
    element_generation: int
    label: str
    expires_at: float
    view_generation: int = 0
    alert_generation: int = 0
    terminal: bool = False


@dataclass
class SkyState:
    weather: WeatherState = field(default_factory=WeatherState)
    weather_ready: asyncio.Event = field(default_factory=asyncio.Event)
    weather_updated_at: float | None = None
    nws_point_covered: bool | None = None
    nws_point_checked: asyncio.Event = field(default_factory=asyncio.Event)
    # `None` means no echo only while radar_at is fresh; without a timestamp it
    # is unavailable/unknown and cannot declare the point dry.
    radar_dbz: float | None = None
    radar_at: float = 0.0            # loop-time of that sample, 0 = never
    radar_covered: bool | None = None  # official RainViewer coverage mask
    om_rain: bool | None = None      # Open-Meteo current precip at our coords
    om_at: float | None = None        # source time on the monotonic clock
    station_rain: bool | None = None  # optional NWS observation evidence
    station_at: float | None = None   # source time on the monotonic clock
    rain_known: bool = False          # at least one provider resolved rain
    rain_at: float | None = None       # source time of resolved last-good rain
    snow_at: float | None = None       # source time of falling-snow evidence
    thunder_at: float | None = None    # source time of thunder evidence
    snow_depth_at: float | None = None # source time of modeled ground snow
    rain_src: str = ""               # which source last decided rain (for logs)
    flash_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=FLASH_QUEUE_MAX)
    )
    scene_files: list = field(default_factory=list)  # live scene anim + prior
    scene_gen: int = 0
    timeline_files: list = field(default_factory=list)  # live timeline + prior
    last_pushed: tuple[bytes, str, str] | None = None  # (png, clock, color)
    last_drawn_at: float = 0.0
    # Monotonic time of the last "no live weather" notice; 0.0 means never.
    stale_notice_at: float = 0.0
    # When the current run of stale weather began; 0.0 means not stale.
    stale_since: float = 0.0
    scene_idx: int = 0
    scene_change: asyncio.Event = field(default_factory=asyncio.Event)
    current_scene_file: str | None = None
    current_scene_frames: tuple[Image.Image, ...] = ()
    view_generation: int = 0
    effect_generation: int = 0
    alert_acked: bool = False
    visual_alert: Alert | None = None
    siren_alert: Alert | None = None
    active_alerts: tuple[Alert, ...] = ()
    alert_generation: int = 0
    alert_changed: asyncio.Event = field(default_factory=asyncio.Event)
    alert_wake_generation: int = 0
    alert_asset_file: str | None = None
    alert_asset_key: str | None = None
    alert_files: list[str] = field(default_factory=list)
    alert_drawn_generation: int = -1
    alert_dismiss_pending: bool = False
    alert_known: bool = False
    siren_file: str | None = None
    siren_repair: int = 0
    siren_retire: set[str] = field(default_factory=set)
    siren_retire_after: dict[str, float] = field(default_factory=dict)
    siren_ambiguous: set[str] = field(default_factory=set)
    siren_asset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    siren_asset_changed: asyncio.Event = field(default_factory=asyncio.Event)
    display_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    audio_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    audio_generation: int = 0
    audio_owner: str | None = None
    audio_path: str | None = None
    audio_stop_pending: bool = False
    switch_position: str | None = None
    switch_generation: int = 0
    shutting_down: bool = False
    forecast: list | None = None  # first two NWS forecast periods
    hourly: list | None = None    # 72h of (local datetime, weather dict)
    obs_history: list | None = None  # 26h of (local datetime, observation)
    report_file: str | None = None  # pre-baked spoken report, on the bar
    report_text: str | None = None  # the words inside report_file
    report_generation: int = 0
    report_status_generation: int = 0
    report_request: ReportRequest | None = None
    report_statuses: list[ReportStatus] = field(default_factory=list)
    report_prepare_text: str | None = None
    report_prepare_task: asyncio.Task | None = None
    report_prepare_pending: str | None = None
    report_prepare_pending_priority: bool = False
    report_asset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    report_files: list[str] = field(default_factory=list)
    report_retire: set[str] = field(default_factory=set)
    report_repairs: dict[str, int] = field(default_factory=dict)
    report_expected_sizes: dict[str, int] = field(default_factory=dict)
    scrub_touched: float = 0.0
    timeline_meta: dict | None = None  # start dt, scene, built dt
    scrub_slot: int | None = None      # where the wheel points, None = live
    revealed: bool = False             # forecast frame currently shown
    reveal_pending: bool = False       # one generation is rendering/drawing
    last_reveal: dict | None = None    # {eid, slot, fname, section} on screen
    reveal_n: int = 0
    readout_gen: int = 0               # bumped after each reveal: fresh ids
    last_readout: dict | None = None   # exact native ids/content for retirement
    anim_reveal_file: str | None = None  # last animated-reveal upload
    anim_reveal_files: list[str] = field(default_factory=list)
    enc_accum: int = 0                 # raw encoder counts toward next detent
    detached_tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def scene(self) -> str:
        return ENABLED_SCENES[self.scene_idx % len(ENABLED_SCENES)]


def spawn_owned(state: SkyState, coro) -> asyncio.Task:
    """Track every detached coroutine so shutdown can cancel and settle it."""
    task = asyncio.create_task(coro)
    state.detached_tasks.add(task)
    task.add_done_callback(state.detached_tasks.discard)
    return task


def load_scene_idx() -> int:
    """Resume the saved scene, or start at the first enabled one.

    The file stores a NAME, so narrowing the enabled set needs no migration:
    a scene you just switched off simply isn't found, and the bar comes up on
    your first enabled scene rather than one you disabled.
    """
    try:
        return ENABLED_SCENES.index(SCENE_FILE.read_text().strip())
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
        SCENE_FILE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, raw_temporary = tempfile.mkstemp(
            dir=SCENE_FILE.parent,
            prefix=f".{SCENE_FILE.name}.",
            suffix=".tmp",
        )
        temporary = Path(raw_temporary)
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            output.write(ENABLED_SCENES[idx % len(ENABLED_SCENES)])
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, SCENE_FILE)
        temporary = None
        return True
    except OSError as exc:
        logger.warning("scene state not persisted to %s: %s", SCENE_FILE, exc)
        return False
    finally:
        if temporary is not None:
            with contextlib.suppress(OSError):
                temporary.unlink()


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _lerp_rgb(c1: Color, c2: Color, t: float) -> Color:
    return (
        _lerp(c1[0], c2[0], t),
        _lerp(c1[1], c2[1], t),
        _lerp(c1[2], c2[2], t),
    )


def _sky_colors(elev: float) -> tuple[Color, Color]:
    frames = SKY_KEYFRAMES
    for (e1, h1, z1), (e2, h2, z2) in zip(frames, frames[1:]):
        if e1 <= elev <= e2:
            t = (elev - e1) / (e2 - e1)
            return _lerp_rgb(h1, h2, t), _lerp_rgb(z1, z2, t)
    return frames[-1][1], frames[-1][2]


def _load_house() -> list[tuple[int, int, RGB]]:
    """Lift the house sprite (and chimney and window) from the original art."""
    img = Image.open(HOUSE_ART).convert("RGB")
    px = _rgb_pixels(img)
    return [
        (x, y, px[x, y])
        for y in range(4, 15)
        for x in range(48, 72)
        if px[x, y] not in HOUSE_SKIP
    ]


HOUSE_SPRITE = _load_house()
WINDOW_PIXELS = [(x, y) for x, y, c in HOUSE_SPRITE if c == WINDOW_COLOR]
WINDOW_CENTER = (
    round(sum(x for x, _ in WINDOW_PIXELS) / len(WINDOW_PIXELS)),
    round(sum(y for _, y in WINDOW_PIXELS) / len(WINDOW_PIXELS)),
)

# Top silhouette of the house, one pixel per column: what moonlight lands
# on, and where a string of Christmas lights hangs. One derivation, two
# readers -- a second copy of this loop would be a copy that can rot.
HOUSE_TOP: dict[int, int] = {}
for _hx, _hy, _hc in HOUSE_SPRITE:
    if _hx not in HOUSE_TOP or _hy < HOUSE_TOP[_hx]:
        HOUSE_TOP[_hx] = _hy

# Fixed constellation across the whole sky (foregrounds occlude naturally).
# Stratified in x so stars never bunch; each has its own static brightness,
# most faint with a few bright anchors.
_star_rng = random.Random(7)
STARS = [
    star
    for star in (
        (
            col * 4 + _star_rng.randrange(1, 4),   # x, one star per 4px band
            _star_rng.randrange(0, 11),            # y
            0.3 + 0.7 * _star_rng.random() ** 1.5,  # magnitude, skewed dim
        )
        for col in range(17)
    )
    # The status corner is a quiet zone: a white star one pixel from a
    # white digit welds onto the letterform now that no shadow separates
    # text from scene. The generator draw stays inside the comprehension
    # so the surviving stars' positions are unchanged.
    if not (star[0] < STATUS_CARD_W and star[1] <= 6)
]

# Grass fringe along the ground, varied heights (0-2 px above the ground
# row), clear of the house; a wind wave travels through the tall blades
_fringe_rng = random.Random(29)
GRASS_FRINGE = [(x, _fringe_rng.choice((0, 0, 0, 1, 1, 1, 2)))
                for x in range(2, 47)]
# The backroads verge: individual grass tufts, not a comb of blades.
#
# Three attempts got here. A brightness ripple was invisible (the panel
# crushes sub-30% luminance deltas). Per-column blade heights driven by one
# sine read as "weird green lines" — every column occupied, one wavelength,
# one hard threshold, so the whole verge pulsed like a metronome.
#
# What the lakefront's water gets right is not its formula but its
# IRREGULARITY: every row there carries its own wavelength and its own
# phase offset, so nothing lines up into a stripe. Grass needs the same
# irregularity with its own physics — discrete tufts that bend, at
# irregular spacings, each with its own stiffness, so identical wind moves
# them by different amounts. Gaps between tufts are what make the ones that
# remain read as objects rather than as a texture.
_verge_rng = random.Random(4127)


# Base height of the grass in each column, above the ground row. Irregular
# so the standing silhouette is ragged rather than a comb; the wind adds to
# these, and a sparse version (11 tufts) left the gust nothing to show
# itself on — 0.7 changed pixels per frame, invisible.
VERGE_BLADES = tuple(_verge_rng.choice((1, 1, 2, 2, 2)) for _ in range(W))
VERGE_FLEX = tuple(0.55 + 0.45 * _verge_rng.random() for _ in range(W))
# Per-column tone: real grass is not one flat green. Without this the verge
# was two colours and read as painted-on rather than grown.
VERGE_TONE = tuple(0.82 + 0.34 * _verge_rng.random() for _ in range(W))


VERGE_GUST_WIDTH = 18.0     # half-width of the front, in columns


# Static field texture. Deliberately LOW FREQUENCY: per-pixel randomness
# put 57 distinct colours across 72 pixels, which is the definition of
# white noise and read as static. What the lakefront gets right is that
# its variation happens over several pixels, so neighbours stay related
# and the eye sees a surface instead of speckle. Two incommensurable
# wavelengths keep the patches from repeating on any visible period.
def field_mottle(x: int, row: int) -> float:
    return (1.0
            + 0.17 * math.sin(x * 0.42 + row * 1.1)
            + 0.11 * math.sin(x * 0.17 + 2.3))


def verge_gust(x: int, phase: float) -> float:
    """One coherent gust front travelling the scene, left to right.

    Real wind in open country arrives as a single front, not as
    independent local motion — everything it passes bends at roughly the
    same moment, which is why the grass, the crop and the trees here all
    read this one function.

    The front starts and ends FAR enough off-panel that the seam is
    silent. At one width off the edge exp(-1) is still 0.37, so the old
    range jumped the left edge from nothing to a third of full gust
    across the loop join.
    """
    reach = 2.6 * VERGE_GUST_WIDTH
    front = -reach + phase * (W + 2 * reach)
    d = (x - front) / VERGE_GUST_WIDTH
    return math.exp(-d * d)


def verge_shimmer(x: int, row: int, phase: float, wind_kmh: float,
                  gust: float) -> float:
    """Per-pixel glint, driven BY the gust front rather than beside it.

    This used to advance on its own clock — `wraps` gave it two or three
    cycles per loop above 18 km/h while the front crossed once — so at any
    real wind the grass shimmered at a rate the poplars did not share, and
    the two read as moving out of sync ("the grass and the tree wind don't
    always move in sync", from the panel).

    Wind speed still sets how hard the grass glints; it no longer sets the
    RATE. The ripple advances once per loop with the front, and its
    amplitude follows the local gust, so grass sparkles where the wind is
    actually pressing and lies quiet where it is not. One event crossing
    the scene, which is what the trees are already doing.
    """
    amp = (4.0 + min(11.0, wind_kmh * 0.42)) / 100.0
    ripple = math.sin(x * (0.62 + 0.17 * row)
                      + math.tau * phase + row * 2.3)
    return 1.0 + amp * (0.2 + 0.8 * gust) * ripple


GRASS_COLOR = (36, 46, 30)
GRASS_COLOR_2 = (28, 38, 24)

# Two trees in the yard: (trunk x, size) — size 1 small, 2 big.
# Canopies go orange in autumn and bare in winter.
TREES = ((11, 2), (31, 1))
TRUNK_NIGHT, TRUNK_DAY = (34, 26, 20), (88, 66, 46)
CANOPY_NIGHT, CANOPY_DAY = (30, 52, 28), (66, 108, 50)
CANOPY_FALL = ((150, 76, 26), (196, 128, 40), (128, 60, 22))

WISP_SHAPE = [(0, 0), (1, 0), (2, 0), (3, -1), (4, -1), (5, 0), (6, 0)]

# Seasonal detail layers
CHIMNEY = (62, 6)  # drawn only in smoke weather; the original art has none
FIREFLY_COLOR = (168, 210, 70)
LEAF_COLORS = [(170, 92, 30), (140, 70, 24), (196, 128, 40)]
BIRD_COLOR = (22, 20, 26)

FLOCK_DIR = Path(__file__).parent / "assets" / "flock"


def _load_flock() -> list[list[tuple[int, int]]]:
    """The August 2026 murmuration, one dot list per frame."""
    frames = []
    for i in range(8):
        img = Image.open(FLOCK_DIR / f"flock_{i}.png").convert("RGB")
        px = _rgb_pixels(img)
        frames.append([
            (x, y) for y in range(14) for x in range(W)
            if 0.3 * px[x, y][0] + 0.59 * px[x, y][1] + 0.11 * px[x, y][2] > 60
        ])
    return frames


FLOCK_FRAMES = _load_flock()

# Forest: tall pines flanking a clearing (x34-49) with a lime tent and a
# campfire. (trunk x, canopy top row) — smaller top = taller tree.
FOREST_PINES = ((3, 4), (9, 2), (16, 6), (25, 3), (31, 7),
                (52, 2), (58, 5), (65, 3), (70, 6))
FOREST_ASPENS = (21, 49)              # deciduous pair: autumn/bare-winter
PINE_NIGHT, PINE_DAY = (16, 32, 26), (44, 88, 56)
FOREST_FLOOR_NIGHT, FOREST_FLOOR_DAY = (26, 24, 18), (74, 64, 44)
TENT_APEX = 41                        # ridge tent, rows 10-13

# Grove: a broadleaf wood — the scene that earns its autumn.
# (trunk x, crown radius); big crowns ride higher than small ones.
# Six trees you can count, with real sky between the crowns, and a hazy
# back row standing in the gaps for density. The first grove packed eight
# touching crowns into 72 columns and read as one continuous wash; the
# second's five separated trees read but sparse ("more trees" — the
# operator, 2026-08-12). Density comes from DEPTH now: the back row is
# small, dim, trunkless, and never touches a front crown, so every tree
# stays countable. Western trees (any column < STATUS_CARD_W) are small
# (r=2, crown top row 7): autumn crowns are orange-family, the clock's
# own hue, so no crown may enter the corner's airspace.
GROVE_TREES = ((5, 2), (16, 2), (29, 3), (41, 2), (53, 3), (66, 2))
# Back row: (x, crown center y), radius 1, standing offset from the
# dapple pools at the gap centers.
GROVE_BACKROW = ((11, 10), (23, 10), (35, 10), (47, 10), (59, 10), (71, 10))
GROVE_FALL = CANOPY_FALL + ((214, 160, 60), (170, 96, 56))
# Backroads: bird's-eye canopy split by a winding two-lane road.
def _road_y(x: int) -> int:
    y = 8 + 2.2 * math.sin(x * 0.09 + 0.9) + 1.1 * math.sin(x * 0.19 + 2.3)
    return max(4, min(11, int(round(y))))
TENT_NIGHT, TENT_DAY = (52, 96, 22), (128, 198, 52)   # lime nylon
TENT_GLOW = (222, 236, 132)           # flashlight through the fabric
FIRE_X = 34                           # campfire in the clearing

# Skyline: (x0, x1, height) back row; (x0, x1, height, kind) front row.
# kind 0 = flat roof, 1 = stepped with twin masts (Willis-ish),
# 2 = tapered with twin masts (Hancock-ish)
SKYLINE_BACK = [(0, 8, 6), (10, 17, 8), (26, 33, 7), (44, 52, 9), (60, 71, 6)]
SKYLINE_FRONT = [
    # The Willis-ish tower starts at 19, not 18: its west edge reached the
    # clock's halo, where a storm-dimmed wall sat under the contrast floor
    # beside the day ink. Structures stay out of the status corner.
    (2, 7, 8, 0), (9, 14, 8, 0), (19, 24, 12, 1), (27, 32, 8, 0),
    (36, 42, 11, 2), (46, 51, 7, 0), (54, 60, 10, 0), (63, 71, 8, 0),
]
WINDOW_WARM = (255, 190, 90)
WINDOW_COOL = (180, 200, 220)
BEACON_RED = (255, 60, 50)
HEADLIGHT = (232, 232, 208)
TAILLIGHT = (205, 52, 40)
CAR_DARK = (28, 30, 34)

# 3x5 clock digits — drawn into the frames so we control every pixel
# (the device's tiny-font 9 reads like a 4)
DIGITS_3X5 = {
    "0": ("111", "101", "101", "101", "111"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("111", "001", "111", "100", "111"),
    "3": ("111", "001", "011", "001", "111"),
    "4": ("101", "101", "111", "001", "001"),
    "5": ("111", "100", "111", "001", "111"),
    "6": ("111", "100", "111", "101", "111"),
    "7": ("111", "001", "001", "010", "010"),
    "8": ("111", "101", "111", "101", "111"),
    "9": ("111", "101", "111", "001", "111"),
    ":": ("0", "1", "0", "1", "0"),
    "°": ("11", "11", "00", "00", "00"),
    "-": ("00", "00", "11", "00", "00"),
}


def _add_glow(px, cx: int, cy: int, color: tuple, radius: float, strength: float) -> None:
    """Additively blend a soft radial glow into the frame."""
    r = int(radius) + 1
    for dy in range(-r, r + 1):
        for dx in range(-r, r + 1):
            x, y = cx + dx, cy + dy
            if not (0 <= x < W and 0 <= y < H):
                continue
            d = math.hypot(dx, dy)
            if d > radius:
                continue
            f = strength * (1.0 - d / radius) ** 2
            old = px[x, y]
            px[x, y] = tuple(min(255, int(o + c * f)) for o, c in zip(old, color))


MOON_DAY_OVERRIDE: float | None = None  # preview-only phase forcing

# Shadowed side: dark enough that a thin crescent's lit sliver visibly
# outshines it. At (52, 54, 68) the earthshine body was within a whisker of
# the crescent's apparent brightness on the panel near new moon, and the
# whole disc read as one flat grey blob (called from the physical bar,
# 2026-08-11) — the sphere hint survives at a third the level.
MOON_EARTHSHINE = (30, 32, 42)
MOON_TERMINATOR = (128, 128, 122)  # one soft step between light and shadow
# The face never changes: fixed maria patches, (dx, dy, darkening factor).
# LED gamma crushes subtle deltas — anything under ~30% darker vanishes on
# the physical panel, so these are far stronger than they look in preview.
MOON_MARIA = {(-1, -1): 0.60, (1, 0): 0.52, (0, 1): 0.68, (-2, 1): 0.64}

# Earth's shadow is not black. What reaches the eclipsed Moon is sunlight
# bent through the whole rim of Earth's atmosphere, with the blue scattered
# out of it long before it arrives — every sunrise on the planet at once,
# which is why a shadowed Moon goes copper rather than dark.
#
# Two levels, not a ramp: the panel crushes anything under ~30% per channel,
# and these clear it (R -51%, G -62%, B -45%) so the gradient survives the
# LEDs. The rim is the bright orange fringe just inside the shadow's edge;
# the ember is the deeper interior. Against MOON_COLOR the rim is a -34% /
# -77% / -89% step, so the shadow's boundary reads as a hard bite.
MOON_UMBRA_RIM = (150, 52, 22)
MOON_UMBRA_EMBER = (74, 20, 12)
# Depth in Moon-radii past the umbra's edge at which rim becomes ember.
MOON_UMBRA_EMBER_DEPTH = 0.9


def _ambient(elev: float, cloud: float, wx) -> Color:
    """RGB multipliers for light falling on STRUCTURES (never on things
    that emit their own light). Golden hour warms, overcast flattens and
    cools, storms drop everything into green-leaning gloom, rain dims."""
    daylight = min(max((elev + 6) / 18, 0.0), 1.0)
    r = g = b = 1.0
    if daylight > 0:
        if elev < 14 and cloud < 0.7:  # low visible sun gilds what it hits
            w = max(0.0, 1 - max(elev, 0) / 14) * (1 - cloud) * daylight
            r *= 1 + 0.60 * w
            g *= 1 + 0.10 * w
            b *= 1 - 0.50 * w
        dim = 0.32 * cloud * daylight  # overcast flattens the day
        r *= 1 - dim
        g *= 1 - dim * 0.95
        b *= 1 - dim * 0.85
    if wx.stormy:
        r *= 0.55; g *= 0.62; b *= 0.58
    elif wx.rain:
        r *= 0.78; g *= 0.80; b *= 0.82
    return (r, g, b)


def _shade(color: Color, amb: Color) -> RGB:
    return (
        max(0, min(255, int(color[0] * amb[0]))),
        max(0, min(255, int(color[1] * amb[1]))),
        max(0, min(255, int(color[2] * amb[2]))),
    )


# Red, green, warm white. Chosen for panel separation, not for accuracy: the
# test asserts >= 30% per-channel difference between neighbours, because below
# that the panel's gamma renders them as one colour and the string reads as a
# smear of warm dots.
#
# The warm bulb is (255, 255, 180), not the more obvious (255, 190, 90) --
# that value is WINDOW_WARM, byte-for-byte, and the lakefront/backroads
# guard tests build `decor = set(XMAS_BULBS) | {XMAS_TREE}` to assert none
# of it ever lands on water or road. A collision there doesn't just look
# wrong, it makes the guard match window light instead of decorations,
# passing only by luck (no lamp happens to sit on a guarded row today).
# (255, 255, 180) keeps >= 30% separation from every neighbour above,
# INCLUDING WINDOW_WARM, so it also reads as a distinct paler warm-white
# next to the windows' amber glow rather than folding into them.
XMAS_BULBS = ((235, 40, 40), (40, 200, 70), (255, 255, 180))
XMAS_SPACING = 3          # one bulb every N points: real strings have gaps
XMAS_TWINKLE = 2          # whole cycles per .anim loop, or the seam jumps


def string_lights(px, points, phase: float, amb: tuple = (1, 1, 1)) -> None:
    """Hang a string of bulbs along `points`, an ordered list of (x, y).

    Single pixels on purpose. Everywhere else on this panel a one-pixel detail
    vanishes -- but a light string is read as a RHYTHM rather than as
    individual lamps, so the gaps are the shape. Fattening the bulbs turns it
    into a lit bar, which is the haze failure.
    """
    # Reduced before it ever reaches math.sin: phase=1.0 must produce the
    # exact same float as phase=0.0, and 4*pi is not bit-identical to 0 even
    # though sin() is mathematically periodic -- so the seam only closes if
    # the two calls land on the identical input, not just an equivalent one.
    phase %= 1.0
    for i, (x, y) in enumerate(points):
        if i % XMAS_SPACING:
            continue
        if not (0 <= x < W and 0 <= y < H):
            continue
        bulb = XMAS_BULBS[(i // XMAS_SPACING) % len(XMAS_BULBS)]
        # Closes exactly at phase 1.0, so the loop seam is invisible.
        swell = 0.75 + 0.25 * math.sin(
            math.tau * (XMAS_TWINKLE * phase + i / len(XMAS_BULBS)))
        px[x, y] = _shade(_rgb_int(c * swell for c in bulb), amb)


XMAS_TREE = (26, 92, 46)        # conifer green, dark enough that bulbs pop


def draw_lit_tree(px, base_x: int, base_y: int, phase: float,
                  amb: tuple = (1, 1, 1)) -> None:
    """A small conifer with a string on it: 3 px wide, 4 tall plus a trunk.

    Wide enough to read as a shape -- a one-pixel-wide tree is an isolated
    dot on this panel, not a tree. The BULBS are single pixels, which is the
    one sanctioned exception, because a string is read as a rhythm.
    """
    body = _shade(XMAS_TREE, amb)
    rows = ((0, 0), (-1, 1), (0, 1), (1, 1), (-1, 2), (0, 2), (1, 2))
    for dx, dy in rows:
        x, y = base_x + dx, base_y - 3 + dy
        if 0 <= x < W and 0 <= y < H:
            px[x, y] = body
    if 0 <= base_x < W and 0 <= base_y < H:
        px[base_x, base_y] = _shade(TRUNK_NIGHT, amb)
    string_lights(px, [(base_x + dx, base_y - 3 + dy)
                       for dx, dy in ((0, 0), (-1, 1), (1, 1), (-1, 2), (1, 2))],
                  phase, amb)


def settle_snow(px, tops: dict[int, int], tier: int,
                amb: tuple = (1, 1, 1)) -> None:
    """Lay settled snow on whatever surface `tops` describes.

    `tops[x]` is the y of the topmost surface pixel in column x; snow lands on
    that pixel. Mutates px in place.

    Drawn MOSTLY-OFF on purpose. The panel's LEDs are 1.23mm lit on a 2.2mm
    pitch, so a filled bright row reads as a haze of separated dots rather
    than as a surface, and it drowns the scene above it. Sparse bright marks
    are what actually read as snow.
    """
    if tier <= 0:
        return
    # Fall back to the deepest DEFINED tier, never to 1.0: an unrecognized
    # tier must degrade to "as much snow as we ever draw", not to "every
    # column lit", which is the haze failure this whole function exists to
    # prevent. Don't "simplify" this back to 1.0.
    take = int(len(_SNOW_ORDER) * SNOW_FRACTION.get(tier, SNOW_FRACTION[3]))
    lit = _shade(SNOW_LIT, amb)
    shade = _shade(SNOW_SHADE, amb)
    for i, x in enumerate(_SNOW_ORDER[:take]):
        y = tops.get(x)
        if y is None or not (0 <= y < 16):
            continue
        px[x, y] = lit if i % 3 else shade   # broken, not a solid bar
        if tier >= 3 and y + 1 < 16:
            px[x, y + 1] = shade             # depth: a second, darker row


def surface_tops(px, x_range, y_range, sky: set) -> dict[int, int]:
    """The topmost non-sky pixel in each column: where snow would land.

    Rooftops, banks and road shoulders are all the same question asked of
    different scenes, so they share one answer. Columns that are sky all the
    way down are omitted rather than defaulted, so nothing snows in mid-air.
    """
    tops = {}
    for x in x_range:
        for y in y_range:
            if px[x, y] not in sky:
                tops[x] = y
                break
    return tops


def _sun_screen_pos(now: datetime, elev: float, wx: "WeatherState",
                    cloud: float) -> tuple[int, int] | None:
    """Where the sun disc sits on screen, or None when it is not drawn.

    Shared by the renderer and the status ink choice: the morning arc
    starts at x=4, inside the status corner, and the ink beside a ~250
    luminance disc must be dark no matter what the weather estimate says.
    """
    if elev <= 0 or wx.stormy or cloud >= 0.9:
        return None
    local = now.astimezone(TZ)
    day_frac = (local.hour * 60 + local.minute) / 1440
    f = min(max((day_frac - 0.23) / 0.58, 0.0), 1.0)  # ~5:30-19:30 arc
    return int(4 + f * 40), int(6 - 4 * math.sin(math.pi * f)) + 1


def _draw_sun(px, cx: int, cy: int, strength: float, breath: float) -> None:
    """The sun with the same care as the moon: the classic round 7px disc
    (sun and moon subtend the same angle, so same size), a near-white
    blinding core inside the gold body, a soft limb, and a wide halo that
    breathes. `strength` fades it all behind real cloud."""
    r = 3.4  # the 3,5,7,7,7,5,3 raster circle, same as the moon
    core_r = 1.8
    _add_glow(px, cx, cy, SUN_COLOR, 5.5, (0.30 + 0.30 * strength
                                           + 0.04 * breath))
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            d2 = dx * dx + dy * dy
            if d2 > r * r:
                continue
            x, y = cx + dx, cy + dy
            if not (0 <= x < W and 0 <= y < H):
                continue
            col = (255, 251, 230) if d2 <= core_r * core_r else SUN_COLOR
            alpha = strength
            if d2 > (r - 0.7) * (r - 0.7):
                alpha *= 0.55  # soft limb into the sky
            px[x, y] = tuple(int(b * (1 - alpha) + c * alpha)
                             for b, c in zip(px[x, y], col))


def _draw_moon(px, cx: int, cy: int, phase_days: float, breath: float,
               eclipse=None) -> None:
    """The moon as it actually looks tonight: correct phase and orientation,
    earthshine on the shadowed part, a soft terminator, and its own face.

    Waxing = lit on the right, waning = lit on the left.

    `eclipse` is an `EclipseState` when Earth's shadow is on the disc. It
    arrives already converted to Moon-radii on screen axes, so the geometry
    here is one circle test per pixel and nothing more. Only the umbra is
    drawn: a penumbral eclipse is a few percent of dimming that nobody sees
    without instruments, and painting one would invent a spectacle.
    """
    # r chosen for the classic round 7px raster circle (row widths
    # 3,5,7,7,7,5,3) — r ~3.1 degenerates into a square with four nubs
    r = 3.4
    synodic = 29.53
    ill = (1.0 - math.cos(math.tau * phase_days / synodic)) / 2  # lit fraction
    waxing = phase_days <= synodic / 2
    for dy in range(-3, 4):
        for dx in range(-3, 4):
            if dx * dx + dy * dy > r * r:
                continue
            half_w = math.sqrt(max(r * r - dy * dy, 1e-4))
            t = half_w * (1.0 - 2.0 * ill)  # terminator x for this row
            metric = (dx - t) if waxing else (-dx - t)  # >=0 means sunlit
            x, y = cx + dx, cy + dy
            if not (0 <= x < W and 0 <= y < H):
                continue
            if metric >= 0.6 or ill >= 0.98:
                color = MOON_COLOR
                factor = MOON_MARIA.get((dx, dy))
                if factor is not None:
                    color = _rgb_int(c * factor for c in color)
            elif metric >= -0.6 and ill > 0.02:
                color = MOON_TERMINATOR
            else:
                color = MOON_EARTHSHINE
            if eclipse is not None and eclipse.in_umbra:
                # Position of this pixel, and of the umbra's centre, both in
                # Moon radii — the only units the shadow geometry speaks.
                depth = eclipse.umbra_r - math.hypot(
                    dx / r - eclipse.umbra_dx, dy / r - eclipse.umbra_dy)
                if depth > 0:
                    # Maria are deliberately dropped inside the shadow: a
                    # 30%-darkened patch of an already-dim ember is a delta
                    # the panel cannot show, so it would only muddy the one
                    # edge that matters, which is the shadow's own.
                    color = (MOON_UMBRA_EMBER
                             if depth >= MOON_UMBRA_EMBER_DEPTH
                             else MOON_UMBRA_RIM)
            px[x, y] = color
    # The halo answers to how much Moon is still in sunlight. A 93%-covered
    # disc lighting the sky as brightly as a full one is the same lie as
    # drawing the disc uncovered.
    lit = ill * (1.0 - eclipse.obscuration) if eclipse is not None else ill
    if lit > 0.15:
        _add_glow(px, cx, cy, MOON_COLOR, 4.8, lit * (0.09 + 0.03 * breath))


def _moon_age_days(on_date) -> float:
    """Convert Astral's 0..28 phase index to a synodic-month age."""
    return moon.phase(on_date) / 28.0 * 29.530588


_ECLIPSE_CACHE: tuple[int, object] | None = None


def _eclipse_now(now: datetime):
    """Earth's shadow on the Moon right now, or None — never fatal.

    Cached to the minute because one ambient loop renders 40 frames of the
    same instant, and because the whole point of local math is that it costs
    nothing. An exception here must lose the shadow, not the sky: this runs
    inside the frame renderer, and a night with no moon at all would be a
    far worse failure than a night with an unmarked eclipse.
    """
    global _ECLIPSE_CACHE
    minute = int(now.timestamp()) // 60
    if _ECLIPSE_CACHE is not None and _ECLIPSE_CACHE[0] == minute:
        return _ECLIPSE_CACHE[1]
    try:
        state = eclipse_visible_state(now, OBSERVER)
    except Exception:  # noqa: BLE001 - ambient detail, never fatal
        state = None
    _ECLIPSE_CACHE = (minute, state)
    return state


MOONLIGHT = (168, 178, 196)  # the cool silver the moon paints with


def _apply_moonlight(px, mx: int, my: int, ill: float, cloud: float,
                     phase: float) -> None:
    """Strong moonlight on the scene: a silver pool on the ground sliding
    with the moon, a kiss on the grass tufts, and rim light along the
    house's top silhouette. Scales with the real lit fraction, dies under
    cloud, and breathes with the loop like everything else here."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return
    for x in range(W):
        mix = 0.85 * strength * math.exp(-((x - mx) / 13.0) ** 2)
        if mix < 0.02:
            continue
        px[x, 15] = tuple(int(c) for c in _lerp_rgb(px[x, 15], MOONLIGHT, mix))
        if px[x, 14] == GRASS_COLOR:
            px[x, 14] = tuple(int(c) for c in
                              _lerp_rgb(GRASS_COLOR, MOONLIGHT, 0.9 * mix))
    for hx, hy in HOUSE_TOP.items():
        d = math.hypot(hx - mx, hy - my)
        mix = 0.75 * strength * math.exp(-(d / 26.0) ** 2)
        if mix >= 0.02:
            px[hx, hy] = tuple(int(c) for c in
                               _lerp_rgb(px[hx, hy], MOONLIGHT, mix))


def _apply_moonlight_forest(px, mx: int, my: int, ill: float, cloud: float,
                            phase: float, fire_on: bool) -> None:
    """Backlight, not floodlight: the moon rides BEHIND the treeline
    here, so no pool reaches the floor in front of the silhouettes.
    What backlight does paint: rim light down the moon-side edges of
    the pines, a glow along the back-ridge crest where light bleeds
    through the woods, a kiss on the aspen crowns, and the faintest
    line on the tent's ridge."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return
    # Glow along the back-ridge crest, bleeding through under the moon
    for x in range(W):
        mix = 0.30 * strength * math.exp(-((x - mx) / 11.0) ** 2)
        if mix < 0.02:
            continue
        crest = 11 + int(1.0 + 1.4 * math.sin(x * 0.55 + 1.3))
        if 0 <= crest < H:
            px[x, crest] = tuple(int(c) for c in _lerp_rgb(
                px[x, crest], MOONLIGHT, mix))
    # Rim light down the moon-side edge of each pine's upper tiers
    for tx, top in FOREST_PINES:
        side = -1 if mx < tx else 1  # each tree rims toward its own moon
        for i in range(0, 6):
            cy = top + i
            if cy >= 13:
                break
            edge_x = tx + side * min(2, i // 2)
            if not (0 <= edge_x < W):
                continue
            d = math.hypot(edge_x - mx, cy - my)
            mix = 0.60 * strength * math.exp(-(d / 20.0) ** 2)
            if mix >= 0.02:
                px[edge_x, cy] = tuple(int(c) for c in _lerp_rgb(
                    px[edge_x, cy], MOONLIGHT, mix))
    # Aspen crowns catch a kiss
    for ax in FOREST_ASPENS:
        d = math.hypot(ax - mx, 9 - my)
        mix = 0.4 * strength * math.exp(-(d / 20.0) ** 2)
        if mix >= 0.02:
            px[ax, 9] = tuple(int(c) for c in _lerp_rgb(
                px[ax, 9], MOONLIGHT, mix))
    # The tent is foreground: backlight only grazes its ridge line
    d = math.hypot(TENT_APEX - mx, 10 - my)
    mix = 0.30 * strength * math.exp(-(d / 22.0) ** 2)
    if mix >= 0.02:
        px[TENT_APEX, 10] = tuple(int(c) for c in _lerp_rgb(
            px[TENT_APEX, 10], MOONLIGHT, mix))


def _skyline_col_top(x0: int, x1: int, h: int, kind: int, x: int) -> int:
    """Highest row a building occupies at column x, honoring crowns/tapers."""
    top = H - 1 - h
    if kind == 0 or x0 + 2 <= x <= x1 - 2:
        return top
    if kind == 2 and x in (x0 + 1, x1 - 1):
        return top + 1
    return top + 2


def _apply_moonlight_skyline(px, mx: int, my: int, ill: float, cloud: float,
                             phase: float) -> None:
    """City moonlight: no pool on the streets — the moon silvers what faces
    it. Rooflines and setback ledges catch it with distance falloff, the
    back row gets a hazy version, and each tower's moon-side glass corner
    takes a faint sheen. Windows are interior pixels and stay untouched."""
    strength = ill * (1.0 - cloud) * (0.9 + 0.1 * math.sin(math.tau * phase))
    if strength < 0.08:
        return

    def kiss(x, y, amt):
        if 0 <= x < W and 0 <= y < H and amt >= 0.02:
            px[x, y] = tuple(int(c) for c in _lerp_rgb(px[x, y], MOONLIGHT, amt))

    front_cover: dict[int, int] = {}
    for x0, x1, h, kind in SKYLINE_FRONT:
        for x in range(x0, x1 + 1):
            t = _skyline_col_top(x0, x1, h, kind, x)
            front_cover[x] = min(front_cover.get(x, H), t)

    # Back-row rooftops: fainter, hazier, and only where the front row
    # doesn't hide them
    for x0, x1, h in SKYLINE_BACK:
        top = H - 1 - h
        for x in range(x0, x1 + 1):
            if front_cover.get(x, H) <= top:
                continue
            d = math.hypot(x - mx, top - my)
            kiss(x, top, 0.35 * strength * math.exp(-(d / 24.0) ** 2))

    for x0, x1, h, kind in SKYLINE_FRONT:
        top = H - 1 - h
        # Rooflines plus the ledges where crowns and tapers step back
        rows = [top] + ([top + 2] if kind == 1 else [top + 1, top + 2] if kind == 2 else [])
        for y in rows:
            span = _building_row_span(x0, x1, h, kind, y)
            if not span:
                continue
            above = _building_row_span(x0, x1, h, kind, y - 1) if y > top else None
            for x in range(span[0], span[1] + 1):
                if above and above[0] <= x <= above[1]:
                    continue  # a storey above covers this pixel: no sky here
                d = math.hypot(x - mx, y - my)
                kiss(x, y, 0.7 * strength * math.exp(-(d / 26.0) ** 2))
        # The moon-side glass corner, a sheen running down the exposed edge
        if mx < x0 or mx > x1:
            for y in range(top, 14):
                span = _building_row_span(x0, x1, h, kind, y)
                if not span:
                    continue
                ex = span[0] if mx < x0 else span[1]
                d = math.hypot(ex - mx, y - my)
                kiss(ex, y, 0.4 * strength * math.exp(-(d / 20.0) ** 2))


# What the air itself glows when it is full of something. These are
# AIRLIGHT colours — the light the medium scatters toward you — not paint
# laid over the scene. See _apply_obscuration for why that distinction is
# the whole design.
# Tuned so every pair clears ~30% on at least two channels, which is what
# the panel needs to tell two colours apart at all. The first attempt put
# haze and dust one channel apart and they were the same colour on the
# strip; the fix was to separate the two grey ones by LIGHTNESS and the two
# warm ones by saturation, rather than nudging all four around a hue wheel
# they were too crowded on. `test_the_four_tints_are_distinguishable` pins it.
OBSCURATION_TINT = {
    "haze": (190, 194, 204),   # bright milky white, faintly blue
    "smoke": (150, 62, 24),    # deep orange-red: a wildfire day
    "dust": (176, 124, 48),    # warm tan; sand shares it
    "ash": (100, 96, 100),     # dark neutral mineral grey, no warmth
}
# How much light survives each medium. Lower = thicker. These are not one
# constant because the media are not equally opaque, and a shared value
# made volcanic ash read as "a slightly dimmer blue day": ash is a dark
# tint, so at high transmission the blue sky simply showed through it.
# Density is what separates an ashfall from a haze, so it belongs here
# rather than in the colour.
OBSCURATION_TRANSMISSION = {
    "haze": 0.55,   # milky, but you can still see the sky through it
    "smoke": 0.32,
    "dust": 0.30,
    "ash": 0.18,    # blots the sky out; the sun becomes a disc you can look at
}


def _apply_obscuration(px, kind: str, daylight: float) -> None:
    """Haze, smoke, dust or volcanic ash filling the whole air column.

    This is the standard atmospheric model, and using the real one is what
    keeps it legal on this panel:

        observed = object * transmission + airlight * (1 - transmission)

    The tempting version — lerp every pixel toward a smoke colour — lights
    up a black night sky, which is precisely the filled-row haze failure
    that `settle_snow` and SNOW_FRACTION exist to prevent. Airlight is
    *scattered sunlight*, so scaling it by daylight makes the formula do
    the right thing at both ends of the day for free: at noon the air
    glows and distance dissolves into it, at midnight there is nothing to
    scatter, so the medium only subtracts and the sky stays properly dark
    with its stars swallowed.

    Nothing here knows which scene it is drawing. Depth comes from the art
    itself: distant things were already drawn dim, so they land closest to
    the airlight and disappear first, while bright emissive marks — lit
    windows, lamps, the campfire — stay above it and survive. That is
    aerial perspective, and it falls out of the physics rather than out of
    a per-scene table.
    """
    tint = OBSCURATION_TINT.get(kind)
    if tint is None:
        return
    t = OBSCURATION_TRANSMISSION[kind]
    airlight = [c * daylight * (1.0 - t) for c in tint]
    for y in range(H):
        for x in range(W):
            px[x, y] = tuple(
                min(255, int(c * t + a))
                for c, a in zip(px[x, y], airlight)
            )


def _draw_clouds(px, now: datetime, cloud: float, daylight: float, stormy: bool) -> None:
    """Soft puffs drifting slowly right-to-left, count scaled by cover."""
    count = min(5, round(cloud * 5) + (1 if cloud > 0.1 else 0))
    if count == 0:
        return
    col: Color
    if stormy:
        col = (52, 62, 58)
    else:
        col = _lerp_rgb((36, 40, 54), (198, 202, 208), daylight)
    drift = (now.timestamp() / 60.0) * 1.2  # px per minute, continuous
    puff_rng = random.Random(31)
    span = W + 28
    for _ in range(count):
        base_x = puff_rng.randrange(0, span)
        cw = puff_rng.randrange(7, 12)
        ch = puff_rng.choice((2, 2, 3))
        cy = puff_rng.randrange(1, 6)
        cx = int((base_x - drift) % span) - 14
        strength = 0.35 + 0.3 * cloud
        for dy in range(-ch, ch + 1):
            for dx in range(-cw, cw + 1):
                x, y = cx + dx, cy + dy
                if not (0 <= x < W and 0 <= y < H):
                    continue
                d = (dx / cw) ** 2 + (dy / ch) ** 2
                if d >= 1.0:
                    continue
                f = strength * (1.0 - d) ** 1.5
                px[x, y] = tuple(
                    int(o * (1 - f) + c * f) for o, c in zip(px[x, y], col)
                )


def _building_row_span(x0: int, x1: int, h: int, kind: int, y: int) -> tuple[int, int] | None:
    """Horizontal extent of a building at row y, honoring setbacks/taper.

    Buildings stand on the street row (15), so bodies span top..14.
    """
    top = H - 1 - h
    if y < top or y > 14:
        return None
    if kind == 1 and y <= top + 1:  # stepped crown
        return (x0 + 2, x1 - 2)
    if kind == 2:  # taper
        if y == top:
            return (x0 + 2, x1 - 2)
        if y == top + 1:
            return (x0 + 1, x1 - 1)
    return (x0, x1)


def _draw_skyline(px, local, elev: float, daylight: float, seed: int,
                  phase: float, horizon: tuple, wx: WeatherState,
                  amb: tuple = (1, 1, 1),
                  storm_day: bool = False) -> None:
    # Snapshot the living sky (and the plain ground row under it) before
    # this scene paints towers over it -- same reasoning as _draw_grove's
    # sky_before: the gradient carries per-pixel noise, so there's no
    # fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(W) for y in range(H)}
    front_col = _shade(_lerp_rgb((24, 26, 34), (126, 130, 138), daylight), amb)
    back_col = tuple(int(c) for c in _lerp_rgb(front_col, horizon, 0.5))
    night = elev < 0

    for x0, x1, h in SKYLINE_BACK:
        for x in range(x0, x1 + 1):
            for y in range(H - 1 - h, H - 1):
                px[x, y] = back_col

    hour = local.hour
    if not night:
        lit_frac = 0.25 if storm_day else 0.0  # storm gloom: lights on
    elif 17 <= hour <= 22:
        lit_frac = 0.55  # the city is home from work
    elif hour >= 23 or hour < 5:
        lit_frac = 0.18  # night owls
    else:
        lit_frac = 0.35  # early risers / late dusk

    # Every lit window this frame lands here, in the exact order drawn --
    # the Christmas recolour below reads this list rather than re-deriving
    # "which pixels are windows" by matching WINDOW_WARM/WINDOW_COOL, since
    # that colour also appears on the house sprite and the lakefront lamps.
    lit_windows: list[tuple[int, int]] = []

    for bi, (x0, x1, h, kind) in enumerate(SKYLINE_FRONT):
        top = H - 1 - h
        # Two facade languages, alternating: curtain-wall glass towers (the
        # whole face is glass, darker floor-slab lines every other row) and
        # punched masonry (lighter wall, clearly darker window grid — by
        # day real windows read DARKER than the facade around them)
        curtain = bi % 2 == 0
        if curtain:
            body = _shade(_lerp_rgb((14, 17, 26), (104, 120, 142), daylight), amb)
            slab = _shade(_lerp_rgb((10, 12, 18), (76, 88, 104), daylight), amb)
        else:
            body = _shade(_lerp_rgb((22, 22, 30), (132, 128, 118), daylight), amb)
            wind_c = _shade(_lerp_rgb((34, 36, 48), (86, 98, 120), daylight), amb)
        for y in range(top, H - 1):
            span = _building_row_span(x0, x1, h, kind, y)
            if span:
                row = (slab if curtain and (y - top) % 2 == 0 else body)
                for x in range(span[0], span[1] + 1):
                    px[x, y] = row
        # Which windows are lit shifts every ten minutes, and some
        # buildings keep a whole office floor burning
        win_rng = random.Random(seed * 13 + bi * 7)
        office_floor = None
        if win_rng.random() < 0.3:
            office_floor = top + 1 + 2 * win_rng.randrange(max(1, (13 - top) // 2))
        for wy in range(top + 1, 14, 2):
            span = _building_row_span(x0, x1, h, kind, wy)
            if not span:
                continue
            for wxx in range(span[0] + 1, span[1], 2):
                if lit_frac > 0 and (wy == office_floor or win_rng.random() < lit_frac):
                    color = WINDOW_WARM if win_rng.random() < 0.7 else WINDOW_COOL
                    px[wxx, wy] = color
                    lit_windows.append((wxx, wy))
                elif not curtain:
                    px[wxx, wy] = wind_c
        # Rooftop furniture on the flat roofs: antennas and water tanks
        if kind == 0:
            if bi % 3 == 0:
                ax = (x0 + x1) // 2
                for ay in (top - 1, top - 2):
                    if ay >= 0:
                        px[ax, ay] = front_col
            elif bi % 3 == 1:
                for ty in (top - 1, top - 2):
                    if ty >= 0:
                        px[x0 + 1, ty] = front_col
                        px[x0 + 2, ty] = front_col
        # Twin masts with aviation beacons on the tall towers
        if kind in (1, 2):
            for mi, mx in enumerate((x0 + 2, x1 - 2)):
                for my in range(max(0, top - 3), top):
                    px[mx, my] = front_col
                # ~one blink every two seconds, like the real ones
                if math.sin(math.tau * 4 * phase + bi * 1.7 + mi) > 0.7:
                    px[mx, max(0, top - 3)] = BEACON_RED
            if kind == 2 and night:  # lit crown on the tapered tower
                span = _building_row_span(x0, x1, h, kind, top)
                if span:
                    for x in range(span[0], span[1] + 1):
                        px[x, top] = (150, 140, 110)

    if is_christmas(local):
        # Recolour, never add: extra lit windows would change the tower's
        # apparent occupancy, and more lit pixels is this panel's haze
        # direction (LEDs lit on a mostly-dark pitch -- see the app skill).
        # Only a minority turn festive, so the skyline still reads as an
        # office tower first and a decoration second.
        #
        # Seeded independently of `seed`/`phase` so the choice of WHICH lit
        # windows go red or green is fixed across frames and across
        # weather -- a window that flips colour every frame reads as a
        # fault, not decoration. lit_windows is itself already deterministic
        # for a given seed/hour (built above from win_rng), so replaying the
        # same festive draws over it in the same order reproduces the same
        # festive windows every time.
        festive = random.Random(1225)
        for fx, fy in lit_windows:
            if festive.random() < 0.35:
                # Raw, not _shade()'d: lit windows are emissive, same as the
                # warm/cool windows beside them (see the raw write above at
                # the base window loop) -- they are the window's own light,
                # not a surface reflecting the ambient. Shading it agrees
                # with the raw write at night (amb is the identity there),
                # but diverges in the storm_day path (lit_frac=0.25,
                # amb != identity): a shaded festive window there lands
                # ~30% dimmer than its unshaded warm neighbour, right at
                # the panel's contrast floor, so it reads as dirty rather
                # than festive.
                px[fx, fy] = XMAS_BULBS[festive.randrange(2)]

    # The street: two streams of traffic at different speeds for parallax —
    # headlights flowing one way, taillights the other
    street = _shade(_lerp_rgb((15, 15, 19), (112, 110, 108), daylight), amb)
    for x in range(W):
        px[x, 15] = street
    span_r = W + 10
    car_rng = random.Random(41)
    for _ in range(4):  # the fast stream, two crossings per loop
        x0 = car_rng.randrange(span_r)
        pos = (x0 + int(phase * 2 * span_r)) % span_r - 5
        if 0 <= pos < W:
            px[pos, 15] = HEADLIGHT if night else CAR_DARK
            if pos > 0:
                px[pos - 1, 15] = tuple(c // 2 for c in HEADLIGHT) if night else CAR_DARK
    for _ in range(3):  # the slow stream, one crossing per loop
        x0 = car_rng.randrange(span_r)
        pos = (x0 - int(phase * span_r)) % span_r - 5
        if 0 <= pos < W:
            px[pos, 15] = TAILLIGHT if night else CAR_DARK
            if pos < W - 1:
                px[pos + 1, 15] = tuple(c // 2 for c in TAILLIGHT) if night else CAR_DARK

    # Settled snow: rooftops, not ground. There's no single "roof" row --
    # towers step to all sorts of heights -- so the answer is the same one
    # every scene asks: the topmost non-sky pixel per column, searched
    # over the whole frame rather than a fixed band near the floor.
    tier = snow_tier(wx.snow_depth_m)
    if tier:
        tops = surface_tops(px, range(W), range(H), sky_before)
        settle_snow(px, tops, tier, amb)


LAKE_TOP = 7                     # horizon row at the bend — the lake dominates
LAKE_DEEP = (18, 40, 56)         # teal, clearly apart from sky and towers on LEDs
GLITTER_MOON = (210, 220, 240)
GLITTER_SUN = (255, 214, 120)
FOAM = (225, 230, 235)

# The Oak Street bend as a gentle S: the water's edge eases right to the
# elbow (row 11) and back, in small 3-5px steps — the corners get a
# blend pixel so the shoreline reads as a curve, not a staircase.
BEND_WATER_END = {7: 46, 8: 50, 9: 54, 10: 58, 11: 60, 12: 56, 13: 51, 14: 46, 15: 41}
# The trail is a thin ribbon on the near rows only; at the bend it
# disappears behind the treeline like in the photos
BEND_PATH = {11: (60, 61), 12: (56, 58), 13: (51, 53), 14: (46, 49), 15: (41, 45)}
BEND_LADDER = ((56, 12), (46, 14))         # railing dots along the edge curve
BEND_LAMPS_FAR = (52, 58, 64, 70)          # light string on the treeline, y7
BEND_LAMPS_NEAR = ((60, 10), (51, 12), (41, 14))
LAMP_WARM = (255, 200, 120)
TREE_DARK = (16, 24, 16)
CONCRETE_NIGHT = (108, 104, 94)  # the lit path glows pale against dark water
CONCRETE_DAY = (160, 158, 150)
# Tower cluster at the bend: (x0, x1, top_row, is_hancock); bodies end at y6,
# the treeline strip hides their feet
BEND_TOWERS = (
    (40, 45, 4, False), (46, 49, 5, False), (50, 58, 1, True),
    (59, 63, 3, False), (64, 67, 5, False), (68, 71, 2, False),
)
# Navy Pier's wheel, tiny above the horizon. East of the status corner:
# at (4, 5) its rim sat one pixel from the clock strokes — dark steel by
# day beside black ink, lit gondolas by night beside white ink, both
# under the contrast floor. The pier genuinely extends into the lake, so
# standing it over the water edge is also the truer picture.
WHEEL_HUB = (23, 5)
# The lit conifer on the bank, near the wheel. Bank rows 13-15 are NOT
# water-free (BEND_WATER_END shows real open water in all three), so this
# is anchored past every row the tree's 3-wide/4-tall footprint touches
# (12 through 15) -- past BEND_PATH's apron too, so it stands on the grass
# beyond the trail rather than in the middle of it. (59, 15) is the
# leftmost -- closest to the wheel -- column where that holds.
LAKEFRONT_TREE = (59, 15)



def is_winter(when: datetime) -> bool:
    """Bare-limb season. Northern-hemisphere meteorological winter -- the same
    assumption the scene art has always made, now stated in one place."""
    return when.month in (12, 1, 2)


def _draw_grove(px, local, elev: float, daylight: float, seed: int,
                phase: float, wx, amb: tuple,
                moon_pos: tuple | None, moon_ill: float,
                cloud: float) -> None:
    """A grove you can count: five separated broadleaf trees on a dark
    meadow, real sky in the gaps between the crowns, and warm dapples on
    the ground beneath those gaps while the sun is up.

    The first version filled rows 9-15 with a haze wall, a back row of
    crowns, and a lit floor, and every crown touched its neighbour: the
    2026-08-11 audit artifacts read as one continuous wash with not a
    single distinguishable tree. Mostly-OFF is the law this scene now
    obeys — the trees are the only filled shapes, the meadow goes truly
    dark at night, and the gaps stay sky."""
    # Snapshot the living sky before this scene paints over it. The
    # gradient carries per-pixel noise (and wisps, stars, birds), so there
    # is no fixed palette to match against -- only "whatever was here a
    # moment ago, before we drew the wood on top of it."
    sky_before = {px[x, y] for x in range(W) for y in range(6, 16)}
    mm = local.month
    fall = mm in (9, 10, 11)
    winter = is_winter(local)
    spring = mm in (3, 4, 5)
    dl = max(0.25, daylight)
    moonf = 0.0
    if moon_pos is not None:
        moonf = moon_ill * max(0.0, 1.0 - cloud * 1.2)
    wind_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
    rustle = wx.wind_kmh >= 8

    # Distant treeline: a one-row silhouette with a ragged top. Dark
    # against the sky reads as depth; the old haze wall read as a wash.
    if fall:
        line_n, line_d = (26, 16, 8), (84, 56, 26)
    elif winter:
        line_n, line_d = (22, 20, 18), (72, 66, 60)
    else:
        line_n, line_d = (10, 18, 10), (36, 58, 30)
    tl_c = _shade(_lerp_rgb(line_n, line_d, dl * 0.6), amb)
    for x in range(W):
        px[x, 12] = tl_c
        if math.sin(x * 0.53 + 0.4) > 0.55:
            px[x, 11] = tl_c

    # Meadow: real daylight, no floor value — near-black on a moonless
    # night, which is what a meadow is. Rows recede downward.
    if winter:
        meadow_n, meadow_d = (12, 12, 12), (118, 116, 110)
    elif fall:
        # Dark enough that the trunks stay distinct: at (96, 66, 30) the
        # fall meadow was byte-close to trunk brown and the trees floated
        # all over again, one season later.
        meadow_n, meadow_d = (18, 13, 7), (58, 40, 18)
    else:
        meadow_n, meadow_d = (8, 10, 6), (52, 74, 36)
    ground = _shade(_lerp_rgb(meadow_n, meadow_d, daylight), amb)
    for x in range(W):
        px[x, 13] = ground
        px[x, 14] = _rgb_int(v * 0.85 for v in ground)
        px[x, 15] = _rgb_int(v * 0.65 for v in ground)

    # The back row: small hazy crowns standing in the front gaps, half
    # blended toward the treeline so they read as depth, not clutter.
    # Trunkless and low — distance eats trunks and height first. Winter
    # bares them entirely (the treeline silhouette carries that season).
    if not winter:
        for bi, (bx, bcy) in enumerate(GROVE_BACKROW):
            b_rng = random.Random(53 * bi + 7)
            if fall:
                b_day = GROVE_FALL[b_rng.randrange(len(GROVE_FALL))]
                b_night = _rgb_int(v * 0.30 for v in b_day)
            elif spring:
                b_day, b_night = (98, 150, 62), (26, 40, 22)
            else:
                b_day, b_night = (86, 140, 58), (17, 28, 16)
            b_c = tuple(int(c) for c in _lerp_rgb(
                _shade(_lerp_rgb(b_night, b_day, max(0.25, daylight) * 0.8),
                       amb),
                tl_c, 0.45))
            for dy in range(-1, 2):
                half = 1 if dy == 0 else 0
                for dx in range(-half, half + 1):
                    x, y = bx + dx, bcy + dy
                    if 0 <= x < W and 0 <= y < H:
                        px[x, y] = b_c

    # Grove trunks: two pixels wide and warmer than the shared constants,
    # at night as well as by day. A one-pixel trunk in shared browns
    # vanished against the treeline and the crowns read as floating in
    # the sky (caught by a fresh-eyes review, 2026-08-11) — and a tree
    # that is not visibly attached to the ground is not a tree.
    trunk_c = _shade(_lerp_rgb(
        (36, 28, 21), _rgb_int(min(255, v * 1.5) for v in TRUNK_DAY), dl),
        amb)
    for ti, (tx, r) in enumerate(GROVE_TREES):
        t_rng = random.Random(47 * ti + 3)
        # Crowns sit ON the treeline (bottom row 11), not above it.
        cy = 8 if r == 3 else 9
        # From the crown's bottom row: leafy seasons overpaint that row,
        # and winter's bare lattice needs the trunk to reach it.
        for txx in (tx, tx + 1):
            for ty in range(cy + r, 14):
                if 0 <= txx < W:
                    px[txx, ty] = trunk_c
        if winter:  # bare lattice: limbs against the sky
            for lx, ly in ((tx - 1, cy), (tx + 1, cy), (tx, cy - 1),
                           (tx - r, cy + 1), (tx + r, cy + 1),
                           (tx - 1, cy + 2), (tx + 1, cy + 2)):
                if 0 <= lx < W:
                    px[lx, ly] = trunk_c
            continue
        if fall:  # each tree commits to its own autumn hue
            base_day = GROVE_FALL[t_rng.randrange(len(GROVE_FALL))]
            base_night = _rgb_int(v * 0.30 for v in base_day)
        elif spring:
            base_day, base_night = (98, 150, 62), (26, 40, 22)
        else:
            # Brighter than the shared CANOPY_DAY by day — a crown dimmer
            # than the midday sky reads as a hole — and dimmer than
            # CANOPY_NIGHT after dark: night trees are silhouettes, and
            # the blind review called the brighter version "glowing".
            base_day, base_night = (86, 140, 58), (17, 28, 16)
        sway = 0
        if rustle:
            gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
            sway = (wind_lean or 1) if gust > 0.3 else 0
        for dy in range(-r, r + 1):
            half = int(math.sqrt(max(0, r * r - dy * dy)) + 0.5)
            row_sway = sway if dy <= -r + 1 else 0
            for dx in range(-half, half + 1):
                x, y = tx + dx + row_sway, cy + dy
                if not (0 <= x < W and 0 <= y < H):
                    continue
                deep = t_rng.random() < 0.3
                lit = (0.5 if deep else 1.0) * max(0.25, daylight)
                if dy == -r and daylight > 0.4:
                    lit = min(1.0, lit * 1.3)   # sun on the crown's top row
                leaf_color = _lerp_rgb(base_night, base_day, lit)
                px[x, y] = _shade(leaf_color, amb)
        # Spring: a few blossom flecks, hue-separated from the leaves —
        # pink against green survives the panel where a tint would not
        if spring and t_rng.random() < 0.7:
            for _ in range(2):
                bx2 = tx + t_rng.randint(-(r - 1), r - 1)
                by2 = cy - t_rng.randint(0, 1)
                if 0 <= bx2 < W and 0 <= by2 < H:
                    px[bx2, by2] = _shade((216, 168, 190), amb)
        # Moonlit rim on the crown, sliding with the moon
        if moonf > 0.12 and moon_pos is not None:
            d = math.hypot(tx - moon_pos[0], (cy - r) - moon_pos[1])
            mix = 0.5 * moonf * math.exp(-(d / 20.0) ** 2)
            if mix >= 0.02 and 0 <= tx < W and 0 <= cy - r < H:
                px[tx, cy - r] = tuple(int(c) for c in _lerp_rgb(
                    px[tx, cy - r], MOONLIGHT, mix))

    # Light through the gaps — the thing that says "grove". By day, warm
    # dapples on the meadow under each sky gap; by night, if the moon is
    # out, a silver pool under the gap nearest the moon. Both sit on
    # ground that is otherwise dark, so two pixels are enough to read.
    gaps = [((a + ra + b - rb) // 2, abs(a - b))
            for (a, ra), (b, rb) in zip(GROVE_TREES, GROVE_TREES[1:])]
    if not winter and daylight > 0.45:
        # A tint OF the ground, not a foreign colour: fixed tan patches
        # read as orphan trunk stubs in the blind review, and stayed tan
        # when autumn recoloured everything around them.
        dap = tuple(int(c) for c in _lerp_rgb(
            ground, (255, 238, 170), 0.5 * daylight))
        for gx, _ in gaps:
            for dx in (-1, 0, 1):
                if 0 <= gx + dx < W:
                    px[gx + dx, 13] = dap
    elif moonf > 0.25 and moon_pos is not None:
        gx = min(gaps, key=lambda g: abs(g[0] - moon_pos[0]))[0]
        pool = _lerp_rgb(ground, MOONLIGHT, 0.45 * moonf)
        px[gx, 13] = tuple(int(c) for c in pool)
        px[gx + 1, 13] = tuple(int(c) for c in pool)

    # Autumn: leaves ride the wind down through the wood
    if fall:
        leaf_rng = random.Random(31)
        for i in range(5):
            off = leaf_rng.random()
            x0 = leaf_rng.randrange(0, W)
            prog = (phase + off) % 1.0
            ly = 3 + int(prog * 10)
            lx = (x0 + int(prog * (4 + wx.wind_kmh / 6)) * (wind_lean or -1)
                  + round(math.sin(math.tau * 2 * phase + i * 1.9)))
            if 0 <= lx < W and 0 <= ly < H:
                px[lx, ly] = LEAF_COLORS[i % 3]

    # Settled snow: everything below the sky takes it on the top edge --
    # the floor, and the upper face of every crown that catches it.
    tier = snow_tier(wx.snow_depth_m)
    if tier:
        snow_tops = surface_tops(px, range(W), range(6, 16), sky_before)
        settle_snow(px, snow_tops, tier, amb)


# --- traffic ---------------------------------------------------------------
#
# Cars are NOT part of the scene loop. Baked into an 8-second loop they
# repeated ~7.5 times a minute and, because the texture seed only turns over
# every ten minutes, the SAME two cars made the same trip about 75 times
# before anything changed — "it looks like it's looping", from the panel
# (2026-08-15). Traffic is now a one-shot overlay on the three rows it
# actually occupies: each episode gets its own entropy, its own vehicles,
# and its own speeds, so no two crossings are alike. A non-looping overlay
# also frees the vehicles from completing whole journeys per cycle, which is
# what lets them have individual speeds at all.
TRAFFIC_PAINTS = ((176, 46, 40), (208, 204, 198), (76, 106, 172),
                  (218, 162, 46), (60, 130, 90))
TRAFFIC_BAND_TOP = 9          # scene rows 9-11: glass, body, wheels
TRAFFIC_BAND_ROWS = 3
TRAFFIC_FPS = 10


def traffic_density(hour: int) -> tuple[float, float]:
    """(mean seconds between episodes, mean vehicles per episode).

    A country road, by the clock: commuters bunch morning and evening,
    midday ambles, and the small hours are a lone pair of headlights.
    """
    if 6 <= hour < 9 or 15 <= hour < 18:
        return 26.0, 2.6         # rush
    if 9 <= hour < 15:
        return 48.0, 1.7         # midday
    if 18 <= hour < 23:
        return 65.0, 1.4         # evening
    return 150.0, 1.0            # the small hours


def _vehicle_kind(rng: random.Random, hour: int) -> str:
    roll = rng.random()
    if hour < 6 or hour >= 23:   # freight owns the night road
        return "semi" if roll < 0.45 else "sedan" if roll < 0.8 else "pickup"
    return ("sedan" if roll < 0.5 else "pickup" if roll < 0.72
            else "semi" if roll < 0.88 else "police")


def _draw_vehicle(put, x_nose: int, row: int, kind: str, paint, trailer,
                  lights_on: bool, amb: tuple, far: bool, blink: bool) -> None:
    """One vehicle, nose at `x_nose`, wheels on `row`.

    `put(x, y, colour)` clips for its own surface, so this draws the same
    whether the target is a full frame or the three-row traffic band.
    """
    if far:
        # The far lane runs leftbound and small: three pixels, dimmer, with
        # its lights on the opposite ends.
        body = _shade(_rgb_int(v * 0.6 for v in paint), amb)
        for dx in range(3):
            put(x_nose + dx, row - 1, body)
        if lights_on:
            put(x_nose - 1, row - 1, (235, 215, 150))
            put(x_nose + 3, row - 1, (150, 24, 16))
        return
    body = _shade(paint, amb)
    glass = _shade((70, 84, 104), amb)
    wheel = (16, 16, 18)
    if kind == "semi":
        trl = _shade(trailer, amb)
        for dx in range(2):
            put(x_nose - dx, row - 1, body)
        put(x_nose - 1, row - 2, body)
        put(x_nose, row - 2, glass)
        for dx in range(3, 11):
            put(x_nose - dx, row - 1, trl)
            put(x_nose - dx, row - 2, trl)
        for wdx in (0, 4, 9, 10):
            put(x_nose - wdx, row, wheel)
        if lights_on:
            put(x_nose + 1, row - 1, (255, 240, 170))
            put(x_nose + 2, row - 1, (170, 150, 95))
            put(x_nose - 11, row - 1, (200, 30, 18))
            put(x_nose - 11, row - 2, (200, 30, 18))
        return
    for dx in range(4):
        put(x_nose - dx, row - 1, body)
    put(x_nose - 1, row - 2, glass)
    if kind == "pickup":
        put(x_nose - 3, row - 1, tuple(int(v * 0.6) for v in body))
    else:
        put(x_nose - 2, row - 2, body)
    if kind == "police":
        put(x_nose - 2, row - 2,
            (255, 40, 40) if blink else (60, 90, 255))
    put(x_nose, row, wheel)
    put(x_nose - 3, row, wheel)
    if lights_on:
        put(x_nose + 1, row - 1, (255, 240, 170))
        put(x_nose + 2, row - 1, (170, 150, 95))
        put(x_nose - 4, row - 1, (200, 30, 18))


def plan_traffic(rng: random.Random, hour: int, lights_on: bool,
                 n_vehicles: int) -> list[dict]:
    """Lay out one episode's vehicles: when each enters, how fast, which way.

    Arrivals are spread by random gaps rather than an even cadence, and each
    vehicle carries its own speed, so nothing about an episode is periodic.
    """
    plan: list[dict] = []
    entry = rng.uniform(4.0, 14.0)
    for _ in range(max(1, n_vehicles)):
        far = rng.random() < 0.45
        kind = "sedan" if far else _vehicle_kind(rng, hour)
        speed = rng.uniform(0.9, 1.5)
        if kind == "semi":
            speed *= 0.8                      # loaded, and slower for it
        paint = TRAFFIC_PAINTS[rng.randrange(len(TRAFFIC_PAINTS))]
        if kind == "police":
            paint = (225, 225, 230)
        trailer = ((222, 222, 218) if rng.random() < 0.6 else (176, 120, 60))
        if lights_on:
            paint = _rgb_int(v * 0.22 for v in paint)
            trailer = _rgb_int(v * 0.28 for v in trailer)
        plan.append({
            "far": far, "kind": kind, "speed": speed, "paint": paint,
            "trailer": trailer, "entry_s": entry,
        })
        # A believable gap: usually a real pause, occasionally a tailgater.
        # Kept short enough that an episode stays an event — a 45-second
        # overlay would be on screen almost continuously, which is the
        # looping problem wearing a different hat.
        entry += rng.uniform(1.0, 2.5) if rng.random() < 0.3 \
            else rng.uniform(3.0, 7.5)
    return plan


def traffic_episode_frames(band: Image.Image, plan: list[dict],
                           lights_on: bool, amb: tuple,
                           foreground=frozenset()) -> list[Image.Image]:
    """Render one traffic episode across the three-row band.

    Nothing here loops, so a vehicle only has to enter off one edge and
    leave off the other; it never has to arrive back where it started.
    """
    row = TRAFFIC_BAND_ROWS - 1        # wheels sit on the band's last row
    bw = band.width
    lead = 16                          # longest sprite plus its lights
    span = bw + 2 * lead
    last_exit = max(
        (v["entry_s"] + span / (v["speed"] * TRAFFIC_FPS) for v in plan),
        default=1.0)
    n = int((last_exit + 1.5) * TRAFFIC_FPS)
    n = max(TRAFFIC_FPS, min(n, 400))   # 40s ceiling per episode
    base_px = _rgb_pixels(band)
    frames: list[Image.Image] = []
    for f in range(n):
        im = band.copy()
        pxb = _rgb_pixels(im)

        def put(x, y, c, _p=pxb):
            if 0 <= x < bw and 0 <= y < TRAFFIC_BAND_ROWS:
                _p[x, y] = c

        for v in plan:
            travelled = (f - v["entry_s"] * TRAFFIC_FPS) * v["speed"]
            if travelled < 0:
                continue
            if v["far"]:                       # leftbound
                x_nose = int(round(bw + lead - travelled))
            else:                              # rightbound
                x_nose = int(round(-lead + travelled))
            if x_nose < -lead or x_nose > bw + lead:
                continue
            _draw_vehicle(put, x_nose, row, v["kind"], v["paint"],
                          v["trailer"], lights_on, amb, v["far"],
                          blink=(f // 2) % 2 == 0)
        # Trunks stand in front of the road: repaint them over the traffic
        # so a car passes behind a poplar instead of through it.
        for fx, fy in foreground:
            if 0 <= fx < bw and 0 <= fy < TRAFFIC_BAND_ROWS:
                pxb[fx, fy] = base_px[fx, fy]
        frames.append(im)
    return frames


def lights_on_train(elev: float, wx) -> bool:
    return elev < 2 or wx.stormy


def _road_R(x: int) -> int:
    """Side-view road line: dead level.

    Every amplitude tried (1.0, then 0.8) rounded into row-steps that a
    context-free review read as disconnected stripes "wrapping around the
    display like text". On 16 rows a two-lane reads as a road when it is
    a line; the poles, dashes, and traffic carry the depth."""
    return 11


# The lit conifer on the shoulder. _road_R(x) is a rolling profile present
# at every column -- there's no off-road column span to sit in, only a row
# that varies with x. x=58..71 is the widest stretch where _road_R holds
# steady at its lowest value (10, giving the most clearance below it), and
# it's clear of the poplar lane (trunks at BACKROADS_POPLARS). (64, 15)
# centers the tree in that stretch, with its whole footprint (rows 12-15)
# sitting below _road_R(x)+1 -- the road and its painted shoulder line --
# for every column the tree actually touches (63-65).
BACKROADS_TREE = (64, 15)
# The poplar lane: five tall flames, evenly spaced, the road running
# behind their trunks — the operator's lookbook pick (option B, then
# tree design G, 2026-08-12). x=21 keeps the first crown east of the
# status corner; x=61 keeps the last clear of the lit conifer above.
BACKROADS_POPLARS = (21, 31, 41, 51, 61)


def _draw_backroads(px, local, elev: float, daylight: float, seed: int,
                    phase: float, wx, amb: tuple, moon_ill: float,
                    cloud: float, lane: bool = True) -> None:
    """Eye-level americana: a two-lane road through rolling farmland.
    Telephone poles carry sagging wire, a farmhouse sits on the far
    hill, and side-profile cars cruise both ways, strobing behind the
    trunks of a five-poplar lane and showing back up. The sky above is
    the living sky, same as every side-view scene."""
    # Snapshot the living sky before this scene paints over it -- same
    # reasoning as _draw_grove's sky_before: the gradient is noisy, so
    # there's no fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(W) for y in range(6, 16)}
    mm = local.month
    fall = mm in (9, 10, 11)
    winter = is_winter(local)
    dl = max(0.22, daylight)          # built structures keep a face at night
    dl_f = max(0.06, daylight)        # the land itself goes honestly dark

    if winter:
        far_n, far_d = (40, 44, 56), (152, 158, 170)
        fld_n, fld_d = (48, 52, 64), (198, 204, 214)
    elif fall:
        far_n, far_d = (38, 32, 16), (124, 100, 48)
        fld_n, fld_d = (46, 38, 16), (164, 132, 52)
    else:
        far_n, far_d = (18, 30, 18), (58, 88, 46)
        fld_n, fld_d = (24, 40, 22), (82, 120, 54)
    far_c = _shade(_lerp_rgb(far_n, far_d, dl_f), amb)
    fld_c = _shade(_lerp_rgb(fld_n, fld_d, dl_f), amb)

    # Far hills roll along the horizon; fields run down to the road
    for x in range(W):
        hill = 8 + round(1.4 * math.sin(x * 0.05 + 0.5))
        R = _road_R(x)
        for y in range(hill, min(R, H)):
            px[x, y] = far_c if y < hill + 2 else fld_c
        for y in range(R + 2, H):   # near verge below the road
            px[x, y] = fld_c

    # The rail line rides a distant ridge along the horizon, east of the
    # clock corner. A crossing train (watch_trains' one-shot overlay)
    # enters at one screen edge and leaves at the other; on the west side
    # the only thing it passes behind is the clock card itself, which
    # touches the frame edge — no invented structure needed. Two earlier
    # versions taught that lesson: a trestle on stilts read as a fence
    # floating in the sky, and a 3-pixel "grain elevator" (then a whole
    # knob-and-silo complex) read as a magician's cabinet eating freights.
    ridge_c = _shade(_lerp_rgb(
        _rgb_int(v * 0.55 for v in far_n),
        _rgb_int(v * 0.7 for v in far_d), dl_f), amb)
    # Full width — starting the ridge at the corner's edge once left a
    # sky-colored slab and a four-row horizon step there (blind review,
    # round two). Red ink is hue-separated from the olive crest, so the
    # crest no longer needs to duck the status corner.
    for x in range(W):
        hill = 8 + round(1.4 * math.sin(x * 0.05 + 0.5))
        for y in range(6, hill):
            px[x, y] = ridge_c
    # Rail bed: one subtle row along the ridge crest, from the status
    # card's fixed edge (x=20) to the screen edge — continuous under the
    # whole train band, so a crossing freight is on rail for every visible
    # column of its journey.
    # Steel, not a brightness whisper: the first version lifted the ridge
    # tone by ~10%, under the 30% floor, and the panel showed no track at
    # all — the operator asked where the rail line went. Cool grey against
    # the olive ridge, day and night.
    # Night floor eased to 0.3: at 0.4 a blind review read the unlit
    # night rail as "a broken underline attached to the clock" floating
    # over an invisible ridge. Steel by day, a whisper by night —
    # headlights own that hour anyway.
    rail_c = _shade(_lerp_rgb((70, 72, 82), (150, 152, 162),
                              max(0.3, daylight)), amb)
    for x in range(W):
        px[x, 5] = rail_c   # hidden beneath the card west of STATUS_CARD_W

    # Farmhouse on the far hill: walls, roof, a window that keeps watch.
    # East of center — its old spot (x=9) hid it under the clock corner.
    hx = 46
    # White clapboard, dark roof: a brown-on-brown 3x3 block was
    # "genuinely unidentifiable" to a viewer who didn't know it was there.
    wall = _shade(_lerp_rgb((46, 44, 40), (206, 200, 188), dl), amb)
    roof = _shade(_lerp_rgb((44, 16, 12), (128, 46, 36), dl), amb)

    # The barn, left of centre and across the road from the house. Placement
    # is deliberate: the scene's structures live in the gaps between poplars,
    # the clock owns columns 0-18, and putting both buildings in the right
    # half left the whole left of the road empty. Red against the fields is
    # also the scene's only strong hue — it measured last of six for colour.
    bx0, bx1 = 23, 31
    barn_body = _shade(_lerp_rgb((44, 14, 10), (176, 48, 34), dl), amb)
    barn_roof = _shade(_lerp_rgb((30, 29, 28), (138, 132, 122), dl), amb)
    for x in range(bx0 + 1, bx1):
        px[x, 6] = barn_roof
    for x in range(bx0, bx1 + 1):
        px[x, 7] = barn_roof
    for x in range(bx0, bx1 + 1):
        for y in range(8, 11):
            px[x, y] = barn_body
    # The white X-brace door: the mark that says BARN at any distance.
    door = _rgb_int(v * 0.45 for v in barn_body)
    trim = _shade(_lerp_rgb((70, 66, 58), (216, 206, 184), dl), amb)
    for x in range(bx0 + 3, bx0 + 7):
        for y in range(9, 11):
            px[x, y] = door
    for tx2, ty2 in ((bx0 + 3, 9), (bx0 + 6, 9), (bx0 + 4, 10),
                     (bx0 + 5, 10)):
        px[tx2, ty2] = trim
    if elev < 2 or wx.stormy:              # the yard lamp, after dark
        px[bx0 + 1, 8] = (255, 190, 90)
    # A five-wide roof with eaves over three-wide walls, plus a chimney:
    # at 3x3 the blind reviews called it "mushroom, tent, buoy, person".
    # The overhang is what says "house".
    for dx in range(-2, 3):
        px[hx + dx, 7] = roof
    for dx in range(-1, 2):
        px[hx + dx, 8] = wall
        px[hx + dx, 9] = wall
    px[hx + 1, 6] = _rgb_int(v * 0.6 for v in roof)
    px[hx + 2, 6] = _rgb_int(v * 0.6 for v in roof)      # chimney
    if elev < 2 or wx.stormy:
        # Two lit windows and a porch lamp. One pixel of warm light was all
        # this scene had at night, against the other scenes' many.
        px[hx, 8] = (255, 190, 90)
        px[hx - 1, 8] = (255, 206, 128)
        px[hx + 1, 9] = _rgb_int(v * 0.72 for v in (255, 196, 96))
    # Woodsmoke when it is cold enough to have the stove going. Three puffs
    # on whole cycles, so the loop seam never jumps.
    if wx.temp_c < 8.0:
        for i in range(3):
            drift = math.sin(math.tau * (phase + i / 3.0))
            sx = hx + 2 + int(round(drift * 1.4))
            sy = 5 - i
            if 0 <= sx < W and 0 <= sy < H:
                px[sx, sy] = _shade(
                    _rgb_int(v * (0.75 - i * 0.2) for v in (168, 168, 174)),
                    amb)

    # The road: surface line + shoulder, dashes suggesting markings.
    # Dimmer floor than the buildings: at night the road is where the
    # headlights are, not a lit ribbon of its own.
    asphalt = _shade(_lerp_rgb((32, 32, 36), (108, 108, 114),
                               max(0.15, daylight)), amb)
    shoulder = tuple(int(v * 0.72) for v in asphalt)
    dash_c = _shade(_lerp_rgb((92, 92, 88), (215, 215, 205), dl), amb)
    moonf = moon_ill * max(0.0, 1.0 - cloud * 1.2)
    lift = int(12 * moonf)
    for x in range(W):
        R = _road_R(x)
        px[x, R] = tuple(min(255, v + lift) for v in asphalt)
        if R + 1 < H:
            px[x, R + 1] = shoulder
        # Three lit, ten dark. The road is one pixel tall, so a dash does
        # not sit ON the asphalt, it REPLACES it — at the old two-on/two-off
        # the row came out 34 dark pixels alternating with 33 near-white
        # ones, which on a gapped panel is speckle rather than a road. Sparse
        # bright marks on a dark ribbon is both the real thing and the way
        # this display wants to be drawn.
        if x % 13 < 3:
            px[x, R] = tuple(min(255, v + lift) for v in dash_c) \
                if daylight > 0.3 else px[x, R]


    # The far fields carry static texture, not motion, and that is a
    # measured decision rather than a concession. The field was one flat
    # colour ("no shading or variance to the grass colour isn't helping"),
    # so it gets shading — but a wind wave does not work in two rows. With
    # the gust at full strength, 16 of 72 pixels per row changed by more
    # than the panel's ~30% visibility floor between gust phases, and a
    # viewer shown two phases blind still called them the same picture.
    # Scattered intensity has no shape to follow; the eye tracks edges and
    # objects. So the wind lives where this scene has both contrast and
    # shape — the poplar crowns against bright sky, and the verge grass,
    # both of which change SILHOUETTE.
    #
    # Rows 9-11 are also the traffic overlay's band, and it composites a
    # snapshot of them; motion here would freeze whenever a car passed.
    field_rows = tuple(y for y in range(8, _road_R(0)) if y >= 0)
    for y in field_rows:
        for x in range(W):
            base_c = px[x, y]
            if base_c == ridge_c:             # the ridge is rock, not crop
                continue
            px[x, y] = _rgb_int(
                v * field_mottle(x, y - field_rows[0]) for v in base_c)

    # One wind direction for everything that bends in this scene: the
    # verge grass below and the poplar crowns above.
    lane_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        lane_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)

    verge_rows = (_road_R(0) + 2, _road_R(0) + 3, _road_R(0) + 4)
    top_row, mid_row, base_row = verge_rows
    # Wind is a change of SHAPE, not of brightness. The panel crushes a
    # luminance ripple (under ~30% it is invisible), so the swell changes
    # how tall each blade stands: the grass line rises and falls as the gust
    # travels, and the tallest tips lean with it. One front per loop, which
    # both edges of the panel are clear of at the seam.
    shade_c = _rgb_int(v * 0.45 for v in px[0, top_row])
    blade_c = _shade(_lerp_rgb((26, 34, 22), (74, 104, 48), daylight), amb)
    tip_c = _shade(_lerp_rgb((32, 42, 26), (96, 132, 60), daylight), amb)
    # The mass of the verge is the bottom row; the tufts stand on it, and
    # the row under the shoulder is shadow the tufts read against.
    for x in range(W):
        if top_row < H:
            px[x, top_row] = shade_c
        if mid_row < H:
            px[x, mid_row] = shade_c
        if base_row < H:
            px[x, base_row] = blade_c
    downwind = lane_lean or 1
    steady = min(1.0, wx.wind_kmh / 30.0)
    for x in range(W):
        gust = verge_gust(x, phase)
        press = (steady * 0.42 + gust * 0.85) * VERGE_FLEX[x]
        height = VERGE_BLADES[x] + (1 if press > 0.46 else 0)
        lean = downwind if press > 0.62 else 0
        for i in range(height):
            y = base_row - i
            if not top_row <= y < H:
                continue
            tip = i == height - 1
            xx = x + lean if tip else x
            if not 0 <= xx < W:
                continue
            base_c = tip_c if tip else blade_c
            # Tone: this column's own green, lifted where the gust has the
            # blades turned over, times the untrackable per-pixel shimmer.
            f = (VERGE_TONE[x] * (1.0 + 0.16 * press)
                 * verge_shimmer(xx, y - top_row, phase, wx.wind_kmh, gust))
            px[xx, y] = tuple(
                max(0, min(255, int(v * f))) for v in base_c)

    if (elev < -2 and local.month in (6, 7, 8) and wx.temp_c >= 15.0
            and not (wx.rain or wx.snow or wx.stormy)):
        fly_rng = random.Random(seed * 61 + 7)
        for _ in range(6):
            fx = fly_rng.randrange(2, W - 2)
            fy = fly_rng.choice(verge_rows)
            blink = math.sin(math.tau * (2 * phase + fly_rng.random()))
            if blink > 0.15 and fy < H:
                px[fx, fy] = _rgb_int(
                    v * (0.45 + 0.55 * blink) for v in FIREFLY_COLOR)

    # The poplar lane: five tall flames the road runs behind — the
    # operator's pick from the lookbook rounds (lane, then poplars).
    # Drawn after the cars, so traffic strobes behind five trunks per
    # crossing. Each tree is seeded to its own height and, in autumn,
    # its own hue; winter bares them to twig spires; the top two rows
    # sway on whole gust cycles so the loop seam never jumps.
    # `lane=False` renders the same road with no poplars. The one-shot
    # freight overlay diffs the two to learn which sky-band pixels are
    # foreground trees, so a passing boxcar never slices a crown.
    p_trunk = _shade(_lerp_rgb(
        (40, 31, 24), _rgb_int(min(255, v * 1.3) for v in TRUNK_DAY), dl),
        amb)
    # The crown lives ABOVE the traffic band; only the trunk crosses it.
    # The first lane put the crown at rows 5-11 — down through the road —
    # five pixels wide, so foliage owned 25 of the road's 52 columns and a
    # car vanished completely between trees, then reappeared on the far
    # side ("cars that spawn from trees or disappear into trees", from the
    # panel, 2026-08-15). A tree hides traffic with its trunk.
    crown_bottom = _road_R(0) - 3
    for pi, txp in enumerate(BACKROADS_POPLARS if lane else ()):
        p_rng = random.Random(97 * pi + 5)
        top = 3 + p_rng.randrange(2)
        # A single-pixel trunk, centred: the doubled trunk read heavy
        # from the physical panel ("the thickness of the trunk throws
        # off the trees"), and a full-height vertical line survives the
        # LED gaps where an isolated single pixel would not. It runs from
        # under the crown to the ground, crossing field, road, and verge,
        # so a passing car is interrupted by exactly one pixel.
        for y in range(crown_bottom + 1, 16):
            px[txp, y] = p_trunk
        if winter:
            # A bare poplar is a twig spire: the trunk keeps rising,
            # with alternating stub branches for texture.
            for y in range(top, crown_bottom + 1):
                px[txp, y] = p_trunk
                stub = _rgb_int(v * 0.8 for v in p_trunk)
                if y % 2 == pi % 2 and txp - 1 >= 0:
                    px[txp - 1, y] = stub
                elif txp + 1 < W:
                    px[txp + 1, y] = stub
            continue
        if fall:
            p_day = GROVE_FALL[p_rng.randrange(len(GROVE_FALL))]
            p_night = _rgb_int(v * 0.30 for v in p_day)
        else:
            p_day, p_night = (58, 118, 44), (16, 27, 15)
        # The same gust front the grass and the crop read. Each tree used
        # its own phase offset (`+ txp` radians, effectively random per
        # tree), so the five of them fidgeted independently and the wind
        # never read as one event crossing the scene. A poplar against
        # bright sky is the highest-contrast thing here and the way people
        # actually SEE wind, so this is where the motion has to land — the
        # far field measured 16 of 72 pixels over the visibility floor
        # between gust phases and a blind viewer still called two phases
        # the same picture, because scattered intensity has no shape to
        # follow.
        sway = 0.0
        if wx.wind_kmh >= 8:
            sway = verge_gust(txp, phase) * (lane_lean or 1)
        for y in range(top, crown_bottom + 1):
            # Odd widths, symmetric about the trunk: 3 wide at the tip
            # and base of the crown, 5 wide through the middle.
            w_row = 1 if y in (top, crown_bottom) else 2
            # Bend tapers to the tip: the crown's base barely moves, the
            # top two rows carry up to two pixels. A whole tree sliding
            # sideways reads as a glitch; a tree bending reads as wind.
            depth = (crown_bottom - y) / max(1, crown_bottom - top)
            row_sway = int(round(sway * 2.0 * depth * depth))
            for dx in range(-w_row, w_row + 1):
                x2 = txp + dx + row_sway
                if not 0 <= x2 < W:
                    continue
                lit = ((0.6 if p_rng.random() < 0.25 else 1.0)
                       * max(0.25, daylight))
                c = _rgb_int(_lerp_rgb(p_night, p_day, lit))
                if y == top and daylight > 0.4:
                    c = _rgb_int(min(255, v * 1.35) for v in c)
                px[x2, y] = _shade(c, amb)

    # Settled snow: everything below the sky takes it on the top edge,
    # EXCEPT the road itself -- a ploughed road reads wet-black, and white
    # shoulders against it is the honest picture. asphalt/dash_c/shoulder
    # are exactly the colours this scene just used to paint the road, so
    # excluding them (same mechanism as sky_before) keeps snow off the
    # pavement without inventing a new predicate for where the road is.
    # The road is level now, so there are no softened step seams to
    # exclude alongside them.
    tier = snow_tier(wx.snow_depth_m)
    if tier:
        road_colors = {
            tuple(min(255, v + lift) for v in asphalt),
            tuple(min(255, v + lift) for v in dash_c),
            shoulder,
        }
        snow_tops = surface_tops(px, range(W), range(6, 16),
                                 sky_before | road_colors)
        settle_snow(px, snow_tops, tier, amb)

    # A tree on the shoulder, drawn dead last so nothing painted above --
    # settled snow included -- lands on top of it. BACKROADS_TREE was
    # chosen against _road_R itself (see its comment) so the road stays
    # clear without a fixed column range, the same problem the
    # settled-snow block above solves by excluding the road's own live
    # colours instead of a column span.
    if is_christmas(local):
        tx, ty = BACKROADS_TREE
        draw_lit_tree(px, tx, ty, phase, amb)


def _draw_forest(px, local, elev: float, daylight: float, seed: int,
                 phase: float, wx, amb: tuple,
                 moon_pos: tuple | None, moon_ill: float,
                 cloud: float) -> None:
    """Deep woods around a clearing: lime tent, campfire, tall pines."""
    # Snapshot the living sky before this scene paints over it -- same
    # reasoning as _draw_grove's sky_before: the gradient is noisy, so
    # there's no fixed palette to match, only what was here a moment ago.
    sky_before = {px[x, y] for x in range(W) for y in range(6, 16)}
    mm = local.month

    wind_lean = 0
    if wx.wind_dir is not None and wx.wind_kmh >= 5:
        comp = math.sin(math.radians(wx.wind_dir + 180))
        wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
    rustle = wx.wind_kmh >= 8

    # Back tree line: a hazy unbroken ridge the tall pines stand against
    ridge = _shade(_lerp_rgb((16, 24, 20), (52, 78, 46), daylight), amb)
    # Fold sky into the ridge from the gradient itself — sampling a live
    # pixel here once made the whole ridge blink when a wisp crossed it
    sky_top, sky_bot = _sky_colors(elev)
    sky_ref = _lerp_rgb(sky_top, sky_bot, 0.2)
    haze = tuple(int(r * 0.55 + s * 0.45) for r, s in zip(ridge, sky_ref))
    for x in range(W):
        top = 11 + int(1.0 + 1.4 * math.sin(x * 0.55 + 1.3))
        for y in range(top, 15):
            px[x, y] = haze

    # Forest floor: needle duff, warmer than the moss row beneath it
    floor = _shade(_lerp_rgb(FOREST_FLOOR_NIGHT, FOREST_FLOOR_DAY,
                             daylight), amb)
    for x in range(W):
        px[x, 14] = floor

    # Aspen pair among the pines: the deciduous accent that keeps seasons
    trunk_c = _shade(_lerp_rgb(TRUNK_NIGHT, TRUNK_DAY, daylight), amb)
    asp_rng = random.Random(43)
    for ax in FOREST_ASPENS:
        for ty in range(11, 14):
            px[ax, ty] = trunk_c
        if is_winter(local):  # bare winter limbs
            for lx, ly in ((ax - 1, 10), (ax + 1, 10), (ax, 9)):
                if 0 <= lx < W:
                    px[lx, ly] = trunk_c
            continue
        for cy in range(9, 12):
            half = 1 if cy > 9 else 0
            for cx in range(ax - half, ax + half + 1):
                if not (0 <= cx < W):
                    continue
                if mm in (9, 10, 11):
                    base = CANOPY_FALL[asp_rng.randrange(3)]
                else:
                    base = CANOPY_DAY
                px[cx, cy] = _shade(
                    _lerp_rgb((22, 34, 20), base, max(0.25, daylight)), amb)

    # Tall pines: triangular evergreens, tips gusting downwind
    pine_rng = random.Random(41)
    for tx, top in FOREST_PINES:
        for ty in range(13, 15):
            px[tx, ty] = trunk_c
        sway = 0
        if rustle:
            gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
            sway = (wind_lean or 1) if gust > 0.3 else 0
        rows = 13 - top
        for i, cy in enumerate(range(top, 13)):
            half = min(2, i // 2)
            if i >= rows - 2:
                half = 3  # the skirt flares at the forest floor
            row_sway = sway if i < 3 else 0  # only the crown moves
            for cx in range(tx - half + row_sway, tx + half + 1 + row_sway):
                if not (0 <= cx < W):
                    continue
                deep = pine_rng.random() < 0.35
                pine_color = _lerp_rgb(
                    PINE_NIGHT, PINE_DAY, 0.55 if deep else 1.0,
                )
                px[cx, cy] = _shade(
                    _lerp_rgb(
                        (12, 22, 18), pine_color, max(0.25, daylight),
                    ),
                    amb,
                )

    # The tent: lime ridge tent, door toward the fire. At night a
    # flashlight breathes inside — someone is up late with a book.
    tent_lit = elev < -2 or (wx.stormy and elev >= 0)
    breath = 0.5 + 0.5 * math.sin(math.tau * phase)
    page = 0.12 * math.sin(math.tau * 3 * phase + 0.7)  # page-turn flicker
    fabric = _shade(_lerp_rgb(TENT_NIGHT, TENT_DAY, daylight), amb)
    for i, ty in enumerate(range(10, 14)):
        half = i
        for tx2 in range(TENT_APEX - half, TENT_APEX + half + 1):
            if not (0 <= tx2 < W):
                continue
            edge = abs(tx2 - TENT_APEX) == half
            tent_color: Color = (
                fabric if edge else _rgb_int(v * 0.82 for v in fabric)
            )
            if tent_lit and i >= 1:
                # interior light through the nylon, strongest mid-panel;
                # even seams transmit a little — nylon glows whole
                lit = (0.55 + 0.40 * breath + page) * (1.0 - i * 0.10)
                if edge:
                    lit *= 0.45
                tent_color = _lerp_rgb(
                    tent_color, TENT_GLOW, max(0.0, min(1.0, lit)),
                )
            px[tx2, ty] = _rgb_int(tent_color)
    door = (TENT_APEX - 2, 13)  # door pixel faces the campfire
    if tent_lit:
        px[door] = tuple(int(v) for v in _lerp_rgb(
            (30, 40, 14), TENT_GLOW, 0.65 + 0.30 * breath))
        _add_glow(px, TENT_APEX, 12, (150, 190, 80), 3.0,
                  0.10 + 0.09 * breath)
    else:
        px[door] = tuple(int(v * 0.5) for v in fabric)

    # Campfire: lit after dusk (and in storm-dark daytime), rained out by
    # real rain or snow. Flames are emissive — never shaded.
    fire_on = elev < 2 and not (wx.rain or wx.snow or wx.stormy)
    log_c = _shade(_lerp_rgb((40, 26, 14), (92, 62, 36), daylight), amb)
    for lx in (FIRE_X - 1, FIRE_X, FIRE_X + 1):
        px[lx, 14] = log_c
    if fire_on:
        flick = math.sin(math.tau * 5 * phase)          # five licks a loop
        flick2 = math.sin(math.tau * 5 * phase + 2.1)
        lean = wind_lean if wx.wind_kmh >= 12 else 0
        px[FIRE_X, 13] = (255, 176, 56)
        px[FIRE_X - 1, 13] = (238, 120, 30) if flick > -0.3 else (150, 70, 20)
        px[FIRE_X + 1, 13] = (238, 120, 30) if flick2 > -0.3 else (150, 70, 20)
        tip_x = FIRE_X + (lean if flick > 0.4 else 0)
        if flick > -0.15:
            px[tip_x, 12] = (255, 140, 36)
        if flick2 > 0.55:
            px[min(W - 1, max(0, tip_x + lean)), 11] = (222, 96, 26)
        _add_glow(px, FIRE_X, 13, (255, 150, 50), 3.4,
                  0.10 + 0.05 * flick)
        # Sparks ride the heat into the dark, each on its own schedule:
        # launch time, climb, wobble, and lifespan all rolled fresh at
        # every rebuild, so no two minutes burn alike. Within a loop
        # every cycle count is an integer — the seam stays invisible.
        spark_rng = random.Random(seed * 11 + 4)
        for k in range(5):
            cycles = spark_rng.choice((1, 1, 2))
            off = spark_rng.random()
            climb = spark_rng.randint(5, 9)
            die = 0.55 + spark_rng.random() * 0.35
            prog = (phase * cycles + off) % 1.0
            if prog >= die:
                continue
            ey = 13 - int(prog * climb)
            ex = (FIRE_X + round(math.sin(math.tau * 2 * phase + k * 2.1))
                  + int(prog * (1 + wx.wind_kmh / 15)) * lean)
            if 0 <= ex < W and 0 <= ey < H:
                f = 1.0 - prog / die
                px[ex, ey] = (int(60 + 195 * f), int(130 * f), int(30 * f))
        # Woodsmoke climbs all the way to the sky, thinning as it goes
        for i in range(4):
            prog = (phase + i / 4) % 1.0
            sy = 11 - int(prog * 8)
            sx = (FIRE_X + int(prog * (2 + wx.wind_kmh / 10)) * (lean or -1)
                  + round(math.sin(math.tau * phase + i * 1.7) * prog))
            if 0 <= sx < W and 0 <= sy < H:
                fade = (1.0 - prog) * 0.38 + 0.06
                px[sx, sy] = tuple(
                    int(o * (1 - fade) + 122 * fade) for o in px[sx, sy])

    # A bunny works the bottom-right patch: grazes the near tuft, turns,
    # hops nose-first to the far tuft, grazes, turns, hops home. The loop
    # ends the way it starts, so the seam is invisible. Sits out the rain.
    if not (wx.rain or wx.snow or wx.stormy):
        base_x = 60 + (seed % 3)  # the patch drifts a little day to day
        tuft_c = _shade(_lerp_rgb((30, 44, 24), (62, 92, 44), daylight), amb)
        for tx2 in (base_x - 1, base_x + 5):
            if 0 <= tx2 < W:
                px[tx2, 13] = tuft_c
        fur = _shade(_lerp_rgb((96, 90, 80), (176, 158, 136), daylight), amb)
        hind = tuple(int(v * 0.78) for v in fur)
        head_c = tuple(min(255, int(v * 1.18)) for v in fur)
        frame = phase * 40
        # (left cell of the 2px body, facing, pose) — the v1 sprite with
        # direction: ear above the leading cell, turns before each hop
        if frame < 13:
            hx, d, pose = base_x, -1, "graze"     # home, at the near tuft
        elif frame < 15:
            hx, d, pose = base_x, 1, "graze"      # turns, eyes the far tuft
        elif frame < 17:
            hx, d, pose = base_x + 2, 1, "hop"    # airborne, ear leading
        elif frame < 29:
            hx, d, pose = base_x + 3, 1, "graze"  # far tuft
        elif frame < 31:
            hx, d, pose = base_x + 3, -1, "graze"  # turns for home
        elif frame < 33:
            hx, d, pose = base_x + 1, -1, "hop"   # hopping home
        else:
            hx, d, pose = base_x, -1, "graze"     # home again == frame 0
        nib = math.sin(math.tau * 3 * phase) > 0.45

        def _put(x, y, c):
            if 0 <= x < W and 0 <= y < H:
                px[x, y] = c
        lead = hx + (1 if d > 0 else 0)  # front cell of the body
        rear = hx + (0 if d > 0 else 1)
        by = 13 if pose == "hop" else 14  # the whole body lifts mid-hop
        _put(rear, by, hind)
        _put(lead, by, fur)
        if not (nib and pose == "graze"):  # ear drops while nibbling
            _put(lead, by - 1, head_c)

        # Fireflies orbit the firelight on summer nights — drawn to the
        # glow, never into it (hard exclusion around the flame columns)
        if elev < -6 and mm in (6, 7, 8) and wx.temp_c > 15:
            fly_rng2 = random.Random(seed * 23 + 9)
            for i in range(3):
                off = fly_rng2.random() * math.tau
                a = math.tau * phase + off  # one lazy orbit per loop
                rx2 = 4.5 + 1.3 * math.sin(math.tau * 2 * phase + i * 2.1)
                fx2 = FIRE_X + round(rx2 * math.cos(a))
                fy2 = 11 - round(1.6 * math.sin(a)) - i % 2
                blink = math.sin(math.tau * 2 * phase + off * 3)
                if blink < 0.1:
                    continue
                if abs(fx2 - FIRE_X) < 3 and fy2 >= 8:
                    fx2 = FIRE_X + (3 if math.cos(a) >= 0 else -3)
                if 0 <= fx2 < W and 6 <= fy2 <= 13:
                    b = 0.45 + 0.55 * blink
                    px[fx2, fy2] = tuple(int(c * b) for c in FIREFLY_COLOR)

    # Settled snow: everything below the sky takes it on the top edge --
    # the floor, and the upper face of every bough that catches it.
    tier = snow_tier(wx.snow_depth_m)
    if tier:
        snow_tops = surface_tops(px, range(W), range(6, 16), sky_before)
        settle_snow(px, snow_tops, tier, amb)

    # Moonlight over the whole scene: pool, pine rims, tent sheen
    if moon_pos is not None:
        moon_x, moon_y = moon_pos
        _apply_moonlight_forest(
            px, moon_x, moon_y, moon_ill, cloud, phase, fire_on,
        )



def _draw_lakefront(px, local, elev: float, daylight: float, seed: int,
                    phase: float, wx: WeatherState, horizon: tuple,
                    sun_pos, moon_pos, moon_ill: float, cloud: float) -> None:
    """The Oak Street bend, water-first: teal lake over ~70% of the frame,
    the shoreline curving out to the elbow and back, the trail a thin lit
    ribbon along the near edge, the Hancock cluster rim-lit by the moon,
    The Drake in pink, Navy Pier's wheel turning far left. Everything moves
    with real weather; every motion wraps integer cycles."""
    night = elev < 0
    hour = local.hour
    amb = _ambient(elev, cloud, wx)

    # Moonlight over the whole lake: a silver sheen scaled by the real lit
    # fraction, shimmering with the same ripples the wind drives
    sheen = 0.0
    if moon_pos is not None:
        sheen = 0.30 * moon_ill * (1.0 - cloud)

    speed_wraps = 1 + min(2, int(wx.wind_kmh // 20))
    amp = (5.0 + min(14.0, wx.wind_kmh * 0.5)) / 100.0
    for y in range(LAKE_TOP, H):
        depth = (y - LAKE_TOP) / (H - 1 - LAKE_TOP)
        base = _lerp_rgb(horizon, LAKE_DEEP, 0.40 + 0.45 * depth)
        base = _lerp_rgb(base, (12, 30, 44), 0.35 * (1.0 - daylight))
        for x in range(BEND_WATER_END[y]):
            ripple = math.sin(x * (0.55 + 0.12 * depth)
                              + math.tau * (speed_wraps * phase) + y * 1.9)
            f = 1.0 + amp * ripple
            c = tuple(max(0, min(255, int(v * f))) for v in base)
            if sheen > 0.03:
                s = sheen * (0.7 + 0.3 * ripple)
                c = tuple(min(255, int(v + g * s))
                          for v, g in zip(c, (120, 130, 150)))
            px[x, y] = c

    # The concentrated glade under the moon or sun, kept inside the water
    glow = moon_pos if moon_pos is not None else sun_pos
    if glow is not None:
        col = GLITTER_MOON if moon_pos is not None else GLITTER_SUN
        strength = (moon_ill if moon_pos is not None else 1.0) * (1.0 - cloud)
        if strength > 0.1:
            g_rng = random.Random(seed * 37 + int(phase * ANIM_FRAMES))
            for y in range(LAKE_TOP, H):
                width = 1.5 + (y - LAKE_TOP) * 0.45
                for dx in range(-4, 5):
                    p = math.exp(-(dx / width) ** 2) * strength
                    x = glow[0] + dx
                    if 0 <= x < BEND_WATER_END[y] and g_rng.random() < 0.75 * p:
                        b = 0.45 + 0.55 * g_rng.random()
                        px[x, y] = tuple(min(255, int(c0 + c * b))
                                         for c0, c in zip(px[x, y], col))

    # Whitecaps once the wind means it
    if wx.wind_kmh >= 25:
        cap_rng = random.Random(seed * 41 + 7)
        for _ in range(min(7, int(wx.wind_kmh // 10))):
            cy0 = cap_rng.randrange(LAKE_TOP, 14)
            cx0 = cap_rng.randrange(0, BEND_WATER_END[cy0])
            x = (cx0 - int(phase * speed_wraps * W)) % BEND_WATER_END[cy0]
            px[x, cy0] = FOAM

    # Rain pricks the water with little bright splashes
    if wx.rain or wx.stormy:
        sp_rng = random.Random(seed * 43 + int(phase * ANIM_FRAMES) * 3)
        for _ in range(10 if wx.stormy else 6):
            y = sp_rng.randrange(LAKE_TOP, H)
            x = sp_rng.randrange(0, BEND_WATER_END[y])
            px[x, y] = tuple(min(255, c + 46) for c in px[x, y])

    # A boat out on the lake: running lights by night, hull by day
    if night and (seed % 3) < 2:
        lane = BEND_WATER_END[8]
        pos = (int(seed * 29) + int(phase * lane)) % lane
        px[pos, 8] = (200, 200, 190)
        if pos > 0:
            px[pos - 1, 8] = (150, 52, 44)  # port light trailing
    elif not night and (seed % 4) < 2:
        hull_x = 4 + (seed * 17) % 30
        for hx in range(hull_x, hull_x + 4):
            px[hx, 8] = (30, 32, 40)
        px[hull_x + 1, 7] = (42, 44, 52)

    # Far shore: treeline strip the trail vanishes behind, rows 7-10
    tree = _shade(_lerp_rgb(TREE_DARK, (88, 122, 66), daylight), amb)
    tree_rng = random.Random(19)
    for y in (7, 8, 9, 10):
        for x in range(BEND_WATER_END[y], W):
            tree_color: Color = (
                tree
                if tree_rng.random() < 0.75
                else _lerp_rgb(tree, (0, 0, 0), 0.3)
            )
            px[x, y] = _rgb_int(tree_color)

    # Near ground: the concrete revetment apron right of the ribbon —
    # pavement and stone blocks, a shade rougher and darker than the
    # lit trail so the curve still reads. Cracks break the slab up.
    apron = _shade(_lerp_rgb((26, 26, 30), (134, 130, 122), daylight), amb)
    crack = _shade(_lerp_rgb((18, 18, 22), (108, 104, 98), daylight), amb)
    apron_rng = random.Random(83)
    for y, (_p0, p1) in BEND_PATH.items():
        for x in range(p1 + 1, W):
            c = crack if apron_rng.random() < 0.18 else apron
            px[x, y] = tuple(int(v) for v in c)

    # The trail itself: a thin lit ribbon tracing the bend
    concrete = _shade(_lerp_rgb(CONCRETE_NIGHT, CONCRETE_DAY, daylight), amb)
    for y, (p0, p1) in BEND_PATH.items():
        for x in range(p0, p1 + 1):
            px[x, y] = tuple(int(v) for v in concrete)

    # Soften every shoreline corner: the first pixel past the water is a
    # half-blend with the water beside it, so steps become a curve
    for y in range(LAKE_TOP + 1, H):
        edge = BEND_WATER_END[y]
        if 0 < edge < W:
            px[edge, y] = tuple(
                (a + b) // 2 for a, b in zip(px[edge - 1, y], px[edge, y]))

    # The tower cluster above the treeline; Hancock crowned, Drake in pink
    front_col = _shade(_lerp_rgb((20, 20, 28), (120, 126, 136), daylight), amb)
    if not night:
        lit_frac = 0.25 if (wx.stormy and elev >= 0) else 0.0
    elif 17 <= hour <= 22:
        lit_frac = 0.5
    elif hour >= 23 or hour < 5:
        lit_frac = 0.18
    else:
        lit_frac = 0.32
    moon_rim = 0.0
    if moon_pos is not None and night:
        moon_rim = 0.55 * moon_ill * (1.0 - cloud)
    for bi, (x0, x1, top, hancock) in enumerate(BEND_TOWERS):
        shade = 16 if bi % 2 else -12
        body = tuple(max(0, min(255, c + int(shade * daylight))) for c in front_col)
        for y in range(top, 7):
            for x in range(x0, x1 + 1):
                px[x, y] = body
        win_rng = random.Random(seed * 17 + bi * 5)
        for wy in range(top + 1, 7, 2):
            for wxx in range(x0 + 1, x1, 2):
                if lit_frac > 0 and win_rng.random() < lit_frac:
                    px[wxx, wy] = (WINDOW_WARM if win_rng.random() < 0.7
                                   else WINDOW_COOL)
        # Moonlight rims the moon-facing (left) edge and the roofline
        if moon_rim > 0.1:
            for y in range(top, 7):
                px[x0, y] = tuple(min(255, int(c + 95 * moon_rim))
                                  for c in px[x0, y])
            for x in range(x0, x1 + 1):
                px[x, top] = tuple(min(255, int(c + 60 * moon_rim))
                                   for c in px[x, top])
        if hancock:
            for mi, mx in enumerate((x0 + 2, x1 - 2)):  # the twin masts
                for my in range(max(0, top - 2), top):
                    px[mx, my] = front_col
                if math.sin(math.tau * 4 * phase + mi * 1.7) > 0.7:
                    px[mx, max(0, top - 2)] = BEACON_RED
            if night:  # crown lights
                for x in range(x0 + 1, x1):
                    px[x, top] = (150, 140, 110)
    # The Drake's pink neon, buzzing very occasionally like real neon
    if night:
        drake_dim = int(phase * ANIM_FRAMES) == (seed * 7) % ANIM_FRAMES
        neon = (140, 45, 85) if drake_dim else (255, 80, 150)
        px[47, 6] = neon
        px[48, 6] = neon
        _add_glow(px, 47, 6, (255, 80, 150), 2.0, 0.10)

    # Trail lamps: the string curving with the ribbon, plus treeline dots
    if night or elev < 4:
        for lx in BEND_LAMPS_FAR:
            px[lx, 7] = LAMP_WARM
        for i, (hx, hy) in enumerate(BEND_LAMPS_NEAR):
            px[hx, hy] = LAMP_WARM
            if i >= 3:  # nearest lamps glow visibly on the path
                _add_glow(px, hx, hy, LAMP_WARM, 2.0, 0.14)
        # lamplight shimmers in the water just off the near edge
        sh_rng = random.Random(seed * 53 + int(phase * ANIM_FRAMES))
        for y in range(11, H):
            edge = BEND_WATER_END[y]
            for x in range(max(0, edge - 3), edge):
                if sh_rng.random() < 0.12:
                    px[x, y] = tuple(
                        min(255, int(c0 + c * 0.35))
                        for c0, c in zip(px[x, y], LAMP_WARM))

    # Railing dots along the seawall edge
    for lx, ly in BEND_LADDER:
        px[lx, ly] = (200, 120, 40)

    # Navy Pier: a few warm lights and the wheel, turning once per loop
    wx0, wy0 = WHEEL_HUB
    if night:
        for pier_x in (wx0 - 3, wx0 - 1, wx0 + 2):  # deck lights under the wheel
            px[pier_x, 7] = (120, 96, 60)
    rim = [(wx0, wy0 - 1), (wx0 + 1, wy0), (wx0, wy0 + 1), (wx0 - 1, wy0)]
    if night:
        lit_i = int(phase * 4) % 4
        for i, (rx, ry) in enumerate(rim):
            px[rx, ry] = (240, 200, 255) if i == lit_i else (130, 110, 140)
        px[wx0, wy0] = (180, 160, 190)
    else:
        for rx, ry in rim + [(wx0, wy0)]:
            px[rx, ry] = (38, 40, 46)

    # Settled snow: the near bank only -- open water must never take it.
    # bank_rows (13-15) is a search-space narrowing, NOT a water-free
    # zone: BEND_WATER_END shows real open water inside all three of
    # those rows (row 13 is water for x in 0..50, bank only from x=51
    # on) -- it only rules out the open lake proper in rows 7-12. The
    # actual defence is water_colors. There is no fixed "water" palette
    # to exclude by -- ripples, sheen, whitecaps, rain splashes, the
    # boat and the lamp shimmer have all repainted it by now -- so
    # water_colors is read back from exactly the pixels BEND_WATER_END
    # says are water, taken this late, after every one of those effects
    # has already run. Each water pixel's own current color is
    # therefore guaranteed to be in the set that excludes it: no code
    # path can mistake it for a bank -- bank_rows narrows where we look,
    # water_colors is what keeps the lake itself bare.
    tier = snow_tier(wx.snow_depth_m)
    if tier:
        bank_rows = range(13, 16)          # shore in front of the water
        water_colors = {px[x, y] for y in bank_rows
                        for x in range(BEND_WATER_END[y])}
        tops = surface_tops(px, range(W), bank_rows, water_colors)
        settle_snow(px, tops, tier, amb)

    # A tree on the bank, drawn dead last so nothing painted above --
    # settled snow included -- lands on top of it. LAKEFRONT_TREE was
    # chosen against BEND_WATER_END itself (see its comment) so it stands
    # on real bank, not merely in a bank row -- the same problem the
    # settled-snow block above solves with water_colors instead of a
    # column range.
    if is_christmas(local):
        tx, ty = LAKEFRONT_TREE
        draw_lit_tree(px, tx, ty, phase, amb)


def render_scene(now: datetime, wx: WeatherState, seed: int,
                 phase: float = 0.0, scene: str = "house",
                 scrubbed: bool = False,
                 lightning: float = 0.0, lane: bool = True) -> Image.Image:
    """Compose one 72x16 frame; `phase` in [0,1) animates a seamless loop."""
    elev = elevation(OBSERVER, now)
    horizon, zenith = _sky_colors(elev)

    cloud = 1.0 if wx.stormy else wx.cloud_frac
    if wx.stormy:
        pull = 0.9 if wx.severe else 0.7
        horizon = _lerp_rgb(horizon, STORM_HORIZON, pull)
        zenith = _lerp_rgb(zenith, STORM_ZENITH, pull)
    elif cloud > 0:
        for which in ("h", "z"):
            c = horizon if which == "h" else zenith
            lum = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
            mixed = _lerp_rgb(c, (lum, lum, lum), 0.65 * cloud)
            if which == "h":
                horizon = mixed
            else:
                zenith = mixed

    dim = 0.6 if (wx.rain or wx.snow) else 1.0
    if wx.snow:
        horizon = _lerp_rgb(horizon, (200, 200, 205), 0.5)
        zenith = _lerp_rgb(zenith, (150, 150, 158), 0.5)

    rng = random.Random(seed)
    img = Image.new("RGB", (W, H))
    px = _rgb_pixels(img)
    for y in range(H):
        t = 1.0 - (y / (H - 1))
        base = _lerp_rgb(zenith, horizon, 1.0 - t * t)
        for x in range(W):
            wave = 1.0 + 0.06 * math.sin((x / W) * math.tau + seed * 0.7 + y * 0.18)
            n = rng.uniform(-3.5, 3.5)
            px[x, y] = tuple(
                max(0, min(255, int(c * wave * dim + n))) for c in base
            )

    # Stars: a static field, like real life — and once in a while a single
    # star flashes for one frame. Three twinkles per 8s loop, total.
    if elev < -8 and cloud < 0.85 and not wx.stormy:
        depth = min(1.0, (-elev - 8) / 6)
        frame_i = int(phase * ANIM_FRAMES)
        glint_rng = random.Random(seed * 31 + 4)
        glints = {
            (glint_rng.randrange(len(STARS)), glint_rng.randrange(ANIM_FRAMES))
            for _ in range(3)
        }
        # An obscuration takes the stars first — they are the dimmest thing
        # in the frame, so they are the first casualty of anything in the air.
        star_clear = 0.25 if wx.obscuration else 1.0
        for si, (sx, sy, mag) in enumerate(STARS):
            b = depth * (1.0 - cloud) * mag * star_clear
            if (si, frame_i) in glints:
                b = min(1.6 * b + 0.5, 1.8)
            if b > 0.1:
                px[sx, sy] = tuple(
                    min(255, int(c0 + s * b))
                    for c0, s in zip(px[sx, sy], STAR_COLOR)
                )

    # Sun by day, moon by night, nothing during handover twilight
    local = now.astimezone(TZ)
    day_frac = (local.hour * 60 + local.minute) / 1440
    moon_pos: tuple[int, int] | None = None
    moon_ill = 0.0
    sun_pos = _sun_screen_pos(now, elev, wx, cloud)
    if sun_pos is not None:
        strength = max(0.2, 1.0 - cloud)
        _draw_sun(px, sun_pos[0], sun_pos[1], strength,
                  math.sin(math.tau * phase))
    elif elev < -4 and cloud < 0.85 and not wx.stormy:
        night_f = (day_frac + 0.5) % 1.0  # 0 at ~noon; ~.33-.66 covers night
        f = min(max((night_f - 0.30) / 0.40, 0.0), 1.0)
        cx = int(20 + f * 24)  # starts right of the clock's corner
        cy = int(6 - 4 * math.sin(math.pi * f)) + 1
        if scene == "skyline":
            cy += 3  # ride low behind the towers: the big city moon
        elif scene == "lakefront":
            cx = int(25 + f * 9)  # over the open water, clear of the clock
        breath = math.sin(math.tau * phase)
        phase_days = (MOON_DAY_OVERRIDE if MOON_DAY_OVERRIDE is not None
                      else _moon_age_days(local.date()))
        eclipse = _eclipse_now(now)
        _draw_moon(px, cx, cy, phase_days, breath, eclipse)
        moon_pos = (cx, cy)
        moon_ill = (1.0 - math.cos(math.tau * phase_days / 29.53)) / 2
        if eclipse is not None:
            # Everything the moon lights — the silver pool, the rim on the
            # roofline, the sheen on the towers — fades with the disc that
            # is still in sunlight. At totality the landscape goes dark,
            # which is the whole reason people go outside to watch.
            moon_ill *= 1.0 - eclipse.obscuration

    # Cloud puffs drift over the stars and under the precipitation
    daylight_now = min(max((elev + 6) / 18, 0.0), 1.0)
    if cloud > 0.1:
        _draw_clouds(px, now, cloud, daylight_now, wx.stormy)

    if lightning > 0:
        # Illuminate the sky *before* any ground, buildings, trees, water, or
        # status ink is composed.  The result is a lit storm backdrop, not the
        # old opaque white rectangle over the entire product.
        strength = min(1.0, max(0.0, lightning)) * 0.72
        lightning_sky = (176, 196, 224)
        for ly in range(H):
            for lx in range(W):
                px[lx, ly] = tuple(
                    min(255, int(base + (lit - base) * strength))
                    for base, lit in zip(px[lx, ly], lightning_sky)
                )

    # Ground: the original moss row, a touch lighter in daylight
    daylight = min(max((elev + 6) / 18, 0.0), 1.0)
    ground = _shade(_lerp_rgb(GROUND_NIGHT, (86, 108, 60), daylight),
                    _ambient(elev, cloud, wx))
    for x in range(W):
        px[x, 15] = tuple(int(c) for c in ground)

    # Wind: wisp streaks riding the actual wind speed
    if wx.wind_kmh >= 8:
        n = min(3, 1 + int(wx.wind_kmh // 15))
        # one drift across per 8s loop in a breeze, up to three in a gale
        wraps = min(3, 1 + int(wx.wind_kmh // 25))
        wisp_rng = random.Random(23)
        span = W + 12
        for _ in range(n):
            x0 = wisp_rng.randrange(0, span)
            wy = wisp_rng.randrange(2, 10)
            head = int(x0 - phase * wraps * span)
            for dx, dy in WISP_SHAPE:
                x = (head + dx) % span - 6
                y = wy + dy
                if 0 <= x < W and 0 <= y < H:
                    px[x, y] = tuple(min(255, c + 26) for c in px[x, y])

    # The murmuration returns at dusk, playing its original eight shapes
    # once per loop, dark or pale per-dot for contrast against the gradient
    if (-8 < elev < 3 and local.hour >= 16 and (seed % 5) < 2
            and not (wx.rain or wx.stormy)):
        shape = FLOCK_FRAMES[int(phase * 8) % 8]
        for fx, fy in shape:
            base = px[fx, fy]
            lum = 0.3 * base[0] + 0.59 * base[1] + 0.11 * base[2]
            dot = (30, 26, 34) if lum > 90 else (172, 172, 186)
            px[fx, fy] = tuple(
                int(o * 0.35 + c * 0.65) for o, c in zip(base, dot))

    amb = _ambient(elev, cloud, wx)
    if scene == "skyline":
        _draw_skyline(px, local, elev, daylight, seed, phase, horizon, wx,
                      amb, storm_day=wx.stormy and elev >= 0)
        # Moonlight on the city: silvered rooflines, no pool on the street
        if moon_pos is not None:
            _apply_moonlight_skyline(px, *moon_pos, moon_ill, cloud, phase)
    elif scene == "lakefront":
        # The lake makes its own moonlight: the glitter path is the pool
        _draw_lakefront(px, local, elev, daylight, seed, phase, wx, horizon,
                        sun_pos, moon_pos, moon_ill, cloud)
    elif scene == "forest":
        _draw_forest(px, local, elev, daylight, seed, phase, wx, amb,
                     moon_pos, moon_ill, cloud)
    elif scene == "grove":
        _draw_grove(px, local, elev, daylight, seed, phase, wx, amb,
                    moon_pos, moon_ill, cloud)
    elif scene == "backroads":
        _draw_backroads(px, local, elev, daylight, seed, phase, wx, amb,
                        moon_ill, cloud, lane=lane)
    else:
        # The house, exactly as drawn in August 2026; lamplight breathes in
        # the window itself and in a glow centered on it, with some flicker.
        # By day the night palette lifts to sunlit walls and sky-glass.
        window_lit = elev < 2 or wx.stormy  # lights on when it's dark out
        # one full breath per 8s loop, with a whisper of flicker over it
        pulse_norm = (0.5 + 0.5 * math.sin(math.tau * phase)
                      + 0.12 * math.sin(math.tau * phase * 5 + 1.7))
        house_day = {
            (26, 24, 32): (118, 104, 96),   # walls in sun
            (38, 30, 40): (140, 116, 104),  # roof
            (18, 16, 22): (92, 82, 78),     # trim/shadow
        }
        for hx, hy, color in HOUSE_SPRITE:
            if color == WINDOW_COLOR:
                if window_lit:
                    color = _rgb_int(
                        c * (0.72 + 0.28 * pulse_norm)
                        for c in WINDOW_COLOR
                    )
                else:
                    color = _rgb_int(_lerp_rgb(
                        (30, 28, 36), (150, 180, 205), daylight,
                    ))
            elif color in house_day:
                color = _shade(_lerp_rgb(color, house_day[color], daylight),
                               amb)
            px[hx, hy] = color
        if window_lit:
            _add_glow(px, *WINDOW_CENTER, WINDOW_COLOR, 3.2,
                      0.08 + 0.10 * pulse_norm)

        # Christmas lights along the roofline, roof drawn but not yet
        # occluded by anything foreground (trees, snow, moonlight below).
        if is_christmas(local):
            eaves = [(x, HOUSE_TOP[x]) for x in sorted(HOUSE_TOP)]
            string_lights(px, eaves, phase, amb)

        # Two trees in the yard, seasonal: green, autumn orange, winter bare
        mm = local.month
        rustle = wx.wind_kmh >= 8
        # Downwind lean: screen-right is east. N/S winds rustle in place.
        wind_lean = 0
        if wx.wind_dir is not None and wx.wind_kmh >= 5:
            comp = math.sin(math.radians(wx.wind_dir + 180))
            wind_lean = 1 if comp > 0.35 else (-1 if comp < -0.35 else 0)
        tree_rng = random.Random(37)
        trunk_c = _shade(_lerp_rgb(TRUNK_NIGHT, TRUNK_DAY, daylight), amb)
        for tx, size in TREES:
            top = 13 - size * 2  # big tree canopy starts at y9, small at y11
            for ty in range(top + size + 2, 15):  # trunk below the canopy
                px[tx, ty] = trunk_c
            if is_winter(local):  # winter: bare limbs
                for lx, ly in ((tx - 1, top + 1), (tx + 1, top + 1),
                               (tx, top), (tx - size, top + 2),
                               (tx + size, top + 2)):
                    if 0 <= lx < W:
                        px[lx, ly] = trunk_c
                continue
            for ci, cy2 in enumerate(range(top, top + size + 2)):
                half = (1, size + 1, size + 1, size)[min(ci, 3)]
                sway = 0
                if rustle and ci == 0:  # the crown gusts downwind, relaxes back
                    gust = max(0.0, math.sin(math.tau * 2 * phase + tx))
                    lean = wind_lean if wind_lean else 1
                    sway = lean if gust > 0.25 else 0
                for cx2 in range(tx - half + sway, tx + half + 1 + sway):
                    if not (0 <= cx2 < W):
                        continue
                    if mm in (9, 10, 11):
                        base = CANOPY_FALL[tree_rng.randrange(3)]
                    else:
                        base = (CANOPY_NIGHT if tree_rng.random() < 0.35
                                else _lerp_rgb(CANOPY_NIGHT, CANOPY_DAY, 1.0))
                    px[cx2, cy2] = _shade(
                        _lerp_rgb((22, 34, 20), base, max(0.25, daylight)), amb)

        # Grass with real height variance; a wind wave rolls through it
        wave_on = wx.wind_kmh >= 5
        g1 = _shade(_lerp_rgb(GRASS_COLOR, (70, 100, 48), daylight), amb)
        g2 = _shade(_lerp_rgb(GRASS_COLOR_2, (58, 86, 40), daylight), amb)
        tier = snow_tier(wx.snow_depth_m)
        if tier >= 3:
            # Buried: the tufts are gone, and that absence IS the depth cue.
            settle_snow(px, {x: 14 for x in range(W)}, tier, amb)
        else:
            # settle_snow's contract: tops[x] is the topmost pixel actually
            # on the ground in that column, and snow lands ON it -- the same
            # question surface_tops answers for every other scene. A wind
            # wave can shift a tall blade's row-13 pixel sideways, so the
            # real top per column can only be read AFTER the fringe is
            # drawn, from what actually landed -- not guessed from height
            # alone (that guess used to land one row above the tuft, and two
            # rows above it once the wave moved the blade out from under the
            # guess entirely).
            fringe_x = range(2, 47)  # GRASS_FRINGE's own span, clear of the house
            sky_before = {px[x, y] for x in fringe_x for y in (13, 14)}
            for gx, gh in GRASS_FRINGE:
                if gh == 0:
                    continue
                color = g1 if gx % 2 else g2
                px[gx, 14] = color
                if gh == 2:
                    xoff = 0
                    if wave_on and math.sin(math.tau * 2 * phase + gx * 0.55) > 0.2:
                        xoff = wind_lean if wind_lean else 1
                    if 0 <= gx + xoff < W:
                        px[gx + xoff, 13] = color
            if tier > 0:
                # Row 15 is in range on purpose: 16 of the 45 fringe columns
                # have gh == 0, so rows 13-14 are still bare sky there and
                # surface_tops would skip them entirely -- losing ~40% of the
                # ground snow on exactly the columns with no tuft to catch it.
                # Bare lawn takes snow too. sky_before deliberately stays at
                # rows 13-14: it is snapshotted before the grass is drawn, so
                # adding row 15 would file the lawn itself as sky and exclude
                # the very pixels this is here to include.
                tops = surface_tops(px, fringe_x, (13, 14, 15), sky_before)
                settle_snow(px, tops, tier, amb)

        # Moonlight falls on the scene, sliding along with the moon itself
        if moon_pos is not None:
            _apply_moonlight(px, *moon_pos, moon_ill, cloud, phase)

    # Precipitation falls in front of every scene's foreground (it used to
    # draw before the scene branch and vanished behind water, walls, towers)
    if is_raining(wx):
        draw_rain(px, wx, seed, phase)
    if wx.snow:
        draw_snow(px, seed, phase)

    month = local.month

    # Fireflies: warm summer nights over the grass, each blinking on its own
    if (scene in ("house", "forest", "grove") and month in (6, 7, 8)
            and elev < -6
            and wx.temp_c > 15
            and not (wx.rain or wx.stormy)):
        fly_rng = random.Random(seed * 7 + 2)
        for i in range(3):
            if scene == "forest":  # they gather over the clearing
                fx = fly_rng.randrange(35, 49)
                fy = fly_rng.randrange(9, 13)
            else:
                fx = fly_rng.randrange(3, 46)
                fy = fly_rng.randrange(9, 14)
            blink = math.sin(math.tau * phase + i * 2.4)  # one cycle per loop
            if blink > 0.35:
                bright = (blink - 0.35) / 0.65
                drift = int(2 * math.sin(math.tau * 2 * phase + i))
                x = min(W - 1, max(0, fx + drift))
                px[x, fy] = tuple(
                    int(c * (0.4 + 0.6 * bright)) for c in FIREFLY_COLOR)

    # Chimney smoke on cold days: a small chimney appears, puffs drift upwind
    if scene == "house" and wx.temp_c < 5:
        ch_x, ch_y = CHIMNEY
        px[ch_x, ch_y] = (26, 24, 32)
        px[ch_x, ch_y + 1] = (26, 24, 32)
        for i in range(4):
            prog = (phase + i / 4) % 1.0  # puffs spaced along the plume
            sy = ch_y - 1 - int(prog * 5)
            sx = ch_x - int(prog * (2 + wx.wind_kmh / 12))
            if 0 <= sx < W and 0 <= sy < H:
                fade = (1.0 - prog) * 0.55
                px[sx, sy] = tuple(
                    int(o * (1 - fade) + 126 * fade) for o in px[sx, sy])

    # A morning bird, some minutes, crossing once per loop with a lazy flap
    if (-4 < elev < 10 and local.hour < 12 and (seed % 7) < 2
            and not (wx.rain or wx.stormy)):
        prog = phase
        bx = int(prog * (W + 10)) - 5
        by = 4 + int(1.5 * math.sin(math.tau * 2 * prog))
        flap = int(phase * ANIM_FRAMES) % 4 < 2
        if 1 <= bx < W - 1 and 0 < by < H - 1:
            px[bx, by] = BIRD_COLOR
            wing_y = by - 1 if flap else by
            px[bx - 1, wing_y] = BIRD_COLOR
            px[bx + 1, wing_y] = BIRD_COLOR

    # Autumn leaves tumbling on the wind
    if month in (9, 10, 11) and wx.wind_kmh >= 5 and not wx.snow:
        leaf_rng = random.Random(seed * 5 + 1)
        span_x = W + 20
        span_y = H - 3
        for i in range(3):
            x0 = leaf_rng.randrange(0, span_x)
            y0 = leaf_rng.randrange(0, span_y)
            prog_x = (x0 - int(phase * span_x)) % span_x - 10
            prog_y = (y0 + int(phase * 2 * span_y)) % span_y
            sway = round(math.sin(math.tau * 3 * phase + i * 2.1))
            lx, ly = prog_x + sway, prog_y
            if 0 <= lx < W and 0 <= ly < H:
                px[lx, ly] = LEAF_COLORS[i % len(LEAF_COLORS)]

    # Ground fog on humid mornings or in genuinely low visibility, two
    # counter-drifting sine layers hugging the grass and the house's feet
    # The air column, before the ground-hugging fog layer and before the
    # status ink — the clock is a readout, not part of the weather, and
    # must stay legible through a smoke day.
    if wx.obscuration:
        _apply_obscuration(px, wx.obscuration, daylight_now)

    fog_d = 0.0
    if wx.visibility_m < 5000:
        fog_d = min(0.65, 0.3 + (5000 - wx.visibility_m) / 5000 * 0.35)
    elif wx.humidity >= 88 and elev < 12:
        fog_d = 0.25 + 0.30 * min(1.0, (wx.humidity - 88) / 10)
    if wx.fog:
        # A source saying "fog" outright is stronger evidence than either
        # number above, both of which are inferences. Patchy fog that leaves
        # the official visibility reading healthy is still fog to anyone
        # standing in it, so it gets a floor rather than a threshold; low
        # visibility still deepens it past this.
        fog_d = max(fog_d, 0.40)
    if fog_d > 0:
        fog_col = _lerp_rgb((52, 56, 66), (198, 203, 209), daylight)
        row_weight = {10: 0.35, 11: 0.6, 12: 0.85, 13: 1.0, 14: 1.0, 15: 0.5}
        for x in range(W):
            wave = (0.5 + 0.3 * math.sin(x / 9 - math.tau * phase)
                    + 0.2 * math.sin(x / 17 + math.tau * phase + 1.3))
            for y, wgt in row_weight.items():
                a = fog_d * wave * wgt
                px[x, y] = tuple(
                    int(o * (1 - a) + c * a) for o, c in zip(px[x, y], fog_col))

    _bake_status(px, now, wx, phase, scene, scrubbed)
    return img


def clock_str(now: datetime) -> str:
    local = now.astimezone(TZ)
    return f"{local.hour % 12 or 12}:{local.minute:02d}"


# Status-clock inks: ONE saturated hue at a time, every hour, every scene.
# Contrast is hue, not brightness — the panel resolves a >=30%
# single-channel separation as readily as a luminance delta (the skill's
# escape hatch; red proved it on hardware, 2026-08-12: "reads very well").
# Red then failed on DESIGN — it is this product's alarm colour — so the
# ink is a closed operator choice: every entry below is pre-proven against
# the corner's measured backgrounds (blue sky, overcast white, the sun's
# cream, night, pine dark, the steel rail) by the contract tests, which
# sweep them all. Barkeep's enum validation refuses anything else, so no
# unreadable clock is even configurable.
STATUS_INKS = {
    # Dominant channels at FULL scale, deliberately: brightness reads as
    # apparent size on this panel, so a 90% ink draws visibly thinner
    # digits than a 100% one for free. Teal was tried and panel-vetoed
    # ("doesn't work well", 2026-08-12); orange is the operator's pick and
    # matches the bar's own industrial design — white body, orange
    # accents. Its G sits at 130 so it stays >=30% from the alarm red in
    # G, and its B of zero is the separator against every sky.
    "orange": (255, 130, 0),  # the hardware's accent colour, on the panel
    "pink": (255, 64, 200),   # The Drake's neon family, dusk-flavoured
    "red": (255, 40, 28),     # reads hardest of all; also the alarm colour
}
CLOCK_INK = STATUS_INKS["orange"]     # overwritten by apply_runtime_config
# Lilac: the Time Machine tell, and nothing else. It was amber for a long
# time, but amber is the orange clock's next-door neighbour — no orange can
# clear both amber and alarm red by 30%. Lilac is unclaimed by any scene,
# time-travel-flavoured, and genuinely clears every corner background —
# R >= +81 against the brightest horizon-glow blue the viz check found
# (two darker violets failed there with every channel under the floor),
# G-separation against overcast and against pink, B against the warm inks
# — which amber never managed. The tell is swept through the same
# extremes as the clock inks.
STATUS_INK_SCRUBBED = (220, 148, 255)



def _bake_status(px, now: datetime, wx: WeatherState, phase: float,
                 scene: str = "house", scrubbed: bool = False) -> None:
    """Top-left status in our 3x5 digits: the time, and for the last stretch
    of each loop, the real temperature in Fahrenheit."""
    if phase >= 0.7:
        if UNITS == "c":
            text = f"{round(wx.temp_c)}°"
        else:
            text = f"{round(wx.temp_c * 9 / 5 + 32)}°"
    else:
        text = clock_str(now)
    # Ink history, condensed because each chapter was paid for: amber-by-day
    # sat 6-39 under the panel's ~76 luminance floor and shipped for months;
    # a brightness lerp fixed clear skies but died on white clouds; a black
    # halo, then a full card, then a translucent shadow each bought
    # guaranteed contrast with scene pixels until the operator ruled any
    # black around the text too expensive; a black/white flip machine then
    # carried four special cases (weather estimate, sun-in-corner, a forest
    # exception, a bough grown to serve it). Now: one saturated hue from
    # the operator-chosen closed set — see STATUS_INKS. Amber remains the
    # Time Machine tell, channel-distinguishable from every choice.
    color = STATUS_INK_SCRUBBED if scrubbed else CLOCK_INK
    cells: set[tuple[int, int]] = set()
    # Center the text in the fixed card: a short string at cx=2 left five
    # black columns on one side and two on the other, which read as
    # asymmetric padding rather than a card.
    text_w = sum(len(DIGITS_3X5[ch][0]) + 1 for ch in text) - 1
    cx = max(1, (STATUS_CARD_W - text_w) // 2)
    for ch in text:
        glyph = DIGITS_3X5[ch]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1" and 0 <= cx + gx < W:
                    cells.add((cx + gx, 1 + gy))
        cx += len(glyph[0]) + 1

    # No halo, no shadow: the strokes land directly on the scene. The
    # corner is a declared quiet zone (STATUS_CARD_W) so point noise the
    # same hue as the ink — a star beside a white digit — cannot weld
    # onto a letterform.
    for x, y in cells:
        px[x, y] = color


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


# --- data feeds -----------------------------------------------------------


def _finite_number(value, low: float, high: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _source_datetime(
    value,
    *,
    now: datetime | None = None,
    allow_naive_utc: bool = False,
    max_age_s: float | None = None,
) -> datetime | None:
    """Parse and bound a timestamp from a remote feed.

    `max_age_s` defaults to WEATHER_LEASE_S, which is what the live path
    wants: an observation older than the lease is not current weather. The
    history path passes its own window instead -- a record from twenty hours
    ago is exactly what it is asking for, not a stale reading.
    """
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(
            value[:-1] + "+00:00" if value.endswith(("Z", "z")) else value
        )
    except ValueError:
        return None
    if parsed.tzinfo is None:
        if not allow_naive_utc:
            return None
        parsed = parsed.replace(tzinfo=timezone.utc)
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    parsed = parsed.astimezone(timezone.utc)
    age = (current - parsed).total_seconds()
    limit = WEATHER_LEASE_S if max_age_s is None else max_age_s
    if age < -SOURCE_FUTURE_SKEW_S or age > limit:
        return None
    return parsed


def _source_monotonic_at(
    source_at: datetime,
    *,
    wall_now: datetime | None = None,
    monotonic_now: float | None = None,
) -> float:
    """Map one validated wall-clock source instant onto the monotonic axis."""
    current = (wall_now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    source_utc = source_at.astimezone(timezone.utc)
    age = max(0.0, (current - source_utc).total_seconds())
    if monotonic_now is None:
        monotonic_now = asyncio.get_running_loop().time()
    return monotonic_now - age


def _mark_weather_fresh(state: SkyState, source_at: datetime) -> None:
    """Set the live-weather lease from the committed snapshot's real age.

    Receipt time is not observation time.  Giving a nearly two-hour-old
    observation a fresh two-hour lease would keep it on screen for almost four
    hours.  Map the wall-clock source age onto the monotonic clock used by the
    runtime gate.  This assignment is deliberate: if a newly committed fused
    state has an older limiting source than its predecessor, its lease must
    shorten with it.
    """
    state.weather_updated_at = _source_monotonic_at(source_at)
    state.weather_ready.set()


def weather_is_fresh(state: SkyState) -> bool:
    if (not state.weather_ready.is_set()
            or state.weather_updated_at is None):
        return False
    return (
        asyncio.get_running_loop().time() - state.weather_updated_at
        <= WEATHER_LEASE_S
    )


RADAR_INTERVAL_S = 300  # RainViewer refreshes its mosaic every ~5 minutes
# zoom comes from busybar_dev.radar.RADAR_MAX_ZOOM (7, ~900 m/px at 42°N —
# the free tile cache rejects anything deeper; verified live 2026-08-05)


def apply_rain(state: SkyState) -> None:
    """Re-resolve the rain flag from the freshest honest source. Called by
    every feed that lands new evidence (same pattern as the temp nowcast)."""
    now = asyncio.get_event_loop().time()
    radar_age = now - state.radar_at if state.radar_at != 0.0 else 1e9
    om_age = now - state.om_at if state.om_at is not None else 1e9
    station_age = (
        now - state.station_at if state.station_at is not None else 1e9
    )
    last_age = now - state.rain_at if state.rain_at is not None else 1e9
    snow_fresh = (
        state.weather.snow
        and state.snow_at is not None
        and now - state.snow_at <= WEATHER_LEASE_S
    )
    rain, tier, src = resolve_rain(
        state.radar_dbz, radar_age,
        state.om_rain,
        om_age,
        state.station_rain,
        station_age,
        state.weather.rain, state.weather.rain_tier,
        state.rain_known, last_age, snow_fresh,
    )
    source_at = {
        "radar": state.radar_at,
        "nowcast": state.om_at,
        "model-aged": state.om_at,
        "station": state.station_at,
        "snow": state.snow_at,
    }.get(src)
    if source_at is not None:
        state.rain_known = True
        state.rain_at = source_at
    state.weather.rain = rain
    state.weather.rain_tier = tier
    if src != state.rain_src:
        logger.info("rain source -> %s (rain=%s tier=%d dbz=%s)",
                    src, rain, tier, state.radar_dbz)
        state.rain_src = src


def _expire_stale_phenomena(state: SkyState) -> None:
    """Suppress weather effects whose own evidence lease has expired.

    Cloud, wind, and temperature can be refreshed by a station row whose
    optional phenomena fields are missing.  Their successful refresh must not
    make an older snowstorm, thunder report, or modeled ground-snow depth live
    forever.
    """
    now = asyncio.get_running_loop().time()
    for field_name, timestamp_name in (
        ("snow", "snow_at"),
        ("thunder", "thunder_at"),
    ):
        observed_at = getattr(state, timestamp_name)
        if (observed_at is None
                or now - observed_at > WEATHER_LEASE_S):
            setattr(state.weather, field_name, False)
    if (state.snow_depth_at is not None
            and now - state.snow_depth_at > WEATHER_LEASE_S):
        state.weather.snow_depth_m = 0.0


async def poll_radar(state: SkyState) -> None:
    """The watch-on-your-wrist treatment for rain: sample the live radar
    mosaic at OUR coordinates instead of trusting an airport 10 miles off.
    RainViewer serves a global composite, keyless; fails soft down the
    resolve chain (Open-Meteo nowcast, then a fresh NWS station or last-good)."""
    if not web_mercator_contains(LAT):
        # RainViewer's slippy-map products cannot represent the polar caps.
        # Never clamp a pole onto the edge tile and pretend that edge radar is
        # local evidence; keep the global nowcast/station chain authoritative.
        state.radar_dbz = None
        state.radar_at = 0.0
        state.radar_covered = False
        apply_rain(state)
        logger.info(
            "radar coverage unavailable outside Web Mercator; using fallback"
        )
        # Park forever rather than waking every RADAR_INTERVAL_S to do nothing.
        # Coverage is a property of the configured coordinates, so it cannot
        # become available without a restart; an Event that is never set is
        # both cancellable and free.
        await asyncio.Event().wait()

    tx, ty, px, py = tile_pixel(
        LAT, LON, RADAR_MAX_ZOOM, RAINVIEWER_TILE_SIZE)
    async with httpx.AsyncClient(headers=NEUTRAL_UA, timeout=20) as client:
        while True:
            try:
                r = await client.get(
                    "https://api.rainviewer.com/public/weather-maps.json")
                r.raise_for_status()
                idx = r.json()
                frame = idx["radar"]["past"][-1]
                frame_time = frame.get("time")
                # Validate before spending two more requests on a cached frame.
                # It is checked again at commit because those requests consume
                # part of the frame's real freshness window.
                rainviewer_frame_age(frame_time, now_unix=time.time())
                host = idx["host"].rstrip("/")
                r = await client.get(
                    f"{host}/v2/coverage/0/{RAINVIEWER_TILE_SIZE}"
                    f"/{RADAR_MAX_ZOOM}/{tx}/{ty}/0/0_0.png")
                r.raise_for_status()
                covered = decode_coverage_mask(
                    r.content, px, py, tile_size=RAINVIEWER_TILE_SIZE)
                previous_coverage = state.radar_covered
                state.radar_covered = covered
                if not covered:
                    # A transparent radar tile is ambiguous by itself: it can
                    # mean "covered and clear" or "there is no radar here".
                    # The official mask resolves that ambiguity. Invalidate the
                    # old radar timestamp so resolve_rain falls through now,
                    # rather than declaring a model-reported storm dry for 15m.
                    state.radar_dbz = None
                    state.radar_at = 0.0
                    apply_rain(state)
                    if previous_coverage is not False:
                        logger.info(
                            "radar coverage unavailable; using global fallback")
                    await asyncio.sleep(RADAR_INTERVAL_S)
                    continue
                if previous_coverage is False:
                    logger.info("radar coverage available again")
                r = await client.get(
                    f"{host}{frame['path']}/{RAINVIEWER_TILE_SIZE}"
                    f"/{RADAR_MAX_ZOOM}"
                    f"/{tx}/{ty}/0/0_0.png")
                r.raise_for_status()
                img = decode_radar_tile(
                    r.content, tile_size=RAINVIEWER_TILE_SIZE)
                dbz = sample_dbz(img, px, py)
                source_age = rainviewer_frame_age(
                    frame_time, now_unix=time.time())
                # Keep source time on the monotonic axis used by resolve_rain.
                # Receipt time would make an old cached mosaic look brand new.
                monotonic_now = asyncio.get_running_loop().time()
                state.radar_dbz = dbz
                state.radar_at = monotonic_now - source_age
                apply_rain(state)
            except Exception as exc:  # noqa: BLE001 - feed is best-effort
                logger.warning("radar poll failed: %s", describe_exception(exc))
                # Preserve the last sample and its original source-mapped time,
                # but re-resolve now so an aged-out sample immediately yields to
                # the current Open-Meteo/station chain.
                apply_rain(state)
            await asyncio.sleep(RADAR_INTERVAL_S)


def _parse_obs(props: object) -> WeatherUpdates:
    if not isinstance(props, dict):
        return {}
    description = props.get("textDescription")
    if isinstance(description, str):
        description_known = bool(description.strip())
        text = description.lower() if description_known else ""
    else:
        description_known = False
        text = ""
    present_value = props.get("presentWeather")
    present_known = isinstance(present_value, list)
    try:
        present_raw = json.dumps(present_value).lower() if present_known else ""
    except (TypeError, ValueError):
        present_known = False
        present_raw = ""
    # The structured `weather` values are the authority; textDescription is
    # prose and only fills in when the envelope is absent. Substring-matching
    # the raw JSON was how `snow_showers` set rain (it contains "shower") and
    # how `ice_pellets` set nothing (the search string had a space in it).
    reported = {
        entry.get("weather") for entry in (present_value or [])
        if isinstance(entry, dict)
    } if present_known else set()

    out: WeatherUpdates = {}
    if description_known or present_known:
        # A valid empty presentWeather list is affirmative dry evidence. If
        # both phenomenon fields are missing/malformed, omit these keys instead
        # of manufacturing a station report of clear weather.
        text_snow = any(w in text for w in ("snow", "sleet", "ice pellets"))
        # "shower" only implies rain when it is not a snow shower — the whole
        # reason the old substring test double-counted.
        text_rain = ("rain" in text or "drizzle" in text
                     or ("shower" in text and not text_snow))
        out.update({
            "rain": bool(reported & OBS_RAIN_WORDS) or text_rain,
            "snow": bool(reported & OBS_SNOW_WORDS) or text_snow,
            # api.weather.gov publishes a 36-value `weather` enum and that
            # is the contract; the METAR check behind it is a deliberate
            # belt-and-braces for thunder, which is the one phenomenon whose
            # absence changes the whole sky. Every populated observation
            # reachable during this audit was empty, so recall here was not
            # something the feed could be made to demonstrate.
            "thunder": (bool(reported & OBS_THUNDER_WORDS)
                        or "thunder" in text or '"ts' in present_raw),
            "fog": bool(reported & OBS_FOG_WORDS) or "fog" in text,
            "obscuration": _obscuration_kind(reported, text),
        })
    layers = props.get("cloudLayers")
    if isinstance(layers, list):
        amounts: list[float] = []
        for layer in layers:
            if not isinstance(layer, dict):
                continue
            amount = layer.get("amount")
            if isinstance(amount, str) and amount in CLOUD_AMOUNT:
                amounts.append(CLOUD_AMOUNT[amount])
        if amounts:
            out["cloud_frac"] = max(amounts)
        elif any(word in text for word in ("clear", "fair", "sunny")):
            out["cloud_frac"] = 0.0
    fields = (
        ("windSpeed", "wind_kmh", 0.0, 500.0),
        ("windDirection", "wind_dir", 0.0, 360.0),
        ("temperature", "temp_c", -100.0, 70.0),
        ("relativeHumidity", "humidity", 0.0, 100.0),
        ("visibility", "visibility_m", 0.0, 200_000.0),
    )
    for source, target, low, high in fields:
        field = props.get(source)
        raw = field.get("value") if isinstance(field, dict) else None
        value = _finite_number(raw, low, high)
        if value is None:
            continue
        if target == "wind_kmh":
            out["wind_kmh"] = value
        elif target == "wind_dir":
            out["wind_dir"] = value
        elif target == "temp_c":
            out["temp_c"] = value
        elif target == "humidity":
            out["humidity"] = value
        else:
            out["visibility_m"] = value
    return out


def _obscuration_kind(reported: set, text: str) -> str:
    """Which obscuration is in the air, most-obscuring first.

    Open-Meteo cannot answer this: its `weather_code` is the WMO subset
    that omits 04-09 (smoke, haze, dust, sand), confirmed against the live
    endpoint on 2026-08-30. So this is NWS-only, and outside NWS point
    coverage the scene simply never shows an obscuration rather than
    inferring one from a visibility number that fog would also explain.
    """
    for kind in OBS_OBSCURATION_ORDER:
        if reported & OBS_OBSCURATION_WORDS[kind]:
            return kind
    # Prose fallback, for a station sending textDescription without the
    # structured envelope. Deliberately narrow: "dust" and "sand" are too
    # common as ordinary English to match on safely.
    for kind, word in (("ash", "volcanic ash"), ("smoke", "smoke"),
                       ("haze", "haze")):
        if word in text:
            return kind
    return ""


def _wmo_phenomena(code) -> tuple[bool, bool, bool, bool]:
    """Rain, snow, thunder and fog from one Open-Meteo WMO weather code.

    45 and 48 are fog and depositing rime fog. They were absent here, so the
    only way fog could reach the scene was by inference from the visibility
    or humidity numbers — and a model reporting fog outright while visibility
    stayed above the threshold drew a clear morning.
    """
    parsed = _finite_number(code, 0, 99)
    if parsed is None:
        return False, False, False, False
    code_i = int(parsed)
    rain = 51 <= code_i <= 67 or 80 <= code_i <= 82
    snow = 71 <= code_i <= 77 or code_i in (85, 86)
    thunder = code_i in (95, 96, 99)
    fog = code_i in (45, 48)
    return rain, snow, thunder, fog


def _alert_signature(alert: Alert | None) -> tuple | None:
    if alert is None:
        return None
    return (
        alert.identifier,
        alert.references,
        alert.event,
        alert.headline,
        alert.severity,
        alert.urgency,
        alert.certainty,
        alert.effective,
        alert.expires,
        alert.ends,
    )


def _claim_audio_stop(state: SkyState, *, force: bool = False) -> int:
    """Invalidate every older PLAY before yielding to the device.

    This synchronous generation bump is the important half of STOP.  A siren
    task blocked in a display POST can resume later, but it can no longer pass
    the generation check immediately before AUDIO PLAY.
    """
    owned = state.audio_owner is not None or state.audio_stop_pending
    state.audio_generation += 1
    # audio_stop is global on the device.  Invalidating a not-yet-started PLAY
    # must not silence audio owned by BUSY or another app.
    state.audio_stop_pending = force or owned
    return state.audio_generation


def _apply_alert_selection(
    state: SkyState,
    visual: Alert | None,
    siren: Alert | None,
    alerts: tuple[Alert, ...] = (),
) -> bool:
    """Atomically install one authoritative CAP selection.

    Returns whether presentation/audio work is needed.  Acknowledgement follows
    CAP identifier/reference lineage only for routine updates; a new episode or
    a material escalation always re-arms.
    """
    previous_visual = state.visual_alert
    previous_siren = state.siren_alert
    old_visual_sig = _alert_signature(previous_visual)
    old_siren_sig = _alert_signature(previous_siren)
    new_visual_sig = _alert_signature(visual)
    new_siren_sig = _alert_signature(siren)
    changed = (old_visual_sig, old_siren_sig) != (new_visual_sig, new_siren_sig)

    was_acked = state.alert_acked
    if visual is None:
        acknowledged = False
    elif previous_visual is not None and old_visual_sig == new_visual_sig:
        acknowledged = was_acked
    else:
        acknowledged = (
            was_acked
            and preserve_acknowledgement(previous_visual, visual)
        )

    state.active_alerts = alerts
    state.visual_alert = visual
    state.siren_alert = siren
    state.alert_acked = acknowledged
    state.weather = replace(
        state.weather,
        severe=visual is not None,
        severe_event=visual.event if visual is not None else "",
    )

    if changed:
        state.alert_generation += 1
        state.alert_drawn_generation = -1
        if visual is None and previous_visual is not None:
            state.alert_dismiss_pending = True
        elif visual is not None:
            state.alert_dismiss_pending = False

        rearmed = visual is not None and not acknowledged and (
            previous_visual is None
            or not preserve_acknowledgement(previous_visual, visual)
        )
        if previous_siren != siren or rearmed or visual is None:
            _claim_audio_stop(state)
        _signal_alert_change(state)

        if visual is None:
            logger.info("weather alert all-clear")
        else:
            logger.warning(
                "weather alert active: %s (CAP severity %s; siren %s)",
                visual.event,
                visual.severity,
                "armed" if siren is not None and not acknowledged else "off",
            )
    return changed


def _locally_active_alerts(
    alerts: tuple[Alert, ...], now: datetime,
) -> tuple[Alert, ...]:
    """Expire last-good CAP state on its own source deadlines during outage."""
    return tuple(
        alert for alert in alerts
        if alert.expires > now and (alert.ends is None or alert.ends > now)
    )


def _signal_alert_change(state: SkyState) -> None:
    """Publish a level wake plus a generation that cannot be cleared away."""
    state.alert_wake_generation += 1
    state.alert_changed.set()


def _stand_down_nws_alerts(state: SkyState) -> None:
    """Clear every NWS alert claim at the confirmed `/points` boundary."""
    if (state.active_alerts or state.visual_alert is not None
            or state.siren_alert is not None):
        _apply_alert_selection(state, None, None, ())
    state.alert_known = True


async def poll_alerts(
    state: SkyState, *, wait_for_point_check: bool = False,
) -> None:
    """Poll CAP independently of the five-minute observation pipeline.

    A valid empty response is an authoritative all-clear.  A malformed or
    failed response preserves last-good alerts only until their own CAP expiry
    times, never forever.
    """
    if wait_for_point_check:
        # The managed runtime starts point discovery and CAP concurrently.
        # Wait for that first bounded attempt so an outside-coverage install
        # never briefly arms an alert before /points establishes the boundary.
        await state.nws_point_checked.wait()
    async with httpx.AsyncClient(headers=NWS_UA, timeout=20) as client:
        while True:
            now = datetime.now(timezone.utc)
            # CAP's own deadline wins even if the next HTTP request stalls for
            # the full transport timeout.
            active = _locally_active_alerts(state.active_alerts, now)
            if active != state.active_alerts:
                _apply_alert_selection(
                    state,
                    select_visual_alert(active),
                    select_siren_alert(active),
                    active,
                )
            if state.nws_point_covered is False:
                # `/points` is the product's locality boundary for every NWS
                # enhancement. Do not query or present point-filtered CAP when
                # the configured coordinate is confirmed outside it.
                _stand_down_nws_alerts(state)
                await asyncio.sleep(ALERTS_INTERVAL_S)
                continue
            try:
                request = client.get(
                    "https://api.weather.gov/alerts/active",
                    params={"point": f"{LAT:.4f},{LON:.4f}"},
                )
                if state.active_alerts:
                    nearest = min(
                        min(alert.expires, alert.ends)
                        if alert.ends is not None else alert.expires
                        for alert in state.active_alerts
                    )
                    deadline_s = max(0.01, (nearest - now).total_seconds())
                    response = await asyncio.wait_for(request, deadline_s)
                else:
                    response = await request
                response.raise_for_status()
                # `/points` can establish the unsupported boundary while this
                # independent CAP request is in flight. Recheck before an old
                # response can arm a card or siren for an unsupported point.
                if state.nws_point_covered is False:
                    _stand_down_nws_alerts(state)
                    continue
                received_at = datetime.now(timezone.utc)
                alerts = parse_active_alerts(response.json(), now=received_at)
                visual = select_visual_alert(alerts)
                siren = select_siren_alert(alerts)
                state.alert_known = True
                _apply_alert_selection(state, visual, siren, alerts)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # The request was bounded by CAP's nearest local deadline.
                # Expire first, then immediately retry without extending the
                # siren through httpx's ordinary 20-second transport timeout.
                expired_at = datetime.now(timezone.utc)
                active = _locally_active_alerts(state.active_alerts, expired_at)
                if active != state.active_alerts:
                    _apply_alert_selection(
                        state,
                        select_visual_alert(active),
                        select_siren_alert(active),
                        active,
                    )
                continue
            except Exception as exc:  # noqa: BLE001
                # Envelope failure is UNKNOWN, not an all-clear.  The local
                # CAP deadlines remain authoritative even while NWS is down.
                failed_at = datetime.now(timezone.utc)
                active = _locally_active_alerts(state.active_alerts, failed_at)
                if active != state.active_alerts:
                    _apply_alert_selection(
                        state,
                        select_visual_alert(active),
                        select_siren_alert(active),
                        active,
                    )
                logger.warning("alert poll failed; retaining unexpired state: %s",
                               describe_exception(exc))

            delay = float(ALERTS_INTERVAL_S)
            if state.active_alerts:
                nearest = min(
                    min(a.expires, a.ends) if a.ends is not None else a.expires
                    for a in state.active_alerts
                )
                remaining = (
                    nearest - datetime.now(timezone.utc)).total_seconds()
                delay = min(delay, max(0.25, remaining))
            await asyncio.sleep(delay)


async def poll_nws(state: SkyState) -> None:
    station = NWS_STATION
    forecast_url = None
    nws_ok = True  # health of this observation/forecast discovery pipeline
    nws_retry_at = 0.0
    async with httpx.AsyncClient(headers=NWS_UA, timeout=20) as client:
        while True:
            t0 = asyncio.get_event_loop().time()
            nws_observed = False
            nws_snapshot: WeatherUpdates | None = None
            nws_source_time: datetime | None = None
            if (not station or forecast_url is None) and t0 >= nws_retry_at:
                points_resolved = False
                try:
                    r = await client.get(
                        f"https://api.weather.gov/points/{LAT:.4f},{LON:.4f}")
                    r.raise_for_status()
                    # Only the status of `/points` defines the geographic
                    # boundary. A later station-list 404 means the covered
                    # point lacks usable station discovery, not that CAP and
                    # every other NWS point product are unavailable there.
                    points_resolved = True
                    props = r.json()["properties"]
                    state.nws_point_covered = True
                    state.nws_point_checked.set()
                    forecast_url = props["forecast"]
                    if not station:
                        rs = await client.get(props["observationStations"])
                        rs.raise_for_status()
                        station = rs.json()["features"][0][
                            "properties"]["stationIdentifier"]
                    logger.info("NWS: station %s, forecast discovered",
                                station)
                    nws_ok = True
                except Exception as exc:  # noqa: BLE001
                    # A 404 from /points genuinely means "outside NWS coverage"
                    # and deserves a slow retry; anything else (DNS cold at
                    # boot, a 503, a timeout) is transient. This used to latch
                    # off forever, silently taking observations, the forecast
                    # AND the severe-weather alarm with it for the life of
                    # the process.
                    status = getattr(getattr(exc, "response", None),
                                     "status_code", None)
                    points_unsupported = status == 404 and not points_resolved
                    if points_unsupported:
                        state.nws_point_covered = False
                        _stand_down_nws_alerts(state)
                    if not state.nws_point_checked.is_set():
                        state.nws_point_checked.set()
                    wait = 6 * 3600 if points_unsupported else 900
                    nws_retry_at = t0 + wait
                    if nws_ok:  # log only on the transition, not every cycle
                        if points_unsupported:
                            logger.info(
                                "NWS point unsupported (%s) — global feeds "
                                "only; retrying in %.0fs",
                                describe_exception(exc), wait)
                        else:
                            logger.info(
                                "NWS observation/forecast discovery failed "
                                "(%s); retrying in %.0fs",
                                describe_exception(exc), wait)
                    nws_ok = False
                    if points_unsupported:
                        # Point coverage is the locality contract. A pinned
                        # station must not smuggle unrelated US observation
                        # history into a location the point API does not cover.
                        state.obs_history = []
            try:
                if not (nws_ok and station):
                    raise RuntimeError("no NWS station")
                r = await client.get(
                    f"https://api.weather.gov/stations/{station}/observations/latest"
                )
                r.raise_for_status()
                obs_props = r.json()["properties"]
                obs_time = _source_datetime(obs_props.get("timestamp"))
                if obs_time is None:
                    raise ValueError("NWS observation timestamp is missing or stale")
                obs = _parse_obs(obs_props)
                nws_observed = {
                    "cloud_frac", "wind_kmh", "temp_c", "humidity",
                    "visibility_m",
                }.issubset(obs)
                if not nws_observed:
                    raise ValueError("NWS observation is incomplete")
                # Stage the station snapshot across the Open-Meteo await.
                # Rendering must continue to see the previous fused snapshot,
                # not temporarily regress to airport values mid-cycle.
                nws_snapshot = obs
                nws_source_time = obs_time
                logger.info(
                    "obs: cloud=%.0f%% rain=%s snow=%s thunder=%s "
                    "wind=%.0fkm/h temp=%.0fC rh=%.0f%% vis=%.0fm",
                    obs["cloud_frac"] * 100, obs.get("rain"), obs.get("snow"),
                    obs.get("thunder"), obs["wind_kmh"], obs["temp_c"],
                    obs["humidity"], obs["visibility_m"],
                )
            except Exception as exc:  # noqa: BLE001 - feed is best-effort
                if nws_ok:
                    logger.warning("obs fetch failed: %s", describe_exception(exc))
            # The NWS station is truth for phenomena but reads at the airport,
            # roughly hourly. Open-Meteo nowcasts temperature at our exact
            # coordinates (what the watch on your wrist does) — use it for
            # the number people compare against.
            try:
                r = await client.get(
                    "https://api.open-meteo.com/v1/forecast",
                    headers=NEUTRAL_UA,  # no contact needed here
                    params={
                        "latitude": LAT, "longitude": LON,
                        "current": "temperature_2m,precipitation,rain,showers,"
                                   "snow_depth,cloud_cover,weather_code,"
                                   "wind_speed_10m,wind_direction_10m,"
                                   "relative_humidity_2m,visibility",
                        "temperature_unit": "celsius",
                        "wind_speed_unit": "kmh",
                        "timezone": "UTC",
                    },
                )
                r.raise_for_status()
                cur = r.json()["current"]
                if not isinstance(cur, dict):
                    raise ValueError("Open-Meteo current snapshot is not an object")
                current_time = _source_datetime(
                    cur.get("time"), allow_naive_utc=True)
                required = {
                    key: _finite_number(cur.get(key), low, high)
                    for key, low, high in (
                        ("temperature_2m", -100, 70),
                        ("precipitation", 0, 1000),
                        ("rain", 0, 1000),
                        ("showers", 0, 1000),
                        ("snow_depth", 0, 20),
                        ("cloud_cover", 0, 100),
                        ("wind_speed_10m", 0, 500),
                        ("wind_direction_10m", 0, 360),
                        ("relative_humidity_2m", 0, 100),
                        ("visibility", 0, 200_000),
                        ("weather_code", 0, 99),
                    )
                }
                if current_time is None or any(
                        value is None for value in required.values()):
                    raise ValueError(
                        "Open-Meteo current snapshot is stale or incomplete")
                complete = cast(dict[str, float], required)

                # Build the entire candidate first.  Nothing below this point
                # reads provider data that can invalidate the envelope, so a
                # malformed/stale response can never half-overwrite the live
                # last-known-good WeatherState.
                model_t = complete["temperature_2m"]
                cloud_cover = complete["cloud_cover"]
                code_value = complete["weather_code"]
                code_rain, code_snow, code_thunder, code_fog = (
                    _wmo_phenomena(code_value))
                # This is the ONLY feed of settled snow depth to the live
                # scene: wx_at() (the Time Machine) reads its own copy from
                # the hourly table, but the bar's actual live scene renders
                # from state.weather, which only poll_nws ever writes.
                # Without this, snow_tier(wx.snow_depth_m) is 0 forever on
                # the device no matter what really fell.
                snow_depth = complete["snow_depth"]
                precip = complete["precipitation"]
                rain_mm = complete["rain"]
                showers = complete["showers"]
                om_rain = (
                    precip > 0.05 or rain_mm > 0 or showers > 0 or code_rain
                )
                modeled: WeatherUpdates = {
                    "temp_c": model_t,
                    "snow_depth_m": snow_depth,
                }
                # A station snapshot can be complete for cloud/wind/temp while
                # its optional phenomena envelope is absent. In that case the
                # valid global model must refresh snow/thunder instead of
                # letting an old station storm state survive indefinitely.
                if nws_snapshot is None or "snow" not in nws_snapshot:
                    modeled["snow"] = code_snow
                if nws_snapshot is None or "thunder" not in nws_snapshot:
                    modeled["thunder"] = code_thunder
                if nws_snapshot is None or "fog" not in nws_snapshot:
                    modeled["fog"] = code_fog
                if not nws_observed:
                    modeled.update({
                        "cloud_frac": cloud_cover / 100.0,
                        "wind_kmh": complete["wind_speed_10m"],
                        "wind_dir": complete["wind_direction_10m"],
                        "humidity": complete["relative_humidity_2m"],
                        "visibility_m": complete["visibility"],
                    })

                prior_temp = state.weather.temp_c
                # Build on the latest live object *after* the network await so
                # a CAP task update to severe/severe_event cannot be lost.
                candidate = state.weather
                if nws_snapshot is not None:
                    # Rain is committed as separately timestamped evidence
                    # below. Letting replace() write it here would allow a
                    # station sample that aged out during the Open-Meteo await
                    # to silently become the last-good fallback.
                    candidate = replace(
                        candidate, **_without_rain(nws_snapshot),
                    )
                candidate = replace(candidate, **modeled)
                state.weather = candidate
                station_source_at = (
                    _source_monotonic_at(nws_source_time)
                    if nws_source_time is not None else None
                )
                model_source_at = _source_monotonic_at(current_time)
                state.snow_depth_at = model_source_at
                if (nws_snapshot is not None and nws_source_time is not None
                        and "rain" in nws_snapshot):
                    state.station_rain = nws_snapshot["rain"]
                    state.station_at = station_source_at
                if nws_snapshot is not None and "snow" in nws_snapshot:
                    state.snow_at = station_source_at
                elif "snow" in modeled:
                    state.snow_at = model_source_at
                if nws_snapshot is not None and "thunder" in nws_snapshot:
                    state.thunder_at = station_source_at
                elif "thunder" in modeled:
                    state.thunder_at = model_source_at
                state.om_rain = om_rain
                state.om_at = model_source_at
                # The fused WeatherState still carries NWS cloud, wind, and
                # observed phenomena.  Its lease is therefore limited by the
                # oldest contributing source, not extended by the newer one.
                fused_source_time = current_time
                if nws_source_time is not None:
                    fused_source_time = min(nws_source_time, current_time)
                _mark_weather_fresh(state, fused_source_time)
                logger.info("temp: prior %.0fC -> nowcast %.1fC",
                            prior_temp, model_t)
            except Exception as exc:  # noqa: BLE001
                logger.warning("nowcast temp failed (%s); station stands",
                               describe_exception(exc))
                if nws_snapshot is not None and nws_source_time is not None:
                    # The global nowcast failed, but the already validated
                    # station observation is still an honest complete fallback.
                    # Commit it only now, once, preserving any concurrent CAP
                    # fields that landed while Open-Meteo was awaited.
                    state.weather = replace(
                        state.weather, **_without_rain(nws_snapshot),
                    )
                    station_source_at = _source_monotonic_at(nws_source_time)
                    if "rain" in nws_snapshot:
                        state.station_rain = nws_snapshot["rain"]
                        state.station_at = station_source_at
                    if "snow" in nws_snapshot:
                        state.snow_at = station_source_at
                    if "thunder" in nws_snapshot:
                        state.thunder_at = station_source_at
                    _mark_weather_fresh(state, nws_source_time)
            # Outside the try on purpose: this must also run when the fetch
            # failed, so a previously-fresh nowcast can age out of the
            # precedence chain instead of freezing the rain flag.
            _expire_stale_phenomena(state)
            apply_rain(state)

            now = asyncio.get_event_loop().time()
            if now >= getattr(poll_nws, "_hourly_due", 0.0):
                poll_nws.__dict__["_hourly_due"] = now + FORECAST_INTERVAL_S
                try:
                    r = await client.get(
                        "https://api.open-meteo.com/v1/forecast",
                        headers=NEUTRAL_UA,
                        params={
                            "latitude": LAT, "longitude": LON,
                            "hourly": "temperature_2m,cloud_cover,"
                                      "precipitation,weather_code,"
                                      "precipitation_probability,"
                                      "wind_speed_10m,wind_direction_10m,"
                                      "relative_humidity_2m,visibility,"
                                      "snow_depth",
                            "past_days": 1, "forecast_days": 2,
                            # UTC, then convert. Asking for local time and
                            # attaching the zone afterwards collapses the two
                            # repeated hours at the autumn fall-back.
                            "timezone": "UTC",
                        },
                    )
                    r.raise_for_status()
                    state.hourly = parse_hourly(r.json()["hourly"])
                    logger.info("hourly: %d rows for the Time Machine",
                                len(state.hourly))
                except Exception as exc:  # noqa: BLE001
                    logger.warning("hourly fetch failed: %s", describe_exception(exc))
            if (nws_ok and station
                    and now >= getattr(poll_nws, "_obs_history_due", 0.0)):
                poll_nws.__dict__["_obs_history_due"] = (
                    now + FORECAST_INTERVAL_S
                )
                try:
                    end = datetime.now(timezone.utc)
                    start = end - timedelta(hours=OBS_HISTORY_HOURS)
                    r = await client.get(
                        f"https://api.weather.gov/stations/{station}"
                        "/observations",
                        headers=NEUTRAL_UA,
                        params={
                            "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "end": end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                            "limit": OBS_HISTORY_MAX,
                        },
                    )
                    r.raise_for_status()
                    state.obs_history = _parse_obs_history(r.json(), start)
                    logger.info(
                        "observations: %d rows of confirmed history",
                        len(state.obs_history))
                except Exception as exc:  # noqa: BLE001
                    # Leave the last good history in place. A failed fetch
                    # must not turn a rained-on afternoon into a dry one.
                    logger.warning("observation history fetch failed: %s",
                                   describe_exception(exc))
            if now >= getattr(poll_nws, "_forecast_due", 0.0):
                poll_nws.__dict__["_forecast_due"] = now + FORECAST_INTERVAL_S
                try:
                    if not forecast_url:
                        raise RuntimeError("no forecast endpoint")
                    r = await client.get(forecast_url)
                    r.raise_for_status()
                    state.forecast = r.json()["properties"]["periods"][:2]
                    logger.info("forecast: %s",
                                state.forecast[0]["shortForecast"])
                except Exception as exc:  # noqa: BLE001
                    if nws_ok:
                        logger.warning("forecast fetch failed: %s", describe_exception(exc))
            await asyncio.sleep(OBS_INTERVAL_S)


def _km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p = math.pi / 180
    a = (0.5 - math.cos((lat2 - lat1) * p) / 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * (1 - math.cos((lon2 - lon1) * p)) / 2)
    return 12742 * math.asin(math.sqrt(a))


def _encoder_delta(update: dict) -> int:
    event = (update.get("input") or {}).get("encoder_event") or {}
    return int(event.get("delta") or 0)


def _is_ok_press(update: dict) -> bool:
    """OK is the wheel's down-click. Proto3 omits zero enums, so an OK
    PRESS can arrive as an empty button_event."""
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return (event.get("button") in (None, 0, "OK")
            and event.get("action") in (None, 0, "PRESS"))


def _is_start_press(update: dict) -> bool:
    """Match a START press in a state-stream input update, tolerating both
    proto enum names and raw values (proto3 omits zero-valued fields)."""
    event = (update.get("input") or {}).get("button_event") or {}
    button = event.get("button")
    action = event.get("action")
    return (button in ("START", 2)) and (action in (None, 0, "PRESS"))


def _switch_position(update: dict) -> str | None:
    """Decode the physical slider event from the vendored protobuf schema.

    Proto3 omits BUSY (enum zero), so an explicitly present empty
    ``switch_event`` means BUSY.  Absence means no new ownership evidence.
    """
    inp = update.get("input") or {}
    if "switch_event" not in inp:
        return None
    event = inp.get("switch_event") or {}
    value = event.get("position", 0)
    if isinstance(value, str):
        value = value.upper()
        return value if value in {"BUSY", "CUSTOM", "OFF", "APPS", "SETTINGS"} else None
    return {0: "BUSY", 1: "CUSTOM", 2: "OFF", 3: "APPS", 4: "SETTINGS"}.get(value)


def _has_committed_start_view(state: SkyState) -> bool:
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


async def _start_is_owned(
    bb,
    state: SkyState,
    switch_generation: int,
    switch_position: str | None,
) -> bool:
    """Resolve START without claiming a firmware BUSY/CUSTOM button press.

    An explicit OFF event is authoritative and stays on the no-I/O fast path.
    UNKNOWN is accepted only when Skystrip has committed a view and the
    device's current Busy snapshot is NOT_STARTED.  Switch evidence changing
    during the settle/query window invalidates the press; errors and timeouts
    deliberately fail closed.
    """
    if (
        state.shutting_down
        or state.switch_generation != switch_generation
        or state.switch_position != switch_position
    ):
        return False
    if switch_position == "OFF":
        return True
    if switch_position is not None or not _has_committed_start_view(state):
        return False

    await asyncio.sleep(START_OWNERSHIP_SETTLE_S)
    if (
        state.shutting_down
        or state.switch_generation != switch_generation
        or state.switch_position is not None
        or not _has_committed_start_view(state)
    ):
        return False
    try:
        snapshot = await asyncio.wait_for(
            bb.busy_snapshot(), timeout=START_OWNERSHIP_TIMEOUT_S)
        not_started = snapshot.snapshot.type == "NOT_STARTED"
    except Exception as exc:  # noqa: BLE001 - ambiguity must not claim START
        logger.warning("START ownership check failed; ignoring press: %s", exc)
        return False
    return (
        not_started
        and not state.shutting_down
        and state.switch_generation == switch_generation
        and state.switch_position is None
        and _has_committed_start_view(state)
    )


HOURLY_MAX_ROWS = 96 + 48        # past_days=1 + forecast_days=2, with slack


def parse_hourly(payload, *, tz=None) -> list:
    """Validate an Open-Meteo hourly table into (local time, row) pairs.

    Built complete, then returned for an atomic swap: a malformed table must
    leave the last good one untouched rather than half-replace it.

    Two things this fixes beyond the obvious.

    **The fold.** The request now asks for UTC and converts, instead of asking
    for local time and calling `.replace(tzinfo=TZ)` on a naive string. At the
    autumn fall-back that `.replace` gave both 01:00 CDT and 01:00 CST the same
    key with fold=0, so the Time Machine's nearest-row search would return the
    wrong hour for an hour a year. `_parse_obs_history` already converts from
    UTC; this now matches it.

    **Unknown rows are dropped, not defaulted.** Open-Meteo returns null for
    hours outside its range. A row without a usable time, temperature and cloud
    cover is not a weaker row, it is an absent one — and `wx_at` falls back to
    live conditions for a moment it has no model data for. That is the skill's
    rule: unknown is a product state, not permission to invent a default.
    """
    zone = TZ if tz is None else tz
    if not isinstance(payload, dict):
        raise ValueError("hourly payload is not an object")
    times = payload.get("time")
    if not isinstance(times, list):
        raise ValueError("hourly payload has no time column")
    if len(times) > HOURLY_MAX_ROWS:
        times = times[:HOURLY_MAX_ROWS]

    def column(name):
        values = payload.get(name)
        return values if isinstance(values, list) else []

    columns = {name: column(name) for name in (
        "temperature_2m", "cloud_cover", "precipitation", "weather_code",
        "precipitation_probability", "wind_speed_10m", "wind_direction_10m",
        "relative_humidity_2m", "visibility", "snow_depth")}

    def at(name, index, low, high):
        values = columns[name]
        if index >= len(values):
            return None
        return _finite_number(values[index], low, high)

    rows = []
    for i, raw_time in enumerate(times):
        if not isinstance(raw_time, str) or len(raw_time) > 64:
            continue
        try:
            when = datetime.fromisoformat(
                raw_time[:-1] + "+00:00" if raw_time.endswith(("Z", "z"))
                else raw_time)
        except ValueError:
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        temp = at("temperature_2m", i, -100.0, 70.0)
        cloud = at("cloud_cover", i, 0.0, 100.0)
        if temp is None or cloud is None:
            continue           # no model data for this hour; not a weak row
        rows.append((when.astimezone(zone), {
            "temp": temp,
            "cloud": cloud,
            "precip": at("precipitation", i, 0.0, 1000.0),
            "prob": at("precipitation_probability", i, 0.0, 100.0),
            "code": at("weather_code", i, 0.0, 99.0),
            "wind": at("wind_speed_10m", i, 0.0, 500.0),
            "wdir": at("wind_direction_10m", i, 0.0, 360.0),
            "rh": at("relative_humidity_2m", i, 0.0, 100.0),
            "vis": at("visibility", i, 0.0, 200_000.0),
            "snow_depth": at("snow_depth", i, 0.0, 20.0),
        }))
    rows.sort(key=lambda row: row[0])
    return rows


def _parse_obs_history(payload, start: datetime,
                       now: datetime | None = None) -> list:
    """Validate a station observation feed into (local time, props) rows.

    Built complete, then returned for an atomic swap: a feed that turns out to
    be malformed must leave the last good history untouched rather than
    half-replace it. Bounded at ingestion -- record count is remote input, and
    it drives a per-slot scan for all 97 timeline frames.
    """
    if not isinstance(payload, dict):
        raise ValueError("observation payload is not an object")
    features = payload.get("features")
    if not isinstance(features, list):
        raise ValueError("observation payload has no feature list")
    if len(features) > OBS_HISTORY_MAX:
        features = features[:OBS_HISTORY_MAX]
    rows = []
    for feature in features:
        props = (feature or {}).get("properties") if isinstance(
            feature, dict) else None
        if not isinstance(props, dict):
            continue
        when = _source_datetime(
            props.get("timestamp"), now=now,
            max_age_s=OBS_HISTORY_HOURS * 3600)
        if when is None or when < start:
            continue
        rows.append((when.astimezone(TZ), props))
    rows.sort(key=lambda r: r[0])
    return rows


def obs_precipitation(props: dict) -> dict | None:
    """What a single station observation says was falling, and how hard.

    Separate from _parse_obs on purpose: that feeds `replace(WeatherState,
    **obs)` on the live path, so every key it returns must be a field there.
    This returns a tier the live path has no business inheriting -- live
    intensity belongs to resolve_rain and the radar.
    """
    if not isinstance(props, dict):
        return None
    entries = [e for e in (props.get("presentWeather") or [])
               if isinstance(e, dict) and e.get("weather") in OBS_PRECIP_WORDS]
    if not entries:
        return None
    words = {e.get("weather") for e in entries}
    text = props.get("textDescription")
    text = text.lower() if isinstance(text, str) else ""
    try:
        raw = json.dumps(props.get("presentWeather") or []).lower()
    except (TypeError, ValueError):
        raw = ""
    snow = bool(words & OBS_SNOW_WORDS)
    return {
        "rain": not snow,
        "snow": snow,
        "thunder": "thunder" in text or "thunder" in raw,
        # Heaviest wins within one observation too: "Heavy Rain and Fog/Mist"
        # is a downpour that happens to also be misty.
        "tier": max(OBS_INTENSITY_TIER.get(e.get("intensity"), 1)
                    for e in entries),
    }


def observed_precip_at(history, target: datetime,
                       window_s: int = OBS_SLOT_WINDOW_S) -> dict | None:
    """The most significant precipitation observed near `target`, or None.

    None means "no observation covers this moment", which is NOT the same as
    "it was dry" -- the caller must draw nothing rather than invent a default.

    Heaviest in the window rather than nearest in time: a three-minute
    downpour inside a half-hour slot is the thing a person remembers, and
    averaging it down to light rain would understate what actually happened.
    """
    if not history:
        return None
    best = None
    for when, props in history:
        if abs((when - target).total_seconds()) > window_s:
            continue
        found = obs_precipitation(props)
        if found is None:
            continue
        if best is None or (found["tier"], found["thunder"]) > \
                (best["tier"], best["thunder"]):
            best = found
    return best


def forecast_precip(row: dict, threshold: int = PRECIP_LIKELY_PCT) -> dict | None:
    """What the forecast says will fall at an hour, or None if it likely won't.

    Gated on probability, sized by expected accumulation. The two are
    different questions and conflating them would draw a downpour for a 90%
    chance of drizzle.
    """
    prob = _finite_number(row.get("prob"), 0.0, 100.0) or 0.0
    code = row.get("code") or 0
    code_precip = (51 <= code <= 67 or 71 <= code <= 77
                   or 80 <= code <= 86 or code >= 95)
    if prob < threshold and not code_precip:
        return None
    mm = _finite_number(row.get("precip"), 0.0, 1000.0) or 0.0
    tier = 2
    for bound, value in PRECIP_TIER_MM:
        if mm < bound:
            tier = value
            break
    snow = 71 <= code <= 77 or code in (85, 86)
    if not snow and not (51 <= code <= 67 or 80 <= code <= 82):
        # The gate passed on likelihood alone, so the code carries no
        # precipitation type. Temperature is the only honest discriminator.
        temp = _finite_number(row.get("temp"), -100.0, 70.0)
        snow = temp is not None and temp <= 0.5
    return {"rain": not snow, "snow": snow, "thunder": code >= 95,
            "tier": tier}


def wx_at(state: SkyState, target: datetime) -> WeatherState:
    """Weather for a scrubbed moment, from the hourly table (real history
    backward, real forecast forward). Falls back to live conditions."""
    if not state.hourly:
        return state.weather
    # Compare as instants, not as wall clocks. Python subtracts two aware
    # datetimes in the SAME zone naively — it ignores fold — so at the autumn
    # fall-back both 01:00 rows read as zero seconds from a 01:00 target and
    # min() silently took the first. Converting to UTC forces real interval
    # arithmetic, which is the half of the fold fix that lives here rather
    # than at ingestion.
    goal = target.astimezone(timezone.utc)

    def distance(row) -> float:
        return abs((row[0].astimezone(timezone.utc) - goal).total_seconds())

    row = min(state.hourly, key=distance)
    if distance(row) > 5400:
        return state.weather
    d = row[1]
    live = state.weather
    # Precipitation is the one thing the hourly table cannot be trusted for in
    # both directions, so it is resolved separately. Everything else -- cloud,
    # temperature, wind, humidity, visibility -- comes from the model, which is
    # good at those and denser than the station record.
    if target <= datetime.now(TZ):
        fall = observed_precip_at(state.obs_history, target)
    else:
        fall = forecast_precip(d)
    return WeatherState(
        cloud_frac=(d["cloud"] or 0) / 100.0,
        # No observation covering a past moment means unknown, not dry. Either
        # way nothing is drawn -- but it must never fall through to the hourly
        # table, which is the model's guess about the past and was wrong by a
        # whole thunderstorm on the day this was written.
        rain=bool(fall and fall["rain"]),
        rain_tier=fall["tier"] if fall else 1,
        snow=bool(fall and fall["snow"]),
        thunder=bool(fall and fall["thunder"]),
        severe=False,  # alarms live in the present only
        # `temp` and `cloud` are guaranteed by parse_hourly — a row without
        # them is dropped there rather than defaulted here. For the rest, the
        # live snapshot is the honest fallback: 20 degrees, 50% humidity and
        # 16 km of visibility were invented constants that rendered as
        # confidently as measurements. Falling back to what is actually
        # outside is at least a real observation.
        wind_kmh=d["wind"] if d["wind"] is not None else live.wind_kmh,
        wind_dir=d["wdir"],
        temp_c=d["temp"],
        humidity=d["rh"] if d["rh"] is not None else live.humidity,
        visibility_m=(d["vis"] if d["vis"] is not None
                      else live.visibility_m),
        snow_depth_m=(d["snow_depth"] if d.get("snow_depth") is not None
                      else live.snow_depth_m),
    )


def _compass(deg) -> str:
    if deg is None:
        return ""
    names = ("north", "northeast", "east", "southeast",
             "south", "southwest", "west", "northwest")
    return names[int((deg + 22.5) // 45) % 8]


# What the voice calls each obscuration. Plain English in every style —
# `genz` gets its own register elsewhere but not its own facts.
OBSCURATION_PHRASE = {
    "haze": "haze",
    "smoke": "smoke in the air",
    "dust": "blowing dust",
    "ash": "volcanic ash in the air",
}


def _moon_phase_name(day: float) -> str:
    for limit, name in ((1.8, "new"), (5.5, "a waxing crescent"),
                        (9.2, "first quarter"), (12.9, "a waxing gibbous"),
                        (16.6, "full"), (20.3, "a waning gibbous"),
                        (24.1, "last quarter"), (27.7, "a waning crescent")):
        if day < limit:
            return name
    return "new"


def _eclipse_report_facts(now_local: datetime) -> dict | None:
    """What the report may say about Earth's shadow, or None.

    One source of facts for all three styles. `genz` promises that every
    number it speaks is identical to what `plain` would have said, and the
    only way to keep that promise structurally is for the styles to share
    the arithmetic and differ solely in wording.

    Reports the *area* of the disc covered, not the umbral magnitude the
    catalogues quote — magnitude is a fraction of the diameter, and a
    93%-magnitude eclipse hides 96% of the visible face. Speaking the
    catalogue number as if it described the face would be wrong by four
    points on exactly the night anyone is listening.
    """
    try:
        now_utc = now_local.astimezone(timezone.utc)
        live = eclipse_visible_state(now_utc, OBSERVER)
        if live is not None and live.in_umbra:
            return {
                "phase": live.phase,
                "pct": round(live.obscuration * 100),
                "at": None,
            }
        # Nothing on the disc yet. If the umbral phase begins soon and the
        # moon will be up for it, that is worth a heads-up — an eclipse you
        # are told about after it ends is not a notice, it is a regret.
        for eclipse in _skystrip_eclipse.eclipses_near(now_utc):
            window = eclipse.contact("partial")
            if window is None:
                continue  # penumbral-only: nothing anyone can see
            begin = window[0]
            if not now_utc < begin <= now_utc + timedelta(hours=ECLIPSE_HEADS_UP_H):
                continue
            if eclipse_visible_state(begin, OBSERVER) is None:
                continue  # happening, but below this horizon
            peak = _skystrip_eclipse.state_at(
                eclipse.greatest, eclipse=eclipse)
            if peak is None:
                # A candidate without geometry at its own greatest instant is
                # incomplete. Omit it instead of inventing an obscuration.
                continue
            local_begin = begin.astimezone(TZ)
            return {
                "phase": eclipse.kind,
                "pct": round(peak.obscuration * 100),
                "at": f"{local_begin.hour % 12 or 12}:{local_begin.minute:02d}",
            }
    except Exception:  # noqa: BLE001 - flavor, never fatal
        return None
    return None


def _forecast_temperature(period: dict) -> int | None:
    value = _finite_number(period.get("temperature"), -150, 200)
    unit = str(period.get("temperatureUnit") or "").strip().upper()
    if value is None or unit not in {"F", "C"}:
        return None
    if UNITS == "c" and unit == "F":
        value = (value - 32.0) * 5.0 / 9.0
    elif UNITS != "c" and unit == "C":
        value = value * 9.0 / 5.0 + 32.0
    return round(value)


def _peak_hour_words(hour: int) -> str:
    """Name an hour the way a person says it out loud."""
    if hour == 12:
        return "noon"
    if hour == 0:
        return "midnight"
    part = (
        "in the evening"
        if hour >= 17
        else "this afternoon"
        if hour >= 12
        else "this morning"
    )
    return f"{hour % 12 or 12} {part}"


def _alert_phrase(wx: WeatherState) -> str:
    """Say which warning it is, not merely that one exists.

    "Severe weather in the area" is true of a Tornado Warning and of a Frost
    Advisory, and you cannot act on it. CAP event names ("Tornado Warning",
    "Flash Flood Warning") are already plain English, so the honest report
    speaks the name the NWS issued. An unnamed alert keeps the old wording:
    vague is bad, invented is worse.
    """
    event = (wx.severe_event or "").strip()
    if not event:
        return "severe weather in the area"
    article = "an" if event[:1].upper() in "AEIOU" else "a"
    return f"{article} {event} in effect"


# How much of a forecast period has to be left before the report will
# still talk about it as something ahead of you.
FORECAST_HANDOVER_MIN = 90


def _period_end(period: dict) -> datetime | None:
    """A period's end as a local datetime, or None if it isn't usable."""
    raw = period.get("endTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        end = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if end.tzinfo is None:
        return None
    return end.astimezone(TZ)


def _forecast_period(forecast: list, now_local: datetime) -> dict | None:
    """The period the report should actually be talking about.

    NWS `periods[0]` is the CURRENT period, not the next one, and every
    style phrases it as something still ahead ("Later we're going for 81
    this afternoon"). The greeting turns over to evening at five o'clock
    while the NWS afternoon runs until six, so for that hour the bar
    greeted you with one and then forecast the other — for a daytime high
    that had already happened.

    So skip a period with less than FORECAST_HANDOVER_MIN left and use the
    next one, which is already fetched. A period whose end cannot be read
    is used as-is: vague is bad, invented is worse.
    """
    if not forecast:
        return None
    for period in forecast:
        end = _period_end(period)
        if end is None:
            return period
        if now_local.tzinfo is None:
            end = end.replace(tzinfo=None)
        if end - now_local > timedelta(minutes=FORECAST_HANDOVER_MIN):
            return period
    return forecast[-1]


def _precip_kind(code: int) -> str:
    """What the hourly weather code is made of."""
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "storms" if code >= 95 else "rain"


def _hourly_today(hourly: list, now_local: datetime) -> list:
    """The rows from now to the end of the local day."""
    end = now_local.replace(hour=23, minute=59)
    return [(dt, r) for dt, r in hourly if now_local <= dt <= end]


def _today_peak(ahead: list) -> tuple[int, str, str]:
    """(whole percent, spoken hour, kind) for the wettest hour left today.

    Model probabilities arrive as floats. Rounding here keeps the type
    artifact out of every style's prose rather than each one remembering.
    """
    pk, pk_dt, pk_row = max(((r.get("prob") or 0, dt, r) for dt, r in ahead),
                            key=lambda t: t[0])
    return (round(pk), _peak_hour_words(pk_dt.hour),
            _precip_kind(pk_row.get("code", 0)))


def _tomorrow_outlook(hourly: list, now_local: datetime,
                      ) -> tuple[int, int, str] | None:
    """(high, whole percent, kind) for tomorrow's daylight, or None."""
    start = (now_local + timedelta(days=1)).replace(hour=7, minute=0)
    rows = [(dt, r) for dt, r in hourly if start <= dt <= start.replace(hour=22)]
    if not rows:
        return None
    hi_c = max(r.get("temp") or 0 for _, r in rows)
    hi = round(hi_c) if UNITS == "c" else round(hi_c * 9 / 5 + 32)
    pk, pk_row = max(((r.get("prob") or 0, r) for _, r in rows),
                     key=lambda t: t[0])
    return hi, round(pk), _precip_kind(pk_row.get("code", 0))


def _wind_words(wx: WeatherState) -> tuple[float, str, str, int, int, int]:
    """Speed in the configured unit, its name, the compass phrase, thresholds."""
    if UNITS == "c":
        return wx.wind_kmh, "kilometers an hour", _compass(wx.wind_dir), 48, 29, 13
    return (wx.wind_kmh * 0.621, "miles an hour", _compass(wx.wind_dir),
            30, 18, 8)


# The genz register, as phrase pools rather than fixed lines.
#
# Variance here is DETERMINISTIC on purpose. The report's text is hashed
# into the device asset filename and the firmware caches assets by path
# forever, so phrasing that rerolled on every render would bake and upload
# a fresh .snd every minute. Seeding on the local date and hour gives a
# different-sounding report each hour while staying byte-stable within one
# — and you rarely hear two reports in the same hour anyway.
#
# Terminology is checked against current usage rather than memory, because
# this slang ages in months: "mid", "ate", "aura", "delulu", "cooked",
# "crashing out" and "standing on business" are the 2026 register, while
# the 2020 TikTok vocabulary now reads as a parody of itself.
GENZ_GREETINGS = {
    "morning": ("Morning, chat.", "Okay so, good morning.",
                "Morning, bestie.", "Good morning, I guess."),
    "afternoon": ("Afternoon, chat.", "Okay so, afternoon.",
                  "Afternoon, bestie.", "Hi, it's the afternoon."),
    "evening": ("Evening, chat.", "Okay so, evening.",
                "Evening, bestie.", "Hello, it's evening."),
    "late": ("Bestie, it's so late.", "Chat, why are we awake.",
             "It's giving no sleep.", "Okay so we're just not sleeping."),
}
GENZ_CONDITIONS = {
    "thunder": ("It's storming and the sky is fully crashing out.",
                "Thunderstorms, which is a lot.",
                "It's storming. Very dramatic."),
    "snow": ("It's snowing, so that's a whole situation.",
             "Snow. We are not beating the winter allegations.",
             "It's snowing, which is genuinely so much."),
    "rain": ("It's raining, which is lowkey rude.",
             "It's raining. Respectfully, no.",
             "Rain again. The sky is crashing out."),
    "hot": ("It is so hot. We're cooked.",
            "It's hot. Genuinely cooked out here.",
            "The heat is standing on business."),
    "cold": ("It's freezing. Respectfully, no.",
             "It's so cold. I'm not okay.",
             "Freezing. We are not beating the winter allegations."),
    # Haze and dust keep the register. Smoke and ash do not: wildfire smoke
    # and volcanic ash are somebody's emergency somewhere upwind, and the
    # bit landing on those is the same misfire as joking under a warning.
    "obscured_haze": ("It's hazy out, everything looks kind of soft.",
                      "Hazy. The sky is giving washed out.",
                      "Haze everywhere. Distance is not a thing today."),
    "obscured_dust": ("There's dust blowing around out there.",
                      "Blowing dust today, so keep your eyes covered.",
                      "It's dusty out. The air is doing something."),
    "obscured_smoke": ("There's smoke in the air today. Take it easy "
                       "outside.",
                       "Smoke in the air. Maybe keep the windows shut.",
                       "There's smoke out there, so go easy today."),
    "obscured_ash": ("There is volcanic ash in the air. Stay inside.",
                     "Volcanic ash out there. Please stay indoors.",
                     "There's ash in the air today. Stay in."),
    "overcast": ("It's fully grey out. Big yikes.",
                 "Overcast. It's giving nothing.",
                 "Grey. All grey. No notes, and not in a good way."),
    "cloudy": ("Pretty cloudy, kind of mid.",
               "Mostly cloudy, which is fine I guess.",
               "Cloudy. Mid, honestly."),
    "partly": ("Some clouds, nothing crazy.",
               "A few clouds. We move.",
               "Partly cloudy, no complaints."),
    "nice": ("Clear skies, and the weather ate.",
             "Clear skies. This one has aura.",
             "Not a cloud. Main character energy."),
    "clear": ("Clear skies, no notes.",
              "Clear out, genuinely nothing happening.",
              "Clear skies. We're so back."),
}
GENZ_WIND_BIG = ("The wind is doing {n} {u}{d}, which is actually insane.",
                 "Wind's standing on business at {n} {u}{d}.",
                 "{n} {u} of wind{d}. That's crazy.")
GENZ_WIND_MID = ("Kind of breezy{d}.", "Breezy{d}, not a big deal.",
                 "A little windy{d}.")
GENZ_WIND_LOW = ("Light breeze{d}.", "Slight breeze{d}.")
GENZ_MUGGY = ("Also it's humid, which is deadass disrespectful.",
              "And it's humid. The air is soup.",
              "Humid too. Respectfully, gross.")
GENZ_UMBRELLA = ("Bring an umbrella, I'm being serious.",
                 "Take the umbrella, I'm not being delulu about this.",
                 "Umbrella. I'm standing on business about it.")
GENZ_SHOVEL = ("Keep the shovel close.", "Shovel weather, sorry.",
               "The shovel is going to be involved.")
GENZ_GORGEOUS = ("Rest of the {s} is genuinely gorgeous, no notes.",
                 "Rest of the {s} absolutely ate.",
                 "We're so back for the rest of the {s}.")
GENZ_DRY = ("Staying dry the rest of the {s}.",
            "Dry the rest of the {s}, at least.",
            "Rest of the {s} stays dry. Mid but fine.")
GENZ_SUNSET = ("Sunset's at {t}. Go touch grass.",
               "Sunset's at {t}, genuinely go outside for that one.",
               "Sunset at {t}. That one's worth it.")
GENZ_MOON = ("There's {a}{p} moon out and it's giving.",
             "{P} moon tonight. It has aura.",
             "The moon is {p} tonight, which is kind of beautiful.")
# The eclipse pools speak the same numbers `plain` does — that invariant is
# enforced by sharing `_eclipse_report_facts`, and pinned by a test. Whole
# words only, like every other pool here: the initialisms this register is
# known for are unpronounceable to a neural voice.
GENZ_ECLIPSE_PARTIAL = (
    "Also the moon is getting eaten by Earth's shadow right now, "
    "{n} percent of it gone. Go look.",
    "Earth's shadow is on the moon right now, {n} percent covered. "
    "That is a lunar eclipse and it is insane.",
    "There is a lunar eclipse happening. Earth's shadow has {n} percent "
    "of the moon. Put your shoes on.")
GENZ_ECLIPSE_TOTAL = (
    "Also the moon is fully inside Earth's shadow right now. "
    "A total lunar eclipse. Go outside, this is not a drill.",
    "Total lunar eclipse happening right now, the whole moon swallowed. "
    "Genuinely once in a while stuff.",
    "Earth's shadow has the entire moon right now. Total eclipse. "
    "Please go look at it.")
GENZ_ECLIPSE_SOON = (
    "Also there is a {k} lunar eclipse starting at {t} tonight. "
    "Set an alarm, seriously.",
    "Heads up, {k} lunar eclipse at {t} tonight. Earth's shadow, "
    "on the moon. Stay up for it.",
    "A {k} lunar eclipse kicks off at {t} tonight and you are going "
    "to want to see that.")
# When a warning is in effect the bit is off for the WHOLE report, not
# just the alert sentence. "Umbrella, I'm standing on business about it"
# under a Tornado Warning is the joke landing on the one line that has to
# be actionable, and the flavour at the end is worse — nobody should be
# told to go touch grass during a warning. Severe reports are short,
# factual and end by saying so.
GENZ_SERIOUS_TOOL = "Take that seriously and keep an eye on the sky."
GENZ_SERIOUS_SIGNOFF = "Stay safe out there."

GENZ_SIGNOFF = ("Anyway. That's the weather, no cap.",
                "Anyway. That's your weather.",
                "That's it. That's the forecast.",
                "Okay that's the weather. Bye.")


# Numbers that a teenager is constitutionally incapable of letting pass.
#
# The digits still get spoken normally — the gag is an ADDITION, never a
# replacement, because a listener came for the temperature. And "6-7" is
# written as words on purpose: `speakable` rewrites "-?\d+" into prose, so
# the digit form comes out of the speaker as "sixnegative seven".
GENZ_NICE = ("Nice.", "Heh. Nice.", "Nice. I said what I said.")
# No "Anyway" here: the sign-off pool owns that word and the two landed
# back to back.
GENZ_SIX_SEVEN = ("Six... seven.", "Six... seven. Sorry, I had to.",
                  "Six... seven. Okay, moving on.")


def _genz_number_gag(rng: random.Random, value) -> str | None:
    """The aside a given number earns, if it earns one."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value == 69:
        return rng.choice(GENZ_NICE)
    if value == 67:
        return rng.choice(GENZ_SIX_SEVEN)
    return None


def _genz_rng(now_local: datetime) -> random.Random:
    """Stable within the hour, different the next one.

    See GENZ_GREETINGS: rerolling per render would churn device assets.
    """
    return random.Random(int(now_local.strftime("%Y%m%d%H")))


def _genz_condition_key(wx: WeatherState) -> str:
    """Which pool describes the sky right now, in precedence order."""
    if wx.thunder:
        return "thunder"
    if wx.snow:
        return "snow"
    if wx.rain:
        return "rain"
    if wx.obscuration:
        # Before the temperature tiers too: on a smoke day the air is the
        # story, and "we're cooked" is a bad joke to make about a wildfire.
        return f"obscured_{wx.obscuration}"
    if wx.temp_c >= 32.0:
        return "hot"
    if wx.temp_c <= -7.0:
        return "cold"
    if wx.cloud_frac >= 0.85:
        return "overcast"
    if wx.cloud_frac >= 0.55:
        return "cloudy"
    if wx.cloud_frac >= 0.25:
        return "partly"
    return "nice" if 18 <= wx.temp_c <= 29 else "clear"


def _compose_report_genz(wx: WeatherState, forecast: list | None,
                         now_local: datetime,
                         hourly: list | None = None) -> str:
    """The same forecast, delivered like a stereotyped teenager.

    Two rules keep this honest. The numbers are never restyled — the
    temperature, the percentages and the clock times are exactly what the
    plain voice would have said, because a register changes the wording
    and not the facts. And a severe alert drops the bit entirely: a
    Tornado Warning is something a person has to act on, so it is named
    plainly and no slang goes anywhere near that sentence.

    The register is deliberately built from whole WORDS. The initialisms
    this slang is best known for are unpronounceable to a neural voice and
    `speakable` does not expand them, so "not gonna lie" earns its place
    and "ngl" would just be letter mush out of the speaker.
    """
    rng = _genz_rng(now_local)
    # One bit per broadcast, mirroring the one-umbrella-mention rule: two
    # gags in a single report is a comedy routine, not a forecast. A
    # warning spends none at all.
    spent = [bool(wx.severe)]

    def gag(value) -> str | None:
        if spent[0]:
            return None
        aside = _genz_number_gag(rng, value)
        if aside:
            spent[0] = True
        return aside

    h = now_local.hour
    if 5 <= h < 12:
        slot = "morning"
    elif 12 <= h < 17:
        slot = "afternoon"
    elif 17 <= h < 22:
        slot = "evening"
    else:
        slot = "late"
    greet = rng.choice(GENZ_GREETINGS[slot])
    f = round(wx.temp_c) if UNITS == "c" else round(wx.temp_c * 9 / 5 + 32)

    if wx.severe:
        # No bit here, and none below either — see GENZ_SERIOUS_TOOL.
        parts = [f"Heads up. There's {_alert_phrase(wx)}, "
                 f"and you need to take that seriously. {f} degrees."]
    else:
        cond = rng.choice(GENZ_CONDITIONS[_genz_condition_key(wx)])
        parts = [f"{greet} {cond} {f} degrees."]
        if (aside := gag(f)):
            parts.append(aside)

    spd, unit_word, dirname, hi, mid, lo = _wind_words(wx)
    outof = f" out of the {dirname}" if dirname else ""
    if spd >= hi:
        parts.append(f"The wind is {round(spd)} {unit_word}{outof}."
                     if wx.severe else
                     rng.choice(GENZ_WIND_BIG).format(
                         n=round(spd), u=unit_word, d=outof))
    elif spd >= mid and not wx.severe:
        parts.append(rng.choice(GENZ_WIND_MID).format(d=outof))
    elif spd >= lo and dirname and not wx.severe:
        parts.append(rng.choice(GENZ_WIND_LOW).format(d=outof))
    if (wx.humidity >= 75 and wx.temp_c >= 24 and not wx.rain
            and not wx.severe):
        parts.append(rng.choice(GENZ_MUGGY))

    # One precip mention per broadcast: if the hourly table is going to
    # name odds, the forecast period keeps quiet about them.
    hourly_pk = 0
    if hourly:
        ahead = _hourly_today(hourly, now_local)
        if len(ahead) >= 3:
            hourly_pk = max((r.get("prob") or 0) for _, r in ahead)

    period = _forecast_period(forecast or [], now_local)
    if period:
        p = period
        name = p.get("name", "later")
        name = name[0].lower() + name[1:] if name else "later"
        temp = _forecast_temperature(p)
        prob = round(
            (p.get("probabilityOfPrecipitation") or {}).get("value") or 0)
        short = (p.get("shortForecast") or "").lower()
        kind = "storms" if "thunder" in short else (
            "snow" if "snow" in short else "rain")
        if temp is None:
            line = f"Looking ahead to {name}"
        elif p.get("isDaytime"):
            line = f"Later we're going for {temp} {name}"
        else:
            line = f"We're dropping to {temp} {name}"
        if prob >= 30 and hourly_pk < 20:
            line += (f", with a {prob} percent chance of {kind}, "
                     "so plan around that")
        parts.append(line + ".")
        if (aside := gag(temp) or gag(prob)):
            parts.append(aside)

    if hourly:
        ahead = _hourly_today(hourly, now_local)
        if len(ahead) >= 3:
            pk, when_words, kind = _today_peak(ahead)
            if pk >= 45:
                tool = GENZ_SERIOUS_TOOL if wx.severe else rng.choice(
                    GENZ_SHOVEL if kind == "snow" else GENZ_UMBRELLA)
                parts.append(f"There's a {pk} percent chance of {kind} "
                             f"around {when_words}. {tool}")
                if (aside := gag(pk)):
                    parts.append(aside)
            elif pk >= 20:
                parts.append(f"Slight chance of {kind} later, like {pk} "
                             "percent. Not a big deal.")
            else:
                clouds = sum(r.get("cloud") or 0 for _, r in ahead) / len(ahead)
                span = "evening" if now_local.hour >= 17 else "day"
                pool = GENZ_GORGEOUS if clouds < 30 else GENZ_DRY
                parts.append(rng.choice(pool).format(s=span))
        else:  # late night: tomorrow instead
            outlook = _tomorrow_outlook(hourly, now_local)
            if outlook:
                hi_t, pk, kind_w = outlook
                if pk >= 30:
                    parts.append(f"Tomorrow we're going for {hi_t}, with a "
                                 f"{pk} percent chance of {kind_w}.")
                else:
                    parts.append(f"Tomorrow we're going for {hi_t} "
                                 "and it looks dry.")
                if (aside := gag(hi_t)):
                    parts.append(aside)

    # No sky flavour under a warning: nobody should be told to go
    # outside and touch grass while one is in effect.
    if not wx.severe:
        try:
            from astral import sun as _sun
            stimes = _sun.sun(OBSERVER, date=now_local.date(), tzinfo=TZ)
            sr, ss = stimes["sunrise"], stimes["sunset"]
            if sr <= now_local < ss:
                parts.append(rng.choice(GENZ_SUNSET).format(
                    t=f"{ss.hour % 12 or 12}:{ss.minute:02d}"))
            else:
                ecl = _eclipse_report_facts(now_local)
                if ecl is None:
                    phase_name = _moon_phase_name(moon.phase(now_local.date()))
                    article = "" if phase_name.startswith("a ") else "a "
                    parts.append(rng.choice(GENZ_MOON).format(
                        a=article, p=phase_name,
                        P=phase_name[0].upper() + phase_name[1:]))
                elif ecl["at"] is not None:
                    parts.append(rng.choice(GENZ_ECLIPSE_SOON).format(
                        t=ecl["at"], k=ecl["phase"]))
                elif ecl["phase"] == "total":
                    parts.append(rng.choice(GENZ_ECLIPSE_TOTAL))
                else:
                    parts.append(rng.choice(GENZ_ECLIPSE_PARTIAL).format(
                        n=ecl["pct"]))
                if now_local >= ss:
                    nxt = _sun.sun(OBSERVER,
                                   date=(now_local + timedelta(days=1)).date(),
                                   tzinfo=TZ)["sunrise"]
                    when = "tomorrow"
                else:
                    nxt, when = sr, "this morning"
                parts.append(
                    f"Sunrise at {nxt.hour % 12 or 12}:{nxt.minute:02d} {when}.")
        except Exception:  # noqa: BLE001 - flavor, never fatal
            pass
    parts.append(GENZ_SERIOUS_SIGNOFF if wx.severe
                 else rng.choice(GENZ_SIGNOFF))
    return " ".join(parts)


def _compose_report(wx: WeatherState, forecast: list | None,
                    now_local: datetime, hourly: list | None = None) -> str:
    """A weather report with some manners: greeting, conditions with
    character, the forecast, and a little sky at the edges of the day."""
    if STYLE == "genz":
        return _compose_report_genz(wx, forecast, now_local, hourly)
    h = now_local.hour
    if 5 <= h < 12:
        greet = "Good morning"
    elif 12 <= h < 17:
        greet = "Good afternoon"
    elif 17 <= h < 22:
        greet = "Good evening"
    else:
        greet = "Still up"
    f = round(wx.temp_c) if UNITS == "c" else round(wx.temp_c * 9 / 5 + 32)
    if wx.severe:
        cond = _alert_phrase(wx)
    elif wx.thunder:
        cond = "thunderstorms"
    elif wx.snow:
        cond = "snow coming down"
    elif wx.rain:
        cond = "rain"
    elif wx.obscuration:
        # Ahead of the cloud tiers on purpose: a smoke day is usually
        # cloudless, so deferring to cloud_frac would have the voice say
        # "clear skies" while the panel is brown.
        cond = OBSCURATION_PHRASE[wx.obscuration]
    elif wx.cloud_frac >= 0.85:
        cond = "overcast skies"
    elif wx.cloud_frac >= 0.55:
        cond = "mostly cloudy skies"
    elif wx.cloud_frac >= 0.25:
        cond = "partly cloudy skies"
    else:
        cond = "clear skies"
    ch = STYLE == "chicago"
    if not ch:
        parts = [f"{greet}. It's {f} degrees right now with {cond}."]
    elif wx.severe:
        parts = [f"{greet}, folks — there's {_alert_phrase(wx)}, "
                 "and I need you to take it seriously."]
    elif wx.thunder:
        parts = [f"{greet}, folks. We've got thunderstorms rolling "
                 f"through — {f} degrees out there."]
    elif wx.snow:
        parts = [f"{greet}, folks. Bundle up — snow coming down "
                 f"and {f} degrees."]
    elif wx.rain:
        parts = [f"{greet}, folks. Grab the umbrella — rain out there "
                 f"and {f} degrees."]
    elif cond == "clear skies" and 18 <= wx.temp_c <= 29:
        parts = [f"{greet}, folks. What a beauty out there — "
                 f"{f} degrees under clear skies."]
    else:
        parts = [f"{greet}, folks. {f} degrees right now with {cond}."]

    spd, unit_word, dirname, hi, mid, lo = _wind_words(wx)
    outof = f" out of the {dirname}" if dirname else ""
    if spd >= hi:
        parts.append(f"And the wind — {round(spd)} {unit_word}"
                     f"{outof}. Hold onto your hat."
                     if ch else
                     f"Properly windy, {round(spd)} {unit_word}{outof}.")
    elif spd >= mid:
        parts.append(f"Breezy out there{outof}." if ch else f"Breezy{outof}.")
    elif spd >= lo and dirname:
        parts.append(f"A gentle breeze{outof}." if ch
                     else f"A light breeze{outof}.")
    if (ch and spd >= lo and wx.wind_dir is not None
            and 45 <= wx.wind_dir <= 135):
        parts.append("Cooler by the lake, as always.")
    if wx.humidity >= 75 and wx.temp_c >= 24 and not wx.rain:
        parts.append("And it is muggy out there — a real steam bath."
                     if ch else "Muggy one.")

    # Peek at the hourly outlook first: if it will name precip odds,
    # the NWS period line stays out of the rain business — one
    # umbrella mention per broadcast
    hourly_pk = 0
    if hourly:
        _ahead = _hourly_today(hourly, now_local)
        if len(_ahead) >= 3:
            hourly_pk = max((r.get("prob") or 0) for _, r in _ahead)

    period = _forecast_period(forecast or [], now_local)
    if period:
        p = period
        name = p.get("name", "later")
        name = name[0].lower() + name[1:] if name else "later"
        temp = _forecast_temperature(p)
        prob = round(
            (p.get("probabilityOfPrecipitation") or {}).get("value") or 0
        )
        short = (p.get("shortForecast") or "").lower()
        kind = "storms" if "thunder" in short else (
            "snow" if "snow" in short else "rain")
        if temp is None:
            line = f"Looking ahead to {name}"
        elif p.get("isDaytime"):
            line = (f"We're heading for {temp} {name}" if ch
                    else f"Heading for a high of {temp} {name}")
        else:
            line = (f"We slide to {temp} {name}" if ch
                    else f"Down to {temp} {name}")
        if prob >= 30 and hourly_pk < 20:
            line += (f", with a {prob} percent chance of {kind} — "
                     "keep an eye on the sky" if ch
                     else f", with a {prob} percent chance of {kind}")
        parts.append(line + ".")

    # The shape of the rest of the day, from the hourly table
    if hourly:
        ahead = _hourly_today(hourly, now_local)
        if len(ahead) >= 3:  # enough of today left to talk about
            pk, when_words, kind = _today_peak(ahead)
            if pk >= 45:
                tool = ("Keep the shovel handy." if kind == "snow"
                        else "Keep the umbrella handy.")
                parts.append(f"Now here's the thing — a {pk} percent "
                             f"chance of {kind} around {when_words}. {tool}"
                             if ch else
                             f"{pk} percent chance of {kind} "
                             f"around {when_words}.")
            elif pk >= 20:
                parts.append(f"Just a slight chance of {kind} later — "
                             f"{pk} percent — nothing to change your "
                             "plans over." if ch else
                             f"A slight chance of {kind} "
                             f"later — {pk} percent at the most.")
            else:
                clouds = sum(r.get("cloud") or 0 for _, r in ahead) / len(ahead)
                span = "evening" if now_local.hour >= 17 else "day"
                if ch:
                    parts.append(f"And the rest of the {span}? Gorgeous. "
                                 "Not a cloud worth mentioning."
                                 if clouds < 30 else
                                 f"Staying dry the rest of the {span}.")
                else:
                    parts.append(f"Staying clear the rest of the {span}."
                                 if clouds < 30 else
                                 f"Dry the rest of the {span}.")
        else:  # late night: talk about tomorrow instead
            outlook = _tomorrow_outlook(hourly, now_local)
            if outlook:
                hi, pk, kind_w = outlook
                if ch and pk >= 30:
                    parts.append(f"And a heads up for tomorrow — "
                                 f"we go for {hi} with a {pk} percent "
                                 f"chance of {kind_w}.")
                elif ch:
                    parts.append(f"Tomorrow we go for {hi}, and it looks "
                                 "dry — get out and enjoy it.")
                elif pk >= 30:
                    parts.append(f"Tomorrow heads for {hi}, with a {pk} "
                                 f"percent chance of {kind_w}.")
                else:
                    parts.append(f"Tomorrow heads for {hi} and looks dry.")

    try:
        from astral import sun as _sun
        stimes = _sun.sun(OBSERVER, date=now_local.date(), tzinfo=TZ)
        sr, ss = stimes["sunrise"], stimes["sunset"]
        if sr <= now_local < ss:
            parts.append(f"Sunset tonight at "
                         f"{ss.hour % 12 or 12}:{ss.minute:02d} — "
                         "don't miss it." if ch else
                         f"Sun sets at {ss.hour % 12 or 12}:{ss.minute:02d}.")
        else:  # after sunset (or before dawn): moon now, sun next
            ecl = _eclipse_report_facts(now_local)
            if ecl is None:
                phase_name = _moon_phase_name(moon.phase(now_local.date()))
                article = "" if phase_name.startswith("a ") else "a "
                parts.append(f"And we've got {article}{phase_name} moon out "
                             "there tonight — worth a look." if ch else
                             f"The moon is {phase_name} tonight.")
            else:
                # The eclipse replaces the phase line rather than joining it.
                # During an eclipse the phase is always full, so saying both
                # would be "the moon is full, and also in Earth's shadow" —
                # the second clause makes the first noise.
                total = ecl["phase"] == "total"
                if ecl["at"] is None and total:
                    parts.append(
                        "And go outside, folks — there's a total lunar "
                        "eclipse happening right now, the whole moon inside "
                        "Earth's shadow. You do not get many of these." if ch
                        else "There is a total lunar eclipse right now — the "
                        "moon is entirely inside Earth's shadow.")
                elif ecl["at"] is None:
                    parts.append(
                        f"And go outside, folks — there's a partial lunar "
                        f"eclipse happening right now, Earth's shadow across "
                        f"{ecl['pct']} percent of the moon. You do not get "
                        "many of these." if ch else
                        f"There is a partial lunar eclipse right now — "
                        f"Earth's shadow covers {ecl['pct']} percent of "
                        "the moon.")
                else:
                    kind = "total" if total else "partial"
                    parts.append(
                        f"And mark this one, folks — a {kind} lunar eclipse "
                        f"starts at {ecl['at']} tonight. Worth staying up "
                        "for." if ch else
                        f"A {kind} lunar eclipse starts at {ecl['at']} "
                        "tonight.")
            if now_local >= ss:
                nxt = _sun.sun(OBSERVER,
                               date=(now_local + timedelta(days=1)).date(),
                               tzinfo=TZ)["sunrise"]
                when = "tomorrow"
            else:
                nxt, when = sr, "this morning"
            parts.append(
                f"Sunrise at {nxt.hour % 12 or 12}:{nxt.minute:02d} {when}.")
    except Exception:  # noqa: BLE001 - flavor, never fatal
        pass
    if ch:
        parts.append("And that's the picture, folks.")
    return " ".join(parts)


# How far ahead the report will announce an eclipse that has not started.
# Long enough that an evening report catches a late-night event, short
# enough that it stays news rather than a calendar.
ECLIPSE_HEADS_UP_H = 6

BAKE_CHECK_S = 60  # how often to see if the report's words changed


def _current_report_text(state: SkyState) -> str:
    return _compose_report(
        state.weather, state.forecast, datetime.now(TZ), state.hourly)


def _unacknowledged_alert_active(state: SkyState) -> bool:
    """Whether an alert still owns the display and the next user gesture."""
    return (
        (state.visual_alert is not None or state.weather.severe)
        and not state.alert_acked
    )


def _begin_report_request(state: SkyState, text: str) -> ReportRequest:
    """Claim one explicit report request and invalidate any older worker."""
    state.report_generation += 1
    request = ReportRequest(
        state.report_generation,
        state.view_generation,
        state.alert_generation,
        text,
    )
    state.report_request = request
    return request


def _report_request_is_current(
    state: SkyState,
    request: ReportRequest,
) -> bool:
    """A slow synthesis may act only for the exact view that requested it."""
    return (
        state.report_request == request
        and state.report_generation == request.generation
        and state.view_generation == request.view_generation
        and state.alert_generation == request.alert_generation
        and not state.shutting_down
        and not _unacknowledged_alert_active(state)
    )


def _report_status_is_current(
    state: SkyState,
    request: ReportRequest,
    label: str,
) -> bool:
    if not _report_request_is_current(state, request):
        return False
    if label != REPORT_READY:
        return True
    return (
        state.report_file is not None
        and state.report_text == request.text
        and _current_report_text(state) == request.text
    )


def _report_play_is_current(
    state: SkyState,
    request: ReportRequest,
    path: str,
) -> bool:
    """A cached PLAY remains bound to the exact resident/current words."""
    return (
        _report_request_is_current(state, request)
        and state.report_file == path
        and state.report_text == request.text
        and _current_report_text(state) == request.text
    )


def _finish_report_request(state: SkyState, request: ReportRequest) -> None:
    if state.report_request == request:
        state.report_request = None


def _live_report_statuses(state: SkyState) -> list[ReportStatus]:
    """Forget cards whose native whole-second timeout has certainly elapsed."""
    now = asyncio.get_running_loop().time()
    state.report_statuses[:] = [
        status for status in state.report_statuses
        if status.expires_at > now
    ]
    return list(state.report_statuses)


def _report_status_elements(status: ReportStatus, timeout: int) -> list:
    """Stable geometry for one native status generation and its retirement."""
    suffix = status.element_generation
    return [
        types.RectangleElement(
            id=f"reportbg{suffix}", type="rectangle",
            x=0, y=0, width=W, height=H,
            fill="solid", fill_colors=["#000000FF"], border_width=0,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
        types.TextElement(
            id=f"reporttx{suffix}", type="text",
            text=device_text(status.label), font="condensed",
            color="#E3B15DFF", align="center", x=36, y=8,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
    ]


def _retired_report_status_elements(statuses: list[ReportStatus]) -> list:
    return [
        element
        for status in statuses
        for element in _report_status_elements(status, timeout=1)
    ]


def _stale_report_statuses(state: SkyState) -> list[ReportStatus]:
    """Cards no longer owned by the exact still-current report request."""
    live = _live_report_statuses(state)
    request = state.report_request
    current_generation = (
        request.generation
        if request is not None and _report_request_is_current(state, request)
        else None
    )
    return [status for status in live if not (
        status.request_generation == current_generation
        or (
            status.terminal
            and status.view_generation == state.view_generation
            and status.alert_generation == state.alert_generation
            and not state.shutting_down
            and not _unacknowledged_alert_active(state)
        )
    )]


def _forget_report_statuses(
    state: SkyState,
    statuses: list[ReportStatus],
) -> None:
    retired = set(statuses)
    state.report_statuses[:] = [
        status for status in state.report_statuses if status not in retired
    ]


async def _post_report_retirement_locked(
    bb,
    state: SkyState,
    statuses: list[ReportStatus],
) -> bool:
    """Retire exact possibly-live ids; ``state.display_lock`` is held."""
    if not statuses:
        return True
    try:
        await asyncio.wait_for(
            bb.display_draw(types.DisplayElements(
                application_name=APP_NAME,
                priority=PRIORITY,
                elements=_retired_report_status_elements(statuses),
            )),
            REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            logger.debug("report status retirement yielded to device owner")
        else:
            logger.warning("report status retirement rejected: %s", exc)
        return False
    except TimeoutError:
        logger.warning("report status retirement exceeded %.1fs",
                       REPORT_IO_TIMEOUT_S)
        return False
    except Exception as exc:  # noqa: BLE001 - a lost response may have committed
        logger.warning("report status retirement failed: %s", exc)
        return False
    _forget_report_statuses(state, statuses)
    return True


async def _retire_report_statuses_serialized(
    bb,
    state: SkyState,
    request: ReportRequest | None = None,
) -> bool:
    """Exact-id retirement while owning the one display lane."""
    async with state.display_lock:
        live = _live_report_statuses(state)
        targets = (
            live if request is None else [
                status for status in live
                if status.request_generation == request.generation
            ]
        )
        return await _post_report_retirement_locked(bb, state, targets)


async def _retire_report_statuses(
    bb,
    state: SkyState,
    request: ReportRequest | None = None,
) -> bool:
    """Bound lock acquisition plus POST for an interactive retirement."""
    try:
        return await asyncio.wait_for(
            _retire_report_statuses_serialized(bb, state, request),
            REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        logger.warning("report status retirement missed %.1fs interaction bound",
                       REPORT_IO_TIMEOUT_S)
        return False


async def _show_report_status_serialized(
    bb,
    state: SkyState,
    request: ReportRequest,
    label: str,
    timeout: int = REPORT_STATUS_TIMEOUT_S,
) -> bool:
    """Replace prior report cards and draw truthful feedback immediately.

    The candidate enters the registry before POST because a transport timeout
    can mean the device committed it. A definite API refusal removes only the
    candidate; an accepted draw retires every older card in the same payload.
    """
    async with state.display_lock:
        if not _report_status_is_current(state, request, label):
            return False
        previous = _live_report_statuses(state)
        state.report_status_generation += 1
        started = asyncio.get_running_loop().time()
        candidate = ReportStatus(
            request.generation,
            state.report_status_generation,
            label,
            started + REPORT_IO_TIMEOUT_S + timeout,
            request.view_generation,
            request.alert_generation,
            label in {REPORT_READY, REPORT_AUDIO_BUSY, REPORT_AUDIO_ERROR},
        )
        state.report_statuses.append(candidate)
        payload = types.DisplayElements(
            application_name=APP_NAME,
            priority=PRIORITY,
            elements=[
                *_retired_report_status_elements(previous),
                *_report_status_elements(candidate, timeout),
            ],
        )
        try:
            await asyncio.wait_for(
                bb.display_draw(payload),
                REPORT_IO_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except exceptions.BusyBarAPIError as exc:
            # An API response is a definite rejection, unlike a lost transport
            # response. Keep older cards because their retirement also failed.
            _forget_report_statuses(state, [candidate])
            if _is_refusal(exc):
                logger.debug("report status yielded to device owner")
            else:
                logger.warning("report status rejected: %s", exc)
            return False
        except TimeoutError:
            logger.warning("report status draw exceeded %.1fs",
                           REPORT_IO_TIMEOUT_S)
            return False
        except Exception as exc:  # noqa: BLE001 - POST may have committed
            logger.warning("report status draw failed: %s", exc)
            return False

        committed = replace(
            candidate,
            expires_at=asyncio.get_running_loop().time() + timeout,
        )
        _forget_report_statuses(state, [*previous, candidate])
        state.report_statuses.append(committed)
        if not _report_status_is_current(state, request, label):
            # A selector/alert can change while the POST is in flight. Keep
            # physical ordering deterministic: retirement lands before the
            # queued newer view can acquire this same display lane.
            await _post_report_retirement_locked(bb, state, [committed])
            return False
        return True


async def _show_report_status(
    bb,
    state: SkyState,
    request: ReportRequest,
    label: str,
    timeout: int = REPORT_STATUS_TIMEOUT_S,
) -> bool:
    """Bound the complete acknowledgement, including display-lock waiting."""
    try:
        return await asyncio.wait_for(
            _show_report_status_serialized(
                bb, state, request, label, timeout),
            REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        # If cancellation interrupted POST, its pre-registered ids remain
        # quarantined until their native lease expires or a later draw retires
        # them. If it interrupted lock acquisition, no card was registered.
        logger.warning("report status missed %.1fs interaction bound",
                       REPORT_IO_TIMEOUT_S)
        return False


async def _finish_report_failure(
    bb,
    state: SkyState,
    request: ReportRequest,
    label: str = REPORT_AUDIO_ERROR,
) -> None:
    """Atomically retire PREPARING and, when still relevant, show the error."""
    if _report_request_is_current(state, request):
        await _show_report_status(bb, state, request, label)
    else:
        await _retire_report_statuses(bb, state, request)
    _finish_report_request(state, request)


async def _remove_report_asset(
    bb,
    state: SkyState,
    name: str,
    *,
    force: bool = False,
) -> bool:
    """Remove one exact immutable take, deferring paths still in use."""
    if not force and name in {state.report_file, state.audio_path}:
        return False
    state.report_files[:] = [path for path in state.report_files if path != name]
    try:
        await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{name}")
    except exceptions.BusyBarAPIError as exc:
        if getattr(exc, "status_code", None) != 404:
            state.report_retire.add(name)
            return False
    except Exception:  # noqa: BLE001 - retry on the next successful generation
        state.report_retire.add(name)
        return False
    state.report_retire.discard(name)
    return True


async def _retry_report_retirements(bb, state: SkyState) -> None:
    for name in list(state.report_retire):
        if name in {state.report_file, state.audio_path}:
            continue
        await _remove_report_asset(bb, state, name)


REPORT_FILE_RE = re.compile(
    r"^(?P<base>report_[0-9a-f]{12})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$"
)


def report_asset_name(
    text: str,
    *,
    voice: str | None = None,
    repair: int = 0,
) -> str:
    """Stable immutable path for exact words in the configured voice."""
    if not 0 <= repair <= 0xff:
        raise ValueError(f"invalid report repair generation: {repair}")
    configured_voice = REPORT_VOICE if voice is None else voice
    digest = hashlib.sha1(
        f"{configured_voice}\n{text}".encode()
    ).hexdigest()[:12]
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    name = f"report_{digest}{suffix}.snd"
    if len(name.encode("ascii")) > 31:
        raise ValueError(f"report asset filename exceeds device limit: {name}")
    return name


def _report_file_identity(name: str) -> tuple[str, int] | None:
    match = REPORT_FILE_RE.fullmatch(name)
    if match is None:
        return None
    return f"{match.group('base')}.snd", int(match.group("repair") or "0", 16)


def _current_report_asset_name(state: SkyState, text: str) -> str:
    base = report_asset_name(text)
    return report_asset_name(
        text, repair=state.report_repairs.get(base, 0))


def _mark_report_unplayable(state: SkyState, name: str, text: str) -> None:
    """Quarantine a definite PLAY-404 and advance its immutable successor."""
    expected_base = report_asset_name(text)
    identity = _report_file_identity(name)
    repair = identity[1] if identity is not None and identity[0] == expected_base else 0
    next_repair = max(state.report_repairs.get(expected_base, 0), repair) + 1
    if next_repair > 0xff:
        raise RuntimeError(f"report repair generations exhausted for {expected_base}")
    state.report_repairs[expected_base] = next_repair
    state.report_retire.add(name)
    if state.report_file == name:
        state.report_file = None
        state.report_text = None


async def _publish_report_take(
    bb,
    state: SkyState,
    text: str,
    fname: str,
) -> None:
    """Publish one take and keep current, predecessor, and a live PLAY path."""
    prev = state.report_file
    state.report_text = text
    state.report_file = fname
    for name in (prev, fname):
        if name and name not in state.report_files:
            state.report_files.append(name)

    # A corrupt predecessor is safe to retire only after its immutable repair
    # successor is resident. Failed deletion must not block that successor.
    await _retry_report_retirements(bb, state)

    protected = {state.report_file, prev, state.audio_path}
    protected.discard(None)
    limit = 3 if len(protected) == 3 else 2
    while len(state.report_files) > limit:
        stale = next((
            name for name in state.report_files
            if name not in protected
        ), None)
        if stale is None:
            break
        if not await _remove_report_asset(bb, state, stale):
            break


async def _adopt_report_take(
    bb,
    state: SkyState,
    text: str,
) -> str | None:
    """Adopt the newest resident deterministic take before synthesising."""
    async with state.report_asset_lock:
        if state.report_file is not None and state.report_text == text:
            return state.report_file
        base = report_asset_name(text)
        try:
            entries = (await bb.storage_list(
                f"/ext/user_assets/{APP_NAME}")).list
        except Exception as exc:  # noqa: BLE001 - cold cache is ordinary
            logger.debug("report cache scan failed: %s", exc)
            return None

        candidates: list[tuple[int, str, int]] = []
        for entry in entries:
            identity = _report_file_identity(getattr(entry, "name", ""))
            if identity is None:
                continue
            name = entry.name
            if name not in state.report_files:
                state.report_files.append(name)
            if identity[0] != base:
                continue
            repair = identity[1]
            size = int(getattr(entry, "size", 0) or 0)
            expected = state.report_expected_sizes.get(name)
            if size <= 0 or (expected is not None and size != expected):
                state.report_repairs[base] = max(
                    state.report_repairs.get(base, 0), repair + 1)
                state.report_retire.add(name)
                continue
            candidates.append((repair, name, size))

        minimum = state.report_repairs.get(base, 0)
        usable = [item for item in candidates if item[0] >= minimum]
        if not usable:
            return None
        repair, fname, _ = max(usable)
        state.report_repairs[base] = repair
        state.report_retire.discard(fname)
        state.report_expected_sizes.pop(fname, None)
        for other_repair, other, _ in candidates:
            if other != fname and other_repair <= repair:
                state.report_retire.add(other)
        await _publish_report_take(bb, state, text, fname)
        logger.info("adopted resident report take %s", fname)
        return fname


async def _ensure_report_take(
    bb,
    state: SkyState,
    text: str,
    snd: bytes,
) -> tuple[str, bool]:
    """Publish one immutable take and retain a bounded safe predecessor."""
    async with state.report_asset_lock:
        if state.report_file is not None and state.report_text == text:
            return state.report_file, False
        fname = _current_report_asset_name(state, text)
        if fname in state.report_retire:
            # This exact name has an unresolved lost-upload result. Never
            # overwrite it; resolve/remove it before attempting the same path.
            await _remove_report_asset(bb, state, fname, force=True)
            if fname in state.report_retire:
                raise RuntimeError(
                    f"ambiguous report upload is still unresolved: {fname}")
        # A lost upload response can mean the immutable file committed. Track
        # its exact expected size before POST so a later scan can safely adopt.
        state.report_retire.add(fname)
        state.report_expected_sizes[fname] = len(snd)
        try:
            await bb.assets_upload(APP_NAME, fname, snd)
        except exceptions.BusyBarAPIError:
            state.report_retire.discard(fname)  # definite API rejection
            state.report_expected_sizes.pop(fname, None)
            raise
        state.report_retire.discard(fname)
        state.report_expected_sizes.pop(fname, None)
        await _publish_report_take(bb, state, text, fname)
        return fname, True


async def _finish_report_ready(
    bb,
    state: SkyState,
    request: ReportRequest,
) -> None:
    if _report_status_is_current(state, request, REPORT_READY):
        await _show_report_status(bb, state, request, REPORT_READY)
    else:
        await _retire_report_statuses(bb, state, request)
    _finish_report_request(state, request)


async def _prepare_report_take(bb, state: SkyState, text: str) -> str:
    """Adopt or fully prepare one exact take (background/CLI path only)."""
    resident = await _adopt_report_take(bb, state, text)
    if resident is not None:
        return resident
    snd = await synth_snd_async(text)
    fname, _ = await _ensure_report_take(bb, state, text, snd)
    return fname


async def _report_prepare_worker(bb, state: SkyState) -> None:
    """One managed synth+upload lane, with the newest requested text queued."""
    owner = asyncio.current_task()
    try:
        while state.report_prepare_text is not None:
            text = state.report_prepare_text
            try:
                await _prepare_report_take(bb, state, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("report bake failed: %s", exc)
                request = state.report_request
                if request is not None and request.text == text:
                    await _finish_report_failure(bb, state, request)
            else:
                logger.info("report baked: %r", text)
                request = state.report_request
                if request is not None and request.text == text:
                    latest = _current_report_text(state)
                    if latest != text and _report_request_is_current(
                            state, request):
                        # Weather changed while the Pi was speaking. Keep the
                        # same fenced user intent, prepare only the newest
                        # truthful line, and never post a non-actionable READY.
                        state.report_request = replace(request, text=latest)
                        state.report_prepare_pending = latest
                        state.report_prepare_pending_priority = True
                    else:
                        await _finish_report_ready(bb, state, request)

            pending = state.report_prepare_pending
            state.report_prepare_pending = None
            state.report_prepare_pending_priority = False
            if pending is None:
                break
            state.report_prepare_text = pending
    finally:
        if state.report_prepare_task is owner:
            state.report_prepare_task = None
            state.report_prepare_text = None
        request = state.report_request
        if request is not None:
            # Cancellation/shutdown cannot leave a logical intent alive. Any
            # accepted card still has only its native three-second lease.
            _finish_report_request(state, request)


def _queue_report_prepare(
    bb,
    state: SkyState,
    text: str,
    *,
    priority: bool,
    force: bool = False,
) -> asyncio.Task:
    """Wake or reprioritize the single background report preparation lane."""
    task = state.report_prepare_task
    if task is not None and not task.done():
        if state.report_prepare_text == text and not force:
            if priority:
                # This exact user request makes unrelated background work that
                # had been queued behind it obsolete.
                state.report_prepare_pending = None
                state.report_prepare_pending_priority = False
            return task
        if priority or not state.report_prepare_pending_priority:
            state.report_prepare_pending = text
            state.report_prepare_pending_priority = priority
        return task

    state.report_prepare_text = text
    state.report_prepare_pending = None
    state.report_prepare_pending_priority = False
    task = spawn_owned(state, _report_prepare_worker(bb, state))
    state.report_prepare_task = task
    return task


async def bake_report(bb, state: SkyState) -> None:
    """Keep a freshly voiced report waiting on the bar. Whenever live
    data changes the report's WORDS, re-synthesize in the background —
    a double press then plays a file that already exists (Kokoro on
    the Pi runs ~1x realtime, too slow to synth at press time)."""
    # Deterministic report paths survive the transient-asset startup sweep and
    # are adopted before synthesis; old timestamp generations are still swept.
    await asyncio.sleep(10)  # let the first observations land
    while True:
        try:
            if state.forecast or state.hourly:
                text = _current_report_text(state)
                if text != state.report_text:
                    _queue_report_prepare(
                        bb, state, text, priority=False)
            await asyncio.sleep(BAKE_CHECK_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("report bake failed: %s", exc)
            await asyncio.sleep(BAKE_CHECK_S)


async def _settle_report_audio(bb, state: SkyState) -> bool:
    """Resolve an ambiguous older PLAY within the interaction budget."""
    if not state.audio_stop_pending:
        return True
    generation = state.audio_generation
    try:
        await asyncio.wait_for(
            stop_audio(bb, state, generation), REPORT_IO_TIMEOUT_S)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - BUSY is the safe outcome
        logger.warning("report audio STOP did not settle: %s", exc)
    return not state.audio_stop_pending


async def weather_report(bb, state: SkyState) -> None:
    """Play only a resident report; prepare a cache miss in the background.

    A miss posts PREPARING, prioritizes one managed synth+upload worker, and
    returns. Completion posts START TWICE for the exact still-current view;
    it never auto-plays thirty seconds after the initiating button press.
    """
    text = _current_report_text(state)
    request = _begin_report_request(state, text)
    if not _report_request_is_current(state, request):
        _finish_report_request(state, request)
        return

    cached = state.report_file if state.report_text == text else None
    repair = False
    if cached:  # the baked take: instant playback
        logger.info("double press — playing baked report: %r",
                    state.report_text)
        try:
            if not await _retire_report_statuses(bb, state):
                _finish_report_request(state, request)
                return
            if not await _settle_report_audio(bb, state):
                await _finish_report_failure(
                    bb, state, request, REPORT_AUDIO_BUSY)
                return
            played = await asyncio.wait_for(
                _play_audio(
                    bb,
                    state,
                    cached,
                    "report",
                    lambda: _report_play_is_current(
                        state, request, cached),
                ),
                REPORT_IO_TIMEOUT_S,
            )
            if played:
                _finish_report_request(state, request)
                return
            if (
                _report_request_is_current(state, request)
                and not _report_play_is_current(state, request, cached)
            ):
                # The report changed while retirement/PLAY was in flight.
                # Re-enter as a cache miss for the newest exact text; never
                # describe a stale take as merely AUDIO BUSY.
                _finish_report_request(state, request)
                await weather_report(bb, state)
                return
            if _report_request_is_current(state, request):
                await _finish_report_failure(
                    bb, state, request, REPORT_AUDIO_BUSY)
            else:
                _finish_report_request(state, request)
            return
        except asyncio.CancelledError:
            _finish_report_request(state, request)
            raise
        except exceptions.BusyBarAPIError as exc:
            if _is_refusal(exc):
                logger.debug("baked report yielded to the active device session")
                await _finish_report_failure(
                    bb, state, request, REPORT_AUDIO_BUSY)
                return
            if getattr(exc, "status_code", None) != 404:
                logger.warning("baked report failed: %s", exc)
                await _settle_report_audio(bb, state)
                await _finish_report_failure(bb, state, request)
                return
            # A definite missing/unplayable path never opened. Invalidate it
            # before queuing a new immutable generation.
            _mark_report_unplayable(state, cached, text)
            repair = True
        except Exception as exc:  # noqa: BLE001
            # A lost/timed-out PLAY response may already be audible. Ordered
            # STOP settles before the error card; never synthesize another take.
            logger.warning("baked report failed: %s", exc)
            await _settle_report_audio(bb, state)
            await _finish_report_failure(bb, state, request)
            return

    try:
        acknowledged = await _show_report_status(
            bb, state, request, REPORT_PREPARING,
        )
    except asyncio.CancelledError:
        _finish_report_request(state, request)
        raise
    if not acknowledged:
        _finish_report_request(state, request)
        return

    # Cache adoption/publication can win while the status POST is in flight.
    # It is still a miss interaction: acknowledge readiness, never autoplay.
    if state.report_file is not None and state.report_text == text:
        await _finish_report_ready(bb, state, request)
        return

    logger.info("double press — preparing weather report: %r", text)
    _queue_report_prepare(
        bb, state, text, priority=True, force=repair)


async def listen_buttons(bb, state: SkyState) -> None:
    """Coalesce one status message into one owned user intent.

    START is ours after an explicit OFF slider event.  Because the status
    stream is delta-only, reconnecting while the slider is already OFF leaves
    its position unknown; that case additionally requires a committed
    Skystrip view and a NOT_STARTED Busy snapshot.  Wheel gestures are app
    controls, and any available gesture first acknowledges an alert without
    also navigating the view underneath it.
    """
    backoff = 1.0
    last_press = 0.0
    pending: asyncio.Task | None = None

    def cancel_pending() -> None:
        nonlocal pending
        if pending is not None:
            pending.cancel()
            pending = None

    def invalidate_switch_evidence() -> None:
        cancel_pending()
        state.switch_position = None
        state.switch_generation += 1

    async def single_press_later(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        # Fires if no second press arrives inside the double window
        await asyncio.sleep(DOUBLE_PRESS_S)
        if (
            state.shutting_down
            or _unacknowledged_alert_active(state)
            or not await _start_is_owned(
                bb, state, switch_generation, switch_position)
        ):
            return
        if pending is asyncio.current_task():
            pending = None
        state.scene_idx = (state.scene_idx + 1) % len(ENABLED_SCENES)
        state.view_generation += 1
        save_scene_idx(state.scene_idx)
        state.scene_change.set()
        logger.info("START — scene: %s", state.scene)

    async def acknowledge_unknown_start(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        if not await _start_is_owned(
            bb, state, switch_generation, switch_position
        ):
            return
        if pending is asyncio.current_task():
            pending = None
        if _unacknowledged_alert_active(state):
            await acknowledge_alert(bb, state, "START")

    async def report_after_unknown_start(
        switch_generation: int,
        switch_position: str | None,
    ) -> None:
        nonlocal pending
        if not await _start_is_owned(
            bb, state, switch_generation, switch_position
        ):
            return
        if pending is asyncio.current_task():
            pending = None
        if not _unacknowledged_alert_active(state):
            spawn_owned(state, weather_report(bb, state))

    try:
        while True:
            try:
                connected_at = asyncio.get_running_loop().time()
                async for message in bb.stream_status_ws():
                    backoff = 1.0
                    if not isinstance(message, dict):
                        continue
                    updates = message.get("updates", [])
                    if not isinstance(updates, list):
                        continue

                    # Preserve input order inside one protobuf State message:
                    # a selector event before START can establish ownership,
                    # but one after START must not retroactively claim it.
                    # Encoder deltas are one detent per count and collapse to
                    # one final readout draw.
                    delta = 0
                    ok_pressed = False
                    start_pressed = False
                    start_switch_generation: int | None = None
                    start_switch_position: str | None = None
                    for update in updates:
                        if not isinstance(update, dict):
                            continue
                        inp = update.get("input") or {}
                        if "switch_event" in inp:
                            # Every selector event supersedes ownership captured
                            # by an earlier START, including OFF arriving after
                            # the press and malformed/unknown enum values.
                            cancel_pending()
                            state.switch_generation += 1
                            state.switch_position = _switch_position(update)
                        delta += _encoder_delta(update)
                        ok_pressed = ok_pressed or _is_ok_press(update)
                        is_start = _is_start_press(update)
                        if is_start and not start_pressed:
                            start_switch_generation = state.switch_generation
                            start_switch_position = state.switch_position
                        start_pressed = start_pressed or is_start

                    active_alert = _unacknowledged_alert_active(state)
                    if active_alert and (delta != 0 or ok_pressed):
                        cancel_pending()
                        reason = "wheel" if delta else "wheel click"
                        await acknowledge_alert(bb, state, reason)
                        continue
                    if active_alert and start_pressed:
                        assert start_switch_generation is not None
                        switch_generation = start_switch_generation
                        position = start_switch_position
                        if (
                            state.switch_generation != switch_generation
                            or state.switch_position != position
                        ):
                            continue
                        if position == "OFF":
                            cancel_pending()
                            await acknowledge_alert(bb, state, "START")
                        elif (
                            position is None
                            and _has_committed_start_view(state)
                        ):
                            cancel_pending()
                            pending = spawn_owned(
                                state,
                                acknowledge_unknown_start(
                                    switch_generation, position),
                            )
                        # An alert gesture is consumed even when ownership
                        # cannot be established; it must never navigate below.
                        continue

                    meta = state.timeline_meta
                    if delta and meta is not None:
                        if state.scrub_slot is None:
                            here = datetime.now(TZ)
                            state.scrub_slot = max(0, min(
                                TIMELINE_SLOTS - 1,
                                round((here - meta["start"]).total_seconds()
                                      / TIMELINE_STEP_S),
                            ))
                        state.scrub_slot = max(
                            0,
                            min(TIMELINE_SLOTS - 1, state.scrub_slot + delta),
                        )
                        state.scrub_touched = asyncio.get_running_loop().time()
                        state.view_generation += 1
                        state.revealed = False

                    if ok_pressed and state.scrub_slot is not None:
                        state.scrub_slot = None
                        state.revealed = False
                        state.view_generation += 1
                        logger.info("wheel click — back to now")
                        try:
                            await draw_scrub_readout(bb, state, "NOW", timeout=1)
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("NOW readout yielded: %s", exc)
                    elif delta and meta is not None and state.scrub_slot is not None:
                        try:
                            await draw_scrub_readout(
                                bb,
                                state,
                                _slot_label(meta, state.scrub_slot),
                            )
                        except Exception as exc:  # noqa: BLE001
                            logger.debug("time readout yielded: %s", exc)

                    if not start_pressed:
                        continue
                    assert start_switch_generation is not None
                    switch_generation = start_switch_generation
                    position = start_switch_position
                    if (
                        state.switch_generation != switch_generation
                        or state.switch_position != position
                    ):
                        continue
                    if position not in (None, "OFF"):
                        continue
                    if position is None and not _has_committed_start_view(state):
                        continue
                    now = asyncio.get_running_loop().time()
                    if now - last_press < START_BOUNCE_S:
                        continue
                    last_press = now
                    if pending is not None and not pending.done():
                        pending.cancel()
                        pending = None
                        if position == "OFF":
                            spawn_owned(state, weather_report(bb, state))
                        else:
                            pending = spawn_owned(
                                state,
                                report_after_unknown_start(
                                    switch_generation, position),
                            )
                    else:
                        pending = spawn_owned(
                            state,
                            single_press_later(switch_generation, position),
                        )

                invalidate_switch_evidence()
                # A clean close ends the loop without raising; a short session
                # means the bar isn't ready, so back off rather than spin.
                if asyncio.get_running_loop().time() - connected_at < 5.0:
                    logger.warning("button stream closed immediately, backing off")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 60)
                else:
                    logger.info("button stream closed cleanly, reconnecting")
                    backoff = 1.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                invalidate_switch_evidence()
                logger.warning("button stream dropped (%s), retrying", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
    finally:
        cancel_pending()


async def listen_lightning(state: SkyState) -> None:
    """Queue ambient flashes only; alerts and sirens come solely from CAP."""
    import websockets

    endpoint = LIGHTNING_WS
    if endpoint is None:
        return
    backoff = 1.0
    invalid_frames = 0
    while True:
        try:
            async with websockets.connect(
                endpoint,
                open_timeout=10,
                max_size=LIGHTNING_FRAME_MAX_BYTES,
                max_queue=LIGHTNING_WS_MAX_QUEUE,
                logger=LIGHTNING_TRANSPORT_LOGGER,
            ) as ws:
                await ws.send(LIGHTNING_SUBSCRIPTION)
                logger.info("lightning: connected to configured secure endpoint")
                connected_at = asyncio.get_running_loop().time()
                async for raw in ws:
                    backoff = 1.0  # a delivered frame proves the session works
                    try:
                        loop = asyncio.get_running_loop()
                        strike = _parse_lightning_strike(
                            raw,
                            wall_now=time.time(),
                            monotonic_now=loop.time(),
                        )
                        dist = _km(
                            LAT, LON, strike.latitude, strike.longitude
                        )
                    except Exception as exc:  # noqa: BLE001 - bad frame: drop
                        invalid_frames += 1
                        if invalid_frames == 1 or invalid_frames % 100 == 0:
                            # Never log the payload or exception text: either
                            # may contain source-specific or sensitive fields.
                            logger.warning(
                                "lightning: discarded invalid frame (%s; %d total)",
                                type(exc).__name__,
                                invalid_frames,
                            )
                        continue
                    observed_at = strike.observed_at
                    if dist <= STRIKE_NEAR_KM:
                        _enqueue_flash(
                            state.flash_queue, dist,
                            observed_at=observed_at,
                        )
                    elif dist <= STRIKE_RADIUS_KM:
                        # far flicker on the horizon: rare by design
                        if observed_at - getattr(
                            listen_lightning, "_far_at", 0.0,
                        ) > FAR_FLASH_GAP_S:
                            listen_lightning.__dict__["_far_at"] = observed_at
                            _enqueue_flash(
                                state.flash_queue, dist,
                                observed_at=observed_at,
                            )
            # A protocol-legal close ends `async for` without raising. Without
            # this, a server that accepts then closes gives a reconnect hot
            # loop — thousands per second at the configured relay/source.
            if asyncio.get_running_loop().time() - connected_at < 5.0:
                logger.warning("lightning: endpoint closed immediately, backing off")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                logger.info("lightning: endpoint closed cleanly, reconnecting")
                backoff = 1.0
                # 370 clean-close reconnects were observed in one host's logs.
                # Blitzortung is community-run and non-commercial (see
                # NOTICE.md); a floor under the rate costs nothing here and is
                # the difference between reconnecting and hammering.
                await asyncio.sleep(
                    random.uniform(RECONNECT_FLOOR_S, RECONNECT_FLOOR_S * 2))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Exception messages from a WebSocket library may echo the URL.
            # Log only the class so query tokens and userinfo stay private.
            logger.warning(
                "lightning: endpoint dropped (%s), retrying",
                type(exc).__name__,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


# --- device side ----------------------------------------------------------


def _weather_led(wx: WeatherState) -> str | None:
    """Top-LED heartbeat color for notable weather; None = stay quiet.
    Severe warnings are the alarm task's job, not the heartbeat's."""
    if wx.severe:
        return None
    if wx.snow:
        return "#AADDFFFF"
    if wx.thunder:
        return "#FFAA33FF"
    if wx.rain:
        return "#3377EEFF"
    if wx.visibility_m < 5000:
        return "#8877AAFF"
    return None


def _led_ping_payload(color: str) -> types.DisplayElements:
    """An invisible draw whose only job is one top-strip blink."""
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        led_notification_color=color,
        elements=[types.RectangleElement(
            id="ledping", type="rectangle", x=0, y=0, width=1, height=1,
            fill="solid", fill_colors=["#00000000"], border_width=0,
            display=types.DisplayName.FRONT, timeout=1,
        )],
    )


def _ambient_mood(state: SkyState) -> tuple[int, int, int]:
    """The top strip breathes the sky's current mood: sky color by sun
    position, weather-tinted, amber while time-traveling, dark during
    alarms so the red blinks own the strip."""
    if state.weather.severe and not state.alert_acked:
        return (0, 0, 0)
    if state.scrub_slot is not None:
        return _rgb_int(c * AMBIENT_LEVEL for c in (224, 160, 70))
    now = datetime.now(timezone.utc)
    elev = elevation(OBSERVER, now)
    wx = state.weather
    horizon, zenith = _sky_colors(elev)
    c = _lerp_rgb(zenith, horizon, 0.35)
    if wx.stormy:
        c = _lerp_rgb(c, STORM_ZENITH, 0.85)
    elif wx.snow:
        c = _lerp_rgb(c, (170, 175, 185), 0.6)
    elif wx.rain:
        c = _lerp_rgb(c, (30, 45, 80), 0.5)
    elif wx.cloud_frac > 0.3:
        lum = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
        c = _lerp_rgb(c, (lum, lum, lum), 0.5 * wx.cloud_frac)
    scene = state.scene
    if scene == "skyline":
        c = _lerp_rgb(c, (255, 190, 90), 0.15)   # city glow
    elif scene == "lakefront":
        c = _lerp_rgb(c, (40, 120, 130), 0.20)   # lake teal
    elif scene == "forest":
        c = _lerp_rgb(c, (46, 110, 44), 0.22)    # deep woods green
    elif scene == "grove":
        c = _lerp_rgb(c, (96, 120, 36), 0.20)    # broadleaf green-gold
    elif scene == "backroads":
        c = _lerp_rgb(c, (120, 104, 76), 0.15)   # headlights on asphalt
    return (
        max(0, min(255, int(c[0] * AMBIENT_LEVEL))),
        max(0, min(255, int(c[1] * AMBIENT_LEVEL))),
        max(0, min(255, int(c[2] * AMBIENT_LEVEL))),
    )


async def ambient_lights(bb, state: SkyState) -> None:
    """Steady mood color on the top strip via the firmware CLI (telnet
    over the USB link) — the StaticColor preset HTTP never exposed."""
    last = None
    warned = False
    while True:
        try:
            r, g, b = _ambient_mood(state)
            q = (r // 4, g // 4, b // 4)  # quantize: only send real changes
            if q != last:
                await bb.usb.send_command(
                    "status_lights", str(r), str(g), str(b))
                last = q
                warned = False
                logger.info("ambient: (%d,%d,%d)", r, g, b)
            await asyncio.sleep(AMBIENT_PERIOD_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - mood is best-effort
            if not warned:
                # The firmware CLI only answers on the USB interface —
                # over Wi-Fi the strip stays dark. Alarm blinks are HTTP
                # and unaffected.
                logger.info("ambient: CLI unreachable (USB-only feature); "
                            "standing down: %s", exc)
                warned = True
            last = None
            await asyncio.sleep(300)


_SIREN_PCM_CACHE: bytes | None = None
SIREN_FILES = re.compile(
    r"^siren_(?P<digest>[0-9a-f]{16})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$"
)


def siren_pcm() -> bytes:
    """Reproducible s16le/44.1 kHz alarm tone; no untracked device asset.

    The two-tone sweep is intentionally generated from code and content-hashed
    before upload.  It is an attention signal, not a civil-defense siren or a
    copy of a third-party recording.
    """
    global _SIREN_PCM_CACHE
    if _SIREN_PCM_CACHE is not None:
        return _SIREN_PCM_CACHE
    rate = 44_100
    count = rate * SIREN_SECONDS
    out = bytearray(count * 2)
    phase = 0.0
    two_pi = 2.0 * math.pi
    fade_samples = int(rate * 0.025)
    for index in range(count):
        # Continuous-phase 620–940 Hz triangle sweep every 1.2 seconds.
        sweep = (index / rate / 1.2) % 1.0
        triangle = 1.0 - abs(2.0 * sweep - 1.0)
        frequency = 620.0 + 320.0 * triangle
        phase = (phase + two_pi * frequency / rate) % two_pi
        edge = min(1.0, index / fade_samples, (count - 1 - index) / fade_samples)
        # A gentle 4 Hz tremolo remains unmistakable without clipping.
        tremolo = 0.74 + 0.26 * math.sin(two_pi * 4.0 * index / rate) ** 2
        sample = int(0.26 * 32767 * edge * tremolo * math.sin(phase))
        struct.pack_into("<h", out, index * 2, sample)
    _SIREN_PCM_CACHE = bytes(out)
    return _SIREN_PCM_CACHE


def _siren_name(digest: str, repair: int = 0) -> str:
    if not 0 <= repair <= 0xFF:
        raise RuntimeError("extreme-weather siren repair generations exhausted")
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    return f"siren_{digest}{suffix}.snd"


def _siren_identity(name: str) -> tuple[str, int] | None:
    match = SIREN_FILES.fullmatch(name)
    if match is None:
        return None
    return match.group("digest"), int(match.group("repair") or "0", 16)


_storage_file_matches = storage_file_matches


def mark_siren_unplayable(state: SkyState, name: str) -> None:
    """Quarantine a definite PLAY-404 and request an immutable successor."""
    identity = _siren_identity(name)
    if identity is None:
        logger.error("cannot repair unrecognised siren asset %r", name)
        state.siren_file = None
        state.siren_asset_changed.set()
        return
    _digest, repair = identity
    if repair >= 0xFF:
        logger.critical("extreme-weather siren repair generations exhausted")
        state.siren_file = None
        state.siren_asset_changed.set()
        return
    state.siren_retire.add(name)
    state.siren_ambiguous.discard(name)
    state.siren_repair = max(state.siren_repair, repair + 1)
    if state.siren_file == name:
        state.siren_file = None
    state.siren_asset_changed.set()


async def _siren_listing(bb) -> list[types.StorageListElement] | None:
    try:
        return (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
    except Exception as exc:  # noqa: BLE001 - caller preserves ambiguity
        logger.debug("could not inspect siren residency: %s", exc)
        return None


def _defer_siren_retirement(state: SkyState, names: set[str]) -> None:
    """Bound old files without deleting one a prior process may still play."""
    if not names:
        return
    due = asyncio.get_running_loop().time() + SIREN_RETIRE_GRACE_S
    state.siren_retire.update(names)
    for name in names:
        state.siren_retire_after.setdefault(name, due)


async def retire_siren_assets(bb, state: SkyState) -> None:
    """Best-effort retirement after one complete possible playback window."""
    if state.siren_file is None:
        return
    now = asyncio.get_running_loop().time()
    for stale in list(state.siren_retire):
        if stale == state.siren_file:
            state.siren_retire.discard(stale)
            state.siren_retire_after.pop(stale, None)
            continue
        due = state.siren_retire_after.setdefault(
            stale, now + SIREN_RETIRE_GRACE_S)
        if now < due:
            continue
        try:
            await bb.storage_remove(
                f"/ext/user_assets/{APP_NAME}/{stale}")
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) != 404:
                logger.debug("siren retirement deferred for %s: %s", stale, exc)
                state.siren_retire_after[stale] = (
                    now + SIREN_PROVISION_RETRY_S)
                continue
        except Exception as exc:  # noqa: BLE001 - successor is ready
            logger.debug("siren retirement deferred for %s: %s", stale, exc)
            state.siren_retire_after[stale] = now + SIREN_PROVISION_RETRY_S
            continue
        state.siren_retire.discard(stale)
        state.siren_ambiguous.discard(stale)
        state.siren_retire_after.pop(stale, None)


async def audit_siren_assets(bb, state: SkyState) -> None:
    """Discover obsolete generations even if startup listing was unavailable."""
    if state.siren_file is None:
        return
    files = await _siren_listing(bb)
    if files is None:
        return
    owned = {
        entry.name for entry in files
        if _siren_identity(getattr(entry, "name", "")) is not None
        and entry.name != state.siren_file
    }
    _defer_siren_retirement(state, owned)


async def ensure_siren_asset(bb, state: SkyState) -> str | None:
    """Adopt or install one verified immutable siren generation.

    A filename match is insufficient: interrupted writes can leave a partial
    file, while PLAY uses 404 for both missing and unplayable content.  Exact
    size is the safe startup admission check; a definite PLAY-404 advances to
    a discoverable ``_rNN`` path rather than overwriting firmware-cached data.
    """
    if state.siren_file is not None:
        return state.siren_file
    async with state.siren_asset_lock:
        if state.siren_file is not None:
            return state.siren_file

        blob = siren_pcm()
        expected_size = len(blob)
        digest = hashlib.sha256(blob).hexdigest()[:16]
        files = await _siren_listing(bb)
        entries: dict[int, types.StorageListElement] = {}
        owned_names: set[str] = set()
        if files is not None:
            for entry in files:
                identity = _siren_identity(getattr(entry, "name", ""))
                if identity is None:
                    continue
                entry_digest, repair = identity
                owned_names.add(entry.name)
                if entry_digest == digest:
                    entries[repair] = entry

            # Resolve uploads whose response was lost before minting another
            # path.  An absent target is safe to retry; an exact resident file
            # is safe to adopt; a wrong-sized one is poison and advances.
            for ambiguous in list(state.siren_ambiguous):
                identity = _siren_identity(ambiguous)
                if identity is None or identity[0] != digest:
                    state.siren_ambiguous.discard(ambiguous)
                    state.siren_retire.add(ambiguous)
                    continue
                repair = identity[1]
                entry = entries.get(repair)
                if entry is None:
                    state.siren_ambiguous.discard(ambiguous)
                elif _storage_file_matches(entry, expected_size):
                    state.siren_ambiguous.discard(ambiguous)
                else:
                    state.siren_ambiguous.discard(ambiguous)
                    state.siren_retire.add(ambiguous)
                    state.siren_repair = max(state.siren_repair, repair + 1)
        elif state.siren_ambiguous:
            # Do not issue another write while the first may have committed
            # and storage cannot tell us.  The lifetime maintainer retries the
            # listing, so a network outage creates at most one ambiguous path.
            return None

        candidates = [
            (repair, entry)
            for repair, entry in entries.items()
            if repair >= state.siren_repair
            and entry.name not in state.siren_retire
            and _storage_file_matches(entry, expected_size)
        ]
        filename: str | None = None
        chosen_repair = state.siren_repair
        if candidates:
            chosen_repair, chosen = max(candidates, key=lambda item: item[0])
            filename = chosen.name
        else:
            chosen_repair = state.siren_repair
            while True:
                filename = _siren_name(digest, chosen_repair)
                if (
                    filename not in state.siren_retire
                    and filename not in state.siren_ambiguous
                    and chosen_repair not in entries
                ):
                    break
                chosen_repair += 1
                if chosen_repair > 0xFF:
                    logger.critical(
                        "extreme-weather siren repair generations exhausted")
                    return None

            try:
                await bb.assets_upload(APP_NAME, filename, blob)
            except Exception as exc:  # noqa: BLE001 - upload may have committed
                verification = await _siren_listing(bb)
                verified = None if verification is None else next(
                    (entry for entry in verification
                     if getattr(entry, "name", None) == filename),
                    None,
                )
                if verified is not None and _storage_file_matches(
                        verified, expected_size):
                    logger.warning(
                        "adopting %s after an ambiguous upload result", filename)
                else:
                    if verification is None:
                        state.siren_ambiguous.add(filename)
                    elif verified is not None:
                        state.siren_retire.add(filename)
                        state.siren_repair = max(
                            state.siren_repair, chosen_repair + 1)
                    logger.error(
                        "extreme-weather siren unavailable; will retry: %s", exc)
                    return None

        assert filename is not None
        state.siren_file = filename
        state.siren_repair = chosen_repair
        state.siren_ambiguous.discard(filename)

        # Only a verified successor makes retirement safe.  This also bounds
        # old digest generations left by future changes to the generated tone.
        _defer_siren_retirement(
            state,
            (owned_names | state.siren_ambiguous | state.siren_retire)
            - {filename},
        )
        await retire_siren_assets(bb, state)

        logger.info("extreme-weather siren ready: %s", filename)
        state.siren_asset_changed.set()
        _signal_alert_change(state)
        return filename


async def maintain_siren_asset(bb, state: SkyState) -> None:
    """Retry provisioning for the daemon lifetime and wake on quarantine."""
    while True:
        state.siren_asset_changed.clear()
        if state.siren_file is None:
            await ensure_siren_asset(bb, state)
        await audit_siren_assets(bb, state)
        await retire_siren_assets(bb, state)
        delays: list[float] = [SIREN_PROVISION_RETRY_S]
        if state.siren_retire_after:
            now = asyncio.get_running_loop().time()
            delays.append(max(
                0.01,
                min(state.siren_retire_after.values()) - now,
            ))
        timeout = min(delays) if delays else None
        try:
            if timeout is None:
                await state.siren_asset_changed.wait()
            else:
                await asyncio.wait_for(state.siren_asset_changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass


def _alert_deadline(alert: Alert) -> datetime:
    return min(alert.expires, alert.ends) if alert.ends is not None else alert.expires


def alert_expiry_label(alert: Alert, *, now: datetime | None = None) -> str:
    """A complete local deadline for the second row of the alert card."""
    local_now = (now or datetime.now(TZ)).astimezone(TZ)
    deadline = _alert_deadline(alert).astimezone(TZ)
    hour = deadline.hour % 12 or 12
    ampm = "AM" if deadline.hour < 12 else "PM"
    day = "" if deadline.date() == local_now.date() else f" {deadline.month}/{deadline.day}"
    return device_text(f"UNTIL{day} {hour}:{deadline.minute:02d} {ampm}")


# The widest event name this card can scroll at ALERT_SCROLL_SPEED_PX_S
# without the frame cap binding. Past it, draw_marquee's per-frame step grows
# and the label runs faster than the declared readability ceiling.
ALERT_EVENT_MAX_PX = max_text_width(
    fps=ALERT_ANIM_FPS, speed_px_s=ALERT_SCROLL_SPEED_PX_S)


def presentable_event(alert: Alert) -> str:
    """The event name, or a truthful generic when it cannot be presented.

    CAP allows a 256-character event, and weather_alerts accepts one because
    REJECTING it would drop the alert entirely — the wrong failure for
    life-safety text. But the panel cannot scroll that: a 256-character name
    is a 1535-pixel strip, which at the declared 12 px/s is 643 frames and a
    129-second loop. Nobody reads a 129-second scroll, and letting the frame
    cap silently absorb it means the label runs at 32 px/s instead.

    So when the name will not fit the presentable width, fall back to the
    product class, which `visual_eligible` has already established: it only
    passes alerts whose event ends in "warning" or "emergency". That is a
    generic derived from the CAP data, not an abbreviation sliced out of it —
    the skill forbids the latter, and `UPLINK` becoming `UPLI` is why.

    The full event name still reaches the log and the spoken report.
    """
    event = device_text(alert.event)
    if text_width(event) <= ALERT_EVENT_MAX_PX:
        return event
    words = alert.event.casefold().split()
    kind = "EMERGENCY" if words and words[-1] == "emergency" else "WARNING"
    logger.warning(
        "alert event name is too long to present (%d chars); "
        "showing %r instead", len(alert.event), f"WEATHER {kind}")
    return f"WEATHER {kind}"


def alert_animation_frames(alert: Alert) -> list[Image.Image]:
    """Host-rendered, frame-provable alert marquee for the full 72×16 panel."""
    event = presentable_event(alert)
    expiry = alert_expiry_label(alert)
    box = (2, W - 3)
    box_width = box[1] - box[0] + 1
    frame_count = max(
        marquee_frame_count(
            event,
            box_width,
            fps=ALERT_ANIM_FPS,
            speed_px_s=ALERT_SCROLL_SPEED_PX_S,
        ),
        marquee_frame_count(
            expiry,
            box_width,
            fps=ALERT_ANIM_FPS,
            speed_px_s=ALERT_SCROLL_SPEED_PX_S,
        ),
    )
    frames: list[Image.Image] = []
    for index in range(frame_count):
        image = Image.new("RGB", (W, H), (0, 0, 0))
        pulse = 255 if (index // 3) % 2 == 0 else 120
        pixels = _rgb_pixels(image)
        # Sparse high-contrast brackets: the physical panel reads filled
        # backgrounds as haze, while these edges stay unmistakably urgent.
        for y in (0, 1, 5, 6, 8, 9, 13, 14):
            pixels[0, y] = (pulse, 18, 12)
            pixels[W - 1, y] = (pulse, 18, 12)
        for x in range(3, W - 3, 6):
            pixels[x, 7] = (120, 12, 8)
        draw_marquee(
            image,
            event,
            y=1,
            color=(255, 54, 42),
            box=box,
            frame_index=index,
            frame_count=frame_count,
        )
        draw_marquee(
            image,
            expiry,
            y=9,
            color=(255, 184, 64),
            box=box,
            frame_index=index,
            frame_count=frame_count,
        )
        frames.append(image)
    return frames


def _alert_asset_key(alert: Alert) -> str:
    value = "\x1f".join((
        alert.identifier,
        alert.event,
        alert.severity,
        _alert_deadline(alert).isoformat(),
    ))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


async def ensure_alert_asset(
    bb, state: SkyState, alert: Alert, generation: int,
) -> str | None:
    """Upload one immutable marquee and discard only definitely unused work."""
    key = _alert_asset_key(alert)
    if state.alert_asset_file is not None and (
        getattr(state, "alert_asset_key", None) in (None, key)
    ):
        # ``None`` supports deterministic fakes that inject a provisioned path.
        return state.alert_asset_file
    frames = alert_animation_frames(alert)
    blob = anim.encode_anim(frames, fps=ALERT_ANIM_FPS)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    filename = f"alert_{digest}.anim"
    try:
        if filename not in state.alert_files:
            await bb.assets_upload(APP_NAME, filename, blob)
        if (
            state.alert_generation != generation
            or state.visual_alert is None
            or _alert_asset_key(state.visual_alert) != key
        ):
            # Upload succeeded and no draw was attempted: definitely unused.
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
            return None
        state.alert_asset_file = filename
        state.alert_asset_key = key
        if filename not in state.alert_files:
            state.alert_files.append(filename)
        while len(state.alert_files) > ALERT_ASSET_KEEP:
            stale = state.alert_files.pop(0)
            if stale == filename:
                state.alert_files.append(stale)
                break
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{stale}")
        return filename
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - an alert retries on its next tick
        logger.warning("alert marquee upload failed: %s", exc)
        return None


def _alert_payload(filename: str, prefix: tuple = ()) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=APP_NAME,
        priority=PRIORITY,
        led_notification_color="#FF2222FF",
        elements=[*prefix, types.AnimationElement(
            id="alert",
            type="animation",
            path=filename,
            loop=True,
            x=0,
            y=0,
            display=types.DisplayName.FRONT,
            timeout=ALERT_ELEMENT_TIMEOUT_S,
        )],
    )


def _audio_already_stopped(exc: Exception) -> bool:
    return (
        getattr(exc, "status_code", None) == 410
        or "already stopped" in str(exc).lower()
    )


async def _stop_audio_locked(bb, state: SkyState, generation: int) -> bool:
    """Generation-owned STOP; caller holds ``state.audio_lock``."""
    if generation != state.audio_generation:
        return False
    try:
        await bb.audio_stop()
    except Exception as exc:  # noqa: BLE001
        if not _audio_already_stopped(exc):
            state.audio_stop_pending = True
            logger.warning("audio stop failed; will retry: %s", exc)
            return False
    if generation == state.audio_generation:
        state.audio_owner = None
        state.audio_path = None
        state.audio_stop_pending = False
        return True
    return False


async def stop_audio(bb, state: SkyState, generation: int) -> bool:
    """Run the generation-owned STOP after every older PLAY has settled."""
    async with state.audio_lock:
        return await _stop_audio_locked(bb, state, generation)


async def _play_audio(
    bb,
    state: SkyState,
    path: str,
    owner: str,
    still_valid,
) -> bool:
    """Serialize PLAY and revalidate its intent while holding the audio lane."""
    if state.audio_stop_pending:
        return False
    state.audio_generation += 1
    generation = state.audio_generation
    async with state.audio_lock:
        if (
            generation != state.audio_generation
            or state.audio_stop_pending
            or state.shutting_down
            or not still_valid()
        ):
            return False
        state.audio_owner = f"{owner}-pending"
        state.audio_path = path
        try:
            await bb.audio_play(path=path, application_name=APP_NAME)
        except Exception as exc:  # noqa: BLE001
            if _is_refusal(exc) or getattr(exc, "status_code", None) == 404:
                # 409 is a definite refusal; PLAY 404 is a definite missing or
                # unplayable asset. Neither committed audio, so answering with
                # the device-global STOP could silence somebody else's app.
                if generation == state.audio_generation:
                    state.audio_owner = None
                    state.audio_path = None
                    state.audio_stop_pending = False
                raise
            if generation == state.audio_generation:
                state.audio_stop_pending = True
            raise
        except BaseException:
            if generation == state.audio_generation:
                # A transport/cancellation may have committed PLAY remotely.
                state.audio_stop_pending = True
            raise
        if (
            generation == state.audio_generation
            and not state.shutting_down
            and still_valid()
        ):
            state.audio_owner = owner
            return True
        if generation == state.audio_generation:
            # PLAY was accepted after its view/alert intent changed. Claim and
            # issue the newer STOP in this same serialized lane, so no later
            # navigation draw can be followed by stale report audio.
            stop_generation = _claim_audio_stop(state)
            await _stop_audio_locked(bb, state, stop_generation)
        return False


def _stale_weather_elements(timeout: int = STALE_ELEMENT_TIMEOUT_S) -> list:
    """The honest stand-in for a sky we are not entitled to draw.

    FIRMWARE LAW: element attributes are immutable after creation, so every
    redraw keeps IDENTICAL ids and geometry and varies only the timeout — the
    same mutation ``draw_scrub_readout`` relies on.  Geometry and colors are
    the proven readout band.
    """
    return [
        types.RectangleElement(
            id="wxstaleb", type="rectangle",
            x=8, y=3, width=56, height=11,
            fill="solid", fill_colors=["#000000C0"], border_width=0,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
        types.TextElement(
            id="wxstalet", type="text",
            text=STALE_WEATHER_TEXT, font="condensed",
            color="#FFD98CFF", align="center", x=36, y=8,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
    ]


def _restore_payload(
    state: SkyState,
) -> tuple[types.DisplayElements | None, dict | None, dict | None]:
    """Build the selected live or Time Machine view after an app clear."""
    elements: list = []
    scene_file = state.current_scene_file or (
        state.scene_files[-1] if state.scene_files else None
    )
    # Clearing an alert must not grant an expired weather scene a brand-new
    # native timeout.  Time Machine content below is independently selected
    # model output and can still be restored while the live lease is closed.
    if scene_file is not None and weather_is_fresh(state):
        elements.append(types.AnimationElement(
            id="sky", type="animation", path=scene_file, loop=True,
            x=0, y=0, display=types.DisplayName.FRONT,
            timeout=ELEMENT_TIMEOUT_S,
        ))

    restored_reveal = None
    restored_readout = None
    if state.revealed and state.scrub_slot is not None:
        source = state.last_reveal
        if source is None and state.timeline_meta is not None:
            source = {
                "slot": state.scrub_slot,
                "fname": state.timeline_meta["file"],
                "section": f"s{state.scrub_slot:02d}",
            }
        if source is not None:
            # display_clear removed the native registry, so reusing the
            # selected reveal id is safe and preserves the logical view.
            # (Without the clear, geometry/path reuse would be unsafe.)
            eid = source.get("eid")
            if eid is None:
                state.reveal_n += 1
                eid = f"rv{state.reveal_n}"
            restored_reveal = {
                "eid": eid,
                "slot": state.scrub_slot,
                "fname": source["fname"],
                "section": source.get("section"),
            }
            elements.append(types.AnimationElement(
                id=restored_reveal["eid"],
                type="animation",
                path=restored_reveal["fname"],
                section=restored_reveal["section"],
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=60,
            ))
    elif state.scrub_slot is not None and state.timeline_meta is not None:
        state.readout_gen = (state.readout_gen + 1) % 100
        label = _slot_label(state.timeline_meta, state.scrub_slot)
        elements.extend(_readout_elements(state.readout_gen, label, timeout=3))
        restored_readout = {
            "generation": state.readout_gen,
            "label": label,
            "timeout": 3,
        }

    if not elements:
        # Nothing truthful to show — but this payload follows a display_clear,
        # so returning None here is not "leave it as it was", it is "leave it
        # black". Say why instead; the scene loop replaces this the moment a
        # real observation lands.
        elements.extend(_stale_weather_elements())
    return types.DisplayElements(
        application_name=APP_NAME,
        priority=PRIORITY,
        led_notification_color=(
            "#FF2222FF" if state.visual_alert is not None and state.alert_acked else None
        ),
        elements=elements,
    ), restored_reveal, restored_readout


async def restore_current_view(bb, state: SkyState) -> bool:
    """Remove every same-app transient, then rebuild exactly the selected view."""
    async with state.display_lock:
        try:
            await bb.display_clear(application_name=APP_NAME)
            # A successful clear removes every same-app transient, including
            # any report POST whose response was lost.
            state.report_statuses.clear()
            payload, restored_reveal, restored_readout = _restore_payload(state)
            if payload is None:
                # The clear already happened, so there is no view left to keep
                # a promise about. Stay armed and let the caller try again
                # rather than reporting a black panel as a restored one.
                state.alert_dismiss_pending = True
                logger.warning(
                    "restore produced no view after clearing; will retry")
                return False
            await bb.display_draw(payload)
            drew_stale_notice = any(
                element.id == "wxstalet" for element in payload.elements)
        except Exception as exc:  # noqa: BLE001
            state.alert_dismiss_pending = True
            if _is_refusal(exc):
                logger.debug("alert dismissal yielded to the active device session")
            else:
                logger.warning("alert dismissal/restore failed; will retry: %s", exc)
            return False
        state.last_reveal = restored_reveal
        state.last_readout = restored_readout
        if drew_stale_notice:
            # Claim the notice so the next good scene retires it by id.
            state.stale_notice_at = asyncio.get_running_loop().time()
        state.alert_dismiss_pending = False
        state.alert_drawn_generation = -1
        # The payload above re-creates "sky" pointing at the file the scene
        # loop last uploaded — an asset the firmware cached BY PATH and held
        # open across the clear we just issued. Rather than trust that redraw,
        # demand a fresh minute: the scene loop wakes within a second and
        # pushes a NEW versioned filename, which always renders. Without this
        # the panel is dark until the next wall-clock boundary, up to a minute
        # of black immediately after acknowledging a warning.
        state.scene_change.set()
        return True


async def acknowledge_alert(bb, state: SkyState, reason: str) -> bool:
    """Consume one input as acknowledgement, STOP, and restore the chosen view."""
    active = state.visual_alert is not None or state.weather.severe
    if not active and not state.alert_dismiss_pending:
        return False
    if not state.alert_acked:
        state.alert_acked = True
        state.alert_generation += 1
        state.alert_drawn_generation = -1
        if state.scrub_slot is not None:
            # An alert can hold the selected Time Machine view longer than
            # its ordinary idle lease. Acknowledgement restores that exact
            # view, so give it a fresh lease instead of snapping to NOW on
            # the very next main-loop tick.
            state.scrub_touched = asyncio.get_running_loop().time()
        logger.warning("weather alert acknowledged by %s", reason)
    state.alert_dismiss_pending = True
    _signal_alert_change(state)
    generation = _claim_audio_stop(state)
    if state.audio_stop_pending:
        await stop_audio(bb, state, generation)
    await restore_current_view(bb, state)
    return True


async def _wait_for_alert_change(
    state: SkyState,
    timeout: float,
    observed_generation: int,
) -> None:
    """Wait without clearing a transition that lands at the timeout edge."""
    if state.alert_wake_generation != observed_generation:
        return
    # Clear only the level that existed when the caller captured its state,
    # then re-check the monotonic generation. A change racing this clear still
    # returns immediately even though Event.clear() erased its level bit.
    state.alert_changed.clear()
    if state.alert_wake_generation != observed_generation:
        return
    try:
        await asyncio.wait_for(state.alert_changed.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def severe_alarm(bb, state: SkyState) -> None:
    """Present Severe/Extreme CAP alerts; sound only exact Extreme severity."""
    last_siren = -1e9
    last_siren_generation = -1
    try:
        while True:
            try:
                wake_generation = state.alert_wake_generation
                if state.audio_stop_pending:
                    await stop_audio(bb, state, state.audio_generation)

                alert = state.visual_alert
                generation = state.alert_generation
                if generation != last_siren_generation:
                    last_siren = -1e9
                    last_siren_generation = generation
                if alert is not None and not state.alert_acked:
                    filename = await ensure_alert_asset(bb, state, alert, generation)
                    if (
                        filename is None
                        or state.alert_generation != generation
                        or state.alert_acked
                        or state.visual_alert is None
                    ):
                        await _wait_for_alert_change(
                            state, ALERT_ASSET_RETRY_S, wake_generation)
                        continue
                    async with state.display_lock:
                        report_statuses = _live_report_statuses(state)
                        await bb.display_draw(_alert_payload(
                            filename,
                            tuple(_retired_report_status_elements(
                                report_statuses)),
                        ))
                        _forget_report_statuses(state, report_statuses)
                    state.alert_drawn_generation = generation

                    # Revalidate after the awaited draw.  This is the race that
                    # previously allowed PLAY to cross an acknowledgement STOP.
                    now = asyncio.get_running_loop().time()
                    siren = state.siren_alert
                    if (
                        siren is not None
                        and state.siren_file is not None
                        and now - last_siren >= SIREN_RETRIGGER_S
                        and state.alert_generation == generation
                        and not state.alert_acked
                    ):
                        siren_path = state.siren_file
                        try:
                            played = await _play_audio(
                                bb,
                                state,
                                siren_path,
                                "alert",
                                # The three loop values are bound as defaults
                                # rather than captured: this predicate decides
                                # whether an already-accepted PLAY still belongs
                                # to the intent that asked for it, so it must
                                # compare against the generation, alert and path
                                # that were current when it was built — never
                                # whatever the next loop pass has moved on to.
                                # `state` stays late-bound on purpose; the whole
                                # point is to read it fresh after the await.
                                lambda gen=generation, armed=siren,
                                path=siren_path: (
                                    state.alert_generation == gen
                                    and not state.alert_acked
                                    and state.siren_alert is not None
                                    and state.siren_alert.identifier == armed.identifier
                                    and state.siren_file == path
                                ),
                            )
                        except exceptions.BusyBarAPIError as exc:
                            if getattr(exc, "status_code", None) != 404:
                                raise
                            mark_siren_unplayable(state, siren_path)
                            logger.error(
                                "extreme-weather siren %s is missing or "
                                "unplayable; provisioning a repair", siren_path)
                            played = False
                        if played:
                            last_siren = now
                    await _wait_for_alert_change(
                        state, ALERT_REDRAW_S, wake_generation)
                elif alert is not None:  # acknowledged: view stays, red pulse stays
                    if state.alert_dismiss_pending:
                        await restore_current_view(bb, state)
                    async with state.display_lock:
                        await bb.display_draw(_led_ping_payload("#FF2222FF"))
                    last_siren = -1e9
                    await _wait_for_alert_change(
                        state, ALERT_REDRAW_S, wake_generation)
                else:
                    if state.audio_owner in {"alert", "alert-pending"}:
                        stop_generation = _claim_audio_stop(state)
                        await stop_audio(bb, state, stop_generation)
                    if state.alert_dismiss_pending:
                        await restore_current_view(bb, state)
                    last_siren = -1e9
                    await _wait_for_alert_change(state, 1, wake_generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - busy session refuses draws
                logger.debug("alarm tick failed: %s", exc)
                await _wait_for_alert_change(
                    state, 2, wake_generation)
    finally:
        if state.audio_owner in {"alert", "alert-pending"} or state.audio_stop_pending:
            generation = _claim_audio_stop(state)
            await stop_audio(bb, state, generation)


def _scene_payload(
    filename: str,
    led_color: str | None = None,
    prefix: tuple = (),
) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=APP_NAME,
        priority=PRIORITY,
        led_notification_color=led_color,
        elements=[*prefix,
            types.AnimationElement(
                id="sky",
                type="animation",
                path=filename,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=ELEMENT_TIMEOUT_S,
            ),
        ],
    )


ANIM_FRAMES = 40
ANIM_FPS = 5  # 8-second seamless loop, played by the device itself


def render_loop_frames(now: datetime, wx: WeatherState, seed: int,
                       scene: str = "house",
                       scrubbed: bool = False,
                       n_frames: int | None = None) -> list[Image.Image]:
    # backroads runs a double-rate loop: full-width traffic needs
    # ~1px/frame to look driven rather than dragged. Precipitation needs it
    # for the same reason -- at 40 frames a downpour jumps 6 of 16 rows per
    # frame and strobes instead of falling. `fps` is derived from the frame
    # count downstream, so the loop stays 8 seconds either way and no other
    # motion in the scene changes rate.
    # Snow is left at 40: it drifts at 0.75 rows/frame already.
    n = n_frames or (80 if scene == "backroads" or is_raining(wx)
                     else ANIM_FRAMES)
    return [render_scene(now, wx, seed, phase=i / n, scene=scene,
                         scrubbed=scrubbed)
            for i in range(n)]


def _timeline_payload(section: str, eid: str, filename: str,
                      timeout: int = 60) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        elements=[types.AnimationElement(
            id=eid, type="animation", path=filename,
            section=section, loop=True, x=0, y=0,
            display=types.DisplayName.FRONT, timeout=timeout,
        )],
    )


async def retire_reveal(bb, state: SkyState) -> None:
    """Firmware law: an element id can never re-seek. Retiring = redrawing
    it with the SAME content and a 1s timeout (verified working)."""
    if state.last_reveal is None:
        return
    r = state.last_reveal
    async with state.display_lock:
        await bb.display_draw(types.DisplayElements(
            application_name=APP_NAME, priority=PRIORITY,
            elements=[types.AnimationElement(
                id=r["eid"], type="animation", path=r["fname"],
                section=r.get("section"), loop=True, x=0, y=0,
                display=types.DisplayName.FRONT, timeout=1,
            )],
        ))
    if state.last_reveal is r:
        state.last_reveal = None


async def animate_reveal(
    bb,
    state: SkyState,
    slot: int,
    *,
    initial: bool = False,
    intent: int | None = None,
) -> None:
    """A revealed moment starts as a held frame; this swaps in the full
    animated loop for that moment once it's rendered — without ever
    blocking the wheel. Aborts silently if the user moved on."""
    if intent is None:
        intent = state.view_generation
    scene = state.scene
    try:
        meta = state.timeline_meta
        if meta is None:
            return
        t = meta["start"] + timedelta(seconds=TIMELINE_STEP_S * slot)
        loop = asyncio.get_running_loop()
        frames = await loop.run_in_executor(
            None, lambda: render_loop_frames(
                t.astimezone(timezone.utc), wx_at(state, t), 7,
                scene=scene, scrubbed=True, n_frames=ANIM_FRAMES))
        if (
            state.view_generation != intent
            or state.scene != scene
            or state.scrub_slot != slot
            or _unacknowledged_alert_active(state)
            or (not initial and not state.revealed)
        ):
            return  # the wheel moved on or a fresh alert took the display
        blob = anim.encode_anim(frames, fps=ANIM_FPS)
        fname = f"rva_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
        await bb.assets_upload(APP_NAME, fname, blob)
        if (
            state.view_generation != intent
            or state.scene != scene
            or state.scrub_slot != slot
            or _unacknowledged_alert_active(state)
            or (not initial and not state.revealed)
        ):
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{fname}")
            return
        state.reveal_n += 1
        eid = f"rv{state.reveal_n}"
        prev = state.last_reveal
        elements: list = []
        if prev is not None:
            elements.append(types.AnimationElement(
                id=prev["eid"], type="animation", path=prev["fname"],
                section=prev.get("section"), loop=True, x=0, y=0,
                display=types.DisplayName.FRONT, timeout=1,
            ))
        elements.append(types.AnimationElement(
                id=eid, type="animation", path=fname, loop=True,
                x=0, y=0, display=types.DisplayName.FRONT, timeout=60,
            ))
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.scrub_slot != slot
                or _unacknowledged_alert_active(state)
                or (not initial and not state.revealed)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{fname}")
                return
            await bb.display_draw(types.DisplayElements(
                application_name=APP_NAME,
                priority=PRIORITY,
                elements=elements,
            ))
        state.last_reveal = {"eid": eid, "slot": slot, "fname": fname,
                             "section": None}
        state.revealed = True
        state.reveal_pending = False
        state.anim_reveal_file = fname
        if fname not in state.anim_reveal_files:
            state.anim_reveal_files.append(fname)
        while len(state.anim_reveal_files) > 3:
            old_file = state.anim_reveal_files.pop(0)
            with contextlib.suppress(Exception):
                await bb.storage_remove(
                    f"/ext/user_assets/{APP_NAME}/{old_file}")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.debug("animated reveal skipped: %s", exc)
    finally:
        if state.view_generation == intent and state.scrub_slot == slot:
            state.reveal_pending = False


def _slot_label(meta: dict, slot: int) -> str:
    dt = meta["start"] + timedelta(seconds=TIMELINE_STEP_S * slot)
    today = datetime.now(TZ).date()
    prefix = "TMW " if dt.date() > today else ("YDA " if dt.date() < today else "")
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{prefix}{dt.hour % 12 or 12}:{dt.minute:02d} {ampm}"


def _readout_elements(generation: int, label: str, timeout: int) -> list:
    return [
        types.RectangleElement(
            id=f"ror{generation}", type="rectangle",
            x=8, y=3, width=56, height=11,
            fill="solid", fill_colors=["#000000C0"], border_width=0,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
        types.TextElement(
            id=f"rot{generation}", type="text",
            text=label, font="condensed",
            color="#FFD98CFF", align="center", x=36, y=8,
            display=types.DisplayName.FRONT, timeout=timeout,
        ),
    ]


def _retired_readout_elements(readout: dict | None) -> list:
    if readout is None:
        return []
    return _readout_elements(readout["generation"], readout["label"], 1)


async def draw_scrub_readout(bb, state: SkyState, label: str,
                             timeout: int = 3) -> None:
    """The big instant time readout that rides the wheel. FIRMWARE LAW:
    element attributes are immutable after creation — every redraw here
    keeps IDENTICAL geometry; only the text string (and timeout) vary,
    the one mutation the firmware honors (the clock element proved it).
    There is no clear: the timeout dissolves it."""
    old_readout = state.last_readout
    old_reveal = state.last_reveal
    generation = state.readout_gen + 1
    report_statuses = _live_report_statuses(state)
    elements = [
        *_retired_report_status_elements(report_statuses),
        *_retired_readout_elements(old_readout),
    ]
    if old_reveal is not None:
        elements.append(types.AnimationElement(
            id=old_reveal["eid"], type="animation",
            path=old_reveal["fname"], section=old_reveal.get("section"),
            loop=True, x=0, y=0, display=types.DisplayName.FRONT, timeout=1,
        ))
    elements.extend(_readout_elements(generation, label, timeout))
    async with state.display_lock:
        # A CAP update can re-arm the warning after the wheel message was
        # decoded but before this draw wins the device lane. Never let that
        # stale readout cover the newly active alert card.
        if _unacknowledged_alert_active(state):
            return
        await bb.display_draw(types.DisplayElements(
            application_name=APP_NAME,
            priority=PRIORITY,
            elements=elements,
        ))
        _forget_report_statuses(state, report_statuses)
    state.readout_gen = generation
    state.last_readout = {
        "generation": generation,
        "label": label,
        "timeout": timeout,
    }
    if state.last_reveal is old_reveal:
        state.last_reveal = None


def _scrub_reveal_ready(state: SkyState, now: float) -> bool:
    """Whether a rested wheel selection may take the front display.

    An acknowledged CAP alert remains selected so its red top-LED reminder
    can continue. Only the unacknowledged alert card owns the front display.
    """
    return (
        state.scrub_slot is not None
        and not state.revealed
        and not state.reveal_pending
        and not _unacknowledged_alert_active(state)
        and now - state.scrub_touched > REVEAL_REST_S
        and state.timeline_meta is not None
    )


def _half_hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0 if dt.minute < 30 else 30,
                      second=0, microsecond=0)


async def build_timeline(bb, state: SkyState) -> None:
    """Pre-render the whole ±24h scrub range as one sectioned .anim so a
    wheel detent is a single draw call. Rebuilds half-hourly and whenever
    the scene changes."""
    while True:
        try:
            meta = state.timeline_meta
            if state.scrub_slot is not None:
                await asyncio.sleep(5)  # never swap files mid-scrub
                continue
            stale = (meta is None or meta["scene"] != state.scene
                     or (datetime.now(TZ) - meta["built"]).total_seconds()
                     > 1800)
            if stale and state.hourly:
                # Snapshot the scene: rendering 97 slots takes seconds and
                # yields below, so a START press lands mid-render. Sampling
                # state.scene per slot would bake two scenes into one file.
                scene = state.scene
                start = _half_hour_floor(datetime.now(TZ)) - timedelta(hours=24)
                frames = []
                for i in range(TIMELINE_SLOTS):
                    t = start + timedelta(seconds=TIMELINE_STEP_S * i)
                    frames.append(render_scene(
                        t.astimezone(timezone.utc), wx_at(state, t), 7,
                        phase=0.0, scene=scene, scrubbed=True))
                    if i % 8 == 0:
                        await asyncio.sleep(0)  # stay cooperative
                if scene != state.scene:
                    # The scene changed while we rendered. Throw the work
                    # away rather than upload a file whose label would claim
                    # a scene it doesn't contain — the reveal guard trusts
                    # that label, and a lying one is what flashes the
                    # previous theme onto the bar mid-scrub.
                    logger.info("timeline: scene changed mid-render "
                                "(%s -> %s), rebuilding", scene, state.scene)
                    continue
                # Each slot holds for 200 display-frames (200s at fps=1):
                # the firmware plays PAST a section's end rather than
                # holding, so the frame itself must outlast any park —
                # idle snap-home at 45s always fires first
                secs = [(f"s{i:02d}", i * 200, i * 200 + 199)
                        for i in range(TIMELINE_SLOTS)]
                blob = anim.encode_anim(frames, fps=1, sections=secs,
                                        durations=[200] * TIMELINE_SLOTS)
                # Versioned name: the firmware caches anim files by path,
                # so overwriting one path serves stale generations
                fname = f"tl_{datetime.now().strftime('%H%M%S')}.anim"
                await bb.assets_upload(APP_NAME, fname, blob)
                state.timeline_meta = {"start": start, "scene": scene,
                                       "built": datetime.now(TZ),
                                       "file": fname}
                # Retire one generation late: a reveal committed while this
                # rebuild was rendering still points at the previous file with
                # a 60s element timeout, and deleting it under the firmware
                # leaves a frozen past-time frame that "back to now" can't
                # clear. A rebuild cycle outlasts any element timeout.
                state.timeline_files.append(fname)
                while len(state.timeline_files) > 2:
                    stale_tl = state.timeline_files.pop(0)
                    try:
                        await bb.storage_remove(
                            f"/ext/user_assets/{APP_NAME}/{stale_tl}")
                    except Exception:  # noqa: BLE001
                        pass
                logger.info("timeline: %d slots ready (%.0f kB)",
                            TIMELINE_SLOTS, len(blob) / 1024)
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("timeline build failed: %s", exc)
            await asyncio.sleep(60)


_is_refusal = is_refusal   # shared: busybar_dev.device


def _retired_stale_notice_elements(state: SkyState) -> list:
    """Same ids and geometry, one-second lease — the sanctioned retirement."""
    if not state.stale_notice_at:
        return []
    return _stale_weather_elements(timeout=1)


async def keep_stale_notice(bb, state: SkyState) -> bool:
    """Hold an honest 'no live weather' card while the sources are down.

    Refreshed on its own cadence rather than every tick: the element carries a
    real lease, and redrawing an unchanged card once a second is device
    traffic that buys nothing.  Returns whether a draw was accepted, so the
    caller can tell a refusal from a no-op.
    """
    now = asyncio.get_running_loop().time()
    if not state.stale_since:
        state.stale_since = now
    if now - state.stale_since < STALE_NOTICE_GRACE_S:
        return False
    if state.stale_notice_at and now - state.stale_notice_at < STALE_REDRAW_S:
        return False
    try:
        async with state.display_lock:
            # Re-check under the lock: a scene or alert draw may have won the
            # lane while this coroutine waited for it, and this card must
            # never land on top of content that outranks it.
            if state.visual_alert is not None or state.scrub_slot is not None:
                return False
            await bb.display_draw(types.DisplayElements(
                application_name=APP_NAME,
                priority=PRIORITY,
                elements=_stale_weather_elements(),
            ))
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.warning("stale-weather notice rejected: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - offline is a state
        logger.debug("stale-weather notice failed: %s", exc)
        return False
    state.stale_notice_at = now
    return True


async def push_scene(bb, state: SkyState, now: datetime,
                     frames: list[Image.Image]) -> None:
    """Encode the loop to .anim, upload, and (re)draw the scene elements."""
    scene = state.scene
    intent = state.view_generation
    # ``frames`` were rendered from the current weather immediately before
    # this coroutine was entered. Snapshot the matching LED before the upload
    # yields; a poll completing during that await must not make the two halves
    # of one scene payload describe different weather.
    led_color = _weather_led(state.weather)
    fps = max(1, round(len(frames) * ANIM_FPS / ANIM_FRAMES))
    blob = anim.encode_anim(frames, fps=fps)
    # Versioned filenames, never reused (firmware caches assets BY PATH and
    # can hold the played file open across element clears — a fixed a/b
    # alternation 508s after cross-instance restarts). Keep the live file
    # plus one predecessor; reap older generations once safely off-screen.
    state.scene_gen += 1
    filename = f"sky_{int(time.time()) % 100000:05d}_{state.scene_gen}.anim"
    await bb.assets_upload(APP_NAME, filename, blob)
    if state.scene != scene or state.view_generation != intent:
        # Upload definitely completed and no draw was attempted.
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        return
    try:
        async with state.display_lock:
            if state.scene != scene or state.view_generation != intent:
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{filename}")
                return
            stale_report_statuses = _stale_report_statuses(state)
            await bb.display_draw(_scene_payload(
                filename,
                led_color,
                (
                    *_retired_report_status_elements(stale_report_statuses),
                    # Retire the "no live weather" card in the same payload
                    # that replaces it. Elements merge by id and accumulate,
                    # so without this the notice rides on top of a good sky
                    # until its own lease runs out.
                    *_retired_stale_notice_elements(state),
                ),
            ))
            _forget_report_statuses(state, stale_report_statuses)
            state.stale_notice_at = 0.0
            state.stale_since = 0.0
            # Commit the token while the accepted draw still owns the display
            # lane. A flash waiting on this lock must never cover a newer
            # same-scene asset merely because this bookkeeping moved later.
            state.last_drawn_at = asyncio.get_running_loop().time()
            state.current_scene_file = filename
            state.current_scene_frames = tuple(frame.copy() for frame in frames)
            state.scene_files.append(filename)
    except exceptions.BusyBarAPIError as exc:
        # 409: a BUSY/CUSTOM session owns the display. The device refused the
        # draw outright, so it never opened this file — reclaim it now, or one
        # ~113kB orphan accrues every minute for the length of the session.
        # Only on a refusal: after a transport error the draw may have landed,
        # and deleting a file the firmware holds open is the 508 trap.
        if _is_refusal(exc):
            try:
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
            except Exception:  # noqa: BLE001 - reclaiming is best-effort
                pass
        raise
    logger.info("scene updated on the bar (%s, %d frames, %.1fkB)",
                clock_str(now), len(frames), len(blob) / 1024)
    while len(state.scene_files) > 2:
        stale = state.scene_files.pop(0)
        try:
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{stale}")
        except Exception:  # noqa: BLE001 - reaping is best-effort
            pass


@dataclass(frozen=True)
class LightningSegment:
    """Exact host-rendered strike frames plus the native top-LED intent."""

    frames: tuple[Image.Image, ...]
    fps: int
    timeout_s: int
    pulse_frames: int
    led_notification_color: str | None


def render_lightning_segment(
    now: datetime,
    wx: WeatherState,
    seed: int,
    *,
    phase0: float,
    scene: str,
    dist_km: float,
) -> LightningSegment:
    """Render one deterministic lightning burst without touching the device.

    The front frames are the exact RGB buffers passed to the native animation
    encoder.  ``led_notification_color`` records the accompanying firmware
    blink request; only a strike inside the measured near radius earns one.
    """
    distance = min(max(dist_km, 0.0), STRIKE_RADIUS_KM)
    closeness = 1.0 - distance / STRIKE_RADIUS_KM
    peak = 0.42 + 0.58 * closeness
    pulse = (0.0, peak, peak * 0.22, peak * 0.86, peak * 0.08, 0.0)
    # A one-shot animation holds its final frame until the native element
    # timeout.  The pulse itself is only 0.5 s, while firmware timeouts are
    # whole seconds; ending the asset there pinned a static storm frame over
    # the still-running sky for the remaining 1.5 s.  Fill the complete lease
    # with ordinary advancing scene frames so rain/cloud motion never pauses.
    frame_count = FLASH_ANIM_FPS * FLASH_ELEMENT_TIMEOUT_S
    intensities = pulse + (0.0,) * (frame_count - len(pulse))
    loop_duration_s = ANIM_FRAMES / ANIM_FPS
    phase_step = 1.0 / (loop_duration_s * FLASH_ANIM_FPS)
    frames = tuple(
        render_scene(
            now,
            wx,
            seed,
            phase=(phase0 + index * phase_step) % 1.0,
            scene=scene,
            lightning=intensity,
        )
        for index, intensity in enumerate(intensities)
    )
    return LightningSegment(
        frames=frames,
        fps=FLASH_ANIM_FPS,
        timeout_s=FLASH_ELEMENT_TIMEOUT_S,
        pulse_frames=len(pulse),
        led_notification_color=(
            "#BBDDFFFF" if distance <= STRIKE_NEAR_KM else None
        ),
    )


async def flash(bb, state: SkyState, dist_km: float) -> None:
    """Flash only the rendered sky backdrop, with one native top-LED pulse.

    Sub-second color changes on a stable Rectangle id are ignored by firmware.
    One short ``.anim`` makes every step real, while recomposing the scene with
    ``lightning=...`` leaves houses, trees, skyline, water, and status ink
    intact instead of washing the entire display white.
    """
    if (
        state.visual_alert is not None
        or state.scrub_slot is not None
        or state.current_scene_file is None
        or not weather_is_fresh(state)
    ):
        return
    scene = state.scene
    scene_file = state.current_scene_file
    intent = state.view_generation
    now = datetime.now(timezone.utc)
    phase0 = (asyncio.get_running_loop().time() % 8.0) / 8.0
    seed = int(asyncio.get_running_loop().time() // 600)
    segment = render_lightning_segment(
        now,
        state.weather,
        seed,
        phase0=phase0,
        scene=scene,
        dist_km=dist_km,
    )
    # Keep the encoder's established list input while the public segment uses
    # an immutable tuple to make its ordered frame contract explicit.
    blob = anim.encode_anim(list(segment.frames), fps=segment.fps)
    filename = f"flash_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
    await bb.assets_upload(APP_NAME, filename, blob)
    if (
        state.view_generation != intent
        or state.scene != scene
        or state.current_scene_file != scene_file
        or state.scrub_slot is not None
        or state.visual_alert is not None
        or not weather_is_fresh(state)
    ):
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        return
    state.effect_generation += 1
    payload = types.DisplayElements(
        application_name=APP_NAME,
        priority=PRIORITY,
        # Distant strikes stay in the rendered backdrop. Only a genuinely
        # nearby strike gets the conspicuous native top-LED notification.
        led_notification_color=segment.led_notification_color,
        elements=[types.AnimationElement(
            id=f"flash{state.effect_generation}",
            type="animation",
            path=filename,
            loop=False,
            x=0,
            y=0,
            display=types.DisplayName.FRONT,
            timeout=segment.timeout_s,
        )],
    )
    try:
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.current_scene_file != scene_file
                or state.scrub_slot is not None
                or state.visual_alert is not None
                or not weather_is_fresh(state)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{filename}")
                return
            await bb.display_draw(payload)
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        raise
    await asyncio.sleep(segment.timeout_s + FLASH_ASSET_RETIRE_GRACE_S)
    with contextlib.suppress(Exception):
        await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
    logger.info("lightning backdrop: strike %.0f km away", dist_km)


async def meteor(bb, state: SkyState) -> None:
    """One native baked shooting-star animation; geometry really advances."""
    if (
        state.visual_alert is not None
        or state.scrub_slot is not None
        or state.current_scene_file is None
        or not weather_is_fresh(state)
    ):
        return
    m_rng = random.Random()
    x = m_rng.randrange(6, 46)
    y = m_rng.randrange(0, 3)
    ddx = m_rng.choice((3, 3, -3))  # mostly left-to-right, 3:1 slope
    scene = state.scene
    intent = state.view_generation
    now = datetime.now(timezone.utc)
    phase0 = (asyncio.get_running_loop().time() % 8.0) / 8.0
    seed = int(asyncio.get_running_loop().time() // 600)
    frames: list[Image.Image] = []
    for index in range(8):
        image = render_scene(
            now,
            state.weather,
            seed,
            phase=(phase0 + index / 96.0) % 1.0,
            scene=scene,
        )
        pixels = _rgb_pixels(image)
        for tail, scale in enumerate((1.0, 0.55, 0.28)):
            tx = x + (index - tail) * ddx
            ty = y + index - tail
            if 0 <= tx < W and 0 <= ty < H:
                pixels[tx, ty] = _rgb_int(
                    channel * scale for channel in (242, 246, 255)
                )
        frames.append(image)
    blob = anim.encode_anim(frames, fps=12)
    filename = f"meteor_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
    await bb.assets_upload(APP_NAME, filename, blob)
    if (
        state.view_generation != intent
        or state.scene != scene
        or state.scrub_slot is not None
        or state.visual_alert is not None
        or not weather_is_fresh(state)
    ):
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        return
    state.effect_generation += 1
    try:
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.scrub_slot is not None
                or state.visual_alert is not None
                or not weather_is_fresh(state)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{filename}")
                return
            await bb.display_draw(types.DisplayElements(
                application_name=APP_NAME,
                priority=PRIORITY,
                elements=[types.AnimationElement(
                    id=f"meteor{state.effect_generation}",
                    type="animation",
                    path=filename,
                    loop=False,
                    x=0,
                    y=0,
                    display=types.DisplayName.FRONT,
                    timeout=2,
                )],
            ))
        await asyncio.sleep(2.05)
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        logger.info("meteor: a shooting star crossed the bar")
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        # A session owns the display; the sky keeps its secret.


# A small rural passenger train, Ghibli-flavoured (the operator asked for
# "my neighbor totoro", 2026-08-15): a few short carriages in one dark
# livery with warm lit windows, not a mile of American boxcars. The read
# comes from the window rhythm — evenly spaced warm squares sliding along a
# dark body — so the body stays dark enough at every hour for them to glow.
RAILCAR_LIVERIES = ((36, 58, 44), (58, 40, 36), (40, 46, 62))
RAILCAR_WINDOW_NIGHT = (255, 206, 128)
RAILCAR_WINDOW_DAY = (150, 170, 186)   # daylight on glass, not lamplight
RAILCAR_LEN = 6                        # 5 body columns and a coupling gap


def _freight_frames(band: Image.Image, night: bool,
                    rng: random.Random,
                    foreground: frozenset = frozenset()) -> list[Image.Image]:
    """Render a short passenger train crossing the full-width sky band
    (scene rows 0-5) at 12fps, one pixel per frame.

    Carriages ride rows 2-4 on the rail at row 5; the caller repaints the
    status digits and the foreground trees on top, so the train passes
    behind both. It enters and leaves at TRUE screen edges.
    """
    livery = RAILCAR_LIVERIES[rng.randrange(len(RAILCAR_LIVERIES))]
    roof = _rgb_int(v * 0.65 for v in livery)
    window = RAILCAR_WINDOW_NIGHT if night else RAILCAR_WINDOW_DAY
    n_cars = rng.randint(3, 5)
    # unroll the consist into columns: (col-within-car, is_last_car)
    cols: list[int | None] = []
    for _ in range(n_cars):
        for c in range(RAILCAR_LEN - 1):
            cols.append(c)
        cols.append(None)              # the gap between carriages
    dirn = rng.choice((1, -1))
    total = len(cols)
    frames: list[Image.Image] = []
    bw = band.width
    base_px = _rgb_pixels(band)
    for f in range(bw + total + 6):
        im = band.copy()
        pxb = _rgb_pixels(im)
        for i, col in enumerate(cols):
            x = (f - i) if dirn > 0 else (bw - 1 - (f - i))
            if col is None or not (0 <= x < bw):
                continue
            pxb[x, 2] = roof
            pxb[x, 3] = livery
            pxb[x, 4] = livery
            # Two warm windows per carriage, always in the same places, so
            # the rhythm reads as a train rather than as noise.
            if col in (1, 3):
                pxb[x, 3] = window
            # The leading edge of the leading carriage carries the lamp.
            leading = i == 0 if dirn > 0 else i == total - 1
            if leading and col == 0 and night:
                pxb[x, 4] = (255, 236, 176)
        for fx, fy in foreground:
            if 0 <= fx < bw and 0 <= fy < im.height:
                pxb[fx, fy] = base_px[fx, fy]
        frames.append(im)
    return frames


async def train_crossing(bb, state: SkyState) -> None:
    """One freight, once — a device-played one-shot overlay on the full
    sky band, so it never repeats with the scene loop.

    The band spans the WHOLE width: with no corner card left to hide
    behind (the band once abutted an opaque card at x=19, and trains
    "emerged from the ether" there once the card was gone), the freight
    now enters at one true screen edge and leaves at the other. The
    status digits are repainted on top of every frame by the production
    painter, so the train passes behind the clock like anything behind a
    HUD — and the crossing waits for a window inside the current minute,
    so the baked clock can never go stale mid-crossing."""
    loop = asyncio.get_running_loop()
    sec = datetime.now().second
    if sec > 35:                      # ~20s crossing + margin fits before :59
        await asyncio.sleep(60 - sec + 0.5)
    now = datetime.now(timezone.utc)
    scene_frames = await loop.run_in_executor(
        None, lambda: render_loop_frames(now, state.weather, seed=1,
                                         scene="backroads"))
    band = scene_frames[0].crop((0, 0, W, 6))
    # Which band pixels are foreground trees? Diff the same frame rendered
    # without the lane, rather than restating the lane's geometry here.
    bare = await loop.run_in_executor(
        None, lambda: render_scene(now, state.weather, 1, phase=0.0,
                                   scene="backroads", lane=False))
    foreground = frozenset(
        (x, y) for y in range(6) for x in range(W)
        if band.getpixel((x, y)) != bare.getpixel((x, y)))
    night = (elevation(OBSERVER, now) < 2) or state.weather.stormy
    frames = await loop.run_in_executor(
        None, _freight_frames, band, night, random.Random(), foreground)
    for frame in frames:
        _bake_status(frame.load(), now, state.weather, 0.0,
                     scene="backroads")
    blob = anim.encode_anim(frames, fps=12)
    stamp = datetime.now().strftime("%H%M%S")
    fname = f"train_{stamp}.anim"
    dur = len(frames) / 12
    await bb.assets_upload(APP_NAME, fname, blob)
    try:
        await bb.display_draw(types.DisplayElements(
            application_name=APP_NAME, priority=PRIORITY,
            elements=[types.AnimationElement(
                id=f"trn{stamp}", type="animation", path=fname,
                loop=False, x=0, y=0,
                display=types.DisplayName.FRONT,
                timeout=int(dur) + 2)]))
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):  # refused outright: the file was never opened
            try:
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{fname}")
            except Exception:  # noqa: BLE001
                pass
        raise
    logger.info("train: a train is crossing (%.0fs)", dur)
    await asyncio.sleep(dur + 4)
    try:
        await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{fname}")
    except Exception:  # noqa: BLE001
        pass


async def traffic_crossing(bb, state: SkyState) -> None:
    """One episode of traffic, once — a device-played overlay on the three
    rows cars occupy, so nothing about it repeats with the scene loop."""
    loop = asyncio.get_running_loop()
    now = datetime.now(timezone.utc)
    local = now.astimezone(TZ)
    elev = elevation(OBSERVER, now)
    lights_on = elev < 2 or state.weather.stormy
    _, mean_vehicles = traffic_density(local.hour)

    def _build():
        scene = render_scene(now, state.weather, 1, phase=0.0,
                             scene="backroads")
        bare = render_scene(now, state.weather, 1, phase=0.0,
                            scene="backroads", lane=False)
        top, rows = TRAFFIC_BAND_TOP, TRAFFIC_BAND_ROWS
        band = scene.crop((0, top, W, top + rows))
        # The poplar trunks cross these rows; learn which pixels they own by
        # diffing the lane away, exactly as the freight overlay does.
        foreground = frozenset(
            (x, y) for y in range(rows) for x in range(W)
            if scene.getpixel((x, top + y)) != bare.getpixel((x, top + y)))
        # Its own entropy, every time: no seed, no bucket, no repetition.
        rng = random.Random()
        n = max(1, int(round(rng.gauss(mean_vehicles, 0.8))))
        plan = plan_traffic(rng, local.hour, lights_on, n)
        # Same ambient the scene itself uses, so an overlay car is lit like
        # the road it drives on (render_scene collapses cloud under storm).
        cloud = 1.0 if state.weather.stormy else state.weather.cloud_frac
        amb = _ambient(elev, cloud, state.weather)
        frames = traffic_episode_frames(band, plan, lights_on, amb,
                                        foreground)
        return frames, plan

    frames, plan = await loop.run_in_executor(None, _build)
    blob = anim.encode_anim(frames, fps=TRAFFIC_FPS)
    stamp = datetime.now().strftime("%H%M%S")
    fname = f"traffic_{stamp}.anim"
    dur = len(frames) / TRAFFIC_FPS
    await bb.assets_upload(APP_NAME, fname, blob)
    try:
        await bb.display_draw(types.DisplayElements(
            application_name=APP_NAME, priority=PRIORITY,
            elements=[types.AnimationElement(
                id=f"trf{stamp}", type="animation", path=fname,
                loop=False, x=0, y=TRAFFIC_BAND_TOP,
                display=types.DisplayName.FRONT,
                timeout=int(dur) + 2)]))
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):  # refused outright: the file was never opened
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{fname}")
        raise
    logger.info("traffic: %d vehicle(s) over %.0fs (%s)", len(plan), dur,
                ", ".join(v["kind"] for v in plan))
    await asyncio.sleep(dur + 3)
    with contextlib.suppress(Exception):
        await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{fname}")


async def watch_traffic(bb, state: SkyState) -> None:
    """Cars come when they come. The gap between episodes is drawn fresh
    each time from the hour's own density, so the road is never on a
    metronome — which is the whole point of taking traffic out of the loop.
    """
    while True:
        mean_gap, _ = traffic_density(datetime.now(TZ).hour)
        await asyncio.sleep(random.expovariate(1.0 / mean_gap) + 4.0)
        if state.scene != "backroads" or state.weather.severe:
            continue
        if state.scrub_slot is not None:
            continue          # the Time Machine owns the road while scrubbing
        try:
            await traffic_crossing(bb, state)
        except exceptions.BusyBarAPIError:
            pass              # display owned elsewhere; the road waits
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
            logger.warning("traffic episode failed: %s",
                           describe_exception(exc))


async def watch_trains(bb, state: SkyState) -> None:
    """Every so often, if the farm road is on stage, a train rolls
    through — an event, not a loop."""
    while True:
        await asyncio.sleep(random.uniform(2.5 * 60, 6 * 60))
        if state.scene == "backroads" and not state.weather.severe:
            try:
                await train_crossing(bb, state)
            except exceptions.BusyBarAPIError:
                pass  # display owned elsewhere; the train waits
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
                # busylib raises BusyBarRequestError (a sibling class) for
                # transport trouble, and the encoder can raise anything. This
                # task used to die silently: no more freights until restart,
                # while barkeep still reported the app healthy.
                logger.warning("train crossing failed: %s", exc)


def snow_tier(depth_m: float | None) -> int:
    """Settled snow depth as one of four looks: 0 none, 1 dusting,
    2 covered, 3 deep.

    Three visible steps rather than a ramp because the panel's ~30% contrast
    floor crushes anything finer -- a smooth scale would be both invisible
    and untestable.
    """
    if not depth_m or depth_m < SNOW_DUSTING_M:
        return 0
    if depth_m < SNOW_COVERED_M:
        return 1
    if depth_m < SNOW_DEEP_M:
        return 2
    return 3


def _flash_distance(event: float | _FlashEvent) -> float:
    if isinstance(event, _FlashEvent):
        return event.distance_km
    return float(event)


def _coalesce_flashes(
    queue: asyncio.Queue,
    event: float | _FlashEvent,
) -> float | _FlashEvent:
    """Drain the backlog, keeping the nearest strike.

    A cell can outrun FLASH_MIN_GAP_S, and buffered strikes would keep
    strobing the bar long after the storm cleared — half an hour of queued
    lightning under a sky that has gone quiet. One flash stands for the
    burst, at its nearest (so brightest) distance.
    """
    while True:
        try:
            candidate = queue.get_nowait()
        except asyncio.QueueEmpty:
            return event
        if _flash_distance(candidate) < _flash_distance(event):
            event = candidate


def _coalesce_fresh_flashes(
    queue: asyncio.Queue,
    event: float | _FlashEvent,
    *,
    now: float,
) -> float | _FlashEvent | None:
    """Drain a burst and keep its nearest still-current strike.

    Plain floats remain accepted for the small host-side helper contracts;
    live listener events are timestamped, so a stalled weather feed cannot
    replay old lightning when the scene becomes eligible again.
    """
    nearest: float | _FlashEvent | None = None
    while True:
        if not isinstance(event, _FlashEvent):
            fresh = True
        else:
            age = now - event.observed_at
            fresh = 0.0 <= age <= FLASH_EVENT_TTL_S
        if fresh and (
            nearest is None
            or _flash_distance(event) < _flash_distance(nearest)
        ):
            nearest = event
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            return nearest


def _enqueue_flash(
    queue: asyncio.Queue,
    dist: float,
    *,
    observed_at: float | None = None,
) -> None:
    """Bound a detector burst and retain its nearest (brightest) strike."""
    event: float | _FlashEvent
    if observed_at is None:
        event = dist
    else:
        event = _FlashEvent(distance_km=dist, observed_at=observed_at)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        nearest: float | _FlashEvent | None
        if observed_at is None:
            nearest = _coalesce_flashes(queue, event)
        else:
            nearest = _coalesce_fresh_flashes(
                queue, event, now=observed_at,
            )
        if nearest is not None:
            queue.put_nowait(nearest)


async def watch_meteors(bb, state: SkyState) -> None:
    """Very infrequently, on a clear enough night, one shooting star."""
    while True:
        await asyncio.sleep(random.uniform(20 * 60, 60 * 60))
        wx = state.weather
        if (elevation(OBSERVER, datetime.now(timezone.utc)) < -8
                and wx.cloud_frac < 0.5
                and not (wx.rain or wx.snow or wx.stormy)):
            try:
                await meteor(bb, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
                logger.debug("meteor failed: %s", exc)


def _git_rev() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent.parent,
            capture_output=True, text=True, timeout=5,
        ).stdout.strip() or "unknown"
    except Exception:  # noqa: BLE001 - a missing git never blocks the sky
        return "unknown"


def _report_inputs_ready(state: SkyState) -> bool:
    """Whether CLI narration has every forecast source available here."""
    return bool(
        state.hourly
        and (state.forecast or state.nws_point_covered is False)
    )


async def report_once() -> None:
    """Prepare and speak the live report synchronously for CLI auditioning."""
    state = SkyState()
    bb = await aconnect()
    poller = asyncio.create_task(poll_nws(state))
    try:
        for _ in range(80):  # up to ~20s for the sources available here
            if _report_inputs_ready(state):
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.0)  # let the obs land too
        text = _current_report_text(state)
        fname = await _prepare_report_take(bb, state, text)
        try:
            await _play_audio(bb, state, fname, "report", lambda: True)
        except Exception as exc:
            # _play_audio re-raises a refusal on purpose — it must not answer a
            # 409 with the device-global STOP, which would silence another app.
            # Catching it belongs here, in the one-shot CLI path the workflow
            # tells you to run on hardware.
            if not _is_refusal(exc):
                raise
            logger.info("a BUSY/CUSTOM session owns audio; the report will keep")
    finally:
        poller.cancel()
        try:
            await poller
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await bb.aclose()


# Every versioned family this app writes. Anything missing here leaks
# permanently: a crossing whose cleanup is skipped (a 409, or SIGTERM during
# its sleep) leaves a train_ file no restart ever reclaims.
GENERATION_FILES = re.compile(
    r"^(tl_|rva_|report_|sky_|train_|traffic_|alert_|flash_|meteor_)"
    r".*\.(anim|snd)$"
)

# Names this app wrote under an OLDER scheme, which no pattern above matches
# and therefore nothing has ever reclaimed. Found resident on a live device:
#
#   siren.snd      2.65 MB  the unversioned siren, before content addressing
#   sky_a.png               the a/b alternation the skill names as a mistake
#   sky_b.png
#   sky_demo.png
#
# The versioned siren (siren_<digest>.snd) is deliberately NOT here: it is the
# live asset with its own retirement path in retire_siren_assets. This set is
# only for schemes we have stopped using, and it must stay a closed list —
# "anything I don't recognise" would eat the deterministic report cache.
LEGACY_ORPHANS = frozenset({
    "siren.snd", "sky_a.png", "sky_b.png", "sky_demo.png", "tts.snd",
})


async def sweep_stale_assets(bb) -> None:
    """Remove transient generations orphaned by previous instances.

    A crash can leave ~130-320 kB animations or legacy multi-megabyte report
    takes behind until this next startup sweep (213 orphans once accumulated
    before this existed). Deterministic text+voice reports are the durable
    cache and are bounded separately when the report worker adopts them. This
    runs before the instance draws anything, so nothing swept can be playing.
    """
    try:
        await bb.display_clear(application_name=APP_NAME)
        files = (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
        # Deterministic text+voice report paths are the durable cache. They are
        # adopted lazily by the report worker; only old timestamp generations
        # and the other transient asset families are restart-orphans.
        stale = [
            f.name for f in files
            if (GENERATION_FILES.match(f.name)
                and _report_file_identity(f.name) is None)
            or f.name in LEGACY_ORPHANS
        ]
        for name in stale:
            try:
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{name}")
            except Exception:  # noqa: BLE001 - reaping is best-effort
                pass
        if stale:
            logger.info("swept %d stale asset generations", len(stale))
    except Exception as exc:  # noqa: BLE001 - hygiene must never be fatal
        logger.warning("asset sweep failed: %s", exc)


async def run(once: bool) -> None:
    """Run Skystrip after the caller has explicitly configured the process.

    CLI and Barkeep callers use :func:`main`, which calls
    :func:`configure_runtime` first. Programmatic callers must do the same when
    they want owner configuration rather than the public-safe import defaults.
    """
    logger.info("skystrip %s starting", _git_rev())
    if unlocated := warn_if_unlocated():
        logger.warning("%s", unlocated)
    state = SkyState()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    bb = await connect_with_retry(
        aconnect, stop, log=logger, describe=describe_exception)
    tasks: list[asyncio.Task] = []
    try:
        # Inside the try: a failure provisioning the siren used to propagate
        # out of run() with no cleanup at all — no aclose(), no display clear,
        # no signal handlers removed. A missing siren must not take the sky
        # down with it.
        if once:
            # --once is the audition path and may run beside a live instance
            # (barkeep's, on the Pi). The sweep is app-scoped with no instance
            # identity, so it would delete the running sky's assets.
            logger.info(
                "--once: skipping the asset sweep, another instance may be live")
        else:
            await sweep_stale_assets(bb)
            try:
                await ensure_siren_asset(bb, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sky outranks the siren
                logger.warning(
                    "siren provisioning failed (%s); continuing without it",
                    describe_exception(exc))

        state.scene_idx = load_scene_idx()
        if once:
            now = datetime.now(timezone.utc)
            try:
                await push_scene(bb, state, now,
                                 render_loop_frames(now, state.weather, seed=0,
                                                    scene=state.scene))
            except exceptions.BusyBarAPIError as exc:
                if not _is_refusal(exc):
                    raise
                logger.info("a BUSY/CUSTOM session owns the display")
        else:
            logger.info("waiting for a fresh weather snapshot before first draw")
        if once:
            logger.info("pushed one loop, exiting (element self-clears in %ss)",
                        ELEMENT_TIMEOUT_S)
            return

        tasks = [
            asyncio.create_task(
                poll_alerts(state, wait_for_point_check=True)
            ),
            asyncio.create_task(poll_nws(state)),
            asyncio.create_task(poll_radar(state)),
            asyncio.create_task(listen_buttons(bb, state)),
            asyncio.create_task(severe_alarm(bb, state)),
            asyncio.create_task(maintain_siren_asset(bb, state)),
            asyncio.create_task(build_timeline(bb, state)),
            asyncio.create_task(ambient_lights(bb, state)),
            asyncio.create_task(bake_report(bb, state)),
            asyncio.create_task(watch_trains(bb, state)),
            asyncio.create_task(watch_traffic(bb, state)),
            asyncio.create_task(watch_meteors(bb, state)),
        ]
        if LIGHTNING_WS is not None:
            tasks.append(asyncio.create_task(listen_lightning(state)))
        else:
            logger.info(
                "live lightning disabled; configure SKYSTRIP_LIGHTNING_WS "
                "with an authorized secure feed to enable it"
            )
        next_draw = 0.0
        device_backoff = 0.0
        while not stop.is_set():
            loop_now = loop.time()
            if loop_now >= next_draw:
                if not weather_is_fresh(state):
                    # Do not refresh a plausible-but-invented default or an
                    # expired last-good snapshot. The native lease will clear
                    # an old scene if sources remain unavailable — but it must
                    # clear to a stated reason, not to an unexplained black
                    # panel that reads as a dead app. Only while this app owns
                    # the view: an alert card and Time Machine both outrank it.
                    if (
                        state.visual_alert is None
                        and state.scrub_slot is None
                        and not state.shutting_down
                    ):
                        await keep_stale_notice(bb, state)
                    next_draw = loop_now + 1.0
                    await asyncio.sleep(0)
                    continue
                # Fire just before each wall-clock minute so the baked clock
                # flips on the display as the minute actually changes
                wall = datetime.now(timezone.utc)
                to_boundary = 60.0 - (wall.second + wall.microsecond / 1e6) - 2.0
                if to_boundary < 5.0:
                    to_boundary += 60.0
                next_draw = loop_now + to_boundary
                seed = int(loop_now // 600)  # texture drifts every 10 min
                # Bake the minute this push will live through: ticks fire
                # just before the boundary, so back-half seconds mean the
                # upcoming minute; a late fire keeps the current one
                if wall.second >= 30:
                    now = wall + timedelta(seconds=61 - wall.second)
                else:
                    now = wall
                frames = render_loop_frames(now, state.weather, seed,
                                            scene=state.scene)
                try:
                    await push_scene(bb, state, now, frames)
                    device_backoff = 0.0
                except exceptions.BusyBarAPIError as exc:
                    if _is_refusal(exc):
                        # A BUSY/CUSTOM session owns the display; the sky yields
                        logger.debug("yielding to higher-priority app")
                    else:
                        logger.warning("draw rejected: %s", exc)
                except Exception as exc:  # noqa: BLE001 - offline is a state
                    device_backoff = min(max(device_backoff * 2, 5), 120)
                    next_draw = loop_now + device_backoff
                    logger.warning("scene push failed (%s); retry in %.0fs",
                                   exc, device_backoff)
            if state.scene_change.is_set():
                state.scene_change.clear()
                next_draw = 0.0  # redraw with the new scene immediately
                continue
            if (
                state.scrub_slot is not None
                and not _unacknowledged_alert_active(state)
                and loop_now - state.scrub_touched > SCRUB_SNAP_S
            ):
                was_revealed = state.revealed
                state.scrub_slot = None
                state.revealed = False
                logger.info("time machine: back to now")
                try:
                    state.readout_gen = (state.readout_gen + 1) % 100
                    await draw_scrub_readout(bb, state, "NOW", timeout=1)
                    if was_revealed:
                        await retire_reveal(bb, state)
                except Exception:  # noqa: BLE001
                    pass
                continue
            if _scrub_reveal_ready(state, loop_now):
                # The wheel rested: jump the scene to the chosen moment
                slot = state.scrub_slot
                meta = state.timeline_meta
                assert slot is not None and meta is not None
                intent = state.view_generation
                state.reveal_pending = True
                try:
                    if meta["scene"] != state.scene:
                        # Timeline still rebuilding for this scene: never
                        # serve the old scene's frame — hold the readout
                        # and jump straight to the animated render
                        await draw_scrub_readout(
                            bb, state,
                            _slot_label(meta, slot), timeout=6)
                        spawn_owned(
                            state,
                            animate_reveal(
                                bb, state, slot, initial=True, intent=intent))
                        continue
                    fname = meta["file"]
                    if (state.last_reveal is not None
                            and state.last_reveal["slot"] == slot):
                        state.revealed = True
                        state.reveal_pending = False
                        continue  # already showing this slot
                    state.reveal_n += 1
                    eid = f"rv{state.reveal_n}"
                    prev = state.last_reveal
                    old_readout = state.last_readout
                    elements = _retired_readout_elements(old_readout)
                    if prev is not None:
                        elements.append(types.AnimationElement(
                            id=prev["eid"], type="animation",
                            path=prev["fname"],
                            section=prev.get("section"), loop=True,
                            x=0, y=0,
                            display=types.DisplayName.FRONT, timeout=1,
                        ))
                    elements.append(types.AnimationElement(
                        id=eid, type="animation", path=fname,
                        section=f"s{slot:02d}", loop=True, x=0, y=0,
                        display=types.DisplayName.FRONT, timeout=60,
                    ))
                    async with state.display_lock:
                        if (
                            state.view_generation != intent
                            or state.scrub_slot != slot
                            or _unacknowledged_alert_active(state)
                        ):
                            state.reveal_pending = False
                            continue
                        await bb.display_draw(types.DisplayElements(
                            application_name=APP_NAME,
                            priority=PRIORITY,
                            elements=elements,
                        ))
                    state.last_reveal = {
                        "eid": eid, "slot": slot, "fname": fname,
                        "section": f"s{slot:02d}"}
                    state.last_readout = None
                    state.revealed = True
                    state.reveal_pending = False
                    # And bring the moment to life in the background
                    spawn_owned(
                        state,
                        animate_reveal(bb, state, slot, intent=intent))
                except Exception as exc:  # noqa: BLE001
                    state.reveal_pending = False
                    logger.warning("reveal failed: %s", exc)
                continue
            try:
                flash_event = await asyncio.wait_for(
                    state.flash_queue.get(),
                    timeout=min(1.0, max(0.05, next_draw - loop_now)),
                )
                flash_event = _coalesce_fresh_flashes(
                    state.flash_queue,
                    flash_event,
                    now=loop.time(),
                )
                if flash_event is None:
                    logger.debug("discarded stale lightning burst")
                    continue
                dist = _flash_distance(flash_event)
                try:
                    await flash(bb, state, dist)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("flash draw failed: %s", exc)
                await asyncio.sleep(FLASH_MIN_GAP_S)
            except asyncio.TimeoutError:
                pass
    finally:
        state.shutting_down = True
        for task in tasks:
            task.cancel()
        for task in tuple(state.detached_tasks):
            task.cancel()
        if tasks or state.detached_tasks:
            await asyncio.gather(
                *tasks,
                *tuple(state.detached_tasks),
                return_exceptions=True,
            )
        if state.audio_owner is not None or state.audio_stop_pending:
            generation = _claim_audio_stop(state)
            await stop_audio(bb, state, generation)
        # Best-effort cleanup: never let it mask the exception that got us here
        try:
            await bb.usb.send_command("status_lights", "0", "0", "0")
        except Exception:  # noqa: BLE001
            pass
        try:
            if not once:
                await bb.display_clear(application_name=APP_NAME)
                logger.info("cleared")
        except Exception as exc:  # noqa: BLE001
            logger.warning("cleanup draw-clear failed: %s", exc)
        try:
            await bb.aclose()
        except Exception:  # noqa: BLE001
            pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--report", action="store_true",
        help=("fetch and speak one live weather report; standalone use also "
              "requires --enable-network-providers"),
    )
    parser.add_argument("--once", action="store_true",
                        help="push one local snapshot; no provider polling")
    parser.add_argument(
        "--enable-network-providers",
        action="store_true",
        help=("standalone live/report modes only: allow Open-Meteo and, for "
              "the watcher, RainViewer requests after reviewing their terms; "
              "this flag does not grant data rights"),
    )
    parser.add_argument(
        "--preview", metavar="PNG",
        help="render current scene to a PNG and exit (no device or provider I/O)",
    )
    parser.add_argument("--at", metavar="HH:MM",
                        help="preview only: pretend it's this local time")
    parser.add_argument("--cloud", type=float, default=None,
                        help="preview only: cloud fraction 0..1")
    parser.add_argument("--storm", action="store_true",
                        help="preview only: storm palette")
    parser.add_argument("--rain", action="store_true", help="preview only")
    parser.add_argument("--raintier", type=int, default=1, choices=(0, 1, 2),
                        help="preview only: 0 drizzle / 1 rain / 2 downpour")
    parser.add_argument("--snow", action="store_true", help="preview only")
    parser.add_argument("--fog", action="store_true",
                        help="force reported fog (preview only)")
    parser.add_argument("--obscuration", choices=("haze", "smoke", "dust",
                                                  "ash"),
                        help="force an obscuration (preview only)")
    parser.add_argument("--snowdepth", type=float, default=0.0,
                        metavar="METRES",
                        help="settled snow on the ground, preview only "
                             "(0.01=dusting, 0.08=covered, 0.25=deep)")
    parser.add_argument("--wind", type=float, default=0.0,
                        help="preview only: wind speed km/h")
    parser.add_argument("--winddir", type=float, default=None,
                        help="preview only: wind FROM direction, degrees")
    parser.add_argument("--temp", type=float, default=20.0,
                        help="preview only: temperature C")
    parser.add_argument("--month", type=int, default=None,
                        help="preview only: pretend month 1-12")
    parser.add_argument("--humidity", type=float, default=50.0,
                        help="preview only: relative humidity %%")
    parser.add_argument("--vis", type=float, default=16000.0,
                        help="preview only: visibility in meters")
    parser.add_argument("--scene", choices=SCENES, default="house",
                        help="preview only: which scene to render")
    parser.add_argument("--moonday", type=float, default=None,
                        help="preview only: force moon phase day 0-29.5")
    parser.add_argument("--christmas", action=argparse.BooleanOptionalAction,
                        default=None,
                        help="force the Christmas treatment on or off, "
                             "preview only (default: follow the date)")
    return parser


OPEN_METEO_NOTICE = (
    "Weather data by Open-Meteo.com (CC BY 4.0; free API non-commercial). "
    "Terms: https://open-meteo.com/en/terms"
)
RAINVIEWER_NOTICE = (
    "Weather radar data by RainViewer (public API for personal, educational, "
    "and small-scale community use). Terms: "
    "https://www.rainviewer.com/api.html"
)


def _provider_network_required(args: argparse.Namespace) -> bool:
    """Whether the selected CLI path starts a built-in provider poller.

    Preview is renderer-only. ``--once`` talks to the bar but renders the
    local default snapshot; it never starts Open-Meteo or RainViewer polling.
    ``--report`` takes precedence over ``--once`` in :func:`main`, so their
    combined spelling still requires the provider boundary.
    """
    return args.preview is None and (args.report or not args.once)


def _provider_network_enabled(args: argparse.Namespace) -> bool:
    """Whether this process crossed a supported provider-use boundary."""
    return (
        not _provider_network_required(args)
        or args.enable_network_providers
        or os.environ.get("BARKEEP_MANAGED") == "1"
    )


def _provider_notice(args: argparse.Namespace) -> str:
    """Return attribution for the providers this exact CLI mode will call."""
    if args.report:
        return f"Skystrip data: {OPEN_METEO_NOTICE}"
    return f"Skystrip data: {OPEN_METEO_NOTICE} {RAINVIEWER_NOTICE}"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # Timestamps: dsn has always had them and skystrip has not, so 800 kB of
    # the primary debugging surface on a headless Pi could not be correlated
    # with anything. The redaction filter is a backstop behind
    # describe_exception, so the next code path that formats a URL is covered
    # by construction rather than by review.
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(CoordinateRedactingFilter())
    logging.getLogger("busylib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        configure_runtime()
    except ValueError as exc:
        # Under systemd a raw ValueError is a silent restart loop with a dark
        # panel; the journal should carry one named sentence instead.
        raise SystemExit(f"skystrip configuration error: {exc}") from None
    # Before app work, and on stderr, because an unconfigured install otherwise
    # looks like a working one.
    if unlocated := warn_if_unlocated():
        print(f"warning: {unlocated}", file=sys.stderr)

    if args.preview:
        now = datetime.now(timezone.utc)
        if args.at:
            hh, mm = map(int, args.at.split(":"))
            now = datetime.now(TZ).replace(hour=hh, minute=mm).astimezone(timezone.utc)
        if args.month:
            now = (now.astimezone(TZ).replace(month=args.month, day=15)
                   .astimezone(timezone.utc))
        wx = WeatherState(
            cloud_frac=args.cloud if args.cloud is not None else 0.0,
            rain=args.rain, rain_tier=args.raintier,
            snow=args.snow, thunder=args.storm,
            wind_kmh=args.wind, wind_dir=args.winddir, temp_c=args.temp,
            humidity=args.humidity, visibility_m=args.vis,
            snow_depth_m=args.snowdepth, fog=args.fog,
            obscuration=args.obscuration or "",
        )
        if args.moonday is not None:
            global MOON_DAY_OVERRIDE
            MOON_DAY_OVERRIDE = args.moonday
        if args.christmas is not None:
            globals()["CHRISTMAS_FORCED"] = args.christmas
        frames = render_loop_frames(now, wx, seed=0, scene=args.scene)
        big = [f.resize((W * 8, H * 8), Image.Resampling.NEAREST) for f in frames]
        if args.preview.endswith(".gif"):
            big[0].save(args.preview, save_all=True, append_images=big[1:],
                        duration=1000 // ANIM_FPS, loop=0)
        else:
            big[0].save(args.preview)
        print(f"saved {args.preview} (clock {clock_str(now)}; "
              f"moon phase day {moon.phase(now.astimezone(TZ).date()):.1f}/29.5)")
        return
    if not _provider_network_enabled(args):
        providers = "Open-Meteo" if args.report else "Open-Meteo/RainViewer"
        parser.error(
            f"standalone {providers} polling is off. Review "
            "apps/skystrip.md#provider-terms-and-commercial-use, then rerun "
            "with --enable-network-providers. The flag enables requests; it "
            "does not grant rights or assert that your use meets provider "
            "terms. --once by itself and --preview do not poll these providers."
        )
    if _provider_network_required(args):
        # Flush before creating the coroutine: the attribution and use limits
        # must be visible before the first provider request, not merely after
        # a fast HTTP response has already arrived.
        print(_provider_notice(args), file=sys.stderr, flush=True)
    if args.report:
        asyncio.run(report_once())
        return
    asyncio.run(run(args.once))


def render_sky(now: datetime, wx: WeatherState, seed: int) -> Image.Image:
    """Back-compat alias for earlier scripts."""
    return render_scene(now, wx, seed)


if __name__ == "__main__":
    main()

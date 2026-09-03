"""dsn — NASA's Deep Space Network, live, on the 72x16 front strip.

    uv run apps/dsn.py --dry-run
    uv run apps/dsn.py --once
    uv run apps/dsn.py

The default Network view is a literal three-site ground-dish roster: site link
total, active dish suffixes, and an attached count when one physical antenna
carries several spacecraft links. A rested wheel choice opens a bounded
selected-dish Focus with one source-reported local alt-az aim and the links
that share it. Hold the wheel to move through that global view, a selected-link
antenna/RF instrument, and the original Earth-to-spacecraft distance journey.
The prior Three Skies triptych and legacy paged contact rows remain available
with DSN_NETWORK_STYLE=skies and rows. The distance renderer remains:
a represented carrier slice crosses at the published one-way light-time
estimate, normally compressed 600x and at that estimated cadence when locked.

Controls: the wheel moves through live signals behind an instant pop-up (the
scene commits when it rests); a tap starts the original real-time light-time
watch; a hold cycles Network, Instrument and Distance; START narrates
the pass. Narration is cache-only on the button path: a missing line is
prioritised for background preparation and acknowledged immediately in pixels.

Drawn mostly-OFF on purpose. The panel's LEDs are physically spaced about a
pixel apart, so a filled background reads as a haze of separated dots and
drowns whatever sits on it; black is truly off, the gaps disappear into it,
and only lit pixels carry shape. Nothing here is a gradient.

Everything is gated on the live feed: no link, no scene.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import OrderedDict
import importlib
import logging
import math
import os
import re
import signal
import sys
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING, Protocol, cast

import hashlib
import json
from pathlib import Path

import httpx
from busylib import exceptions, types
from PIL import Image

from busybar_dev import aconnect, anim, load_env
from busybar_dev.device import (
    connect_with_retry,
    is_refusal,
    storage_file_matches,
)
from busybar_dev.tts import synth_snd

if TYPE_CHECKING:
    import dsn_config as _dsn_config
    import dsn_source as _dsn_source
elif __package__:
    _dsn_config = importlib.import_module(".dsn_config", __package__)
    _dsn_source = importlib.import_module(".dsn_source", __package__)
else:
    _dsn_config = importlib.import_module("dsn_config")
    _dsn_source = importlib.import_module("dsn_source")

DsnConfig = _dsn_config.DsnConfig
DEFAULT_DSN_CONFIG = _dsn_config.DEFAULT_DSN_CONFIG
NETWORK_STYLES = _dsn_config.NETWORK_STYLES
REPO_ROOT = _dsn_config.REPO_ROOT
VIEW_ORDER = _dsn_config.VIEW_ORDER
_positive_seconds = _dsn_config._positive_seconds
parse_runtime_config = _dsn_config.parse_runtime_config
resolve_cache_dir = _dsn_config.resolve_cache_dir
resolve_managed_cache_root = _dsn_config.resolve_managed_cache_root

# Keep the established ``apps.dsn`` import surface while the source trust
# boundary lives in a small, independently testable module. Standalone
# ``python apps/dsn.py`` and package imports both resolve the same objects.
DownStream = _dsn_source.DownStream
UpStream = _dsn_source.UpStream
Link = _dsn_source.Link
SourceValidationError = _dsn_source.SourceValidationError
C_KM_S = _dsn_source.C_KM_S
FEED_FUTURE_SKEW_S = _dsn_source.FEED_FUTURE_SKEW_S
MIN_UNIX_TIMESTAMP_MS = _dsn_source.MIN_UNIX_TIMESTAMP_MS
MAX_UNIX_TIMESTAMP_MS = _dsn_source.MAX_UNIX_TIMESTAMP_MS
FEED_XML_MAX_BYTES = _dsn_source.FEED_XML_MAX_BYTES
CONFIG_XML_MAX_BYTES = _dsn_source.CONFIG_XML_MAX_BYTES
SOURCE_CODE_MAX = _dsn_source.SOURCE_CODE_MAX
SOURCE_DISH_CODE_MAX = _dsn_source.SOURCE_DISH_CODE_MAX
SOURCE_NAME_MAX = _dsn_source.SOURCE_NAME_MAX
SOURCE_ACTIVITY_MAX = _dsn_source.SOURCE_ACTIVITY_MAX
FEED_DISH_ELEMENTS_MAX = _dsn_source.FEED_DISH_ELEMENTS_MAX
FEED_DISHES_PER_SITE_MAX = _dsn_source.FEED_DISHES_PER_SITE_MAX
FEED_LINKS_PER_DISH_MAX = _dsn_source.FEED_LINKS_PER_DISH_MAX
FEED_LINKS_MAX = _dsn_source.FEED_LINKS_MAX
FEED_SIGNAL_RECORDS_PER_DISH_MAX = _dsn_source.FEED_SIGNAL_RECORDS_PER_DISH_MAX
CONFIG_SPACECRAFT_MAX = _dsn_source.CONFIG_SPACECRAFT_MAX
CONFIG_DISHES_MAX = _dsn_source.CONFIG_DISHES_MAX
CONFIG_SITES_MAX = _dsn_source.CONFIG_SITES_MAX
MAX_SOURCE_NUMERIC_ID = _dsn_source.MAX_SOURCE_NUMERIC_ID
MAX_SOURCE_NUMBER_CHARS = _dsn_source.MAX_SOURCE_NUMBER_CHARS
MAX_RANGE_KM = _dsn_source.MAX_RANGE_KM
RECEIVE_POWER_MIN_DBM = _dsn_source.RECEIVE_POWER_MIN_DBM
SITE_NAMES = _dsn_source.SITE_NAMES
REQUIRED_SITE_NAMES = _dsn_source.REQUIRED_SITE_NAMES
NOT_SPACECRAFT = _dsn_source.NOT_SPACECRAFT
_rate = _dsn_source._rate
_uplink_power = _dsn_source._uplink_power
_angle = _dsn_source._angle
_dbm = _dsn_source._dbm
_signal_dbm = _dsn_source._signal_dbm
_bounded_source_text = _dsn_source._bounded_source_text
_required_source_text = _dsn_source._required_source_text
_source_numeric_id = _dsn_source._source_numeric_id
canonical_site_name = _dsn_source.canonical_site_name
feed_timestamp_ms = _dsn_source.feed_timestamp_ms
source_timestamp_valid = _dsn_source.source_timestamp_valid
parse_feed = _dsn_source.parse_feed
parse_config = _dsn_source.parse_config
band_key = _dsn_source.band_key


class PixelBuffer(Protocol):
    """The RGB pixel-access operations used by the pure renderers."""

    def __getitem__(self, xy: tuple[int, int]) -> float | tuple[int, ...]: ...

    def __setitem__(
        self, xy: tuple[int, int], colour: float | tuple[int, ...],
    ) -> None: ...


def image_pixels(image: Image.Image) -> PixelBuffer:
    """Return writable pixels for an in-memory image created by this module."""
    pixels = image.load()
    if pixels is None:
        raise RuntimeError("Pillow did not expose pixels for an in-memory image")
    return cast(PixelBuffer, pixels)


APP_NAME = "dsn"
PRIORITY = 30           # ambient foreground, same tier as skystrip
W, H = 72, 16
UA = {"User-Agent": "dsn (busybar hobby project)"}

DSN_XML = "https://eyes.nasa.gov/dsn/data/dsn.xml"
CONFIG_XML = "https://eyes.nasa.gov/dsn/config.xml"
HORIZONS = "https://ssd.jpl.nasa.gov/api/horizons.api"
AU_LIGHT_S = 499.004784        # seconds of light per astronomical unit


POLL_S = DEFAULT_DSN_CONFIG.poll_s
ROTATE_S = DEFAULT_DSN_CONFIG.rotate_s
VOICE = DEFAULT_DSN_CONFIG.voice
NARRATION_STARTING = "STARTING UP"
NARRATION_PREPARING = "PREPARING..."
NARRATION_READY = "PRESS START"
NARRATION_BUSY = "AUDIO BUSY"
NARRATION_ERROR = "AUDIO ERROR"
DEFAULT_VIEW = DEFAULT_DSN_CONFIG.default_view
NETWORK_FOCUS_STYLES = frozenset({"dishes", "skies"})
DSN_NETWORK_STYLE = DEFAULT_DSN_CONFIG.network_style
# Kokoro runs at about 1x realtime on a Pi 5: a 12-second line costs 12
# seconds to synthesise. Far too slow to start on a button press, so lines
# are baked ahead of time and cached on device by a hash of their own text.
# Must exceed a full rotation, or the cache converges on nothing: a sequential
# scan of a working set larger than the cache misses EVERY time, because the
# entry evicted to make room is the one needed next. Measured on the Pi at 10:
# 13 dish->craft pairs in rotation, 26 synths per 13-line lap, one core pegged
# indefinitely. Both premises of the old value were also wrong -- lines are
# ~2.2 MB, not 900 KB, and the flash is 7.5 GB with 7.4 GB free, so the whole
# cache was 0.3% of the device. 48 lines is ~106 MB, 1.4% of free space.
SPEECH_CACHE_MAX = 48
# Network can retain one ambient selection, one Focus Lens and the two existing
# detail views per contact. Forty-eight sparse assets are still only a few MB
# and prevent wheel A→B→A from turning into repeated flash writes.
SCENE_CACHE_MAX = 48
RANGE_NEAR_EARTH_TTL_S = 5 * 60
RANGE_INTERMEDIATE_TTL_S = 30 * 60
RANGE_TTL_S = 6 * 3600         # maximum, for genuinely deep-space targets
RANGE_RETRY_S = 60             # failure backoff is not a successful zero range
RANGE_UNAVAILABLE_RETRY_S = RANGE_TTL_S  # Horizons has no record for this id
RANGE_CACHE_VERSION = 1
SHUTDOWN_TIMEOUT_S = 2.0
# A released task needs a moment to unwind its own await points. This is
# that moment, and it is bounded so a genuinely stuck task still gets
# reported rather than waited on.
SHUTDOWN_SETTLE_S = 0.25
INTERACTIVE_IO_TIMEOUT_S = 1.0
ELEMENT_TIMEOUT_S = 180        # self-clears if we stop pushing
REALTIME_ELEMENT_TIMEOUT_S = 360  # exceeds the 300s outer-planet redraw cap
REDRAW_S = 60
SCENE_RENEW_TARGET_S = 120
ANIM_FRAMES = 40
ANIM_FPS = 5
LOOP_S = ANIM_FRAMES / ANIM_FPS   # 8s, FIXED: text scroll must not depend
INSTRUMENT_FRAMES = 40
INSTRUMENT_FPS = 5
INSTRUMENT_LOOP_S = INSTRUMENT_FRAMES / INSTRUMENT_FPS
SCROLL_SPEED_PX_S = 20.0  # matches the established eight-second Distance feel
# A generated asset is built eagerly as PIL frames before it can be uploaded.
# Keep a hostile source value from turning one native scene into unbounded RAM
# and upload time.  Forty-eight seconds is long enough to expose every column
# of the longest accepted source label at the panel-readable scroll ceiling.
MAX_ANIMATION_FRAMES = 240
FEED_DELAYED_S = max(POLL_S * 2.5, 15.0)
FEED_STALE_S = max(POLL_S * 5.0, 35.0)
# A FRESH claim is a short native-element lease, separate from the animation.
# NASA's advancing source timestamp moves a two-LED native dash without touching
# (and therefore restarting) the native carrier loop. If this process dies,
# the live claim expires soon after a running process would draw FEED DELAY.
LIVE_LEASE_TIMEOUT_S = max(30, int(FEED_DELAYED_S + POLL_S))
EVENT_TIMEOUT_S = 4
EVENT_QUEUE_MAX = 4
EVENT_MAX_AGE_S = 120
EVENT_FRAMES = 20
EVENT_FPS = 5
EVENT_EFFECTS = ("acquire", "loss", "handoff", "split", "merge",
                 "array", "unarray")
# Firmware 1.1.1 parses an asset filename through a 32-byte C buffer, leaving
# 31 visible ASCII bytes plus NUL; longer names get a bare HTTP 400.  This
# ceiling is absent from the OpenAPI schema, so keep it explicit and test every
# generated path against the physical contract.
DEVICE_ASSET_FILENAME_MAX = 31
EVENT_ASSET_VERSION = "v2"
EVENT_ASSET_CODES = {
    "acquire": "acq",
    "loss": "los",
    "handoff": "hof",
    "split": "spl",
    "merge": "mrg",
    "array": "arr",
    "unarray": "una",
}
OK_HOLD_S = 0.7
                                   # on how far away the spacecraft is
TRACK0, TRACK1 = 15, 60  # the signal's road, between globe edge and craft
RT_PACKETS = 12          # marks in a full chain, when locked to real time
# An 8-second loop can only tell the truth about a crossing it can fit. Below
# this the device animates a locked link at its real speed; above it, the
# chain is placed from the wall clock and re-pushed as it creeps.
RT_SEAMLESS_MAX_S = 120.0
DETENT_COUNTS = 1        # verified in skystrip: one detent IS one count.
                         # This was 4, which quietly demanded four physical
                         # clicks per step and made the wheel feel dead.
PICK_REST_S = 0.6        # stillness before the picked signal commits
MANUAL_DWELL_S = 120.0   # a chosen instrument remains long enough to observe

# The API's status-LED request can only blink one colour once on a draw; it has
# no addressing, patterns, or sustained-colour mode. So it
# is wired to genuine EVENTS only. Attaching it to the ordinary redraw would
# turn it into a metronome every time the scene refreshes.
LED_ARRIVAL = "#FFF4D0FF"   # a real-time light-crossing watch completed
LED_LOCKED = "#FFB300FF"    # real time engaged
LED_RELEASED = "#3355AAFF"  # back to browsing

# A PROPORTIONAL 5-tall font: most glyphs are 4 wide, M and W are 5, I and 1
# are 3. Three sizes, each forced by the panel rather than chosen.
#
# 3 columns was too cramped for the alphabet at all: it rendered '0'
# identical to 'O' and '5' identical to 'S', plus 26 pairs a single pixel
# apart, and 'ACE' read as '55'. The zero is still slashed so it can never
# be an O.
#
# 4 columns then held everything except M and W, which need two outer
# strokes AND a centre one. Cramming them in left each 14 cells of 20 with
# two ADJACENT fully-filled rows — a filled rectangle, not a letter — and
# every lighter 4-wide attempt landed a single pixel away from N. They get
# a fifth column; I and 1 give back a third. Tests enforce all of it.
FONT = {
    "A": ('0110', '1001', '1111', '1001', '1001'),     "B": ('1110', '1001', '1110', '1001', '1110'),
    "C": ('0111', '1000', '1000', '1000', '0111'),     "D": ('1110', '1001', '1001', '1001', '1110'),
    "E": ('1111', '1000', '1110', '1000', '1111'),     "F": ('1111', '1000', '1110', '1000', '1000'),
    "G": ('0111', '1000', '1011', '1001', '0111'),     "H": ('1001', '1001', '1111', '1001', '1001'),
    "I": ('111', '010', '010', '010', '111'),
    "J": ('0011', '0001', '0001', '1001', '0110'),
    "K": ('1001', '1010', '1100', '1010', '1001'),     "L": ('1000', '1000', '1000', '1000', '1111'),
    "M": ('10001', '11011', '10101', '10001', '10001'),
    "N": ('1001', '1101', '1011', '1001', '1001'),
    "O": ('0110', '1001', '1001', '1001', '0110'),     "P": ('1110', '1001', '1110', '1000', '1000'),
    "Q": ('0110', '1001', '1001', '1011', '0111'),     "R": ('1110', '1001', '1110', '1010', '1001'),
    "S": ('0110', '1000', '0110', '0001', '0110'),     "T": ('1111', '0100', '0100', '0100', '0100'),
    "U": ('1001', '1001', '1001', '1001', '0110'),     "V": ('1001', '1001', '1001', '1010', '0100'),
    "W": ('10001', '10001', '10101', '11011', '10001'),
    "X": ('1001', '1001', '0110', '1001', '1001'),
    "Y": ('1001', '1001', '0110', '0100', '0100'),     "Z": ('1111', '0001', '0110', '1000', '1111'),
    "0": ('0110', '1011', '1101', '1001', '0110'),
    "1": ('010', '110', '010', '010', '111'),
    "2": ('0110', '1001', '0010', '0100', '1111'),     "3": ('1110', '0001', '0110', '0001', '1110'),
    "4": ('0010', '0110', '1010', '1111', '0010'),     "5": ('1111', '1000', '1110', '0001', '1110'),
    "6": ('0110', '1000', '1110', '1001', '0110'),     "7": ('1111', '0001', '0010', '0100', '0100'),
    "8": ('0110', '1001', '0110', '1001', '0110'),     "9": ('0110', '1001', '0111', '0001', '0110'),
    " ": ('0000', '0000', '0000', '0000', '0000'),     ".": ('0000', '0000', '0000', '0000', '0100'),
    "-": ('0000', '0000', '1111', '0000', '0000'),     "?": ('0110', '1001', '0010', '0000', '0100'),
    "+": ('0000', '0100', '1110', '0100', '0000'),
    "/": ('0001', '0010', '0100', '1000', '0000'),     ":": ('0000', '0100', '0000', '0100', '0000'),
    "(": ('001', '010', '100', '010', '001'),           ")": ('100', '010', '001', '010', '100'),
    ">": ('1000', '0100', '0010', '0100', '1000'),
}
GLYPH_GAP = 1            # blank columns between glyphs
DEFAULT_GLYPH_W = 4      # most glyphs; M and W are 5, I and 1 are 3


def glyph_width(ch: str) -> int:
    """This font is PROPORTIONAL. Four columns cannot hold an M or a W: they
    need two outer strokes and a centre one, and every 4-wide attempt either
    kept a solid row (which reads as a filled block on a spaced panel) or
    landed a single pixel from N. They get five. I and 1 get three, which
    buys back the space."""
    glyph = FONT.get(ch.upper())
    return len(glyph[0]) if glyph else DEFAULT_GLYPH_W


logger = logging.getLogger(APP_NAME)


# --- data ------------------------------------------------------------------


@dataclass
class Watch:
    """One immutable, locally timed represented light crossing.

    The DSN contact may end while this local representation continues.
    `on_air` reports source visibility; it never moves the frozen deadline.
    """
    link: Link
    started_at: float
    light_s: float
    deadline: float
    generation: int
    return_view: str
    live_key: str | None
    on_air: bool = True


@dataclass(frozen=True)
class NarrationRequest:
    """One explicit START intent, separate from background cache ordering."""
    generation: int
    key: str
    name: str | None
    view: str


@dataclass(frozen=True)
class NarrationNotice:
    """Terminal user feedback bound to the exact request that produced it."""
    generation: int
    key: str
    name: str | None
    view: str
    label: str


@dataclass
class State:
    links: list[Link] = field(default_factory=list)
    ranges: dict[int, tuple[float, float]] = field(default_factory=dict)  # naif -> (km, at)
    range_retry_at: dict[int, float] = field(default_factory=dict)
    range_unavailable: set[int] = field(default_factory=set)
    focus: str | None = None      # user real-time lock; None = auto-rotate
    narration_focus: str | None = None  # orthogonal hold while audio plays
    completion_pending: str | None = None  # hold until arrival blink is accepted
    cursor: int = 0
    scene_files: list[str] = field(default_factory=list)
    scene_cache: OrderedDict[tuple, str] = field(default_factory=OrderedDict)
    scene_gen: int = 0
    enc_accum: int = 0
    names: dict[str, str] = field(default_factory=dict)
    dish_types: dict[str, str] = field(default_factory=dict)  # DSS43 -> 70M
    site_lons: dict[str, float] = field(default_factory=dict)  # Canberra -> 148.98
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    seen: dict[str, dict] = field(default_factory=dict)  # craft -> first/last/passes
    speech: dict[str, float] = field(default_factory=dict)  # filename -> seconds
    # A PLAY 404 means a resident path can be corrupt rather than absent.
    # Repairs get a new immutable generation; the base-name mapping lets a
    # later process rediscover the newest usable generation from storage.
    speech_repairs: dict[str, int] = field(default_factory=dict)
    speech_retire: set[str] = field(default_factory=set)
    speech_cache_ready: bool = False
    speaking: bool = False
    synth: asyncio.Lock = field(default_factory=asyncio.Lock)
    picking: bool = False         # wheel is being turned; the picker is up
    pick_at: float = 0.0          # loop time of the last detent
    manual_until: float = 0.0     # loop time; a deliberate selection wins
    realtime_since: float | None = None   # wall clock when the lock was taken
    watch: Watch | None = None
    rt_generation: int | None = None      # immutable countdown id for this watch
    rt_counter: int = 0
    rt_nonce: str = field(default_factory=lambda: f"{time.time_ns() & 0xffffffff:x}")
    led_blink: str | None = None          # colour for the NEXT draw, then cleared
    led_generation: int = 0
    countdown_up: bool = False            # a live countdown element is on screen
    countdown_id: str | None = None
    view: str = field(default_factory=lambda: DEFAULT_VIEW)
    view_before_lock: str | None = None
    feed_timestamp_ms: int | None = None
    feed_advanced_at: float | None = None
    feed_seeded: bool = False
    freshness: str = "offline"
    aim_trails: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    event_queue: list[dict] = field(default_factory=list)
    next_event_at: float = 0.0
    last_scene_signature: tuple | None = None
    last_scene_filename: str | None = None
    live_lease_up: bool = False
    last_live_lease_timestamp_ms: int | None = None
    narration_texts: dict[str, str] = field(default_factory=dict)
    narration_frozen_at: dict[str, int] = field(default_factory=dict)
    # text, distinct source snapshots observed, last NASA source timestamp
    narration_candidates: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    narration_priority: str | None = None
    narration_request_counter: int = 0
    narration_request: NarrationRequest | None = None
    narration_notice: NarrationNotice | None = None
    narration_notice_retry_at: float = 0.0
    narration_notice_failures: int = 0
    interactive_draw: asyncio.Lock = field(default_factory=asyncio.Lock)
    interactive_layer: int = 0
    interactive_visible_until: float = 0.0
    active_event_label: str | None = None
    active_event_asset: str | None = None
    active_event_embedded_label: bool = False
    active_event_until: float = 0.0
    audio_stop_pending: bool = False
    audio_stop_retry_at: float = 0.0
    audio_generation: int = 0
    audio_stop_generation: int | None = None
    audio_io: asyncio.Lock = field(default_factory=asyncio.Lock)
    speech_tasks: set[asyncio.Task] = field(default_factory=set)
    ok_down_at: float | None = None
    ok_hold_fired: bool = False
    ok_hold_task: asyncio.Task | None = None
    status_up: bool = False
    completion_link: Link | None = None
    completion_generation: int | None = None
    narration_revision: int = 0
    narration_changed: asyncio.Event = field(default_factory=asyncio.Event)
    narration_return_view: str | None = None
    heartbeat_id: str | None = None
    heartbeat_y: int | None = None
    heartbeat_generation: int = 0
    heartbeat_pending_timestamp_ms: int | None = None
    heartbeat_pending_id: str | None = None
    heartbeat_pending_y: int | None = None
    # A transport failure can lose the response after the device committed a
    # draw. Keep every such id until one accepted payload retires it.
    heartbeat_uncertain: dict[str, int] = field(default_factory=dict)
    heartbeat_uncertain_until: dict[str, float] = field(default_factory=dict)
    event_assets: dict[str, str] = field(default_factory=dict)
    event_warm_task: asyncio.Task | None = None
    network_page: int = 0
    network_page_pending: bool = False
    network_warm_task: asyncio.Task | None = None
    network_warm_signature: tuple | None = None
    # ``inf`` means a wheel-rest Focus Lens is waiting for its first accepted
    # draw.  Once accepted it becomes a loop-time deadline, guaranteeing one
    # complete native marquee without making Focus an ambient carousel.
    network_focus_until: float = 0.0
    network_focus_key: str | None = None
    # Focus is one deliberate semantic-zoom snapshot. NASA's independent
    # native heartbeat can still advance during it, but changing telemetry
    # cannot restart the full-name marquee and consume the user's dwell.
    network_focus_links: tuple[Link, ...] = ()
    network_focus_names: dict[str, str] = field(default_factory=dict)
    network_focus_trails: dict[str, list[tuple[int, int]]] = field(
        default_factory=dict)

    def current(self) -> Link | None:
        if self.watch is not None and self.realtime_since is not None:
            return self.watch.link
        if self.completion_link is not None and self.completion_pending:
            return self.completion_link
        if not self.links:
            return None
        held = self.focus or self.narration_focus or self.completion_pending
        if held:
            for link in self.links:
                if link.key == held:
                    return link
            return None                 # never put another craft on its timer
        return self.links[self.cursor % len(self.links)]


def request_led(state: State, colour: str) -> None:
    state.led_generation += 1
    state.led_blink = colour


def clear_led(state: State, colour: str | None = None) -> None:
    if colour is None or state.led_blink == colour:
        state.led_generation += 1
        state.led_blink = None


def note_narration_change(state: State) -> None:
    """Wake an accepted PLAY so stale dish/craft audio cannot continue."""
    state.narration_revision += 1
    state.narration_changed.set()
    state.narration_changed = asyncio.Event()


def _f(value: str | None, default: float = 0.0) -> float:
    """The feed uses '', '-1' and 'NaN' interchangeably for 'no data'."""
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out < 0 or not math.isfinite(out) else out


def device_text(value: object, fallback: str = "?") -> str:
    """Printable ASCII for the firmware's native bitmap TextElement.

    NFKD preserves the readable base letters of Latin names (Zuerich's
    umlaut becomes U) while the final filter is the API contract itself.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = "".join(ch for ch in text if 0x20 <= ord(ch) <= 0x7E).strip()
    return text or fallback


# Plain imports use documented public-safe defaults. Parsing owner environment,
# resolving custom cache paths and emitting configuration warnings happen only
# through configure_runtime(), immediately before the CLI starts work.
RUNTIME_CONFIG = DEFAULT_DSN_CONFIG
MANAGED_CACHE_ROOT = DEFAULT_DSN_CONFIG.managed_cache_root
CACHE_DIR = DEFAULT_DSN_CONFIG.cache_dir
RANGE_CACHE = CACHE_DIR / "dsn_ranges.json"
HISTORY_PATH = CACHE_DIR / "dsn_history.jsonl"


def apply_runtime_config(config: DsnConfig) -> None:
    """Apply one validated immutable configuration to the legacy runtime globals."""
    global CACHE_DIR, DEFAULT_VIEW, DSN_NETWORK_STYLE, FEED_DELAYED_S
    global FEED_STALE_S, HISTORY_PATH, LIVE_LEASE_TIMEOUT_S
    global MANAGED_CACHE_ROOT, POLL_S, RANGE_CACHE, ROTATE_S, RUNTIME_CONFIG
    global VOICE

    RUNTIME_CONFIG = config
    POLL_S = config.poll_s
    ROTATE_S = config.rotate_s
    VOICE = config.voice
    DEFAULT_VIEW = config.default_view
    DSN_NETWORK_STYLE = config.network_style
    MANAGED_CACHE_ROOT = config.managed_cache_root
    CACHE_DIR = config.cache_dir
    RANGE_CACHE = CACHE_DIR / "dsn_ranges.json"
    HISTORY_PATH = CACHE_DIR / "dsn_history.jsonl"
    FEED_DELAYED_S = max(POLL_S * 2.5, 15.0)
    FEED_STALE_S = max(POLL_S * 5.0, 35.0)
    LIVE_LEASE_TIMEOUT_S = max(30, int(FEED_DELAYED_S + POLL_S))
    for warning in config.warnings:
        logger.warning("%s", warning)


def configure_runtime() -> DsnConfig:
    """Load owner dotenv values, validate the resulting environment and apply it."""
    load_env()
    config = parse_runtime_config(os.environ)
    apply_runtime_config(config)
    return config


def range_ttl_s(km: float) -> float:
    """A range-sensitive TTL for geocentric observer distance.

    Earth-orbiting observatories can change range several-fold in six hours;
    truly deep-space targets move slowly enough for the former maximum TTL.
    """
    if km < 2_000_000:
        return RANGE_NEAR_EARTH_TTL_S
    if km < 50_000_000:
        return RANGE_INTERMEDIATE_TTL_S
    return RANGE_TTL_S


def range_cache_fresh(entry: tuple[float, float], now: float) -> bool:
    """Validate a cached (km, observed_at) pair without trusting JSON types."""
    try:
        km, observed_at = float(entry[0]), float(entry[1])
    except (IndexError, TypeError, ValueError, OverflowError):
        return False
    if not (math.isfinite(km) and 0 < km <= MAX_RANGE_KM
            and math.isfinite(observed_at)):
        return False
    age = now - observed_at
    return 0.0 <= age < range_ttl_s(km)


def cached_range(state: State, naif: int, now: float | None = None) -> float | None:
    """A currently defensible range, expiring invalid entries in memory."""
    entry = state.ranges.get(naif)
    if entry is None:
        return None
    current = time.time() if now is None else now
    if not range_cache_fresh(entry, current):
        state.ranges.pop(naif, None)
        return None
    return float(entry[0])


def load_ranges(state: State) -> None:
    """Load a versioned range cache atomically; malformed JSON is a cold start."""
    try:
        raw = json.loads(RANGE_CACHE.read_text())
        if (not isinstance(raw, dict)
                or raw.get("version") != RANGE_CACHE_VERSION
                or not isinstance(raw.get("ranges"), dict)):
            raise ValueError("unsupported range-cache schema")
        now = time.time()
        loaded: dict[int, tuple[float, float]] = {}
        for raw_naif, raw_entry in raw["ranges"].items():
            if (not isinstance(raw_naif, str)
                    or not isinstance(raw_entry, list)
                    or len(raw_entry) != 2
                    or any(isinstance(value, bool) for value in raw_entry)):
                raise ValueError("invalid range-cache entry")
            naif = int(raw_naif)
            km, observed_at = float(raw_entry[0]), float(raw_entry[1])
            if not (math.isfinite(km) and 0 < km <= MAX_RANGE_KM
                    and math.isfinite(observed_at)):
                raise ValueError("invalid range-cache value")
            entry = (km, observed_at)
            if range_cache_fresh(entry, now):
                loaded[naif] = entry
    except Exception as exc:  # noqa: BLE001 - a cold/corrupt cache is normal
        logger.debug("range cache ignored: %s", exc)
        return
    state.ranges = loaded
    if state.ranges:
        logger.info("loaded %d cached distances", len(state.ranges))


def save_ranges(state: State) -> None:
    try:
        now = time.time()
        RANGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        RANGE_CACHE.write_text(json.dumps(
            {"version": RANGE_CACHE_VERSION,
             "ranges": {
                 str(k): [v[0], v[1]] for k, v in state.ranges.items()
                 if range_cache_fresh(v, now)
             }}, sort_keys=True))
    except Exception as exc:  # noqa: BLE001 - never fatal
        logger.debug("range cache not written: %s", exc)


async def poll_names(state: State) -> None:
    """Keep the name map fresh, and KEEP TRYING if it never arrived.

    This ran exactly once at startup. A transient failure — the Pi booting
    before DHCP settles, a 500 — once left names, dish sizes and site
    longitudes empty for the life of the process. Unknown truth-bearing facts
    now stay unknown rather than becoming a 34-metre dish or longitude zero,
    but retries still recover the richer scene. A craft added to the network
    mid-run must likewise acquire its name without a restart.
    """
    delay = 5.0
    while True:
        if await fetch_names(state):
            delay = 5.0
            await asyncio.sleep(3600)      # new craft appear; refresh hourly
        else:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def fetch_names(state: State) -> bool:
    """Atomically refresh NASA's names, dish facts and site longitudes."""
    try:
        async with httpx.AsyncClient(headers=UA, timeout=20) as client:
            r = await client.get(CONFIG_XML)
            r.raise_for_status()
        names, dish_types, site_lons = parse_config(r.content)
        changed = ((names, dish_types, site_lons)
                   != (state.names, state.dish_types, state.site_lons))
        state.names, state.dish_types, state.site_lons = (
            names, dish_types, site_lons)
        if changed:
            state.dirty.set()
        logger.info("names: %d spacecraft, %d dishes",
                    len(state.names), len(state.dish_types))
        return True
    except Exception as exc:  # noqa: BLE001 - retain the last valid snapshot
        logger.warning("DSN config unavailable/invalid (%s); retrying", exc)
        return False


# --- what the network was doing while you were not looking -----------------
# Every poll used to overwrite the last snapshot and discard it, so the app
# was structurally unable to say "Voyager was tracked for six hours today" or
# "first Psyche pass this week". It is an append-only log of TRANSITIONS, not
# of polls: a poll every 30 seconds would be 2,880 identical lines a day, and
# the interesting thing is the change.
# Bounded by BYTES, because that is what protects an SD card, and trimmed
# back to a line count comfortably under it so the trim is rare. Sizing these
# close together is the trap: 5000 lines of ~100 bytes is ~500 KB, so a 512 KB
# ceiling would rewrite the whole file on nearly every append forever.
HISTORY_MAX_BYTES = 1024 * 1024   # ceiling: about a month of ordinary traffic
HISTORY_MAX_LINES = 4000          # ~400 KB, so ~60% headroom after a trim


def link_events(before: list[Link], after: list[Link],
                now: float) -> list[dict]:
    """What changed between two snapshots. Pure, so it can be tested.

    Three kinds of thing are worth remembering: a craft appearing on a dish,
    that pass ending, and the special modes changing mid-pass — an array
    forming, a DDOR fix starting. Everything else is the same pass continuing.
    """
    was = {l.key: l for l in before}
    now_by_key = {l.key: l for l in after}
    events: list[dict] = []

    for key, link in now_by_key.items():
        flags = sorted(f for f, on in (("arrayed", link.arrayed),
                                       ("mspa", link.mspa),
                                       ("ddor", link.ddor)) if on)
        if key not in was:
            events.append({"t": round(now, 1), "event": "appear",
                           "dish": link.dish, "craft": link.craft,
                           "band": link.band, "bps": link.down_bps,
                           "flags": flags})
        else:
            old = was[key]
            before_flags = sorted(f for f, on in (("arrayed", old.arrayed),
                                                  ("mspa", old.mspa),
                                                  ("ddor", old.ddor)) if on)
            if before_flags != flags:
                events.append({"t": round(now, 1), "event": "flags",
                               "dish": link.dish, "craft": link.craft,
                               "flags": flags})

    for key, old in was.items():
        if key not in now_by_key:
            events.append({"t": round(now, 1), "event": "vanish",
                           "dish": old.dish, "craft": old.craft})
    return events


def _stream_signature(link: Link) -> tuple:
    return tuple((band_key(stream.band), stream.bps is not None,
                  rate_bucket(stream.bps),
                  receive_power_bucket(stream.dbm))
                 for stream in link_streams(link))


def visual_events(before: list[Link], after: list[Link],
                  now: float) -> list[dict]:
    """Glanceable semantic transitions, deliberately blind to raw jitter."""
    old_by_key = {link.key: link for link in before}
    new_by_key = {link.key: link for link in after}
    removed = [link for key, link in old_by_key.items() if key not in new_by_key]
    added = [link for key, link in new_by_key.items() if key not in old_by_key]
    events: list[dict] = []

    # A same-craft disappear/appear in one feed update is a handoff, not a
    # loss immediately followed by an acquisition.
    handed_old: set[str] = set()
    handed_new: set[str] = set()
    crafts = {link.craft for link in removed} | {link.craft for link in added}
    for craft in crafts:
        old_matches = [link for link in removed if link.craft == craft]
        new_matches = [link for link in added if link.craft == craft]
        if len(old_matches) == len(new_matches) == 1:
            old, new = old_matches[0], new_matches[0]
            handed_old.add(old.key)
            handed_new.add(new.key)
            events.append({"t": round(now, 1), "event": "handoff",
                           "craft": new.craft, "dish": new.dish,
                           "from_dish": old.dish,
                           "complex": new.complex_name,
                           "azimuth": new.azimuth,
                           "elevation": new.elevation,
                           "pointing_valid": new.pointing_valid,
                           "from_complex": old.complex_name,
                           "from_azimuth": old.azimuth,
                           "from_elevation": old.elevation,
                           "from_pointing_valid": old.pointing_valid})

    for link in added:
        if link.key not in handed_new:
            events.append({"t": round(now, 1), "event": "acquire",
                           "craft": link.craft, "dish": link.dish,
                           "complex": link.complex_name})
    for link in removed:
        if link.key not in handed_old:
            events.append({"t": round(now, 1), "event": "loss",
                           "craft": link.craft, "dish": link.dish,
                           "complex": link.complex_name})

    for key in old_by_key.keys() & new_by_key.keys():
        old, new = old_by_key[key], new_by_key[key]
        old_flags = (old.arrayed, old.mspa, old.ddor)
        new_flags = (new.arrayed, new.mspa, new.ddor)
        if old_flags != new_flags:
            events.append({"t": round(now, 1), "event": "modes",
                           "craft": new.craft, "dish": new.dish,
                           "before_flags": old_flags, "flags": new_flags})
        old_streams, new_streams = _stream_signature(old), _stream_signature(new)
        old_bands = tuple(item[0] for item in old_streams)
        new_bands = tuple(item[0] for item in new_streams)
        if old_bands != new_bands or len(old_streams) != len(new_streams):
            events.append({"t": round(now, 1), "event": "streams",
                           "craft": new.craft, "dish": new.dish,
                           "before_streams": len(old_streams),
                           "streams": len(new_streams), "bands": new_bands})
        old_direction = (old.up_active, bool(old_streams))
        new_direction = (new.up_active, bool(new_streams))
        if old_direction != new_direction:
            events.append({"t": round(now, 1), "event": "direction",
                           "craft": new.craft, "dish": new.dish,
                           "up": new_direction[0], "down": new_direction[1]})
    return events


def queue_events(state: State, events: list[dict]) -> None:
    for event in events:
        # Freshness is state, not history. A recovery makes an unseen stale
        # card false (and vice versa), so only the newest state may remain.
        if event.get("event") in {"stale", "recovered"}:
            state.event_queue[:] = [queued for queued in state.event_queue
                                    if queued.get("event") not in
                                    {"stale", "recovered"}]
        state.event_queue.append(event)
    if len(state.event_queue) > EVENT_QUEUE_MAX:
        del state.event_queue[:-EVENT_QUEUE_MAX]


def reconciled_selection_key(
        after: list[Link], selected_key: str | None,
        selected_craft: str | None,
        ) -> str | None:
    """Choose the same semantic selection independent of feed record order.

    Exact identity wins.  A dish handoff keeps the craft only when that
    continuation is unique. The final key sort is deliberately independent
    of both snapshots' XML ordering, which has no selection semantics.
    """
    if not after:
        return None
    live_by_key = {link.key: link for link in after}
    if selected_key in live_by_key:
        return selected_key
    same_craft = ([link for link in after if link.craft == selected_craft]
                  if selected_craft is not None else [])
    if len(same_craft) == 1:
        return same_craft[0].key
    return min(live_by_key)


def reconcile_links(state: State, links: list[Link], now: float) -> list[dict]:
    """Update a snapshot without moving the user's selection to another craft."""
    before = state.links
    selected = state.current()
    selected_key = selected.key if selected is not None else None
    selected_craft = selected.craft if selected is not None else None
    narrated = next((link for link in before
                     if link.key == state.narration_focus), None)
    completed = next((link for link in before
                      if link.key == state.completion_pending), None)
    events = visual_events(before, links, now) if state.feed_seeded else []

    if state.watch is not None:
        # The represented crossing belongs to the click, not to continued DSN
        # coverage. Reconcile its separate live-contact annotation on *every*
        # snapshot: a one-poll gap must not leave an hours-long watch claiming
        # OFF AIR after the same dish (or its handoff) comes back.
        watch = state.watch
        craft_links = [link for link in links if link.craft == watch.link.craft]
        live = next((link for link in craft_links
                     if link.key == state.focus), None)
        if live is None:
            live = next((link for link in craft_links
                         if link.key == watch.link.key), None)
        if live is None and watch.live_key:
            live = next((link for link in craft_links
                         if link.key == watch.live_key), None)
        if live is None and len(craft_links) == 1:
            live = craft_links[0]

        previous_live = watch.live_key if watch.on_air else None
        watch.on_air = live is not None
        watch.live_key = live.key if live is not None else None
        if live is not None:
            state.focus = live.key
            selected_key = live.key
            if previous_live != live.key:
                logger.info("watch contact live: %s", live.key)
        elif previous_live is not None:
            logger.info("watch continues off-air: %s", state.focus)
    elif state.focus and not any(link.key == state.focus for link in links):
        handoff = [link for link in links if link.craft == selected_craft]
        if len(handoff) == 1:
            state.focus = handoff[0].key
            selected_key = handoff[0].key
            logger.info("focus handoff: %s", state.focus)
        else:
            logger.info("focused link left the network: %s", state.focus)
            if state.view_before_lock is not None:
                state.view = state.view_before_lock
            state.focus = None
            state.realtime_since = None
            state.rt_generation = None
            state.view_before_lock = None

    if (state.narration_focus
            and not any(link.key == state.narration_focus for link in links)):
        handoff = ([link for link in links if link.craft == narrated.craft]
                   if narrated is not None else [])
        old_narration_focus = state.narration_focus
        state.narration_focus = handoff[0].key if len(handoff) == 1 else None
        note_narration_change(state)
        if selected_key == old_narration_focus and state.narration_focus:
            selected_key = state.narration_focus

    if state.completion_pending and state.completion_link is None:
        state.completion_link = completed

    state.links = links
    live_keys = {link.key for link in links}
    for mapping in (state.narration_texts, state.narration_frozen_at,
                    state.narration_candidates):
        for key in list(mapping):
            if key not in live_keys:
                mapping.pop(key, None)
    if state.narration_priority not in live_keys:
        state.narration_priority = None
    requested_key = (state.narration_request.key
                     if state.narration_request is not None else
                     state.narration_notice.key
                     if state.narration_notice is not None else None)
    if requested_key is not None and requested_key not in live_keys:
        clear_narration_request(state)
    if (state.network_focus_key is not None
            and state.network_focus_key not in live_keys):
        # A Network semantic zoom is exact-link context. A handoff may
        # preserve the craft selection, but silently reusing another dish's
        # aim would change the physical owner. Return to the ambient Network.
        clear_network_focus(state)
    selected_key = reconciled_selection_key(
        links, selected_key, selected_craft)
    if selected_key:
        for index, link in enumerate(links):
            if link.key == selected_key:
                state.cursor = index
                break
        else:
            state.cursor = min(state.cursor, max(0, len(links) - 1))
    else:
        state.cursor = min(state.cursor, max(0, len(links) - 1))
    note_pointing(state, links)
    state.feed_seeded = True
    return events


def append_history(events: list[dict]) -> None:
    """Append, and keep the file bounded. Never fatal: losing history is not
    a reason to stop showing the sky."""
    if not events:
        return
    try:
        HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with HISTORY_PATH.open("a") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        if HISTORY_PATH.stat().st_size > HISTORY_MAX_BYTES:
            kept = HISTORY_PATH.read_text().splitlines()[-HISTORY_MAX_LINES:]
            HISTORY_PATH.write_text("\n".join(kept) + "\n")
    except OSError as exc:
        logger.debug("history append failed: %s", exc)


def load_history(state: State) -> None:
    """Rebuild 'have I seen this craft before' from the log.

    A corrupt or half-written line is skipped rather than fatal — this file is
    appended to by a process that can be SIGKILLed mid-write.
    """
    state.seen = {}
    try:
        text = HISTORY_PATH.read_text()
    except OSError:
        return
    for line in text.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") != "appear":
            continue
        craft = (event.get("craft") or "").lower()
        when = float(event.get("t") or 0.0)
        if not craft:
            continue
        record = state.seen.setdefault(craft, {"first": when, "last": when,
                                               "passes": 0})
        record["first"] = min(record["first"], when)
        record["last"] = max(record["last"], when)
        record["passes"] += 1
    if state.seen:
        logger.info("history: %d craft seen before, %d passes",
                    len(state.seen), sum(r["passes"] for r in state.seen.values()))


def note_seen(state: State, events: list[dict]) -> None:
    """Keep the in-memory view current without re-reading the file."""
    for event in events:
        if event["event"] != "appear":
            continue
        craft = event["craft"].lower()
        record = state.seen.setdefault(craft, {"first": event["t"],
                                               "last": event["t"],
                                               "passes": 0})
        record["last"] = event["t"]
        record["passes"] += 1


async def poll_feed(state: State) -> None:
    async with httpx.AsyncClient(headers=UA, timeout=20) as client:
        while True:
            try:
                r = await client.get(DSN_XML)
                r.raise_for_status()
                received_at = time.time()
                source_timestamp = feed_timestamp_ms(r.content)
                if source_timestamp is None:
                    logger.warning("ignoring DSN snapshot without a source timestamp")
                    await asyncio.sleep(POLL_S)
                    continue
                if not source_timestamp_valid(source_timestamp, received_at):
                    logger.warning("ignoring DSN snapshot with implausible source "
                                   "timestamp: %s", source_timestamp)
                    await asyncio.sleep(POLL_S)
                    continue
                if (source_timestamp is not None
                        and state.feed_timestamp_ms is not None
                        and source_timestamp < state.feed_timestamp_ms):
                    logger.warning("ignoring older DSN snapshot: %s < %s",
                                   source_timestamp, state.feed_timestamp_ms)
                    await asyncio.sleep(POLL_S)
                    continue
                if (state.feed_timestamp_ms is not None
                        and source_timestamp == state.feed_timestamp_ms):
                    # The source timestamp is the snapshot version. Do not
                    # accept different-looking transport bytes under an old
                    # lease; simply let freshness age toward delayed/stale.
                    await asyncio.sleep(POLL_S)
                    continue
                links = parse_feed(r.content)
                for link in links:                      # fill gaps from Horizons
                    if link.range_km is not None and link.naif:
                        state.range_unavailable.discard(link.naif)
                    if link.range_km is None and link.naif:
                        link.range_km = cached_range(
                            state, link.naif, received_at)
                seeded = state.feed_seeded
                history_events = (link_events(state.links, links, received_at)
                                  if seeded else [])
                changed = [l.key for l in links] != [l.key for l in state.links]
                events = reconcile_links(state, links, received_at)
                append_history(history_events)
                note_seen(state, history_events)
                queue_events(state, events)
                state.feed_advanced_at = received_at
                state.feed_timestamp_ms = source_timestamp
                if changed:
                    logger.info("links: %s", ", ".join(
                        f"{l.dish}->{l.craft}" for l in links) or "(none)")
                # Waking is cheap. The main loop compares a device-resolution
                # signature before it renders or uploads anything.
                state.dirty.set()
            except Exception as exc:  # noqa: BLE001 - a feed outage is a state
                logger.warning("dsn feed failed: %s", exc)
            await asyncio.sleep(POLL_S)


class HorizonsUnavailable(ValueError):
    """The official service answered, but has no ephemeris for this target."""


def horizons_au(body: str) -> float:
    """Extract Horizons' observer range, preserving a useful source error."""
    if "$$SOE" not in body or "$$EOE" not in body:
        detail = next(
            (line.strip() for line in body.splitlines()
             if line.strip() and not line.startswith("API ")),
            "no ephemeris data",
        )
        if "no such record" in body.lower():
            raise HorizonsUnavailable(detail)
        # A proxy page, truncated success response or future API-format drift
        # is not evidence that the target lacks an ephemeris. Keep it on the
        # short transient retry path instead of suppressing checks for hours.
        raise ValueError(f"missing Horizons ephemeris table: {detail}")
    row = body.split("$$SOE", 1)[1].split("$$EOE", 1)[0].strip().split()
    if len(row) < 3:
        raise ValueError("empty Horizons ephemeris row")
    au = float(row[2])
    max_au = MAX_RANGE_KM / (AU_LIGHT_S * C_KM_S)
    if not math.isfinite(au) or not 0 < au <= max_au:
        raise ValueError("invalid Horizons observer range")
    return au


async def poll_ranges(state: State) -> None:
    """Fill in light-time for craft the feed doesn't range.

    The feed's own rtlt has been -1 since NASA degraded it, and downlegRange
    is populated for only some targets — so ask Horizons for the rest, keyed
    on the NAIF id the feed hands us. Cache lifetime follows geocentric range:
    minutes for near-Earth observatories, hours only for deep-space targets.
    """
    async with httpx.AsyncClient(headers=UA, timeout=30) as client:
        while True:
            # Short idle poll, not a long one: the first pass runs before the
            # feed has landed, so state.links is still empty. Sleeping a full
            # minute there left every unranged craft showing "?" for the first
            # minute of every run — which is most of a short run.
            now = time.time()
            pending_by_naif: dict[int, Link] = {}
            for link in list(state.links):
                if not link.naif or link.range_km:
                    continue
                cached = cached_range(state, link.naif, now)
                if cached is not None:
                    link.range_km = cached
                    state.dirty.set()
                    continue
                if now >= state.range_retry_at.get(link.naif, 0.0):
                    # Several aliases/dishes can share one NAIF id. One query
                    # fills all of them; source multiplicity must not multiply
                    # requests to JPL.
                    pending_by_naif.setdefault(link.naif, link)
            pending = list(pending_by_naif.values())
            if not pending:
                await asyncio.sleep(10)
                continue
            for link in pending:
                naif = link.naif
                if naif is None:
                    continue
                try:
                    jd = 2440587.5 + time.time() / 86400.0
                    r = await client.get(HORIZONS, params={
                        "format": "text", "COMMAND": f"'{naif}'", "OBJ_DATA": "NO",
                        "MAKE_EPHEM": "YES", "EPHEM_TYPE": "OBSERVER",
                        "CENTER": "'500@399'", "QUANTITIES": "'20'",
                        "TLIST_TYPE": "JD", "TLIST": f"'{jd:.5f}'"})
                    r.raise_for_status()
                    # " 2026-Aug-12 00:00:00.000   142.946932178947  26.198"
                    au = horizons_au(r.text)
                    km = au * AU_LIGHT_S * C_KM_S
                    observed_at = time.time()
                    state.ranges[naif] = (km, observed_at)
                    state.range_retry_at.pop(naif, None)
                    state.range_unavailable.discard(naif)
                    save_ranges(state)
                    for matching in state.links:
                        if matching.naif == naif and matching.range_km is None:
                            matching.range_km = km
                    logger.info("horizons: %s at %.3f AU (%.0f min light)",
                                link.craft, au, au * AU_LIGHT_S / 60)
                    state.dirty.set()
                except HorizonsUnavailable as exc:
                    # A valid negative answer is not a six-hour success cache,
                    # but asking the same unsupported spacecraft every minute
                    # only hammers JPL and floods the log. Keep '?' truthful and
                    # reconsider after the ordinary range-cache horizon.
                    logger.info("horizons %s unavailable: %s", link.craft, exc)
                    state.range_unavailable.add(naif)
                    state.range_retry_at[naif] = (
                        time.time() + RANGE_UNAVAILABLE_RETRY_S)
                except Exception as exc:  # noqa: BLE001 - optional enrichment
                    logger.warning("horizons %s failed: %s", link.craft, exc)
                    state.range_retry_at[naif] = time.time() + RANGE_RETRY_S
                await asyncio.sleep(2)                        # be a good citizen
            await asyncio.sleep(10)


# --- rendering -------------------------------------------------------------


def _text(px, x: int, y: int, s: str, color: tuple[int, int, int],
          clip: tuple[int, int] | None = None) -> None:
    """Draw text; `clip` is an (x0, x1) window outside which nothing lands —
    which is what makes a scrolling label stay inside its box."""
    for ch in s.upper():
        glyph = FONT.get(ch)
        if glyph:
            for dy, row in enumerate(glyph):
                for dx, bit in enumerate(row):
                    gx = x + dx
                    if bit != "1" or not (0 <= gx < W and 0 <= y + dy < H):
                        continue
                    if clip and not (clip[0] <= gx <= clip[1]):
                        continue
                    px[gx, y + dy] = color
        x += glyph_width(ch) + GLYPH_GAP


# The physical panel spaces its LEDs about a pixel apart, so a filled
# background does not read as a surface — it reads as a haze of separated
# dots and it drowns everything drawn on top. Black is genuinely OFF, and
# the gaps merge with it, so bright sparse shapes on black are what read.
# Every colour here is high-contrast against black; nothing is a gradient.
OFF = (0, 0, 0)
OCEAN = (24, 74, 190)         # open water
SHELF = (48, 126, 235)        # nearer a coast: shallower, lighter
LAND = (56, 165, 82)          # vegetated
DESERT = (198, 156, 74)       # Sahara, Arabia, the Australian interior
ICE = (222, 234, 246)         # Antarctica, Greenland, the Arctic


def terrain(lat: float, lon: float, is_land: bool, coastal: bool):
    """Four surfaces instead of two.

    A globe of one green and one blue is legible but flat, and at 13 pixels
    across the ice caps are what make it read as EARTH rather than as a
    green-and-blue ball: Antarctica along the bottom and Greenland at the top
    are the two shapes anyone recognises instantly. Deserts break up the green
    across Africa and Australia. All four are far enough apart to survive the
    panel's gamma, which erases anything under about a 30% step.
    """
    # +/-55 rather than the true polar circles: at 13 pixels across, the
    # projection compresses latitude hard (the row below the pole is already
    # only 56 degrees), so stricter thresholds put ice on exactly one pixel
    # and the caps vanish. These are the top and bottom rows of the disc, and
    # showing them as ice is what makes the ball read as EARTH.
    # The caps are cut at 55 because that is the only threshold that renders
    # ANY ice. The -6 in the disc's radius test drops the cardinal pixels, so
    # the outermost drawn row is dy = +/-5 and asin(5/6) = 56.44 degrees is
    # all the latitude this projection ever reaches - eleven rows, and the
    # top and bottom ones ARE the polar regions as far as this globe is
    # concerned. (A Greenland-specific rule used to sit further down at
    # lat > 58. It could never fire at any ordering, and is gone.)
    if lat > 55.0 or lat < -55.0:
        return ICE
    if not is_land:
        return SHELF if coastal else OCEAN
    # The Sahara stops at the Sahel. Running it to 30S swallowed the Congo
    # basin, which is the wettest place on the continent.
    if 12.0 < lat < 33.0 and -18.0 < lon < 52.0:       # Sahara + Arabia
        return DESERT
    if -30.0 < lat < -18.0 and 12.0 < lon < 25.0:      # Kalahari + Namib
        return DESERT
    if -32.0 < lat < -18.0 and 118.0 < lon < 142.0:    # Australian interior
        return DESERT
    # ...and the Gobi is not the Ganges plain or the North China Plain.
    if 30.0 < lat < 48.0 and 55.0 < lon < 108.0:       # Iranian plateau, Gobi
        return DESERT
    return LAND
DAYSIDE = (120, 170, 255)
CRAFT = (235, 238, 245)
PULSE = (255, 160, 60)       # historic/default X pulse used by distance art
UNKNOWN_PULSE = (100, 40, 255)  # honest unknown: distinct violet, never fake X
# S, X, K, Ka. Not decoration: hue reports only the source-published band,
# never a ranking or cause of live throughput. Distance, spacecraft power,
# antenna, ground aperture, coding and atmosphere all matter too. The palette
# follows centre frequency only — S lowest and reddest, Ka highest and nearly
# white — and remains warm so none can be mistaken for the cold blue uplink.
# Each pair differs by at least 77/255 in one channel: the physical panel's
# measured 30% visibility floor. Warm S/X remain radio-like; K and Ka step
# through mint and white, while unknown is the violet fallback above.
BAND_PULSE = {"S": (255, 40, 10), "X": PULSE,
              "K": (180, 255, 80), "KA": (255, 255, 170)}

NAME = (215, 225, 240)
DIST = (224, 160, 70)
DISH_NO = (110, 145, 190)
RATE = (150, 190, 120)

GLOBE_CX, GLOBE_CY, GLOBE_R = 7, 8, 6
TRACK_Y = 8

# The world at 10-degree cells: 36 longitudes x 18 latitudes, row 0 = 85N,
# column 0 = 180W. Coarse, but sampled through a real spherical projection
# it turns like Earth — Africa and Eurasia swing past, then a wide empty
# Pacific, then the Americas. A longitude-only mask (which this replaced)
# renders as vertical stripes, which is not a planet.
WORLD = (
    "....................................",  # 85N
    "...####......###.....##########.....",  # 75N  N.Canada, Greenland, Siberia
    "..#######....##...############......",  # 65N
    "..#######.........###########.......",  # 55N
    "...######.......############........",  # 45N
    "....####........###########.........",  # 35N
    ".....###.......############.........",  # 25N  Mexico, Sahara, Arabia, India
    "......##......##########...###......",  # 15N
    ".........#....########....####......",  # 5N
    ".........##...#######.....###.......",  # 5S
    ".........###..######.......####.....",  # 15S  Brazil, Africa, Australia
    ".........###..#####........#####....",  # 25S
    "..........##...###.........####.....",  # 35S
    "..........#.................##......",  # 45S  Argentina tip, New Zealand
    "....................................",  # 55S
    "....................................",  # 65S
    "####################################",  # 75S  Antarctica
    "####################################",  # 85S
)
NIGHT_DIM = 0.28        # a 72% drop: gamma erases anything subtler


def subsolar(when: datetime) -> tuple[float, float]:
    """Where the Sun is directly overhead: (latitude, longitude) in degrees.

    Low-precision solar position, good to about 0.01 deg, which is far past
    what 13 pixels of Earth can show. It is here because the LATITUDE is not
    optional: the terminator only tilts with the season if you know the
    Sun's declination, and without it the globe sits at a permanent equinox
    with no midnight sun and no polar night.

    The equation of time comes out of the same arithmetic - it is just the
    mean longitude minus the apparent right ascension - so the longitude is
    the true subsolar meridian rather than the mean-time one. That is worth
    perhaps four degrees, under half a pixel at the centre of the disc and
    less at the limbs, so it is invisible. It is in anyway, because a real
    declination paired with a mean-time hour angle is a hybrid nobody can
    reason about, and the correction costs two lines once the rest exists.
    """
    n = when.timestamp() / 86400.0 - 10957.5      # days since J2000.0
    mean_lon = (280.460 + 0.9856474 * n) % 360.0
    anomaly = math.radians((357.528 + 0.9856003 * n) % 360.0)
    ecliptic = math.radians(mean_lon + 1.915 * math.sin(anomaly)
                            + 0.020 * math.sin(2 * anomaly))
    obliquity = math.radians(23.439 - 4e-7 * n)
    decl = math.degrees(math.asin(math.sin(obliquity) * math.sin(ecliptic)))
    ra = math.degrees(math.atan2(math.cos(obliquity) * math.sin(ecliptic),
                                 math.cos(ecliptic)))
    eot = ((mean_lon - ra + 180.0) % 360.0) - 180.0        # degrees
    hours = (when.timestamp() % 86400.0) / 3600.0
    lon = ((180.0 - 15.0 * hours - eot) + 180.0) % 360.0 - 180.0
    return decl, lon


def _globe(px, spin: float, sun_lat: float, sun_lon: float) -> None:
    """Earth, lit where the sun is.

    Each pixel of the disc is projected back to a latitude and longitude, so
    continents foreshorten toward the limbs the way a sphere's do.

    Day and night are decided per LONGITUDE — `away` is measured from the same
    `lon` the land is looked up with — so the shading is a property of the
    geography and travels with it as the globe turns. A version that pinned
    the terminator to screen space instead was wrong twice over: the shadow
    sat there while the planet moved under it, and at the hours when the
    centred complex was in full day or full night the disc showed no
    terminator at all for the whole loop.
    """
    for dy in range(-GLOBE_R, GLOBE_R + 1):
        for dx in range(-GLOBE_R, GLOBE_R + 1):
            # -6 rather than a bare R^2: the plain test leaves a single
            # protruding pixel at each cardinal point and the disc reads as
            # a diamond rather than a sphere.
            if dx * dx + dy * dy > GLOBE_R * GLOBE_R - 6:
                continue
            x, y = GLOBE_CX + dx, GLOBE_CY + dy
            if not (0 <= x < W and 0 <= y < H):
                continue
            lat = math.degrees(math.asin(max(-1.0, min(1.0, -dy / GLOBE_R))))
            cos_lat = math.cos(math.radians(lat))
            if cos_lat < 0.08:                      # a pole: no useful longitude
                lon_off = 0.0
            else:
                s = max(-1.0, min(1.0, dx / (GLOBE_R * cos_lat)))
                lon_off = math.degrees(math.asin(s))
            lon = (spin * 360.0 + lon_off + 180.0) % 360.0 - 180.0

            row = min(len(WORLD) - 1, max(0, int((90.0 - lat) / 10.0)))
            col = int((lon + 180.0) / 10.0) % 36
            is_land = WORLD[row][col] == "#"
            # Shallow water reads as a coast: check whether any neighbouring
            # cell is land. It costs nothing and gives the oceans some depth
            # instead of one flat blue.
            coastal = not is_land and any(
                WORLD[min(len(WORLD) - 1, max(0, row + dr))][(col + dc) % 36] == "#"
                for dr, dc in ((0, 1), (0, -1), (1, 0), (-1, 0)))
            color = terrain(lat, lon, is_land, coastal)

            # Day or night: the true angular distance from the subsolar
            # POINT, not from its meridian. Longitude alone put the
            # terminator on a great circle through both poles all year, so
            # the globe sat at a permanent equinox - no midnight sun over
            # the Arctic in July, no dark Antarctic winter, which is the
            # most recognisable thing about how Earth is lit.
            phi, dec = math.radians(lat), math.radians(sun_lat)
            away = math.degrees(math.acos(max(-1.0, min(1.0,
                math.sin(phi) * math.sin(dec)
                + math.cos(phi) * math.cos(dec)
                * math.cos(math.radians(lon - sun_lon))))))
            if away > 95.0:
                color = tuple(int(c * NIGHT_DIM) for c in color)
            elif away > 80.0:                       # the terminator itself
                color = tuple(int(c * 0.6) for c in color)
            px[x, y] = color


def _unknown_globe(px) -> None:
    """An Earth-shaped unknown, without invented site-centred daylight."""
    for dy in range(-GLOBE_R, GLOBE_R + 1):
        for dx in range(-GLOBE_R, GLOBE_R + 1):
            radius2 = dx * dx + dy * dy
            if GLOBE_R * GLOBE_R - 15 <= radius2 <= GLOBE_R * GLOBE_R:
                x, y = GLOBE_CX + dx, GLOBE_CY + dy
                if 0 <= x < W and 0 <= y < H:
                    px[x, y] = SCOPE_RING
    _text(px, GLOBE_CX - 2, GLOBE_CY - 2, "?", DISH_NO,
          clip=(GLOBE_CX - 2, GLOBE_CX + 2))


# Spacecraft silhouettes, 11x6, drawn at x61..71 / y5..10 — the whole space
# between the end of the tether and the right edge, with the label rows above
# and below. These are PORTRAITS, not archetypes: each one is built around the
# single feature that makes the real machine recognisable, because at this
# size one true feature reads and a faithful outline does not.
#
# What survives on this panel is proportion and count. Juno's three enormous
# blades read; the shape of its bus does not. Lucy's arrays are circular and
# nothing else in the fleet has that. Parker leads with a shield. A rover has
# wheels on the ground and a mast, and looks nothing like anything in orbit.
#
# Craft that genuinely look alike share a sprite on purpose — Voyager 1 and 2
# ARE identical, as are STEREO A and B, GRAIL A and B, and the MarCO pair.
# Sharing where the truth is shared is accuracy; inventing differences that
# nobody can see on the panel is not.
#
# P = solar panel   B = bus/body   D = high-gain dish   S = shield/sunshade
# lowercase = the same ink at 45%, for booms and structure that should recede
SPRITES = {
    # --- the great dish-dominant probes -----------------------------------
    # Voyager: the 3.7 m dish IS the spacecraft, with the magnetometer boom
    # trailing 13 m behind it and an RTG slung underneath.
    "voyager": (".DDD.......",
                "D...D......",
                "D.#.DBBbbbb",
                "D...D.P....",
                ".DDD.......",
                "..........."),
    # New Horizons: the same big dish, but on a squat triangular bus with the
    # RTG cylinder sticking straight out one side.
    "newhorizons": (".DDD.......",
                    "D...D.BB...",
                    "D.#.DBBBB..",
                    "D...D.BB.PP",
                    ".DDD.......",
                    "..........."),
    # Cassini: the dish sat on TOP of a tall stacked bus, not out front.
    "cassini": (".DDDDD.....",
                "D.....D....",
                "..BBB......",
                "..BBB..bbbb",
                "..BBB......",
                "..........."),
    # --- the array giants --------------------------------------------------
    # Juno: three nine-metre blades at 120 degrees. The most recognisable
    # outline in the fleet.
    "juno": ("PP.........",
             "..PP.......",
             "DBBBPPPPP..",
             "..PP.......",
             "PP.........",
             "..........."),
    # Lucy: two circular arrays, seven metres across. Nothing else is round.
    "lucy": (".PP.....PP.",
             "P..P...P..P",
             "P..PBBBP..P",
             "P..P...P..P",
             ".PP.....PP.",
             "..........."),
    # Psyche: cross-shaped arrays, an X on each side of the bus.
    "psyche": ("P.P.....P.P",
               ".P.......P.",
               "..PBBBBBP..",
               ".P.......P.",
               "P.P.....P.P",
               "..........."),
    # Europa Clipper: a small bus between the largest arrays NASA has flown
    # into deep space — a 30 m span.
    "clipper": ("PPPP...PPPP",
                "...........",
                ".DDBBBBB...",
                "...........",
                "PPPP...PPPP",
                "..........."),
    # Rosetta, Dawn: two enormous straight wings, the whole craft a crossbar.
    "wings": ("...........",
              "PPPP...PPPP",
              "...BBB.....",
              "PPPP...PPPP",
              "...........",
              "..........."),
    # --- shielded ----------------------------------------------------------
    # Parker Solar Probe: the shield leads and everything hides behind it.
    "parker": ("SS.........",
               "SS.P.......",
               "SSBBBB.....",
               "SS.P.......",
               "SS.........",
               "..........."),
    # Solar Orbiter, MESSENGER, BepiColombo: a shield out front, arrays behind
    # it rather than tucked away.
    "sunshade": ("S..........",
                 "S..P...PPP.",
                 "SSBBBB.....",
                 "S..P...PPP.",
                 "S..........",
                 "..........."),
    # --- observatories -----------------------------------------------------
    # JWST: the segmented mirror standing over a stepped sunshield.
    "jwst": (".BBB.......",
             "B...B......",
             ".BBB.......",
             "SSSSSSS....",
             ".SSSSS.....",
             "..........."),
    # Hubble, Chandra, XMM: a tube with the aperture open at one end.
    "telescope": (".BBBBBB....",
                  "PB....B....",
                  "PB....B....",
                  "PB....B....",
                  ".BBBBBB....",
                  "..........."),
    # Gaia: a wide conical sunshade skirt with the instrument above it.
    "skirt": ("...........",
              "...BBB.....",
              "..BBBBB....",
              "SSSSSSSSS..",
              "...........",
              "..........."),
    # --- orbiters at other worlds -----------------------------------------
    # MRO, Odyssey, TGO, Mars Express: relay dish on one end, two wings.
    "marsorbiter": (".DDD....PP.",
                    "D...D..P...",
                    "D.#.DBB....",
                    "D...D..P...",
                    ".DDD....PP.",
                    "..........."),
    # LRO, Chandrayaan, Danuri: the dish hangs off one boom and the single
    # array off the other, with the bus slung between them.
    "lunarorbiter": ("...........",
                     ".DD...BBB..",
                     "D..DddBBBPP",
                     ".DD...BBB..",
                     "...........",
                     "..........."),
    # --- on a surface ------------------------------------------------------
    # Perseverance, Curiosity: a nuclear rover. Six wheels ON THE GROUND and
    # a mast with a camera head. Nothing in orbit looks remotely like this.
    "rover": ("...........",
              "...B.......",
              "..BBB......",
              "...B.......",
              "BBBBBBB....",
              "b.b.b.b...."),
    # Spirit and Opportunity: smaller, and solar rather than nuclear — the
    # deck is one big panel.
    "solarrover": ("...........",
                   "...B.......",
                   "..BBB......",
                   "PPPPPPP....",
                   "..BBB......",
                   ".b.b.b....."),
    # InSight, Phoenix, the lunar landers: a squat body on legs with two
    # circular arrays either side.
    "lander": ("...........",
               ".PP.....PP.",
               "P..P.B.P..P",
               ".PP.BBB.PP.",
               "....BBB....",
               "...b...b..."),
    # --- spinners and boxes ------------------------------------------------
    # ACE, Wind, IMAP, Ulysses: spin-stabilised drums, panels wrapped round
    # the outside rather than on wings.
    "spinner": ("..BBBBB....",
                ".B.....B...",
                "bB.....Bb..",
                ".B.....B...",
                "..BBBBB....",
                "..........."),
    # SOHO, DSCOVR, TESS and the general case: a box bus with two wings. This
    # is the fallback, and it is what most of the fleet honestly looks like.
    "boxwing": ("...........",
                "PP..BBB..PP",
                "PP..B.B..PP",
                "PP..BBB..PP",
                "...........",
                "..........."),
    # STEREO: a compact box with one wing and a boom, flying in pairs.
    "stereo": ("...........",
               "...BBB..PP.",
               "bbBBBBBP...",
               "...BBB..PP.",
               "...........",
               "..........."),
    # --- small craft -------------------------------------------------------
    # MarCO, LICIACube and the rest of the cubesats. The point is SIZE: this
    # one reads as tiny beside everything else, which is the truth.
    "cubesat": ("...........",
                "...........",
                "PP.BB.PP...",
                "...BB......",
                "...........",
                "..........."),
    # OSIRIS-REx, Hayabusa2, Hera: a box with wings and a sampling arm
    # reaching down off the front.
    "sampler": ("...........",
                "PPP.BBB.PPP",
                "...BBBBB...",
                "...B.......",
                "..BB.......",
                "..........."),
    # Orion: the capsule cone with the service module's four arrays behind.
    "orion": ("......P..P.",
              ".BB....PP..",
              "BBBBBBB....",
              ".BB....PP..",
              "......P..P.",
              "..........."),
    # ICPS, EUS: not spacecraft at all, but the network tracks them. A rocket
    # stage is a plain cylinder with a nozzle, and should not pretend
    # otherwise.
    "stage": ("...........",
              "..bBBBBBB..",
              ".bBBBBBBBb.",
              "..bBBBBBB..",
              "...........",
              "..........."),
}

# Which craft is which. Codes are the DSN feed's own, lowercased.
CRAFT_SHAPES = {
    # identical twins share, because they ARE identical
    "vgr1": "voyager", "vgr2": "voyager",
    "nhpc": "newhorizons",
    "cas": "cassini",
    "jno": "juno",
    "lucy": "lucy",
    "psyc": "psyche",
    "eurc": "clipper",
    "rose": "wings", "dawn": "wings", "juice": "wings",
    "spp": "parker",
    "bepi": "sunshade", "msgr": "sunshade",
    "jwst": "jwst",
    "hst": "telescope", "chdr": "telescope", "xmm": "telescope",
    "intg": "telescope", "stf": "telescope", "kepl": "telescope",
    "gaia": "skirt",
    "mro": "marsorbiter", "mros": "marsorbiter", "m01o": "marsorbiter",
    "m01s": "marsorbiter", "mvn": "marsorbiter", "tgo": "marsorbiter",
    "mex": "marsorbiter", "emm": "marsorbiter", "mom": "marsorbiter",
    "mgs": "marsorbiter", "vex": "marsorbiter", "plc": "marsorbiter",
    "escb": "marsorbiter", "escg": "marsorbiter",
    "lro": "lunarorbiter", "ch1": "lunarorbiter", "ch2": "lunarorbiter",
    "ch2o": "lunarorbiter", "kplo": "lunarorbiter", "sele": "lunarorbiter",
    "lade": "lunarorbiter", "grla": "lunarorbiter", "grlb": "lunarorbiter",
    "lcro": "lunarorbiter", "ltb": "lunarorbiter",
    # Rosalind Franklin is a ROVER. It was drawn as a Mars orbiter,
    # which is the exact error this whole set exists to correct.
    "m20": "rover", "msl": "rover", "rsp": "rover",
    # VIPER and the MERs are solar, not nuclear
    "rp": "solarrover",
    "mer1": "solarrover", "mer2": "solarrover",
    "nsyt": "lander", "phx": "lander", "ch3": "lander", "ch2l": "lander",
    "slim": "lander", "spil": "lander", "apm1": "lander", "agm1": "lander",
    "omot": "lander", "lnd1": "lander",
    "ace": "spinner", "wind": "spinner", "imap": "spinner",
    "ulys": "spinner", "gtl": "spinner", "polr": "spinner",
    "imag": "spinner", "ice": "spinner",
    "sta": "stereo", "stab": "stereo", "stb": "stereo",
    "mcoa": "cubesat", "mcob": "cubesat", "lici": "cubesat",
    "argo": "cubesat", "bios": "cubesat", "equl": "cubesat",
    "hmap": "cubesat", "cusp": "cubesat", "tm": "cubesat", "tmm": "cubesat",
    "cue3": "cubesat", "caps": "cubesat", "neas": "cubesat",
    "mlic": "cubesat",          # Lunar IceCube is a 6U cubesat
    "lfl": "cubesat", "olin": "cubesat", "jnsa": "cubesat", "jnsb": "cubesat",
    "orx": "sampler", "hyb2": "sampler", "musc": "sampler",
    "hera": "sampler", "dart": "sampler", "dif": "sampler", "sdu": "sampler",
    "em1": "orion", "em2": "orion", "em3": "orion",
    "icps": "stage", "ltst": "stage", "eus": "stage",
}
DEFAULT_SHAPE = "boxwing"
CRAFT_SPRITE = SPRITES[DEFAULT_SHAPE]    # kept: some tests and tools import it

CRAFT_X, CRAFT_Y = 61, 5                 # the box, hard against the right edge
CRAFT_W, CRAFT_H = 11, 6

PANEL = (90, 150, 255)
BUS = (225, 228, 238)
DISH = (255, 236, 190)
SHIELD = (255, 190, 120)
HOT = (255, 255, 255)


def _dim(c):
    return tuple(int(v * 0.45) for v in c)


INK = {"P": PANEL, "B": BUS, "D": DISH, "S": SHIELD, "#": HOT,
       "p": _dim(PANEL), "b": _dim(BUS), "d": _dim(DISH), "s": _dim(SHIELD)}


# Some of these craft genuinely move, and eleven pixels can show exactly one
# kind of motion: a specular highlight walking a path. That is enough, because
# for the craft listed here the highlight IS the rotation rather than a
# decoration on top of it.
#
# Juno is spin-stabilised at 2 rpm and ACE, Wind, IMAP and Ulysses are drums
# that spin for the same reason, so sunlight really does sweep their arrays.
# A spent upper stage tumbles, end over end, which is why it gets one too.
# Craft that hold a controlled attitude - JWST, Parker, Lucy, Europa Clipper,
# the rovers - get no glint, because inventing motion they do not have is the
# same error as inventing a silhouette they do not have.
CRAFT_GLINT = {
    # Juno turns at 2 rpm - one revolution every 30 s - so across an
    # 8 s loop the glint should advance about a quarter turn, not three
    # whole blades. Repeating each tip makes the sweep read at the
    # right rate without a longer loop.
    "juno": ((2, 8), (2, 8), (2, 8), (0, 0), (0, 0), (0, 0),
             (4, 0), (4, 0), (4, 0)),
    "spinner": ((0, 3), (2, 8), (4, 3), (2, 1)),  # round the drum
    "stage": ((1, 3), (3, 8)),                    # tumbling
}


def craft_shape(code: str) -> str:
    return CRAFT_SHAPES.get(code.lower(), DEFAULT_SHAPE)


def craft_sprite(code: str) -> tuple[str, ...]:
    """The portrait for a craft, falling back to a plain box-and-wings sat."""
    return SPRITES[craft_shape(code)]


def _craft(px, x0: int, y0: int, code: str = "", phase: float = 0.0) -> None:
    """The spacecraft, drawn as itself.

    Placed by its top-left corner rather than its centre: the box is fixed
    against the right edge of the panel and the sprites are not all the same
    width, so centring them would make the fleet jitter as the scene changed.
    """
    for row, line in enumerate(craft_sprite(code)):
        for col, ch in enumerate(line):
            if ch == ".":
                continue
            x, y = x0 + col, y0 + row
            if 0 <= x < W and 0 <= y < H:
                px[x, y] = INK[ch]
    path = CRAFT_GLINT.get(craft_shape(code))
    if path:
        row, col = path[int(phase * len(path)) % len(path)]
        x, y = x0 + col, y0 + row
        if 0 <= x < W and 0 <= y < H:
            px[x, y] = HOT


TIME_COMPRESSION = 600.0   # one second on the strip = ten minutes of real flight
CROSS_KNEE_S = 1200.0      # 20 min of light time: above this the law is linear
CROSS_KNEE_CROSS = CROSS_KNEE_S / TIME_COMPRESSION      # ...and 2.0s there
CROSS_MIN_S = 0.7          # the Moon. Three frames at 5 fps, still motion
CROSS_MAX_S = 180.0


def crossing_seconds(light_s: float | None) -> float:
    """How long ONE message takes to cross the strip.

    Above the knee this is exactly 1/600 of the estimated light time, so the
    range ratios survive: Jupiter is roughly twice Mars, Voyager roughly twenty-three
    times Jupiter.

    Below it the law has to bend, and pretending otherwise was the old bug.
    A flat 2-second floor made the Moon, the Lagrange points and Mars at
    closest approach render IDENTICALLY - 1.3 seconds of light time and 190
    seconds of it drawn at the same speed - while the docstring claimed true
    ratios. The real span is 55,000:1 and the strip has about 200:1, so no
    linear map can hold it.

    So: linear above 20 minutes of light time, and a log squeeze below, which
    keeps the Moon visibly quicker than L1, and L1 quicker than Mars, instead
    of collapsing all three onto the floor. The number on the panel is always
    the published or ephemeris-estimated light time either way.
    """
    # A missing range has no defensible crossing speed. Return one neutral loop
    # only as a planning bound; render_frames() holds the active carrier still
    # instead of turning this placeholder into apparent distance or velocity.
    if not light_s:
        return LOOP_S
    if light_s >= CROSS_KNEE_S:
        return min(CROSS_MAX_S, light_s / TIME_COMPRESSION)
    # continuous at the knee: both branches give CROSS_KNEE_CROSS there
    frac = math.log1p(light_s) / math.log1p(CROSS_KNEE_S)
    return CROSS_MIN_S + (CROSS_KNEE_CROSS - CROSS_MIN_S) * frac


# Meaning-bearing lines must clear the measured 30% physical-panel step from
# OFF.  The old (58, 40, 14) line vanished through the LED gaps even though it
# looked present in a solid-pixel preview.
TETHER = (78, 35, 10)
UPLINK = (120, 190, 255)   # Earth talking. Cold and bright against the amber.
UP_TETHER = (26, 48, 78)
UP_Y = TRACK_Y - 2         # Earth's half of the conversation, above
DOWN_Y = TRACK_Y           # the spacecraft's half, on the original row


def _link_row(px, y: int, track0: int, track1: int, phase: float,
              spacing: float, outward: bool, span: int,
              colour: tuple[int, int, int],
              tether: tuple[int, int, int]) -> list[float]:
    """One direction of the conversation, on its own row.

    Returns the pulse positions so the caller can tell when one has landed.
    `outward` sends the signal from Earth to the craft; the wake always
    trails behind whichever way it is going.
    """
    for x in range(track0, track1 + 1):
        px[x, y] = tether
    positions = []
    pos = track1 - phase * spacing
    while pos >= track0 - spacing:
        positions.append(pos)
        pos -= spacing
    if outward:
        positions = [track0 + track1 - p for p in positions]
    behind = -1 if outward else 1
    for pos in positions:
        base = int(round(pos))
        for step in range(span):
            xs = base + step * behind
            if track0 <= xs <= track1:
                px[xs, y] = colour
        if spacing > 6:                                   # room for a tail
            for fade, dx in ((0.5, 1), (0.22, 2)):
                xs = base + (span - 1 + dx) * behind
                if track0 <= xs <= track1:
                    px[xs, y] = tuple(max(a, int(c * fade))
                                      for a, c in zip(tether, colour))
    return positions


def _static_link_row(px, y: int, track0: int, track1: int, *, span: int,
                     colour: tuple[int, int, int],
                     tether: tuple[int, int, int]) -> None:
    """An active direction whose range is unknown: presence, never velocity.

    A centred, stationary mark carries direction and (for receive) the rate
    bucket without assigning it an invented crossing time, spacing or endpoint.
    """
    for x in range(track0, track1 + 1):
        px[x, y] = tether
    centre = (track0 + track1) // 2
    first = centre - (span - 1) // 2
    for x in range(first, first + span):
        if track0 <= x <= track1:
            px[x, y] = colour


def pulse_span(bps: float | None) -> int:
    """How long a pulse is drawn, from the data rate.

    The chain already carries DISTANCE in its spacing. This puts the other
    half of the link on screen: a 2 Mbit downlink from Mars arrives as a fat
    dash, Voyager's 160 bits per second as a bare flicker. Three steps, not a
    ramp — the panel's gamma erases anything finer.
    """
    if bps is not None and math.isfinite(bps) and bps >= 1e6:
        return 3
    if bps is not None and math.isfinite(bps) and bps >= 1e3:
        return 2
    return 1


def packet_spacing(cross_s: float, track_px: int) -> float:
    """Pixels between packets in the chain, for a FIXED-duration loop.

    This is what decouples everything. The loop used to be as long as the
    crossing, so text scrolled at whatever speed the spacecraft's distance
    dictated — a name crawling for two minutes on Voyager. Now the loop is
    always LOOP_S and distance shows up as SPACING instead: each packet
    still takes the represented crossing time to cross (speed = spacing / LOOP_S =
    track / cross_s), but a distant craft simply has more of them in flight.

    That count is physically real — it is how many LOOP_S-long chunks of
    signal are in transit at once. Voyager carries about fifteen; Mars has
    one packet and a pause.
    """
    return max(2.0, track_px * LOOP_S / max(cross_s, 0.1))


DISTANCE_SPACING_QUANTA = 16
DISTANCE_PROGRESS_QUANTA = 3


def represented_packet_spacing(light_s: float | None, live: bool) -> float | None:
    """Canonical spacing shared by the Distance cache key and renderer."""
    if not light_s:
        return None
    crossing = light_s if live else crossing_seconds(light_s)
    raw = packet_spacing(crossing, TRACK1 - TRACK0)
    return round(raw * DISTANCE_SPACING_QUANTA) / DISTANCE_SPACING_QUANTA


def represented_progress(
        light_s: float, now_ts: float, since: float,
        ) -> tuple[int, float]:
    """Physical-panel progress bucket and its exact canonical fraction."""
    width = TRACK1 - TRACK0
    units = int(round(
        realtime_progress(light_s, now_ts, since)
        * width * DISTANCE_PROGRESS_QUANTA))
    return units, units / (width * DISTANCE_PROGRESS_QUANTA)


NAME_CHARS = 11          # roughly, at the default glyph width
SCROLL_GAP_PX = 8        # blank run between a scrolling label and its repeat


def realtime_progress(light_s: float, ts: float, since: float) -> float:
    """How far a light-speed traversal has got: 0 at the craft, 1 at Earth.

    A chain of evenly spaced marks was the first attempt and it failed as a
    picture. Twelve identical dots strung across the strip say "signal exists"
    but never say how far along anything is -- and at these distances they do
    not move either, so there is nothing to read. One bright head and the
    ground already covered behind it answer the question actually being asked:
    how far does light get across this distance after this much real time.

    Measured from the moment you locked on, so the head starts AT the craft
    and you watch it go. It used to be anchored to absolute time, which meant
    locking on dropped you into the middle of a crossing already in progress
    with no way to tell why -- and worse, the clock label was reporting a
    different time reference entirely. The head and countdown now share one
    lock instant and one arrival deadline.

    This is an honest distance/time ruler, not decoded telemetry. A DSN
    downSignal says a carrier is arriving at Earth now; it cannot prove that a
    particular bit left the spacecraft at the moment the user clicked.
    """
    if not light_s or light_s <= 0:
        return 0.0
    return min(1.0, max(0.0, (ts - since) / light_s))


def _mark(px, x: float, y: int, color: tuple[int, int, int]) -> None:
    """Draw a mark at a FRACTIONAL x by splitting it across two LEDs.

    The panel has no spatial subpixels -- one pixel is one RGB package on a
    2.2 mm pitch -- but apparent size tracks brightness, so splitting intensity
    between the two LEDs a mark straddles reads as a position between them.
    Gamma crushes fine steps (deltas under ~30% are invisible on the panel),
    so this quantises to about three usable positions per pixel rather than a
    smooth ramp. That is what makes a 27-minute-per-pixel creep perceptible.
    """
    base = int(math.floor(x))
    frac = x - base
    for xi, weight in ((base, 1.0 - frac), (base + 1, frac)):
        if not (TRACK0 <= xi <= TRACK1) or weight < 0.25:
            continue
        step = 1.0 if weight > 0.8 else (0.65 if weight > 0.5 else 0.4)
        lit = tuple(int(c * step) for c in color)
        px[xi, y] = tuple(max(a, b) for a, b in zip(lit, px[xi, y]))


def _scale_rgb(colour: tuple[int, int, int], scale: float) -> tuple[int, int, int]:
    """Scale an RGB triple without widening its static type to tuple[int, ...]."""
    return (
        int(colour[0] * scale),
        int(colour[1] * scale),
        int(colour[2] * scale),
    )


def countdown_label(light_s: float | None, since: float, now_ts: float) -> str:
    """Time left in the real-time light-crossing watch.

    This replaced a clock showing when the signal DEPARTED, which answered a
    question nobody asked and needed a paragraph to explain. A countdown needs
    none: it and the travelling pulse tell the same story, and the number is
    the point of the whole app -- how long it takes to cross that distance.

    It runs at real speed and it really does finish. Voyager's is 19 hours 48
    minutes, which completes tomorrow morning on a device that sits on a desk
    all day; completing the watch is a real elapsed-time event rather than a
    compressed imitation of one.
    """
    if not light_s:
        return "?"
    left = max(0.0, light_s - (now_ts - since))
    if left >= 3600:
        hours, rest = divmod(int(left), 3600)
        return f"{hours}:{rest // 60:02d}"
    minutes, secs = divmod(int(left), 60)
    return f"{minutes}:{secs:02d}"


def arrived(light_s: float | None, since: float, now_ts: float) -> bool:
    """Has one full light time elapsed? That ends the watch."""
    if not light_s:
        return False
    return (now_ts - since) >= light_s


def realtime_redraw_s(light_s: float | None) -> float:
    """How often a locked scene must be re-pushed for the chain to creep.

    Roughly the time one mark takes to cross a single pixel — pushing faster
    just re-uploads an identical frame. Bounded so a Mars pass updates often
    enough to watch and Voyager doesn't spend the day uploading.
    """
    if not light_s or light_s <= RT_SEAMLESS_MAX_S:
        return float(REDRAW_S)          # the device is animating it itself
    return max(10.0, min(300.0, light_s / (TRACK1 - TRACK0)))


# The ground antenna beside the dish number — and it LEANS THE WAY THE REAL
# DISH LEANS. Elevation was parsed off the feed for every link and used
# nowhere but a log line; here it becomes the one thing the icon says.
#
# Two rules were learned the hard way drawing about forty of these, and both
# are why the previous 4x5 glyph read as a letter Y rather than an antenna:
#
#   1. HUE separates more than shape does. In DISH_NO — the digits' own blue
#      — the glyph is read as another character in "43". A hue of its own is
#      read as a picture. Do not fold this back into the text colour.
#   2. A cup on a CENTRED stem is a letter Y at every size that fits here.
#      The mast has to run off-centre for the same pixels to read as an
#      object.
#
# Six rows, not five: y5 is free at this x (the tether does not start until
# y6), and a base line one row below the digits is what makes the thing look
# ground-mounted instead of floating.
ANTENNA = (200, 170, 110)
DISH_ICON_W = 6
MODE_X = 14          # the one free column between the globe's limb and the icon
DDOR_MARK = (150, 230, 160)

# (elevation ceiling, rows). THREE steps, not five: a fourth would differ by
# a single pixel and this panel's gamma erases differences that small, the
# same rule that keeps texture above 30% contrast.
# 34 m dishes. Same six-column footprint as the 70 m set below, so the label
# layout never shifts — only the aperture grows.
DISH_TILTS = (
    # near the horizon — a pass just starting, or nearly over
    (30.0, ("......",
            "1.....",
            "11....",
            "1.1...",
            "..1...",
            ".11111")),
    # the working middle of a pass
    (60.0, ("11....",
            "1.1...",
            "11....",
            "..1...",
            "..1...",
            ".11111")),
    # overhead, the best part of the pass
    (91.0, ("1..1..",
            ".11...",
            "..1...",
            "..1...",
            "..1...",
            ".11111")),
)

# The 70 m antennas — DSS-14, 43 and 63 — have four times the collecting area
# of a 34 m, and it is not a decorative difference: Voyager can essentially
# only be heard by these three. A bigger cup on the same mount says so without
# costing a column of the spacecraft name.
DISH_TILTS_70 = (
    (30.0, ("1.....",
            "11....",
            "1..1..",
            "1..1..",
            ".11...",
            ".11111")),
    (60.0, ("11....",
            "1..1..",
            "1..1..",
            "11....",
            "..1...",
            ".11111")),
    (91.0, ("1...1.",
            "1...1.",
            ".111..",
            "..1...",
            "..1...",
            ".11111")),
)


def dish_tilt(elevation: float, big: bool = False) -> tuple[str, ...]:
    """The antenna leaning the way the real dish is leaning.

    A parked dish reports elevation 90 with azimuth 0 and no signals, but it
    also reports its target as DSN or DSS, so NOT_SPACECRAFT has already
    dropped it before a Link exists. Missing pointing is intercepted by
    `_dish_icon` and rendered as unknown rather than passed here as zero.
    """
    table = DISH_TILTS_70 if big else DISH_TILTS
    for ceiling, rows in table:
        if elevation < ceiling:
            return rows
    return table[-1][1]


def _dish_icon(px, x: int, y: int, elevation: float | None,
               big: bool = False) -> None:
    if elevation is None:
        # Missing pointing is not a horizon-pointing dish. A question mark in
        # the antenna's own ink preserves the footprint and says exactly what
        # is unknown.
        _text(px, x + 1, y, "?", ANTENNA)
        return
    for dy, row in enumerate(dish_tilt(elevation, big)):
        for dx, bit in enumerate(row):
            if bit == "1" and 0 <= x + dx < W and 0 <= y + dy < H:
                px[x + dx, y + dy] = ANTENNA


def craft_label(code: str, names: dict[str, str]) -> str:
    """NASA's friendlyName when known, otherwise code; never sliced.

    Anything wider than the box scrolls (see scroll_offset), so 'Advanced
    Composition Explorer' is shown rather than clipped to a stub. Unsupported
    custom-font characters keep their position as an explicit question mark.
    """
    full = names.get(code.lower(), "")
    full = "".join(ch for ch in full if ch.isprintable()).strip()
    # The config feed is live vocabulary, not a frozen allowlist. Silently
    # skipping a future apostrophe/ampersand leaves a mysterious blank in the
    # promised complete marquee. Preserve its position with an explicit
    # drawable unknown glyph instead.
    label = (full or code).upper()
    return "".join(ch if ch in FONT else "?" for ch in label)


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


def contact_label(link: Link, names: dict[str, str]) -> str:
    """Full craft identity followed by a meaningful live activity badge."""
    craft = craft_label(link.craft, names)
    badge = activity_badge(link.activity)
    # TTC/telemetry is the ordinary meaning of a visible RF contact. Keep the
    # network row focused on identity; surface exceptional engineering state.
    return f"{craft} / {badge}" if badge in {"DEMO", "UPGRADE", "ENGINEER"} else craft


def scroll_offset(text: str, phase: float, box_px: int) -> int:
    """Pixels to shift a too-wide label left, looping seamlessly.

    The label travels exactly one full cycle (its own width plus a gap) per
    animation loop, so the last frame hands back to the first with no jump.
    Text that fits doesn't move at all.
    """
    text_px = text_width(text)
    if text_px <= box_px:
        return 0
    cycle = text_px + SCROLL_GAP_PX
    return int(phase * cycle)


def scroll_frame_count(text: str, box_px: int,
                       minimum: int = INSTRUMENT_FRAMES) -> int:
    """A whole native-loop length that keeps marquees physically readable.

    Long labels used to cover their entire width in four seconds, jumping as
    many as eight spaced LEDs per frame. Round up to complete eight-second
    RF cycles so the name stays below a fixed apparent speed and both the RF
    motion and the final->first animation seam remain continuous.
    """
    if text_width(text) <= box_px:
        return minimum
    needed = math.ceil(
        (text_width(text) + SCROLL_GAP_PX) / SCROLL_SPEED_PX_S
        * INSTRUMENT_FPS)
    cycles = max(1, math.ceil(needed / INSTRUMENT_FRAMES))
    return min(MAX_ANIMATION_FRAMES,
               max(minimum, cycles * INSTRUMENT_FRAMES))


def independent_scroll_offset(
        text: str, box_px: int, index: int, frame_count: int,
        ) -> int:
    """A seam-safe per-label clock capped at ``SCROLL_SPEED_PX_S``.

    Several labels may share one native asset without sharing one apparent
    speed.  Each completes an integer number of its own cycles over the asset,
    which makes the last-to-first step ordinary rather than a jump.  Flooring
    the cycle count keeps every label at or below the physical readability
    ceiling; a short label may move more slowly, never faster.
    """
    return independent_pixel_scroll_offset(
        text_width(text), box_px, index, frame_count)


def independent_pixel_scroll_offset(
        width: int, box_px: int, index: int, frame_count: int,
        ) -> int:
    """The measured-strip form of :func:`independent_scroll_offset`."""
    if width <= box_px or frame_count <= 0:
        return 0
    cycle = width + SCROLL_GAP_PX
    duration_s = frame_count / INSTRUMENT_FPS
    turns = max(1, math.floor(
        SCROLL_SPEED_PX_S * duration_s / cycle))
    return int(index * turns * cycle / frame_count) % cycle


def text_width(text: str) -> int:
    """Ink width. The trailing inter-glyph gap isn't ink, and counting it
    was enough to make 'VOYAGER 2' scroll in a box it fits exactly."""
    if not text:
        return 0
    return sum(glyph_width(ch) + GLYPH_GAP for ch in text.upper()) - GLYPH_GAP


RATE_LABEL_MAX_GBPS = 999.0
NARRATION_RECORD_DETAIL_MAX = 4


def rate_label(bps: float | None) -> str:
    """Always ...BPS: '160B' next to '19.8H' reads as bytes, and the units
    are the whole point of putting the number there. Values beyond the compact
    display's range get an inequality, never a false capped exact value."""
    if bps is None or not math.isfinite(bps) or bps < 0:
        return "RATE?"
    if bps >= 1e9:
        gbps = bps / 1e9
        return (f">{RATE_LABEL_MAX_GBPS:.0f}GBPS"
                if gbps > RATE_LABEL_MAX_GBPS else f"{gbps:.0f}GBPS")
    if bps >= 1e6:
        return f"{bps / 1e6:.0f}MBPS"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f}KBPS"
    if bps > 0:
        return f"{int(bps)}BPS"
    return "0BPS"


def fit_row(left: str, right: str, room: int, gap: int = 3) -> tuple[str, str]:
    """Fit two complete labels, or tell the caller the right one must move.

    Anchoring to opposite ends is not enough on its own: '18:43' beside
    '246KBPS' wants 61 px of a 56 px row and the two overlapped outright on
    the panel. Returning an empty right label is an explicit layout signal;
    the Distance renderer responds by marqueeing the complete semantic token.
    This helper must never invent a prefix such as UPLI or strip a unit.
    """
    left = fit_label(left, room)
    if text_width(left) + gap + text_width(right) <= room:
        return left, right
    return left, ""


def fit_label(text: str, room: int) -> str:
    """Clip a hostile/source token to actual pixel width, never into a gutter."""
    text = "".join(ch for ch in text.upper() if ch in FONT)
    while text and text_width(text) > room:
        text = text[:-1]
    return text


def light_label(light_s: float | None) -> str:
    """Distance as time — the thing actually worth reading."""
    if not light_s:
        return "?"
    if light_s < 1:
        return "SUBSEC"
    if light_s < 90:
        return f"{light_s:.0f}SEC"
    if light_s < 5400:
        return f"{light_s / 60:.0f}M"
    return f"{light_s / 3600:.1f}H"


def _draw_text_segments(
        px, x: int, y: int,
        segments: tuple[tuple[str, tuple[int, int, int]], ...],
        clip: tuple[int, int],
        ) -> None:
    """Draw adjacent semantic text segments without losing glyph columns."""
    cursor = x
    for text, colour in segments:
        for ch in text:
            _text(px, cursor, y, ch, colour, clip=clip)
            cursor += glyph_width(ch) + GLYPH_GAP


def distance_header_layout(
        link: Link, names: dict[str, str] | None = None,
        ) -> tuple[str, int | None, tuple[int, int], str, bool, int]:
    """Complete Distance identity, with a combined future-width fallback.

    Current two-digit dishes keep the established coloured dish/name layout.
    If a future complete dish suffix would consume the craft box, both tokens
    become one segmented marquee rather than clipping, renaming, or indexing
    outside the framebuffer.
    """
    tx = GLOBE_CX + GLOBE_R + 3
    num_x = tx + DISH_ICON_W + 2
    dish = _dish_suffix(link.dish)
    craft = craft_label(link.craft, names or {})
    sep_candidate = num_x + text_width(dish) + 2
    if sep_candidate + 3 <= W - max(glyph_width("?"), 1):
        sep: int | None = sep_candidate
        box = (sep_candidate + 3, W - 1)
        moving = craft
        combined = False
    else:
        sep = None
        box = (num_x, W - 1)
        moving = f"{dish}/{craft}"
        combined = True
    frames = scroll_frame_count(
        moving, box[1] - box[0] + 1, ANIM_FRAMES)
    return dish, sep, box, moving, combined, frames


def distance_frame_count(
        link: Link, names: dict[str, str] | None = None,
        ) -> int:
    """Native asset length required by the complete Distance identity."""
    return distance_header_layout(link, names)[-1]


def distance_loop_s(
        link: Link, names: dict[str, str] | None = None,
        ) -> float:
    return distance_frame_count(link, names) / ANIM_FPS


def render_frames(link: Link, now: datetime,
                  names: dict[str, str] | None = None,
                  site_lons: dict[str, float] | None = None,
                  realtime_since: float | None = None,
                  dish_types: dict[str, str] | None = None,
                  on_air: bool | None = None,
                  handoff: bool = False,
                  freshness: str = "fresh",
                  ) -> tuple[list[Image.Image], int, int]:
    """One message's journey home, looped. Returns (frames, fps, frame_hold).

    Mostly-off by design: the panel's LEDs are physically spaced, so only
    lit pixels carry shape. Everything not part of Earth, the spacecraft,
    the pulse or the labels stays black.
    """
    # Where the sun actually is: the subsolar longitude is noon's meridian,
    # so the terminator on the globe is the real one.
    sun_lat, sun_lon = subsolar(now)
    # The globe is centred on the complex doing the listening, so its lit or
    # dark face is the real day or night AT THAT ANTENNA. The camera used to
    # drift instead — the centre advanced 15 deg/hr while the subsolar point
    # retreated 15 deg/hr, so they lapped twice a day and the disc went from
    # fully lit to fully dark and back every twelve hours, which no vantage
    # point in the universe does.
    centre_lon = (site_lons or {}).get(_site_name(link.complex_name))
    if (centre_lon is not None and math.isfinite(centre_lon)
            and -180.0 <= centre_lon <= 180.0):
        site_geometry_known = True
        true_spin = (centre_lon % 360.0) / 360.0
    else:
        site_geometry_known = False
        true_spin = 0.0
    # Locked: the crossing is the real one, not the 1/600 browsing speed.
    pulse = BAND_PULSE.get(band_key(link.band), UNKNOWN_PULSE)
    big_dish = dish_metres(link.dish, dish_types) == "70"
    light_s = link.light_s
    live = realtime_since is not None and light_s is not None
    range_known = light_s is not None
    # Past a couple of minutes an 8-second loop cannot animate the truth, so
    # the chain is placed from the wall clock instead and simply sits there
    # between redraws. It is not frozen; it is moving at 0.0006 px/s.
    listening = link.down_active
    if (realtime_since is not None and light_s is not None
            and light_s > RT_SEAMLESS_MAX_S):
        creeping = True
        progress = represented_progress(
            light_s, now.timestamp(), realtime_since)[1]
    else:
        creeping = False
        progress = 0.0

    dish_no, header_sep_x, header_box, header_text, combined_header, frame_count = (
        distance_header_layout(link, names))

    track0, track1 = TRACK0, TRACK1
    frames: list[Image.Image] = []
    for i in range(frame_count):
        rf_index = i % ANIM_FRAMES
        phase = rf_index / ANIM_FRAMES
        # A browsing carrier is source-driven and freezes when the source
        # lease ages out. A locked journey is a disclosed local stopwatch, so
        # its represented head may continue under an explicit stale label.
        rf_phase = phase if freshness == "fresh" or live else 0.0
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)

        # One turn per loop: seamless, and identical for every spacecraft
        # because the loop is a fixed 8 seconds. (It was once tied to a loop
        # whose length was the crossing time, so Earth span faster whenever a
        # nearer craft came up. That must not come back.)
        #
        # The shadow turns WITH the planet, because night belongs to the
        # geography and not to the screen. Every frame is a true picture of
        # which longitudes are in daylight at this instant; the loop simply
        # carries you around the planet faster than anyone could travel.
        # Turning past the terminator is also what guarantees you see one at
        # all — pinning it to the screen left the disc entirely lit or
        # entirely dark for the whole loop at some hours.
        # Anchored to the wall clock, not to the start of this animation:
        # every push previously restarted the turn wherever the loop began,
        # so a new scene snapped the planet to a different rotation. Deriving
        # it from the clock means a fresh loop picks up exactly where the last
        # one left off.
        # MINUS, not plus. `spin` is the longitude at the centre of the disc,
        # and for an observer fixed in space that longitude DECREASES as Earth
        # turns east - which is exactly why the subsolar longitude above
        # decreases too. Adding spun the planet backwards: Greenwich drifted
        # left across the disc where the real thing drifts right, and since
        # the shadow rides the geography the terminator swept the wrong way
        # with it.
        spin = (true_spin - phase - now.timestamp() / LOOP_S) % 1.0
        if site_geometry_known:
            _globe(px, spin, sun_lat, sun_lon)
        else:
            _unknown_globe(px)
        _craft(px, CRAFT_X, CRAFT_Y, link.craft, phase)

        # The chain, travelling spacecraft -> Earth. Every packet advances
        # exactly one spacing across the loop, so the pattern at the last
        # frame is the pattern at the first: seamless at any speed.
        # TWO links, stacked, because a pass is a conversation. Earth's
        # half runs on the upper row and the spacecraft's on the lower, each
        # with its own tether. Both on one line would be ambiguous no matter
        # how they were coloured; the renderer used to dodge that by drawing
        # only one direction, so on a two-way pass half the conversation was
        # simply invisible.
        spacing = represented_packet_spacing(link.light_s, live) or 2.0
        positions = []      # the downlink chain, reused by the arrival flare

        if link.up_active and creeping:
            # Locked, Earth's half is one represented carrier slice too. The distance is the
            # same in both directions, so a full chain outbound beside a
            # single creeping pulse inbound would claim they travel at
            # different speeds.
            for tx_ in range(track0, track1 + 1):
                px[tx_, UP_Y] = UP_TETHER
            uphead = track0 + progress * (track1 - track0)
            back = int(math.floor(uphead)) - 2
            while back >= track0:
                px[back, UP_Y] = _scale_rgb(UPLINK, 0.28)
                back -= 2
            for dx in (0, -1, -2):
                _mark(px, uphead + dx, UP_Y,
                      UPLINK if dx == 0 else
                      _scale_rgb(UPLINK, 0.5 if dx == -1 else 0.25))
        elif link.up_active and range_known:
            # Earth SHOUTS: tens of kilowatts. Drawn heavy and bright.
            _link_row(px, UP_Y, track0, track1, rf_phase, spacing,
                      outward=True, span=3, colour=UPLINK, tether=UP_TETHER)
        elif link.up_active:
            _static_link_row(px, UP_Y, track0, track1, span=3,
                             colour=UPLINK, tether=UP_TETHER)

        if listening:
            if creeping:
                head = track1 - progress * (track1 - track0)
                positions = [head]
                for tx_ in range(track0, track1 + 1):
                    px[tx_, DOWN_Y] = TETHER
                trail = int(math.ceil(head)) + 2
                while trail <= track1:
                    px[trail, DOWN_Y] = _scale_rgb(pulse, 0.28)
                    trail += 2
                _mark(px, head, DOWN_Y, pulse)
                for dx in (1, 2):                # a short bright wake
                    _mark(px, head + dx, DOWN_Y,
                          _scale_rgb(pulse, 0.5 if dx == 1 else 0.25))
            elif range_known:
                # ...and what comes back is attowatts. Thin, and never as
                # bright as the uplink: 18 kW out against eight attowatts back is
                # a ratio near 10^21, and
                # the contrast IS the story. Bounded by the panel's gamma
                # floor so "faint" still survives on real LEDs.
                positions = _link_row(
                    px, DOWN_Y, track0, track1, rf_phase, spacing,
                    outward=False, span=pulse_span(link.down_bps),
                    colour=pulse, tether=TETHER)
            else:
                _static_link_row(
                    px, DOWN_Y, track0, track1,
                    span=pulse_span(link.down_bps), colour=pulse, tether=TETHER)
        else:
            # Nothing coming down, but the link is still there. Drawing the
            # quiet direction as a bare tether says "open, and silent", which
            # is the truth on an uplink-only pass.
            for tx_ in range(track0, track1 + 1):
                px[tx_, DOWN_Y] = TETHER

        # Flare the limb when a packet actually lands, whatever the spacing.
        if any(track0 <= int(round(p)) <= track0 + 1 for p in positions):
            for dy in (-1, 0, 1):
                y = DOWN_Y + dy
                gx = GLOBE_CX + GLOBE_R
                if 0 <= gx < W and 0 <= y < H:
                    px[gx, y] = (255, 240, 200)

        # Two text rows, both clear of the globe's column.
        # Top:    who it is                        | which dish
        # Bottom: how far away, how fast
        if (live and freshness == "fresh" and on_air is not False
                and not handoff and (rf_index // 10) % 2 == 0):
            # Two pixels in the free corner, blinking once every four seconds:
            # the chain itself may not visibly move for half an hour, so
            # something has to say the strip is alive and showing real time.
            for mx in (0, 1):
                px[mx, 0] = (255, 120, 60)

        # Each label sits at the end it describes: the antenna beside the
        # globe it is standing on, the spacecraft's name beside the
        # spacecraft. They used to be the other way round, which made the
        # eye cross the strip to connect either one to its subject.
        # Arraying, MSPA and DDOR were narrated and never drawn, so a
        # four-dish array looked exactly like one dish. One column at x14 —
        # the only empty space on this row — carries them: three marks for
        # several dishes on one spacecraft, two for several spacecraft in one
        # beam, one for a navigation fix. They can co-occur and never collide.
        if link.arrayed:
            for yy in (0, 2, 4):
                px[MODE_X, yy] = ANTENNA
        if link.mspa:
            for yy in (1, 3):
                px[MODE_X, yy] = NAME
        if link.ddor:
            px[MODE_X, 2] = DDOR_MARK

        tx = GLOBE_CX + GLOBE_R + 3
        _dish_icon(px, tx, 0,
                   link.elevation if link.pointing_valid else None, big_dish)
        num_x = tx + DISH_ICON_W + 2
        header_box_px = header_box[1] - header_box[0] + 1
        header_off = independent_scroll_offset(
            header_text, header_box_px, i, frame_count)
        if combined_header:
            segments = ((dish_no, DISH_NO), ("/", (70, 85, 105)),
                        (craft_label(link.craft, names or {}), NAME))
            _draw_text_segments(
                px, header_box[0] - header_off, 0, segments, header_box)
            if header_off:
                _draw_text_segments(
                    px, header_box[0] - header_off + text_width(header_text)
                    + SCROLL_GAP_PX, 0, segments, header_box)
        else:
            _text(px, num_x, 0, dish_no, DISH_NO)
            # A dim rule, not a '|' glyph: it costs 1px instead of 5, and
            # prevents "43" and "VOYAGER 2" reading as one token.
            assert header_sep_x is not None
            for yy in range(0, 5):
                px[header_sep_x, yy] = (40, 55, 75)
            _text(px, header_box[0] - header_off, 0, header_text, NAME,
                  clip=header_box)
            if header_off:
                _text(px, header_box[0] - header_off
                      + text_width(header_text) + SCROLL_GAP_PX,
                      0, header_text, NAME, clip=header_box)
        # Left/right rather than one string keeps the stable distance beside
        # a right-hand status. When both complete tokens do not fit, only the
        # right token moves; it is never shortened into a misleading prefix.
        # Locked, the countdown is a DEVICE element that ticks by itself once
        # a second (see _countdown_payload). Baked into these frames it would
        # only change when the scene is re-pushed, which on Voyager is once
        # every five minutes — a clock that jumps five minutes at a time.
        # Its text is still measured here so the rate label on the right is
        # laid out against the space it occupies, then simply not drawn.
        if live:
            assert realtime_since is not None
            far = countdown_label(light_s, realtime_since, now.timestamp())
        else:
            far = light_label(light_s)
        fast_full = ("DELAY" if freshness == "delayed" else
                     "STALE" if freshness in {"stale", "offline"} else
                     "OFF AIR" if live and on_air is False else
                     "HANDOFF" if live and handoff else
                     rate_label(link.down_bps) if listening else
                     "UPLINK" if link.up_active else "QUIET")
        far, fast = fit_row(far, fast_full, W - 1 - tx)
        if not live:                       # the device draws it when locked
            _text(px, tx, H - 5, far, DIST)
        if fast == fast_full:
            fast_x = W - 1 - text_width(fast)
            _text(px, fast_x, H - 5, fast, RATE)
        else:
            # A semantic status or unit must never be made to "fit" by
            # amputating its suffix. MMS2's SUBSEC + UPLINK used to become
            # UPLI here. Keep the distance fixed and marquee the complete
            # right-hand label through the remaining semantic box.
            fast = fit_label(fast_full, W)
            fast_x = tx + text_width(far) + 3
            fast_box = (fast_x, W - 1)
            off = independent_scroll_offset(
                fast, fast_box[1] - fast_box[0] + 1, i, frame_count)
            _text(px, fast_box[0] - off, H - 5, fast, RATE,
                  clip=fast_box)
            _text(px, fast_box[0] - off + text_width(fast)
                  + SCROLL_GAP_PX, H - 5, fast, RATE, clip=fast_box)
        # Same divider as the top row. On Voyager these two close to within a
        # couple of pixels and '19.8H' '160BPS' reads as one long number.
        gap0, gap1 = tx + text_width(far), fast_x
        if gap1 - gap0 >= 3:
            for yy in range(H - 5, H):
                px[(gap0 + gap1) // 2, yy] = (40, 55, 75)
        frames.append(img)
    return frames, ANIM_FPS, 1


# --- live instrument -------------------------------------------------------

SCOPE_CX, SCOPE_CY, SCOPE_R = 7, 7, 6
INSTRUMENT_X0, INSTRUMENT_X1 = 16, 60
INSTRUMENT_CONTENT_X1 = 69
FRESH_GUTTER_X = 70
FRESH_X = 71
INSTRUMENT_METRIC_MIN_FRAMES = 8  # 1.6s/page at 5fps; round by RF loop
SCOPE_RING = (34, 66, 82)
SCOPE_TRAIL = (105, 170, 190)
SCOPE_HEAD = (255, 232, 150)
INSTRUMENT_TETHER = (46, 74, 96)
FRESH = (70, 220, 235)
DELAYED = (255, 174, 50)
STALE = (245, 65, 55)


def pointing_pixel(azimuth: float, elevation: float) -> tuple[int, int]:
    """Project DSN az/el into the 15x15 polar scope.

    North is up, east is right, zenith is the centre and the horizon is the
    ring.  This is actual antenna geometry, not a decorative orbit.
    """
    az = math.radians(azimuth % 360.0)
    radius = (90.0 - min(90.0, max(0.0, elevation))) / 90.0 * SCOPE_R
    return (int(round(SCOPE_CX + math.sin(az) * radius)),
            int(round(SCOPE_CY - math.cos(az) * radius)))


def link_pointing_pixel(link: Link) -> tuple[int, int] | None:
    return (pointing_pixel(link.azimuth, link.elevation)
            if link.pointing_valid else None)


def rate_bucket(bps: float | None) -> int:
    """Device-resolution occupancy buckets; rate never changes propagation speed."""
    if bps is None or not math.isfinite(bps) or bps <= 0:
        return 0
    if bps < 1_000:
        return 1
    if bps < 100_000:
        return 2
    if bps < 1_000_000:
        return 3
    if bps < 10_000_000:
        return 4
    return 5


def receive_power_bucket(dbm: float | None) -> int:
    if (dbm is None or not math.isfinite(dbm)
            or not RECEIVE_POWER_MIN_DBM <= dbm < 0.0):
        return 0
    if dbm < -150:
        return 1
    if dbm < -135:
        return 2
    return 3


def transmit_power_bucket(kw: float | None) -> int:
    if kw is None or not math.isfinite(kw) or kw <= 0:
        return 0
    if kw < 1:
        return 1
    if kw < 10:
        return 2
    return 3


def wind_bucket(kmh: float | None) -> int | str | None:
    """Five km/h steps are useful live context without 1 km/h asset churn."""
    if kmh is None or not math.isfinite(kmh) or kmh < 0:
        return None
    if kmh > 995:
        return "UNKNOWN"  # malformed/extreme input, never a false exact cap
    return int(round(kmh / 5.0) * 5)


def link_streams(link: Link) -> tuple[DownStream, ...]:
    """Keep old fixtures/caches useful while the parser now retains streams."""
    if link.down_streams:
        streams = link.down_streams
    elif ((link.down_bps is not None and link.down_bps > 0)
          or link.streams > 0):
        streams = (DownStream(link.band, link.down_bps, link.down_dbm),)
    else:
        streams = ()
    # XML ordering is not telemetry. Canonicalise by what the panel can
    # distinguish so a harmless source reorder cannot swap lanes or emit an
    # event; raw jitter inside a bucket likewise cannot reshuffle them.
    return tuple(sorted(streams, key=lambda stream: (
        band_key(stream.band), rate_bucket(stream.bps),
        receive_power_bucket(stream.dbm))))


def link_upstreams(link: Link) -> tuple[UpStream, ...]:
    """Canonical active up-signal records, with legacy-fixture fallback."""
    if link.up_streams:
        streams = link.up_streams
    elif link.up_active:
        streams = (UpStream(link.up_band, link.up_kw, ""),)
    else:
        streams = ()
    return tuple(sorted(streams, key=lambda stream: (
        band_key(stream.band), transmit_power_bucket(stream.kw or 0.0),
        stream.signal_type)))


def feed_freshness(state: State, now: float | None = None) -> str:
    """Require both source advancement and a reasonably current source epoch."""
    if state.feed_timestamp_ms is None or state.feed_advanced_at is None:
        return "offline"
    current = now if now is not None else time.time()
    timestamp = state.feed_timestamp_ms
    if (not isinstance(timestamp, int)
            or not MIN_UNIX_TIMESTAMP_MS <= timestamp <= MAX_UNIX_TIMESTAMP_MS
            or not math.isfinite(current)
            or not math.isfinite(state.feed_advanced_at)):
        return "stale"
    age = current - state.feed_advanced_at
    source_age = current - timestamp / 1000.0
    if source_age < -FEED_FUTURE_SKEW_S or age < -FEED_FUTURE_SKEW_S:
        return "stale"
    age = max(age, source_age)
    if age <= FEED_DELAYED_S:
        return "fresh"
    if age <= FEED_STALE_S:
        return "delayed"
    return "stale"


def note_pointing(state: State, links: list[Link]) -> None:
    """Retain only real, pixel-visible antenna motion for the tracking tail."""
    live_keys = {link.key for link in links}
    for key in list(state.aim_trails):
        if key not in live_keys:
            state.aim_trails.pop(key, None)
    for link in links:
        point = link_pointing_pixel(link)
        if point is None:
            state.aim_trails.pop(link.key, None)
            continue
        trail = state.aim_trails.setdefault(link.key, [])
        if not trail or trail[-1] != point:
            trail.append(point)
            del trail[:-7]


def _ring_points() -> set[tuple[int, int]]:
    points = set()
    for degrees in range(0, 360, 15):
        angle = math.radians(degrees)
        points.add((int(round(SCOPE_CX + math.sin(angle) * SCOPE_R)),
                    int(round(SCOPE_CY - math.cos(angle) * SCOPE_R))))
    return points


SCOPE_POINTS = _ring_points()


def _carrier_marks(px, y: int, phase: float, count: int,
                   colour: tuple[int, int, int], outward: bool,
                   span: int = 1) -> None:
    """Move every carrier at one fixed symbolic speed; density carries bps."""
    if count <= 0:
        return
    width = INSTRUMENT_X1 - INSTRUMENT_X0
    for index in range(count):
        # Start away from an endpoint so a one-carrier stream is visible in
        # the first frame instead of spending its opening beat under a flare.
        fraction = (index / count + phase + 0.20) % 1.0
        # Sample width+1 positions so a 40-frame clock reaches both physical
        # endpoints without dwelling twice on either one. That keeps motion
        # to one or two LEDs per 200 ms and leaves a single intentional wrap.
        offset = max(0, min(width, int(round(fraction * (width + 1)))))
        x = (INSTRUMENT_X0 + offset if outward else INSTRUMENT_X1 - offset)
        direction = -1 if outward else 1
        for offset in range(span):
            xx = x + direction * offset
            if INSTRUMENT_X0 <= xx <= INSTRUMENT_X1:
                px[xx, y] = colour


def _draw_mode_glyph(px, x: int, rows: tuple[str, ...],
                     colour: tuple[int, int, int]) -> None:
    for dy, row in enumerate(rows):
        for dx, bit in enumerate(row):
            if bit == "1":
                px[x + dx, 6 + dy] = colour


def _metric_pages(left: str, right: str) -> tuple[tuple[str, str], ...]:
    """Lay out complete metric tokens, splitting instead of amputating them."""
    room = INSTRUMENT_CONTENT_X1 - INSTRUMENT_X0 + 1
    left = "".join(ch for ch in left.upper() if ch in FONT)
    right = "".join(ch for ch in right.upper() if ch in FONT)
    # All callers cap/normalise their generated vocabulary. A future token
    # wider than the entire metric rail is a programmer contract violation,
    # not permission to ship a misleading prefix.
    if text_width(left) > room or text_width(right) > room:
        raise ValueError(f"instrument metric token exceeds {room}px: {left!r}, {right!r}")
    if text_width(left) + 3 + text_width(right) <= room:
        return ((left, right),)
    return ((left, ""), (right, ""))


POWER_LABEL_MAX = 999.0


def receive_power_label(dbm: float | None) -> str:
    """Compact plausible spacecraft receive power without a false exact cap."""
    if dbm is None:
        return "NONE"
    if not math.isfinite(dbm):
        return "POWER?"
    if not RECEIVE_POWER_MIN_DBM <= dbm < 0.0:
        return "RANGE?"
    return f"{dbm:.0f}DBM"


def transmit_power_label(kw: float | None) -> str:
    """Compact source power with explicit upper-bound and invalid states."""
    if kw is None or not math.isfinite(kw) or kw < 0:
        return "POWER?"
    if kw == 0:
        return "NONE"
    if kw >= 1:
        return (f">{POWER_LABEL_MAX:.0f}KW" if kw > POWER_LABEL_MAX
                else f"{kw:.0f}KW")
    watts = kw * 1000
    return (f">{POWER_LABEL_MAX:.0f}W" if watts > POWER_LABEL_MAX
            else f"{watts:.0f}W")


def signal_count_label(count: int, suffix: str) -> str:
    """A compact exact record count, or an explicit two-digit overflow."""
    bounded = max(0, int(count))
    return (f">99{suffix}" if bounded > 99 else f"{bounded}{suffix}")


def _instrument_metrics(link: Link,
                        contact_state: str = "live") -> tuple[tuple[str, str], ...]:
    streams = link_streams(link)
    up_streams = link_upstreams(link)
    bands = []
    for down_stream in streams:
        key = band_key(down_stream.band)
        label = key if key in BAND_PULSE else "?"
        if label not in bands:
            bands.append(label)
    if streams:
        band = ("/".join(bands) if 0 < len(bands) <= 3 else
                f"{len(bands)}BAND" if bands else "?")
        # Record multiplicity does not prove independent, summable throughput.
        # Use the record itself only when exactly one is present; never trust a
        # legacy aggregate scalar beside several published receiver records.
        rate = rate_label(streams[0].bps if len(streams) == 1 else None)
    else:
        up_bands = []
        for up_stream in up_streams:
            key = band_key(up_stream.band)
            label = key if key in BAND_PULSE else "?"
            if label not in up_bands:
                up_bands.append(label)
        band = "/".join(up_bands) or ("UP" if link.up_active else "?")
        rate = "TX" if link.up_active else "QUIET"
    pages = list(_metric_pages(band, rate))
    rx = receive_power_label(link.down_dbm)
    known_up_powers = tuple(
        stream.kw for stream in up_streams
        if stream.kw is not None and math.isfinite(stream.kw)
        and stream.kw > 0)
    # An active uplink with no positive published power is unknown, not a
    # claim that a zero-power transmitter is operating. Inactive links retain
    # the useful NONE state.
    tx_power = (max(known_up_powers) if known_up_powers else
                None if up_streams or link.up_active else 0.0)
    tx = transmit_power_label(tx_power)
    pages.extend(_metric_pages("RX", rx))
    pages.extend(_metric_pages("TX", tx))
    count = (signal_count_label(max(link.streams, len(streams)), "RX")
             if streams else "NO RX")
    direction = ("DUPLEX" if link.up_active and streams else
                 "RX" if streams else "TX" if link.up_active else "QUIET")
    pages.extend(_metric_pages(direction, count))
    badge = activity_badge(link.activity)
    badge_label = {"ENGINEER": "ENG", "UPGRADE": "UPGRD"}.get(
        badge, badge or "UNKNOWN")
    pages.extend(_metric_pages("ACT", badge_label))
    if contact_state == "off_air":
        pages.insert(0, ("OFF AIR", ""))
    elif contact_state == "handoff":
        pages.insert(0, ("HANDOFF", ""))
    for mode, active in (("ARRAY", link.arrayed),
                         ("MSPA", link.mspa), ("DDOR", link.ddor)):
        if active:
            pages.extend(_metric_pages("MODE", mode))
    wind = wind_bucket(link.wind_kmh)
    if wind is not None:
        value = wind if isinstance(wind, str) else f"{wind}KMH"
        pages.extend(_metric_pages("WIND", value))
    if len(up_streams) > 1:
        pages.extend(_metric_pages(
            "UP", signal_count_label(len(up_streams), "SIG")))
    return tuple(pages)


def instrument_signature(link: Link, trail: list[tuple[int, int]],
                         freshness: str,
                         names: dict[str, str] | None = None,
                         contact_state: str = "live") -> tuple:
    """Only fields that can change a physical LED belong in this signature."""
    streams = tuple((band_key(s.band), rate_bucket(s.bps),
                     receive_power_bucket(s.dbm)) for s in link_streams(link))
    upstreams = tuple(transmit_power_bucket(s.kw or 0.0)
                      for s in link_upstreams(link)[:3])
    return ("instrument", link.key, craft_label(link.craft, names or {}), tuple(trail),
            link_pointing_pixel(link), streams, upstreams,
            link.arrayed, link.mspa,
            link.ddor, _instrument_metrics(link, contact_state), freshness,
            contact_state)


def instrument_header_layout(
        link: Link, names: dict[str, str] | None = None,
        ) -> tuple[str, int | None, tuple[int, int], str, int]:
    """Stable header geometry plus a marquee-safe native frame count."""
    dish = _dish_suffix(link.dish)
    sep_candidate = INSTRUMENT_X0 + text_width(dish) + 2
    sep: int | None = sep_candidate
    box = (sep_candidate + 2, INSTRUMENT_CONTENT_X1)
    craft = craft_label(link.craft, names or {})
    label = craft
    if box[1] - box[0] + 1 < max(glyph_width("?"), 1):
        # A future identifier may consume the old craft box.  One complete
        # combined marquee is truthful; taking the last three digits silently
        # renames the antenna.
        dish = ""
        sep = None
        box = (INSTRUMENT_X0, INSTRUMENT_CONTENT_X1)
        label = f"{_dish_suffix(link.dish)}/{craft}"
    frame_count = scroll_frame_count(label, box[1] - box[0] + 1)
    return dish, sep, box, label, frame_count


def instrument_frame_count(
        link: Link, names: dict[str, str] | None = None,
        contact_state: str = "live",
        ) -> int:
    """Whole header/RF cycles needed to give every metric a readable dwell."""
    header_frame_count = instrument_header_layout(link, names)[-1]
    metric_frames = (len(_instrument_metrics(link, contact_state))
                     * INSTRUMENT_METRIC_MIN_FRAMES)
    return max(
        header_frame_count,
        math.ceil(metric_frames / header_frame_count) * header_frame_count)


def render_instrument_frames(
        link: Link, trail: list[tuple[int, int]] | None = None,
        freshness: str = "fresh", names: dict[str, str] | None = None,
        contact_state: str = "live",
        ) -> tuple[list[Image.Image], int, int]:
    """A literal live antenna/RF instrument for one selected DSN link."""
    streams = list(link_streams(link))
    upstreams = list(link_upstreams(link))
    if len(streams) > 3:
        # Three physical rows fit. Keep two records literal, then use one
        # explicit overflow reference lane. Record multiplicity does not prove
        # independent, summable throughput or power, so the overflow lane must
        # not manufacture either scalar.
        rest = streams[2:]
        rest_bands = {band_key(s.band) for s in rest if band_key(s.band)}
        streams = streams[:2] + [DownStream(
            next(iter(rest_bands)) if len(rest_bands) == 1 else "",
            None, None, f"{len(rest)} RECORDS")]
    ys = {1: [9], 2: [8, 10], 3: [8, 9, 10]}.get(len(streams), [])
    metrics = _instrument_metrics(link, contact_state)
    frozen = freshness != "fresh" or contact_state == "off_air"
    dish, sep, box, label, header_frame_count = instrument_header_layout(
        link, names)
    # Metrics may need several RF loops, but they must not slow the unrelated
    # identity marquee.  The header owns its own fixed-speed native cycle;
    # extend the asset by whole header cycles so every repeated seam remains
    # exact while the metric pages get a readable dwell.
    frame_count = instrument_frame_count(link, names, contact_state)
    frames: list[Image.Image] = []
    for index in range(frame_count):
        text_phase = ((index % header_frame_count) / header_frame_count)
        phase = (0.0 if frozen else
                 (index % INSTRUMENT_FRAMES) / INSTRUMENT_FRAMES)
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)

        for point in SCOPE_POINTS:
            px[point] = SCOPE_RING
        px[SCOPE_CX, SCOPE_CY] = SCOPE_RING
        for point in (trail or [])[:-1]:
            px[point] = SCOPE_TRAIL
        current_point = link_pointing_pixel(link)
        if current_point is not None:
            px[current_point] = SCOPE_HEAD
        else:
            _text(px, 5, 5, "?", DISH_NO)
        px[SCOPE_CX, 0] = FRESH                    # north fiducial

        _text(px, INSTRUMENT_X0, 0, dish, DISH_NO)
        for yy in range(5):
            if sep is not None and sep < FRESH_X:
                px[sep, yy] = (50, 70, 90)
        off = scroll_offset(label, text_phase, box[1] - box[0] + 1)
        _text(px, box[0] - off, 0, label, NAME, clip=box)
        if off:
            _text(px, box[0] - off + text_width(label) + SCROLL_GAP_PX,
                  0, label, NAME, clip=box)

        # Every published active up-signal record gets a spatially separate
        # row (up to the three physical rows available). Several records are
        # not called several transmitters: the source does not promise that.
        up_rows = {1: [6], 2: [5, 7], 3: [5, 6, 7]}.get(
            min(3, len(upstreams)), [])
        if upstreams:
            for up_index, (y, up_stream) in enumerate(
                    zip(up_rows, upstreams[:3])):
                for x in range(INSTRUMENT_X0, INSTRUMENT_X1 + 1):
                    px[x, y] = UP_TETHER
                _carrier_marks(px, y, phase + up_index / len(up_rows), 3,
                               UPLINK, True, span=2)
                strength = transmit_power_bucket(up_stream.kw or 0.0)
                # Power grows sideways within the RF band, never upward into
                # the dish number. The old strong flare changed DSS43's digit.
                for dx in range(strength):
                    xx = INSTRUMENT_X0 - 1 + dx
                    if INSTRUMENT_X0 - 1 <= xx <= INSTRUMENT_X1:
                        px[xx, y] = UPLINK
        else:
            # A silent outbound direction remains a bare reference tether.
            for x in range(INSTRUMENT_X0, INSTRUMENT_X1 + 1):
                px[x, 6] = UP_TETHER

        if streams:
            for y, down_stream in zip(ys, streams):
                colour = BAND_PULSE.get(
                    band_key(down_stream.band), UNKNOWN_PULSE)
                for x in range(INSTRUMENT_X0, INSTRUMENT_X1 + 1):
                    px[x, y] = INSTRUMENT_TETHER
                _carrier_marks(
                    px, y, phase,
                    (0, 1, 2, 3, 5, 7)[rate_bucket(down_stream.bps)],
                    colour, False)
                strength = receive_power_bucket(down_stream.dbm)
                if strength:
                    flare = ((175, 105, 35), (225, 145, 55), colour)[strength - 1]
                    for dx in range(strength):
                        px[INSTRUMENT_X0 + dx, y] = flare
        else:
            for x in range(INSTRUMENT_X0, INSTRUMENT_X1 + 1):
                px[x, 9] = INSTRUMENT_TETHER

        if link.arrayed:
            _draw_mode_glyph(px, 61, ("101", "111", "010", "010", "010"), ANTENNA)
        if link.mspa:
            _draw_mode_glyph(px, 64, ("010", "010", "111", "101", "101"), NAME)
        if link.ddor:
            _draw_mode_glyph(px, 67, ("010", "101", "010", "101", "010"), DDOR_MARK)

        metric_page = min(len(metrics) - 1,
                          index * len(metrics) // frame_count)
        left, right = metrics[metric_page]
        # X-band carrier amber runs immediately above this row. DIST amber was
        # only 8 luminance points and 12% channel separation away, so a moving
        # carrier could merge into the label's top stroke on the physical
        # panel. Keep the first band/rate page green; later semantic labels use
        # the neutral identity ink, which remains distinct from every carrier.
        _text(px, INSTRUMENT_X0, 11, left,
              RATE if metric_page == 0 else NAME)
        _text(px, INSTRUMENT_CONTENT_X1 - text_width(right), 11, right,
              RATE if metric_page == 0 else UPLINK)

        # Fresh is intentionally absent here: one two-LED native rectangle
        # forms a short lease renewed only when NASA's timestamp advances. That
        # proves host/source life without redrawing and restarting this loop.
        if freshness == "delayed":
            if (index // 5) % 2 == 0:
                for y in (0, H // 2, H - 1):
                    px[FRESH_X, y] = DELAYED
        elif freshness in {"stale", "offline"}:
            for y in (0, H // 2, H - 1):
                px[FRESH_X, y] = STALE
        frames.append(img)
    return frames, INSTRUMENT_FPS, 1


# --- all-network live contact board ---------------------------------------

NETWORK_SITES = (("Goldstone", "G", 0), ("Madrid", "M", 5),
                 ("Canberra", "C", 10))
NETWORK_X0, NETWORK_X1 = 40, 62
NETWORK_CONTACT_FRAMES = INSTRUMENT_FRAMES  # at least eight seconds/contact


def _site_name(name: str) -> str:
    folded = (name or "").strip().lower()
    if folded in SITE_NAMES:
        return SITE_NAMES[folded]
    if "gold" in folded:
        return "Goldstone"
    if "madrid" in folded or "roble" in folded:
        return "Madrid"
    if "canberra" in folded or "tidbin" in folded:
        return "Canberra"
    return name


def _network_links(links: list[Link], site: str) -> list[Link]:
    return sorted((link for link in links if _site_name(link.complex_name) == site),
                  key=lambda link: (link.dish, link.craft))


def network_signature(links: list[Link], freshness: str,
                      names: dict[str, str] | None = None,
                      page: int | None = None) -> tuple:
    """Only topology that the three-site board actually turns into pixels."""
    contacts = []
    for site, _, _ in NETWORK_SITES:
        rows = []
        site_links = _network_links(links, site)
        visible = (site_links if page is None or not site_links else
                   [site_links[page % len(site_links)]])
        for link in visible:
            bands = tuple(band_key(stream.band) if band_key(stream.band) in BAND_PULSE
                          else "" for stream in link_streams(link)[:3])
            up_count = min(3, len(link_upstreams(link)))
            rows.append((link.dish, contact_label(link, names or {}), up_count,
                         bands, _compact_count(len(link_streams(link))),
                         link.arrayed,
                         link.mspa, link.ddor))
        contacts.append((site, tuple(rows)))
    return ("network" if page is None else "network-page",
            tuple(contacts), freshness)


def _network_mark(px, y: int, phase: float, colour: tuple[int, int, int],
                  outward: bool, offset: float = 0.0) -> None:
    width = NETWORK_X1 - NETWORK_X0
    fraction = (phase + offset + 0.2) % 1.0
    step = max(0, min(width, int(round(fraction * (width + 2))) - 1))
    x = NETWORK_X0 + step if outward else NETWORK_X1 - step
    px[x, y] = colour


def network_page_durations(
        links: list[Link], names: dict[str, str] | None = None,
        ) -> tuple[dict[str, list[Link]], list[int]]:
    """Contact grouping and whole-RF-cycle dwell for every global page."""
    grouped = {site: _network_links(links, site) for site, _, _ in NETWORK_SITES}
    pages = max(1, *(len(grouped[site]) for site, _, _ in NETWORK_SITES))
    durations: list[int] = []
    for page in range(pages):
        duration = NETWORK_CONTACT_FRAMES
        for site, _, _ in NETWORK_SITES:
            contacts = grouped[site]
            if contacts:
                contact = contacts[page % len(contacts)]
                label = contact_label(contact, names or {})
                duration = max(duration, scroll_frame_count(
                    label, 21, NETWORK_CONTACT_FRAMES),
                    scroll_frame_count(
                        _dish_suffix(contact.dish), 10,
                        NETWORK_CONTACT_FRAMES))
        durations.append(duration)
    return grouped, durations


def network_page_count(links: list[Link]) -> int:
    return max(1, *(len(_network_links(links, site))
                    for site, _, _ in NETWORK_SITES))


def network_page_duration_s(
        links: list[Link], page: int,
        names: dict[str, str] | None = None) -> float:
    _, durations = network_page_durations(links, names)
    return durations[page % len(durations)] / INSTRUMENT_FPS


def render_network_frames(
        links: list[Link], freshness: str = "fresh",
        names: dict[str, str] | None = None,
        page: int | None = None,
        ) -> tuple[list[Image.Image], int, int]:
    """All three DSN complexes, each paging through its actual live contacts.

    A runtime page is one bounded native asset; the host advances only at its
    loop boundary and resident page assets are reused. With ``page=None`` the
    full deterministic sequence remains available to previews and dry-run.
    Every friendly name completes a full marquee before the next contact.
    """
    grouped, page_durations = network_page_durations(links, names)
    page_indices = (range(len(page_durations)) if page is None
                    else (page % len(page_durations),))
    schedule = [(page_index, local, page_durations[page_index])
                for page_index in page_indices
                for local in range(page_durations[page_index])]
    frozen = freshness != "fresh"
    frames: list[Image.Image] = []
    for index, (page, local, duration) in enumerate(schedule):
        phase = (0.0 if frozen else
                 (local % INSTRUMENT_FRAMES) / INSTRUMENT_FRAMES)
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        for site, initial, y0 in NETWORK_SITES:
            contacts = grouped[site]
            _text(px, 0, y0, initial, ANTENNA)
            if not contacts:
                # NO LINK is 33px in the proportional font: x=7..39.  The
                # old craft-label box ended at 37 and amputated the K's outer
                # stroke on every empty complex. There is no RF lane in this
                # branch, so use the full space up to its x=40 boundary.
                _text(px, 7, y0, "NO LINK", (85, 105, 130),
                      clip=(7, NETWORK_X0 - 1))
                continue
            link = contacts[page % len(contacts)]
            dish = _dish_suffix(link.dish)
            dish_box = (6, 15)
            dish_off = independent_scroll_offset(
                dish, dish_box[1] - dish_box[0] + 1, local, duration)
            _text(px, dish_box[0] - dish_off, y0, dish, DISH_NO,
                  clip=dish_box)
            if dish_off:
                _text(px, dish_box[0] - dish_off + text_width(dish)
                      + SCROLL_GAP_PX, y0, dish, DISH_NO, clip=dish_box)
            craft = contact_label(link, names or {})
            craft_box = (17, 37)
            craft_off = independent_scroll_offset(
                craft, craft_box[1] - craft_box[0] + 1, local, duration)
            _text(px, craft_box[0] - craft_off, y0, craft, NAME, clip=craft_box)
            if craft_off:
                _text(px, craft_box[0] - craft_off + text_width(craft)
                      + SCROLL_GAP_PX, y0, craft, NAME, clip=craft_box)

            # Two rows make direction readable in a still glance as well as
            # through motion. A silent direction has no lit tether.
            upstreams = link_upstreams(link)
            if upstreams:
                for x in range(NETWORK_X0, NETWORK_X1 + 1):
                    px[x, y0 + 1] = UP_TETHER
                for up_index, _ in enumerate(upstreams[:3]):
                    _network_mark(px, y0 + 1, phase, UPLINK, True,
                                  up_index / max(1, min(3, len(upstreams))))
            streams = link_streams(link)
            if streams:
                for x in range(NETWORK_X0, NETWORK_X1 + 1):
                    px[x, y0 + 3] = INSTRUMENT_TETHER
                # Several real streams remain several coloured carriers even
                # though this overview has only one receive row per complex.
                for stream_index, stream in enumerate(streams[:3]):
                    colour = BAND_PULSE.get(band_key(stream.band), UNKNOWN_PULSE)
                    _network_mark(px, y0 + 3, phase, colour, False,
                                  stream_index / max(1, min(3, len(streams))))
            count = _compact_count(len(streams))
            _text(px, 66, y0, count, RATE if streams else (85, 105, 130),
                  clip=(66, 69))

            # Dish-scoped modes change the node geometry instead of hiding in
            # prose: array converges, MSPA forks, DDOR adds a reference point.
            if link.arrayed:
                px[NETWORK_X0 - 2, y0] = ANTENNA
                px[NETWORK_X0 - 2, y0 + 2] = ANTENNA
            if link.mspa:
                px[NETWORK_X1 + 1, y0 + 2] = NAME
                px[NETWORK_X1 + 1, y0 + 4] = NAME
            if link.ddor:
                px[NETWORK_X1 + 2, y0 + 2] = DDOR_MARK

        if freshness == "delayed" and (index // 5) % 2 == 0:
            for y in (0, H // 2, H - 1):
                px[FRESH_X, y] = DELAYED
        elif freshness in {"stale", "offline"}:
            for y in (0, H // 2, H - 1):
                px[FRESH_X, y] = STALE
        frames.append(img)
    return frames, INSTRUMENT_FPS, 1


def render_network_page_frames(
        links: list[Link], page: int, freshness: str = "fresh",
        names: dict[str, str] | None = None,
        ) -> tuple[list[Image.Image], int, int]:
    """The bounded runtime form of the three-site Network board."""
    return render_network_frames(links, freshness, names, page=page)


# --- Dish/link network ----------------------------------------------------

# Network answers ground topology first: site link total, physical dish, and
# an attached count when one dish carries several tracked-target associations.
# Pointing geometry belongs to the deliberate selected-dish Focus below.
DISH_NETWORK_ROSTER_X0, DISH_NETWORK_ROSTER_X1 = 11, 69
DISH_NETWORK_TOKEN_GAP = 1
DISH_NETWORK_SELECTED = (238, 242, 250)
DISH_NETWORK_COUNT = (40, 255, 180)
DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R = 7, 10, 5
DISH_FOCUS_CRAFT_BOX = (16, 37)
DISH_FOCUS_TX_X = 39
DISH_FOCUS_RX_X = 54


@dataclass(frozen=True)
class DishRosterRow:
    """Named render plan so timing cannot be confused with geometry."""
    site_total: str
    y: int
    groups: tuple[tuple[str, tuple[Link, ...]], ...]
    width: int
    row_frames: int
    roster_x0: int


@dataclass(frozen=True)
class DishFocusPage:
    """One bounded semantic page; ``omitted`` is an explicit summary."""
    contacts: tuple[Link, ...]
    duration: int
    omitted: int = 0


def _dish_suffix(dish: str) -> str:
    """Complete source dish identity after the conventional DSS prefix."""
    raw = (dish or "").upper().removeprefix("DSS")
    return "".join(ch if ch in FONT else "?" for ch in (raw or "?"))


def group_links_by_dish(
        links: list[Link], site: str | None = None,
        ) -> list[tuple[str, tuple[Link, ...]]]:
    """Canonical physical-dish groups, optionally within one complex."""
    selected = [link for link in links
                if site is None or _site_name(link.complex_name) == site]
    by_dish: dict[str, list[Link]] = {}
    for link in sorted(selected, key=lambda item: (item.dish, item.craft,
                                                    item.key)):
        by_dish.setdefault(link.dish, []).append(link)
    return [(dish, tuple(by_dish[dish]))
            for dish in sorted(by_dish, key=lambda item: (_dish_suffix(item), item))]


def _dish_group_token_width(dish: str, links: tuple[Link, ...]) -> int:
    width = text_width(_dish_suffix(dish))
    if len(links) > 1:
        width += GLYPH_GAP + text_width(f"({len(links)})")
    return width


def _dish_roster_width(groups: list[tuple[str, tuple[Link, ...]]]) -> int:
    if not groups:
        return 0
    return (sum(_dish_group_token_width(dish, links)
                for dish, links in groups)
            + DISH_NETWORK_TOKEN_GAP * (len(groups) - 1))


def _pixel_scroll_frame_count(
        width: int, box_px: int, minimum: int = INSTRUMENT_FRAMES,
        ) -> int:
    """Whole RF clocks for an already-measured coloured token strip."""
    if width <= box_px:
        return minimum
    needed = math.ceil(
        (width + SCROLL_GAP_PX) / SCROLL_SPEED_PX_S * INSTRUMENT_FPS)
    cycles = max(1, math.ceil(needed / INSTRUMENT_FRAMES))
    return min(MAX_ANIMATION_FRAMES,
               max(minimum, cycles * INSTRUMENT_FRAMES))


def _draw_dish_roster_strip(
        px, x: int, y: int,
        groups: (list[tuple[str, tuple[Link, ...]]]
                 | tuple[tuple[str, tuple[Link, ...]], ...]),
        selected_key: str | None, clip: tuple[int, int],
        ) -> int:
    """One complete coloured site roster; returns its measured width."""
    cursor = x
    for group_index, (dish, group_links) in enumerate(groups):
        suffix = _dish_suffix(dish)
        selected = bool(selected_key and
                        any(link.key == selected_key for link in group_links))
        _text(px, cursor, y, suffix,
              DISH_NETWORK_SELECTED if selected else DISH_NO, clip=clip)
        cursor += text_width(suffix)
        if len(group_links) > 1:
            cursor += GLYPH_GAP
            count = f"({len(group_links)})"
            _text(px, cursor, y, count, DISH_NETWORK_COUNT, clip=clip)
            cursor += text_width(count)
        if group_index + 1 < len(groups):
            cursor += DISH_NETWORK_TOKEN_GAP
    return cursor - x


def dish_network_frame_count(links: list[Link]) -> int:
    """One bounded asset clock; each row keeps its own seam-safe phase."""
    row_clocks = []
    for site, initial, _ in NETWORK_SITES:
        site_total = f"{initial}{len(_network_links(links, site))}"
        roster_x0 = max(DISH_NETWORK_ROSTER_X0,
                        text_width(site_total) + GLYPH_GAP)
        box_px = DISH_NETWORK_ROSTER_X1 - roster_x0 + 1
        groups = group_links_by_dish(links, site)
        row_clocks.append(_pixel_scroll_frame_count(
            _dish_roster_width(groups), box_px))
    return max(row_clocks, default=INSTRUMENT_FRAMES)


def render_dish_network_frames(
        links: list[Link], freshness: str = "fresh",
        selected_key: str | None = None,
        ) -> tuple[list[Image.Image], int, int]:
    """Literal site → physical dish → live-link-count Network board.

    Ordinary rows are static. A future dense source state that cannot fit its
    remaining roster box scrolls the complete coloured token strip through a
    whole native cycle; no dish or attached multiplicity is clipped or dropped.
    """
    models = []
    for site, initial, y in NETWORK_SITES:
        site_links = _network_links(links, site)
        site_total = f"{initial}{len(site_links)}"
        roster_x0 = max(DISH_NETWORK_ROSTER_X0,
                        text_width(site_total) + GLYPH_GAP)
        box_px = DISH_NETWORK_ROSTER_X1 - roster_x0 + 1
        groups = group_links_by_dish(links, site)
        width = _dish_roster_width(groups)
        row_frames = _pixel_scroll_frame_count(width, box_px)
        models.append(DishRosterRow(
            site_total, y, tuple(groups), width, row_frames, roster_x0))
    # LCM made three ordinary independent rows multiply into multi-minute,
    # multi-megabyte assets.  One bounded clock is enough: each strip below
    # completes an integer number of its own cycles at its own readable rate.
    frame_count = max(
        (model.row_frames for model in models),
        default=INSTRUMENT_FRAMES)

    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        for model in models:
            box_px = DISH_NETWORK_ROSTER_X1 - model.roster_x0 + 1
            _text(px, 0, model.y, model.site_total, ANTENNA,
                  clip=(0, model.roster_x0 - GLYPH_GAP - 1))
            if not model.groups:
                _text(px, 13, model.y, "NO LINKS", (85, 105, 130),
                      clip=(13, 50))
                continue
            offset = independent_pixel_scroll_offset(
                model.width, box_px, index, frame_count)
            start = model.roster_x0 - offset
            _draw_dish_roster_strip(
                px, start, model.y, model.groups, selected_key,
                (model.roster_x0, DISH_NETWORK_ROSTER_X1))
            if offset:
                _draw_dish_roster_strip(
                    px, start + model.width + SCROLL_GAP_PX, model.y,
                    model.groups, selected_key,
                    (model.roster_x0, DISH_NETWORK_ROSTER_X1))
        _draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames, INSTRUMENT_FPS, 1


def _visual_fixture_links() -> list[Link]:
    """A fixed, representative Network snapshot for deterministic rendering.

    Values mirror the shapes the contract tests exercise: three Goldstone
    contacts, one Canberra uplink, one Madrid contact. No feed, no clock.
    """
    def link(*, site: str, dish: str, craft: str, azimuth: float,
             elevation: float, up: bool = False) -> Link:
        down_streams = (DownStream("X", 2_000_000.0, -130.0),)
        up_streams = (UpStream("X", 18.0, "command"),) if up else ()
        return Link(
            complex_name=site, dish=dish, craft=craft, elevation=elevation,
            band="X", down_bps=2_000_000.0, up_active=up, range_km=100_000.0,
            down_dbm=-130.0, up_kw=18.0 if up else 0.0,
            streams=len(down_streams), azimuth=azimuth, pointing_valid=True,
            down_streams=down_streams, up_band="X" if up else "",
            up_streams=up_streams,
        )

    return [
        link(site="Goldstone", dish="DSS23", craft="IMAP",
             azimuth=210.0, elevation=45.0),
        link(site="Goldstone", dish="DSS24", craft="CHDR",
             azimuth=120.0, elevation=30.0),
        link(site="Goldstone", dish="DSS26", craft="SOHO",
             azimuth=70.0, elevation=50.0),
        link(site="Canberra", dish="DSS34", craft="M01O",
             azimuth=48.0, elevation=22.0, up=True),
        link(site="Madrid", dish="DSS54", craft="JWST",
             azimuth=150.0, elevation=60.0),
    ]


def render_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """The pure zero-argument seam declared by `[dsn.viz]` in apps.toml.

    Renders the default dish/link Network board over the fixed snapshot
    above, through the same code path the live app draws with.
    """
    frames, fps, _hold = render_dish_network_frames(_visual_fixture_links())
    return {"front": (frames, fps)}


def _visual_fixture_contact() -> Link:
    """One richer fixed contact for the single-link Instrument/Distance views."""
    return Link(
        complex_name="Madrid", dish="DSS54", craft="JWST", elevation=60.0,
        band="X", down_bps=2_000_000.0, up_active=True,
        range_km=1_500_000.0, naif=-170, down_dbm=-130.0, up_kw=18.0,
        streams=2, azimuth=150.0, pointing_valid=True,
        down_streams=(DownStream("X", 2_000_000.0, -130.0),
                      DownStream("K", 8_000_000.0, -128.0)),
        up_band="X", up_streams=(UpStream("X", 18.0, "command"),),
    )


def render_instrument_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """`[dsn.viz.scenarios.instrument]`: one antenna and contact, in detail."""
    frames, fps, _hold = render_instrument_frames(_visual_fixture_contact())
    return {"front": (frames, fps)}


def render_distance_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """`[dsn.viz.scenarios.distance]`: the light-time journey, at a fixed instant."""
    fixed_now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    frames, fps, _hold = render_frames(_visual_fixture_contact(), fixed_now)
    return {"front": (frames, fps)}


def dish_network_signature(
        links: list[Link], freshness: str = "fresh",
        selected_key: str | None = None,
        ) -> tuple:
    """Pixel-exact immutable-cache key for the dish roster."""
    frames, fps, hold = render_dish_network_frames(
        links, freshness, selected_key)
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return ("dish-network", fps, hold, len(frames), digest.hexdigest())


def dish_network_loop_s(links: list[Link]) -> float:
    return dish_network_frame_count(links) / INSTRUMENT_FPS


def _dish_focus_group(
        links: list[Link], selected_key: str | None,
        ) -> tuple[Link | None, list[Link]]:
    selected = next((link for link in links if link.key == selected_key), None)
    if selected is None:
        return None, []
    same_dish = [link for link in links
                 if (_site_name(link.complex_name)
                     == _site_name(selected.complex_name)
                     and link.dish == selected.dish)]
    ordered = [selected]
    ordered.extend(sorted((link for link in same_dish if link.key != selected.key),
                          key=lambda item: (item.craft, item.key)))
    return selected, ordered


def _dish_focus_pages(
        group: list[Link], names: dict[str, str] | None = None,
        header: str = "", header_box_px: int = 70,
        ) -> list[DishFocusPage]:
    pages: list[DishFocusPage] = []
    # Keep the exact wheel/START target visible on every page.  The second
    # row walks the other links sharing this physical dish, so semantic zoom
    # never turns into an unexplained page where the action target vanished.
    page_groups = ([[group[0]]] if len(group) <= 1 else
                   [[group[0], other] for other in group[1:]])
    for page_links in page_groups or [[]]:
        duration = scroll_frame_count(
            header, header_box_px, INSTRUMENT_FRAMES)
        for link in page_links:
            duration = max(duration, scroll_frame_count(
                craft_label(link.craft, names or {}),
                DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
                INSTRUMENT_FRAMES))
        pages.append(DishFocusPage(tuple(page_links), duration))
    if not pages:
        return [DishFocusPage((), INSTRUMENT_FRAMES)]
    if sum(page.duration for page in pages) <= MAX_ANIMATION_FRAMES:
        return pages

    # Friendly names and co-dish multiplicity are independent source axes. A
    # valid but hostile combination must not multiply into thousands of eager
    # PIL frames. Keep as many complete identity pages as fit while reserving
    # one exact ``+N TARGETS`` page for everything deliberately omitted.
    selected = (group[0],) if group else ()

    def overflow_page(count: int) -> DishFocusPage:
        duration = scroll_frame_count(
            header, header_box_px, INSTRUMENT_FRAMES)
        if selected:
            duration = max(duration, scroll_frame_count(
                craft_label(selected[0].craft, names or {}),
                DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
                INSTRUMENT_FRAMES))
        duration = max(duration, scroll_frame_count(
            f"+{count} TARGETS",
            DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
            INSTRUMENT_FRAMES))
        return DishFocusPage(selected, duration, count)

    bounded: list[DishFocusPage] = []
    used = 0
    for index, focus_page in enumerate(pages):
        omitted = len(pages) - index
        summary = overflow_page(omitted)
        if (used + focus_page.duration + summary.duration
                > MAX_ANIMATION_FRAMES):
            bounded.append(summary)
            break
        bounded.append(focus_page)
        used += focus_page.duration
    return bounded


def _dish_focus_aim(
        group: list[Link],
        ) -> tuple[int, int] | None:
    """One source aim per physical dish, or no invented geometry."""
    if not group or any(not link.pointing_valid for link in group):
        return None
    source_aims = {(round(link.azimuth % 360.0, 3),
                   round(link.elevation, 3)) for link in group}
    if len(source_aims) != 1:
        return None
    selected = group[0]
    return _project_angles(selected.azimuth, selected.elevation,
                           DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R)


def _dish_focus_rx_colour(link: Link) -> tuple[int, int, int] | None:
    streams = link_streams(link)
    if not streams:
        return None
    keys = [band_key(stream.band) for stream in streams]
    if keys and len(set(keys)) == 1 and keys[0] in BAND_PULSE:
        return BAND_PULSE[keys[0]]
    return UNKNOWN_PULSE


def _draw_focus_contact(
        px, link: Link, y: int, selected_key: str,
        local: int, duration: int, names: dict[str, str] | None = None,
        ) -> None:
    label = craft_label(link.craft, names or {})
    box_px = DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1
    offset = independent_scroll_offset(label, box_px, local, duration)
    colour = DISH_NETWORK_SELECTED if link.key == selected_key else NAME
    _text(px, DISH_FOCUS_CRAFT_BOX[0] - offset, y, label, colour,
          clip=DISH_FOCUS_CRAFT_BOX)
    if offset:
        _text(px, DISH_FOCUS_CRAFT_BOX[0] - offset + text_width(label)
              + SCROLL_GAP_PX, y, label, colour,
              clip=DISH_FOCUS_CRAFT_BOX)
    if link.key == selected_key:
        # White vs ordinary name ink is below the panel's measured contrast
        # floor. A five-pixel mint owner bar makes the exact START target
        # unmistakable without taking a character from the complete name.
        for yy in range(y, y + 5):
            px[14, yy] = DISH_NETWORK_COUNT
    if link_upstreams(link):
        _text(px, DISH_FOCUS_TX_X, y, "TX", UPLINK,
              clip=(DISH_FOCUS_TX_X, DISH_FOCUS_TX_X + 8))
    rx_colour = _dish_focus_rx_colour(link)
    if rx_colour is not None:
        _text(px, DISH_FOCUS_RX_X, y, "RX", rx_colour,
              clip=(DISH_FOCUS_RX_X, DISH_FOCUS_RX_X + 8))


def _draw_focus_overflow(
        px, count: int, y: int, local: int, duration: int,
        ) -> None:
    """Name the omitted associations instead of silently dropping them."""
    label = f"+{count} TARGETS"
    box_px = DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1
    offset = independent_scroll_offset(label, box_px, local, duration)
    _text(px, DISH_FOCUS_CRAFT_BOX[0] - offset, y, label,
          DISH_NETWORK_COUNT, clip=DISH_FOCUS_CRAFT_BOX)
    if offset:
        _text(px, DISH_FOCUS_CRAFT_BOX[0] - offset + text_width(label)
              + SCROLL_GAP_PX, y, label, DISH_NETWORK_COUNT,
              clip=DISH_FOCUS_CRAFT_BOX)


def _draw_dish_focus_header(
        px, header: str, colour: tuple[int, int, int], local: int,
        duration: int,
        ) -> None:
    box = (0, 69)
    box_px = box[1] - box[0] + 1
    offset = independent_scroll_offset(header, box_px, local, duration)
    _text(px, box[0] - offset, 0, header, colour, clip=box)
    if offset:
        _text(px, box[0] - offset + text_width(header) + SCROLL_GAP_PX,
              0, header, colour, clip=box)


def _dish_focus_header(
        selected: Link, group: list[Link],
        ) -> tuple[str, tuple[int, int, int], tuple[int, int] | None]:
    """Complete plain-language aim header shared by render and timing."""
    initial = next((initial for site, initial, _ in NETWORK_SITES
                    if site == _site_name(selected.complex_name)), "?")
    identity = f"{initial}{_dish_suffix(selected.dish)}"
    aim = _dish_focus_aim(group)
    if aim is None:
        return f"{identity} NO AIM", DISH_NETWORK_COUNT, None
    azimuth = int(round(group[0].azimuth)) % 360
    elevation = max(0, min(90, int(round(group[0].elevation))))
    return (f"{identity} AZ{azimuth:03d} EL{elevation:02d}",
            DISH_NETWORK_SELECTED, aim)


def render_dish_focus_frames(
        links: list[Link], freshness: str = "fresh",
        names: dict[str, str] | None = None,
        selected_key: str | None = None,
        ) -> tuple[list[Image.Image], int, int]:
    """One selected physical dish aim and every link that shares it."""
    selected, group = _dish_focus_group(list(links), selected_key)
    if selected is None:
        return render_dish_network_frames(list(links), freshness, selected_key)
    header, header_colour, aim = _dish_focus_header(selected, group)
    pages = _dish_focus_pages(group, names, header, 70)
    schedule = [(page, local)
                for page in pages
                for local in range(page.duration)]

    frames = []
    for index, (page, local) in enumerate(schedule):
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        _draw_dish_focus_header(
            px, header, header_colour, local, page.duration)
        if aim is None:
            _text(px, 5, 8, "?", DISH_NETWORK_COUNT, clip=(5, 8))
        else:
            _draw_scope(px, DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R)
            px[aim] = DISH_NETWORK_SELECTED
        for row, link in zip((6, 11), page.contacts):
            _draw_focus_contact(
                px, link, row, selected.key, local, page.duration, names)
        if page.omitted:
            _draw_focus_overflow(
                px, page.omitted, 11, local, page.duration)
        _draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames, INSTRUMENT_FPS, 1


def dish_focus_signature(
        links: list[Link], freshness: str = "fresh",
        names: dict[str, str] | None = None,
        selected_key: str | None = None,
        ) -> tuple:
    frames, fps, hold = render_dish_focus_frames(
        links, freshness, names, selected_key)
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return ("dish-focus", fps, hold, len(frames), digest.hexdigest())


def dish_focus_loop_s(
        links: list[Link], names: dict[str, str] | None,
        selected_key: str | None,
        ) -> float:
    selected, group = _dish_focus_group(list(links), selected_key)
    if selected is None:
        return dish_network_loop_s(list(links))
    header = _dish_focus_header(selected, group)[0]
    return (sum(page.duration for page in
                _dish_focus_pages(group, names, header, 70))
            / INSTRUMENT_FPS)


# --- Three Skies network --------------------------------------------------

# Each complex owns a literal local alt-az sky.  These are three independent
# coordinate frames, not three pieces of one inertial spacecraft map.
THREE_SKIES_SCOPE_CENTERS = ((15, 7), (39, 7), (63, 7))
THREE_SKIES_SCOPE_R = 6
THREE_SKIES_MAIN_CENTER = (35, 8)
THREE_SKIES_MAIN_R = 7
THREE_SKIES_CONTEXT_CENTERS = ((66, 4), (66, 12))
THREE_SKIES_CONTEXT_R = 3
THREE_SKIES_SELECTED = (238, 242, 250)
# Mint is a non-RF ledger ink. It clears the measured 30%/77-channel panel
# step from every semantic node colour, including the selected white head.
THREE_SKIES_LEDGER = (40, 255, 180)
THREE_SKIES_NORTH = (145, 145, 145)
THREE_SKIES_TRAIL = (30, 160, 30)


def network_focus_active(state: State, now: float | None = None) -> bool:
    """Whether a deliberate wheel-rest Focus Lens still owns Network.

    ``inf`` is the pre-accept state: a refused display draw must not consume
    the user's selection.  The first accepted Focus scene replaces it with a
    monotonic deadline equal to one complete native name cycle.
    """
    if state.network_focus_until <= 0:
        return False
    current = now
    if current is None:
        try:
            current = asyncio.get_running_loop().time()
        except RuntimeError:  # pure host-side callers need no running loop
            current = time.monotonic()
    return state.network_focus_until > current


def clear_network_focus(state: State) -> None:
    """Return Network to its ambient overview and discard frozen Focus."""
    state.network_focus_key = None
    state.network_focus_until = 0.0
    state.network_focus_links = ()
    state.network_focus_names.clear()
    state.network_focus_trails.clear()


def commit_picker_selection(state: State, now: float | None = None) -> None:
    """Commit a rested wheel selection without making asset upload interactive."""
    state.picking = False
    source_fresh = (not state.feed_seeded or feed_freshness(state) == "fresh")
    if (DSN_NETWORK_STYLE in NETWORK_FOCUS_STYLES and state.view == "network"
            and source_fresh):
        selected = state.current()
        state.network_focus_key = selected.key if selected is not None else None
        if state.network_focus_key:
            state.network_focus_until = float("inf")
            state.network_focus_links = tuple(replace(link)
                                              for link in state.links)
            state.network_focus_names = dict(state.names)
            state.network_focus_trails = {
                key: list(points) for key, points in state.aim_trails.items()
            }
        else:
            clear_network_focus(state)
    else:
        clear_network_focus(state)


def _scope_points(cx: int, cy: int, radius: int) -> set[tuple[int, int]]:
    """Sparse physical-panel-safe ring points for one local sky."""
    step = 45 if radius < 5 else 15
    points = set()
    for degrees in range(0, 360, step):
        angle = math.radians(degrees)
        points.add((int(round(cx + math.sin(angle) * radius)),
                    int(round(cy - math.cos(angle) * radius))))
    return points


def _draw_scope(px, cx: int, cy: int, radius: int) -> None:
    for point in _scope_points(cx, cy, radius):
        px[point] = SCOPE_RING
    px[cx, cy] = SCOPE_RING
    # One neutral cardinal fiducial makes north explicit without a fake moving
    # sweep or another RF-coloured point.
    px[cx, cy - radius] = THREE_SKIES_NORTH


def _project_angles(azimuth: float, elevation: float, cx: int, cy: int,
                    radius: int) -> tuple[int, int]:
    """Literal local alt-az projection at an arbitrary scope radius."""
    az = math.radians(azimuth % 360.0)
    distance = ((90.0 - min(90.0, max(0.0, elevation))) / 90.0 * radius)
    return (int(round(cx + math.sin(az) * distance)),
            int(round(cy - math.cos(az) * distance)))


def _project_link(link: Link, cx: int, cy: int,
                  radius: int) -> tuple[int, int] | None:
    """Literal local az/el projection at an arbitrary scope radius."""
    if not link.pointing_valid:
        return None
    return _project_angles(link.azimuth, link.elevation, cx, cy, radius)


def _group_scope_links(links: list[Link], site: str, cx: int, cy: int,
                       radius: int) -> tuple[list[dict], int]:
    """Group dish-scoped aims, then honest pixel collisions.

    MSPA can publish several spacecraft contacts for one dish.  They share one
    physical aim and therefore one spatial node.  Independently aimed dishes
    can quantize to the same LED too; those contacts remain one cell with a
    nonspatial tally rather than being jittered into invented positions.
    """
    site_links = _network_links(links, site)
    by_dish: dict[str, list[Link]] = {}
    for link in site_links:
        by_dish.setdefault(link.dish, []).append(link)

    cells: dict[tuple[int, int], dict] = {}
    missing = 0
    for dish in sorted(by_dish):
        dish_links = sorted(by_dish[dish], key=lambda item: item.craft)
        # A dish has one source aim.  Missing or internally inconsistent
        # coordinates cannot be resolved by first quantizing distinct angles
        # onto the same LED.  Validate source geometry before projection.
        if any(not link.pointing_valid for link in dish_links):
            missing += len(dish_links)
            continue
        source_aims = {(round(link.azimuth % 360.0, 3),
                        round(link.elevation, 3)) for link in dish_links}
        if len(source_aims) != 1:
            missing += len(dish_links)
            continue
        point = _project_link(dish_links[0], cx, cy, radius)
        assert point is not None
        cell = cells.setdefault(point, {"point": point, "links": [], "dishes": []})
        cell["links"].extend(dish_links)
        cell["dishes"].append(dish)

    groups = []
    for point in sorted(cells):
        cell = cells[point]
        cell["links"] = sorted(cell["links"], key=lambda item: (item.dish, item.craft))
        cell["dishes"] = tuple(sorted(cell["dishes"]))
        groups.append(cell)
    return groups, missing


def _scope_group_colour(group: dict, selected_key: str | None) -> tuple[int, int, int]:
    links = group["links"]
    if selected_key and any(link.key == selected_key for link in links):
        return THREE_SKIES_SELECTED
    downstreams = [stream for link in links for stream in link_streams(link)]
    if downstreams:
        keys = [band_key(stream.band) for stream in downstreams]
        if keys and all(key in BAND_PULSE for key in keys) and len(set(keys)) == 1:
            return BAND_PULSE[keys[0]]
        return UNKNOWN_PULSE
    if any(link_upstreams(link) for link in links):
        return UPLINK
    return UNKNOWN_PULSE


def _distinct_trail(points: list[tuple[int, int]] | None) -> list[tuple[int, int]]:
    """At most five distinct observed cells, including the current head."""
    newest_first: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for point in reversed(points or []):
        if point not in seen:
            newest_first.append(point)
            seen.add(point)
    return list(reversed(newest_first[:5]))


def _map_trail_point(point: tuple[int, int], cx: int, cy: int,
                     radius: int) -> tuple[int, int]:
    """Map the retained R6 pixel observation into this scope's resolution."""
    return (int(round(cx + (point[0] - SCOPE_CX) * radius / SCOPE_R)),
            int(round(cy + (point[1] - SCOPE_CY) * radius / SCOPE_R)))


def _site_count_label(initial: str, count: int) -> str:
    # Ten or more cannot fit as an exact numeral in this rollback cell.  G>
    # is visibly an overflow state; G9 remains exactly nine, never a silent
    # numeric cap pretending that twelve associations are nine.
    return f"{initial}{count}" if count <= 9 else f"{initial}>"


def _compact_count(count: int) -> str:
    """One-glyph exact-or-overflow tally for the rollback ledgers."""
    return str(count) if count <= 9 else "+"


def _missing_count(count: int) -> str:
    return f"?{_compact_count(count)}"


def _selected_link(links: list[Link], selected_key: str | None) -> Link | None:
    return next((link for link in links if link.key == selected_key), None)


def _scope_collision(groups: list[dict]) -> int:
    return max((len(group["links"]) for group in groups), default=1)


def _draw_collision_ledger(px, base: int, y: int, collision: int) -> None:
    _text(px, base, y, _compact_count(collision), THREE_SKIES_LEDGER,
          clip=(base, base + 4))
    # A bracket beside the count says "one spatial cell contains this many
    # contacts". It is a ledger mark, never a second plotted position.
    for x, yy in ((base + 6, y), (base + 6, y + 2), (base + 6, y + 4),
                  (base + 7, y), (base + 7, y + 4)):
        px[x, yy] = THREE_SKIES_LEDGER


def _draw_focus_collision_ledger(px, collision: int) -> None:
    """A complete tally between the identity rail and the R7 main sky."""
    # The left text box ends at x20; exhaustive rounded R7 projection starts
    # at x28. A full four-column digit, one OFF moat and a compact bracket fit
    # in that honest nonspatial gap. Do not move this onto a reachable cell.
    _text(px, 21, 11, _compact_count(collision), THREE_SKIES_LEDGER,
          clip=(21, 24))
    for x, y in ((26, 11), (26, 13), (26, 15),
                 (27, 11), (27, 15)):
        px[x, y] = THREE_SKIES_LEDGER


def _draw_site_ledger(px, base: int, site_links: list[Link], groups: list[dict],
                      missing: int, selected_key: str | None,
                      index: int, frame_count: int) -> None:
    """Nonspatial facts that must not masquerade as another sky position."""
    collision = _scope_collision(groups)
    if missing and collision > 1:
        # Two complete five-pixel rows fit before the ring. Never make one
        # truthful warning erase the other merely because both happened.
        _text(px, base, 5, _missing_count(missing), THREE_SKIES_LEDGER,
              clip=(base, base + 8))
        _draw_collision_ledger(px, base, 10, collision)
        return
    if missing:
        _text(px, base, 10, _missing_count(missing), THREE_SKIES_LEDGER,
              clip=(base, base + 8))
        return
    if collision > 1:
        _draw_collision_ledger(px, base, 10, collision)
        return
    selected = _selected_link(site_links, selected_key)
    if selected is not None:
        dish = _dish_suffix(selected.dish)
        box = (base, base + 8)
        offset = independent_scroll_offset(
            dish, box[1] - box[0] + 1, index, frame_count)
        _text(px, box[0] - offset, 10, dish, DISH_NO, clip=box)
        if offset:
            _text(px, box[0] - offset + text_width(dish) + SCROLL_GAP_PX,
                  10, dish, DISH_NO, clip=box)


def _draw_freshness_frame(px, freshness: str, index: int) -> None:
    # Fresh is a separate native element lease; baking it would let a dead
    # host continue looking live.  Delayed/stale are last-known-source states.
    if freshness == "delayed" and (index // 5) % 2 == 0:
        for y in (0, H // 2, H - 1):
            px[FRESH_X, y] = DELAYED
    elif freshness in {"stale", "offline"}:
        for y in (0, H // 2, H - 1):
            px[FRESH_X, y] = STALE


def _render_three_skies_ambient(
        links: list[Link], freshness: str, selected_key: str | None,
        trails: dict[str, list[tuple[int, int]]],
        ) -> list[Image.Image]:
    models = []
    for (site, initial, _), centre in zip(NETWORK_SITES, THREE_SKIES_SCOPE_CENTERS):
        site_links = _network_links(links, site)
        groups, missing = _group_scope_links(
            links, site, centre[0], centre[1], THREE_SKIES_SCOPE_R)
        models.append((site, initial, centre, site_links, groups, missing))

    selected = _selected_link(links, selected_key)
    frame_count = (scroll_frame_count(
        _dish_suffix(selected.dish), 9, INSTRUMENT_FRAMES)
        if selected is not None else INSTRUMENT_FRAMES)
    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        for base, model in zip((0, 24, 48), models):
            _, initial, centre, site_links, groups, missing = model
            _text(px, base, 0, _site_count_label(initial, len(site_links)),
                  ANTENNA, clip=(base, base + 9))
            _draw_scope(px, centre[0], centre[1], THREE_SKIES_SCOPE_R)
            selected = _selected_link(site_links, selected_key)
            if selected is not None:
                trail = _distinct_trail(trails.get(selected.key))
                for point in trail[:-1]:
                    px[_map_trail_point(
                        point, centre[0], centre[1], THREE_SKIES_SCOPE_R)] = THREE_SKIES_TRAIL
            for group in groups:
                px[group["point"]] = _scope_group_colour(group, selected_key)
            _draw_site_ledger(
                px, base, site_links, groups, missing, selected_key,
                index, frame_count)
        _draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames


def _focus_index_label(links: list[Link], selected_key: str) -> str:
    if len(links) > 99:
        return "MANY"
    index = next((i for i, link in enumerate(links, 1)
                  if link.key == selected_key), 1)
    return f"{index}/{len(links)}"


def _draw_focus_marquee(px, text: str, y: int,
                         colour: tuple[int, int, int], index: int,
                         frame_count: int) -> None:
    """Draw one complete 21-pixel Focus token, scrolling only if required."""
    box = (0, 20)
    width = text_width(text)
    off = independent_scroll_offset(text, 21, index, frame_count)
    _text(px, box[0] - off, y, text, colour, clip=box)
    if width > 21:
        _text(px, box[0] - off + width + SCROLL_GAP_PX,
              y, text, colour, clip=box)


def _render_three_skies_focus(
        links: list[Link], freshness: str, names: dict[str, str],
        selected: Link, trails: dict[str, list[tuple[int, int]]],
        ) -> list[Image.Image]:
    label = craft_label(selected.craft, names)
    selected_site = _site_name(selected.complex_name)
    main_groups, main_missing = _group_scope_links(
        links, selected_site, THREE_SKIES_MAIN_CENTER[0],
        THREE_SKIES_MAIN_CENTER[1], THREE_SKIES_MAIN_R)
    context_sites = [item for item in NETWORK_SITES if item[0] != selected_site]
    dish = _dish_suffix(selected.dish)
    site_initial = next((initial for site, initial, _ in NETWORK_SITES
                         if site == selected_site), "?")
    identity = f"{site_initial}{dish}"
    index_label = _focus_index_label(links, selected.key)
    if main_missing:
        index_label += f" {_missing_count(main_missing)}"
    frame_count = max(
        scroll_frame_count(identity, 21, INSTRUMENT_FRAMES),
        scroll_frame_count(label, 21, INSTRUMENT_FRAMES),
        scroll_frame_count(index_label, 21, INSTRUMENT_FRAMES),
    )
    trail = _distinct_trail(trails.get(selected.key))
    main_collision = _scope_collision(main_groups)

    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        _draw_focus_marquee(px, identity, 0, DISH_NO, index, frame_count)
        _draw_focus_marquee(px, label, 6, NAME, index, frame_count)
        _draw_focus_marquee(px, index_label, 11, ANTENNA,
                             index, frame_count)

        _draw_scope(px, *THREE_SKIES_MAIN_CENTER, THREE_SKIES_MAIN_R)
        for point in trail[:-1]:
            px[_map_trail_point(
                point, *THREE_SKIES_MAIN_CENTER, THREE_SKIES_MAIN_R)] = THREE_SKIES_TRAIL
        for group in main_groups:
            px[group["point"]] = _scope_group_colour(group, selected.key)
        if main_collision > 1:
            _draw_focus_collision_ledger(px, main_collision)

        for (site, initial, _), centre, y0 in zip(
                context_sites, THREE_SKIES_CONTEXT_CENTERS, (0, 8)):
            site_links = _network_links(links, site)
            _text(px, 48, y0, _site_count_label(initial, len(site_links)),
                  ANTENNA, clip=(48, 57))
            _draw_scope(px, centre[0], centre[1], THREE_SKIES_CONTEXT_R)
            groups, missing = _group_scope_links(
                links, site, centre[0], centre[1], THREE_SKIES_CONTEXT_R)
            collision = _scope_collision(groups)
            context_status = ""
            if missing and collision > 1:
                # Focus lasts a whole 40-frame clock, so both nonspatial facts
                # receive one complete four-second block instead of one hiding
                # the other in this compact context rail.
                context_status = ("?" if (index // 20) % 2 == 0
                else _compact_count(collision))
            elif missing:
                context_status = "?"
            elif collision > 1:
                context_status = _compact_count(collision)
            if context_status:
                _text(px, 58, y0, context_status, THREE_SKIES_LEDGER,
                      clip=(58, 61))
            for group in groups:
                px[group["point"]] = _scope_group_colour(group, None)

        _draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames


def render_three_skies_frames(
        links: list[Link], freshness: str = "fresh",
        names: dict[str, str] | None = None,
        selected_key: str | None = None,
        trails: dict[str, list[tuple[int, int]]] | None = None,
        focus: bool = False,
        ) -> tuple[list[Image.Image], int, int]:
    """Three literal local skies, or the user-invoked selected Focus Lens."""
    links = list(links)
    selected = _selected_link(links, selected_key)
    if focus and selected is not None:
        frames = _render_three_skies_focus(
            links, freshness, names or {}, selected, trails or {})
    else:
        frames = _render_three_skies_ambient(
            links, freshness, selected_key, trails or {})
    return frames, INSTRUMENT_FPS, 1


def three_skies_signature(
        links: list[Link], freshness: str = "fresh",
        names: dict[str, str] | None = None,
        selected_key: str | None = None,
        trails: dict[str, list[tuple[int, int]]] | None = None,
        focus: bool = False,
        ) -> tuple:
    """A pixel-exact cache signature without raw telemetry jitter."""
    frames, fps, hold = render_three_skies_frames(
        links, freshness, names, selected_key, trails, focus)
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return ("three-skies-focus" if focus and _selected_link(links, selected_key)
            else "three-skies", fps, hold, len(frames), digest.hexdigest())


def three_skies_loop_s(links: list[Link], names: dict[str, str] | None,
                       selected_key: str | None, focus: bool) -> float:
    """Native duration of the exact ambient or Focus asset."""
    selected = _selected_link(links, selected_key)
    if focus and selected is not None:
        selected_site = _site_name(selected.complex_name)
        main_groups, main_missing = _group_scope_links(
            links, selected_site, THREE_SKIES_MAIN_CENTER[0],
            THREE_SKIES_MAIN_CENTER[1], THREE_SKIES_MAIN_R)
        del main_groups
        initial = next((initial for site, initial, _ in NETWORK_SITES
                        if site == selected_site), "?")
        identity = f"{initial}{_dish_suffix(selected.dish)}"
        index_label = _focus_index_label(links, selected.key)
        if main_missing:
            index_label += f" {_missing_count(main_missing)}"
        frames = max(
            scroll_frame_count(identity, 21, INSTRUMENT_FRAMES),
            scroll_frame_count(
                craft_label(selected.craft, names or {}),
                21, INSTRUMENT_FRAMES),
            scroll_frame_count(index_label, 21, INSTRUMENT_FRAMES))
        return frames / INSTRUMENT_FPS
    if selected is not None:
        frames = scroll_frame_count(
            _dish_suffix(selected.dish), 9, INSTRUMENT_FRAMES)
        return frames / INSTRUMENT_FPS
    return INSTRUMENT_LOOP_S


def network_focus_inputs(
        state: State,
        ) -> tuple[list[Link], dict[str, str],
                   dict[str, list[tuple[int, int]]]]:
    """The accepted snapshot a deliberate Focus Lens is allowed to claim."""
    if state.network_focus_links:
        return (list(state.network_focus_links),
                dict(state.network_focus_names),
                {key: list(points)
                 for key, points in state.network_focus_trails.items()})
    # Direct pure-test/state restoration callers may arm Focus without going
    # through the wheel helper. Falling back stays honest and deterministic.
    return (list(state.links), dict(state.names),
            {key: list(points) for key, points in state.aim_trails.items()})


# --- source-triggered transition grammar ---------------------------------

EVENT_DISH = (210, 175, 80)
EVENT_CRAFT = (225, 235, 250)
EVENT_DISH_DIM = (55, 45, 10)
EVENT_CRAFT_DIM = (60, 70, 85)
EVENT_TEXT = (159, 232, 255)
# Neutral topology, deliberately >= the measured 30% physical-panel step from
# every semantic RF hue. Cyan was mathematically different from UPLINK but
# looked identical on the actual LEDs and therefore still implied direction.
EVENT_LINK = (160, 160, 160)
EVENT_DIM = (48, 72, 96)


def event_effect(event: dict) -> str | None:
    """Map a real feed transition to one of a finite set of native assets."""
    kind = event.get("event")
    if kind in {"acquire", "loss", "handoff"}:
        return kind
    if kind == "streams":
        before_streams = int(event.get("before_streams") or 0)
        after_streams = int(event.get("streams") or 0)
        if after_streams > before_streams:
            return "split"
        if after_streams < before_streams:
            return "merge"
        return None
    if kind == "modes":
        before_flags = tuple(
            event.get("before_flags") or (False, False, False))
        after_flags = tuple(event.get("flags") or (False, False, False))
        if bool(before_flags[0]) != bool(after_flags[0]):
            return "array" if after_flags[0] else "unarray"
        # MSPA and DDOR have different physical geometry. Until they have
        # dedicated truthful art, their exact native text card is the effect.
        return None
    if kind == "direction":
        # A finite asset cannot infer which lane appeared/disappeared from the
        # after-state alone. The exact TX/RX/DUPLEX/QUIET label stays truthful.
        return None
    return None


def _event_line(px, start: tuple[int, int], end: tuple[int, int],
                colour: tuple[int, int, int], fraction: float = 1.0,
                reverse: bool = False) -> None:
    """A clipped integer line whose lit extent makes assembly legible."""
    fraction = max(0.0, min(1.0, fraction))
    x0, y0 = start
    x1, y1 = end
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    count = int(round(steps * fraction))
    indices = range(steps - count, steps + 1) if reverse else range(count + 1)
    for step in indices:
        x = int(round(x0 + (x1 - x0) * step / steps))
        y = int(round(y0 + (y1 - y0) * step / steps))
        if 0 <= x < W and 0 <= y < H:
            px[x, y] = colour


def _event_node(px, x: int, y: int, colour: tuple[int, int, int]) -> None:
    for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        xx, yy = x + dx, y + dy
        if 0 <= xx < W and 0 <= yy < H:
            px[xx, yy] = colour


def _handoff_echo_endpoint(
        event: dict, prefix: str,
        ) -> tuple[int, tuple[int, int]] | None:
    """One observed endpoint in its own local-sky coordinate frame."""
    valid_key = f"{prefix}pointing_valid"
    site_key = f"{prefix}complex"
    azimuth_key = f"{prefix}azimuth"
    elevation_key = f"{prefix}elevation"
    if not event.get(valid_key):
        return None
    site = _site_name(str(event.get(site_key) or ""))
    site_index = next((index for index, (name, _initial, _y)
                       in enumerate(NETWORK_SITES) if name == site), None)
    if site_index is None:
        return None
    try:
        azimuth = float(event[azimuth_key])
        elevation = float(event[elevation_key])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(azimuth) and math.isfinite(elevation)):
        return None
    centre = THREE_SKIES_SCOPE_CENTERS[site_index]
    return (site_index, _project_angles(
        azimuth, elevation, centre[0], centre[1], THREE_SKIES_SCOPE_R))


def handoff_echo_signature(event: dict) -> tuple | None:
    """Content identity for one truthful, fully composed handoff card."""
    if event.get("event") != "handoff":
        return None
    old = _handoff_echo_endpoint(event, "from_")
    new = _handoff_echo_endpoint(event, "")
    label = event_label(event)
    if (old is None or new is None or not label.isascii()
            or any(ch.upper() not in FONT for ch in label)
            or text_width(label) > W):
        return None
    return ("handoff-echo", old, new, label)


def render_handoff_echo_frames(
        event: dict,
        ) -> tuple[list[Image.Image], int, int]:
    """Pulse exact old/new cells around a complete, non-overlapping label.

    Geometry and text occupy separate time phases in one immutable asset, so
    firmware element composition can never paint the label over an observed
    endpoint. The pulses alter only the exact measured cells; no halo or
    connector invents another point in either local coordinate frame.
    """
    signature = handoff_echo_signature(event)
    if signature is None:
        raise ValueError("handoff echo requires valid aims and a fitting label")
    _kind, old, new, label = signature
    _old_site, old_point = old
    _new_site, new_point = new
    frames: list[Image.Image] = []
    for index in range(EVENT_FRAMES):
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)
        if index < 6 or index >= 14:
            for base, ((_site, initial, _y), centre) in zip(
                    (0, 24, 48), zip(
                        NETWORK_SITES, THREE_SKIES_SCOPE_CENTERS)):
                _text(px, base, 0, initial, ANTENNA,
                      clip=(base, base + 4))
                _draw_scope(px, centre[0], centre[1], THREE_SKIES_SCOPE_R)
            if index < 6:
                px[old_point] = (
                    EVENT_DISH if index % 2 == 0 else EVENT_DISH_DIM)
            else:
                px[new_point] = (
                    EVENT_CRAFT if index % 2 == 0 else EVENT_CRAFT_DIM)
        else:
            x = (W - text_width(label)) // 2
            _text(px, x, 5, label, EVENT_TEXT, clip=(0, W - 1))
        frames.append(img)
    return frames, EVENT_FPS, 1


def render_event_frames(effect: str) -> tuple[list[Image.Image], int, int]:
    """Prebaked transition art. Only a source event chooses an asset.

    Motion inside the four-second asset is representational; the event that
    starts it is observed. Keeping the vocabulary finite lets every asset be
    resident before an event happens, so a live transition never waits on an
    encode or upload.
    """
    if effect not in EVENT_EFFECTS:
        raise ValueError(f"unknown DSN event effect: {effect}")
    frames: list[Image.Image] = []
    for index in range(EVENT_FRAMES):
        progress = index / max(1, EVENT_FRAMES - 1)
        img = Image.new("RGB", (W, H), OFF)
        px = image_pixels(img)

        # The label occupies rows 5..10 as a native element. The visual
        # grammar lives above and below it so neither one destroys the other.
        if effect in {"acquire", "loss"}:
            amount = progress if effect == "acquire" else 1.0 - progress
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), (66, 2), EVENT_DIM)
            # Assemble from both endpoints. A one-way sweep would imply RF
            # direction that the generic acquire/loss event does not supply.
            _event_line(px, (5, 2), (36, 2), EVENT_LINK, amount)
            _event_line(px, (36, 2), (66, 2), EVENT_LINK, amount, reverse=True)
        elif effect == "handoff":
            old_amount = max(0.0, 1.0 - progress * 2.0)
            new_amount = max(0.0, min(1.0, progress * 2.0 - 1.0))
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 3, 13, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), (66, 2), EVENT_DIM)
            _event_line(px, (5, 13), (66, 13), EVENT_DIM)
            _event_line(px, (5, 2), (66, 2), EVENT_LINK, old_amount)
            _event_line(px, (5, 13), (66, 13), EVENT_LINK, new_amount)
            # Both old and new dishes meet the same spacecraft endpoint.
            _event_line(px, (66, 13), (66, 2), EVENT_LINK, new_amount)
            # The actual site/dish transition is the source event; the
            # crossing dot merely helps the eye connect old to new. Route it
            # at the Earth end, outside the native label's safety mask, so it
            # cannot disappear for half the animation on the physical LEDs.
            _event_line(px, (5, 2), (5, 13), EVENT_DIM)
            # Keep the moving cross one pixel clear of both five-pixel dish
            # glyphs.  At y=2/13 its left arm recoloured 20% of the endpoint.
            hand_y = int(round(3 + 9 * progress))
            _event_node(px, 5, hand_y, EVENT_LINK)
        elif effect in {"split", "merge"}:
            amount = progress if effect == "split" else 1.0 - progress
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_node(px, 3, 2, EVENT_DISH)
            _event_line(px, (5, 2), (66, 2), EVENT_LINK, 1.0)
            # A stream-count change is several records on this same
            # dish/craft contact—not a second antenna. Assemble a parallel
            # lane symmetrically, joined to the one endpoint at each side.
            _event_line(px, (5, 2), (5, 13), EVENT_LINK, amount)
            _event_line(px, (66, 2), (66, 13), EVENT_LINK, amount)
            _event_line(px, (5, 13), (36, 13), EVENT_LINK, amount)
            _event_line(px, (36, 13), (66, 13), EVENT_LINK,
                        amount, reverse=True)
        else:  # array / unarray
            amount = progress if effect == "array" else 1.0 - progress
            join = (35, 2)
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 3, 7, EVENT_DISH)
            _event_node(px, 3, 13, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), join, EVENT_LINK, amount)
            _event_line(px, (5, 7), join, EVENT_LINK, amount)
            _event_line(px, (5, 13), join, EVENT_LINK, amount)
            _event_line(px, join, (66, 2), EVENT_LINK, amount)

        # Firmware text is centred at (36, 8). Keep its entire likely glyph
        # box black even when a branch or handoff crosses the middle; the
        # source event label is information and the effect is supporting art.
        for yy in range(5, 11):
            for xx in range(7, 65):
                px[xx, yy] = OFF

        frames.append(img)
    return frames, EVENT_FPS, 1


# --- device ----------------------------------------------------------------


_is_refusal = is_refusal   # shared: busybar_dev.device


def _is_asset_path_failure(exc: Exception) -> bool:
    """A definite missing/corrupt animation, not an ownership refusal."""
    status = getattr(exc, "status_code", None)
    detail = str(exc).lower()
    return (status == 404
            or status in {400, 422}
            and any(word in detail for word in
                    ("asset", "animation", "path", "file", "invalid")))


_storage_file_matches = storage_file_matches


GENERATION_FILES = re.compile(r"^dsn_[A-Za-z0-9_]+\.anim$")
EVENT_FILES = re.compile(r"^dsnevt_[A-Za-z0-9_]+\.anim$")


def event_asset_name(effect: str, blob: bytes) -> str:
    """A content address that also fits the bar's undocumented 31-byte cap."""
    try:
        code = EVENT_ASSET_CODES[effect]
    except KeyError as exc:
        raise ValueError(f"unknown DSN event effect: {effect}") from exc
    digest = hashlib.sha256(blob).hexdigest()[:10]
    name = f"dsnevt_{EVENT_ASSET_VERSION}_{code}_{digest}.anim"
    if len(name.encode("ascii")) > DEVICE_ASSET_FILENAME_MAX:
        raise ValueError(f"event asset filename exceeds device limit: {name}")
    return name


async def sweep_stale_assets(bb) -> None:
    """Reap generations abandoned by a previous process. The in-memory list
    dies with the process, so without this, flash fills up over months."""
    try:
        await bb.display_clear(application_name=APP_NAME)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.debug("startup clear failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.debug("startup clear failed: %s", exc)
    # Display ownership and app-scoped storage are independent. A routine 409
    # from an active focus session must not disable orphan cleanup.
    try:
        files = (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
        stale = [f.name for f in files if GENERATION_FILES.match(f.name)]
        for name in stale:
            try:
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{name}")
            except Exception:  # noqa: BLE001
                pass
        if stale:
            logger.info("swept %d stale asset generations", len(stale))
    except Exception as exc:  # noqa: BLE001 - hygiene is never fatal
        logger.warning("asset storage sweep failed: %s", exc)


async def prepare_event_assets(bb, state: State) -> None:
    """Keep the finite live-event vocabulary resident before it is needed.

    Paths are content-addressed and survive a restart. Dynamic scene
    generations are swept separately; these tiny, reusable assets are only
    replaced when their rendered bytes change. No event path calls this
    function, so an acquisition can never stall behind encoding or upload.
    """
    prepared: dict[str, tuple[str, bytes]] = {}
    for effect in EVENT_EFFECTS:
        frames, fps, hold = render_event_frames(effect)
        blob = encode_native_frames(frames, fps, hold)
        name = event_asset_name(effect, blob)
        prepared[effect] = (name, blob)

    try:
        files = (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
        resident_entries = {entry.name: entry for entry in files}
        resident = set(resident_entries)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - upload can still warm the set
        logger.debug("event asset scan failed: %s", exc)
        resident_entries = {}
        resident = set()

    pending = set(EVENT_EFFECTS)
    while pending:
        for effect in tuple(effect for effect in EVENT_EFFECTS if effect in pending):
            name, blob = prepared[effect]
            try:
                existing = resident_entries.get(name)
                if (existing is not None
                        and not _storage_file_matches(existing, len(blob))):
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{name}")
                    resident.discard(name)
                    resident_entries.pop(name, None)
                if name not in resident:
                    await bb.assets_upload(APP_NAME, name, blob)
                state.event_assets[effect] = name
                resident.add(name)
                pending.remove(effect)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - warm in the background
                if await scene_asset_exists(bb, name, len(blob)):
                    state.event_assets[effect] = name
                    resident.add(name)
                    pending.remove(effect)
                else:
                    logger.debug("event asset %s not warm yet: %s", effect, exc)
        if pending:
            await asyncio.sleep(30)

    expected = {name for name, _ in prepared.values()}
    for stale in sorted(name for name in resident
                        if EVENT_FILES.match(name) and name not in expected):
        try:
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{stale}")
        except Exception as exc:  # noqa: BLE001 - bounded cleanup can retry next boot
            logger.debug("old event asset retained: %s", exc)
    logger.info("event grammar warm: %d native animations", len(state.event_assets))


def start_event_asset_warm(bb, state: State) -> asyncio.Task:
    """One tracked repair/prewarm worker for the finite event vocabulary."""
    if state.event_warm_task is not None and not state.event_warm_task.done():
        return state.event_warm_task
    state.event_warm_task = asyncio.create_task(prepare_event_assets(bb, state))
    state.event_warm_task.add_done_callback(
        lambda task: logger.debug("event asset warm failed: %s", task.exception())
        if not task.cancelled() and task.exception() is not None else None)
    return state.event_warm_task


def picker_label(state: State) -> str:
    """What the picker shows: which signal, and where it sits in the list.

    The device font fits about 12 characters, so this is the feed's short
    code rather than the full name — 'MARS RECONNAISSANCE ORBITER' would
    overflow, and the point here is to move quickly, not to read.
    """
    total = len(state.links)
    if not total:
        return "NO SIGNAL"
    link = state.current()
    return device_text(
        f"{link.craft if link else '?'} {state.cursor % total + 1}/{total}")


def _picker_payload(label: str, timeout: int = 3, id_suffix: str = "",
                    prefix: tuple = ()) -> types.DisplayElements:
    """Native interaction layer with immutable geometry for each stable id.

    Runtime normally reuses one suffix. If it must interrupt a later-created
    opaque event card, the caller advances that finite layer generation once;
    this creates the picker above the event, whose own ids are retired in the
    same draw. Timed elements prevent abandoned generations from stacking.
    """
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        elements=[*prefix,
            types.RectangleElement(
                id=f"pickbg{id_suffix}", type="rectangle",
                x=0, y=0, width=W, height=H,
                fill="solid", fill_colors=["#000000FF"], border_width=0,
                display=types.DisplayName.FRONT, timeout=timeout),
            types.TextElement(
                id=f"picktx{id_suffix}", type="text",
                text=device_text(label), font="condensed",
                color="#FFD98CFF", align="center", x=36, y=8,
                scroll_rate=1400,
                display=types.DisplayName.FRONT, timeout=timeout),
        ])


def _interactive_payload(state: State, label: str,
                         timeout: int) -> types.DisplayElements:
    """Build a picker/readout that also retires a visible event atomically."""
    now = asyncio.get_running_loop().time()
    prefix: tuple = ()
    if state.active_event_label is not None:
        if now < state.active_event_until:
            # Event ids may sit above the current picker layer. Retire them
            # and mint exactly one newer stable interaction layer in this
            # same accepted draw; no four-second card can resurface later.
            retire = _event_payload(
                state.active_event_label, state.active_event_asset, timeout=1,
                embedded_label=state.active_event_embedded_label)
            prefix = tuple(retire.elements)
            state.interactive_layer += 1
        else:
            state.active_event_label = None
            state.active_event_asset = None
            state.active_event_embedded_label = False
            state.active_event_until = 0.0
    suffix = f"{state.rt_nonce}{state.interactive_layer}"
    return _picker_payload(label, timeout, suffix, prefix)


async def draw_picker(bb, state: State, timeout: int = 3) -> None:
    """The pop-up that rides the wheel.

    Device text elements, not a re-rendered animation: a scene costs an 80 KB
    upload and about a second, which is far too slow to keep up with a wheel
    and would make every detent feel stuck.
    """
    try:
        async with state.interactive_draw:
            await asyncio.wait_for(
                bb.display_draw(_interactive_payload(
                    state, picker_label(state), timeout)),
                INTERACTIVE_IO_TIMEOUT_S)
            state.active_event_label = None
            state.active_event_asset = None
            state.active_event_embedded_label = False
            state.active_event_until = 0.0
            state.interactive_visible_until = (
                asyncio.get_running_loop().time() + timeout)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.warning("picker draw failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("picker draw failed: %s", exc)


async def _post_readout(bb, state: State, label: str, timeout: int) -> bool:
    """Post native text; caller owns ``state.interactive_draw`` ordering."""
    try:
        await asyncio.wait_for(
            bb.display_draw(_interactive_payload(state, label, timeout)),
            INTERACTIVE_IO_TIMEOUT_S)
        state.active_event_label = None
        state.active_event_asset = None
        state.active_event_embedded_label = False
        state.active_event_until = 0.0
        state.interactive_visible_until = (
            asyncio.get_running_loop().time() + timeout)
        return True
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.warning("readout draw failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        logger.warning("readout draw failed: %s", exc)
    return False


async def draw_readout(bb, state: State, label: str, timeout: int = 2) -> bool:
    """An instant native-text acknowledgement while the next asset prepares."""
    async with state.interactive_draw:
        return await _post_readout(bb, state, label, timeout)


def event_label(event: dict) -> str:
    craft = str(event.get("craft") or "DSN").upper()
    dish = str(event.get("dish") or "").upper().replace("DSS", "")
    kind = event.get("event")
    if kind == "acquire":
        return device_text(f"+{craft} {dish}".strip())
    if kind == "loss":
        return device_text(f"-{craft} {dish}".strip())
    if kind == "handoff":
        old = str(event.get("from_dish") or "").upper().replace("DSS", "")
        return device_text(f"{craft} {old}>{dish}")
    if kind == "streams":
        count = int(event.get("streams") or 0)
        raw_bands = tuple(event.get("bands") or ())
        bands = "/".join(band_key(band) or "?" for band in raw_bands)
        detailed = f"{craft} {count} {bands}".strip()
        return device_text(detailed if bands else f"{craft} {count} SIGNALS")
    if kind == "direction":
        suffix = ("DUPLEX" if event.get("up") and event.get("down") else
                  "TX" if event.get("up") else "RX" if event.get("down") else "QUIET")
        return device_text(f"{craft} {suffix}")
    if kind == "modes":
        active = [name for name, on in zip(("ARRAY", "MSPA", "DDOR"),
                                            event.get("flags") or ()) if on]
        suffix = active[0] if len(active) == 1 else "MODES" if active else "NORMAL"
        return device_text(f"{craft} {suffix}")
    if kind == "stale":
        return "FEED STALE"
    if kind == "recovered":
        return "FEED LIVE"
    return device_text(craft)


def _event_payload(label: str, asset: str | None = None,
                   timeout: int = EVENT_TIMEOUT_S,
                   embedded_label: bool = False) -> types.DisplayElements:
    elements: list = [
        types.RectangleElement(
            id="eventbg", type="rectangle", x=0, y=0, width=W, height=H,
            fill="solid", fill_colors=["#000000FF"], border_width=0,
            display=types.DisplayName.FRONT, timeout=timeout),
    ]
    if asset is not None:
        elements.append(types.AnimationElement(
            id="eventanim", type="animation", path=asset, loop=False,
            x=0, y=0, display=types.DisplayName.FRONT, timeout=timeout))
    if not embedded_label:
        elements.append(types.TextElement(
            id="eventtx", type="text", text=device_text(label), font="condensed",
            color="#9FE8FFFF", align="center", x=36, y=8,
            scroll_rate=1400,
            display=types.DisplayName.FRONT, timeout=timeout))
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        led_notification_color="#73DDEBFF",
        elements=elements)


def _status_payload(label: str, timeout: int = 15) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        elements=[
            types.RectangleElement(
                id="statusbg", type="rectangle", x=0, y=0, width=W, height=H,
                fill="solid", fill_colors=["#000000FF"], border_width=0,
                display=types.DisplayName.FRONT, timeout=timeout),
            types.TextElement(
                id="statustx", type="text", text=device_text(label), font="condensed",
                color="#E3B15DFF", align="center", x=36, y=8,
                display=types.DisplayName.FRONT, timeout=timeout),
        ])


def feed_status_label(freshness: str, fresh_label: str = "NO LINK DATA") -> str:
    """One vocabulary for ambient and START feed-state acknowledgements."""
    return (fresh_label if freshness == "fresh" else
            "FEED DELAY" if freshness == "delayed" else
            "FEED STALE" if freshness == "stale" else "DSN OFFLINE")


async def draw_feed_status(bb, state: State, timeout: int = 15) -> None:
    fresh = feed_freshness(state)
    await bb.display_draw(_status_payload(feed_status_label(fresh), timeout))
    state.status_up = timeout > 1


async def prepare_handoff_echo_asset(
        bb, state: State, event: dict,
        ) -> tuple[tuple, str] | None:
    """Build/reuse one immutable data-specific Three Skies handoff asset."""
    signature = handoff_echo_signature(event)
    if signature is None:
        return None
    filename = state.scene_cache.get(signature)
    if filename is not None:
        state.scene_cache.move_to_end(signature)
        return signature, filename

    frames, fps, hold = render_handoff_echo_frames(event)
    blob = encode_native_frames(frames, fps, hold)
    filename = next_scene_filename(state)
    try:
        await bb.assets_upload(APP_NAME, filename, blob)
    except asyncio.CancelledError:
        raise
    except Exception:
        if not await scene_asset_exists(bb, filename, len(blob)):
            raise
        logger.info("adopted ambiguous handoff echo upload: %s", filename)
    remember_scene_asset(state, signature, filename)
    await trim_scene_cache(bb, state)
    return signature, filename


async def show_next_event(bb, state: State) -> bool:
    """Show and acknowledge one queued transition only after an accepted draw."""
    cutoff = time.time() - EVENT_MAX_AGE_S
    state.event_queue[:] = [event for event in state.event_queue
                            if not event.get("t") or event["t"] >= cutoff]
    if (not state.event_queue or state.picking or state.speaking
            or state.ok_down_at is not None
            or state.realtime_since is not None
            or network_focus_active(state)
            or asyncio.get_running_loop().time()
            < state.interactive_visible_until):
        return False
    event = state.event_queue[0]
    label = event_label(event)
    effect = event_effect(event)
    dynamic_signature: tuple | None = None
    if DSN_NETWORK_STYLE == "skies" and event.get("event") == "handoff":
        # Generic handoff art cannot place two observations from independent
        # local coordinate frames. Generate the rare event-specific composed
        # card; if either aim or complete label is unavailable, exact native
        # scrolling text is the honest fallback.
        try:
            prepared = await prepare_handoff_echo_asset(bb, state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - text remains available
            logger.warning("handoff echo preparation failed: %s", exc)
            prepared = None
        if prepared is None:
            asset = None
        else:
            dynamic_signature, asset = prepared
    else:
        asset = state.event_assets.get(effect) if effect is not None else None

    async def draw_if_still_current(payload: types.DisplayElements,
                                    shown_asset: str | None,
                                    shown_embedded_label: bool = False) -> bool:
        """Serialize opaque event cards with newer wheel/readout feedback.

        The wheel marks ``picking`` before waiting on this lock. If this event
        POST began first, the picker therefore commits last; if the picker won
        the lock, this second gate suppresses the now-obsolete event card.
        """
        async with state.interactive_draw:
            if (not state.event_queue or state.event_queue[0] is not event
                    or state.picking or state.speaking
                    or state.ok_down_at is not None
                    or state.realtime_since is not None
                    or network_focus_active(state)
                    or asyncio.get_running_loop().time()
                    < state.interactive_visible_until):
                return False
            await asyncio.wait_for(
                bb.display_draw(payload), INTERACTIVE_IO_TIMEOUT_S)
            state.active_event_label = label
            state.active_event_asset = shown_asset
            state.active_event_embedded_label = shown_embedded_label
            state.active_event_until = (
                asyncio.get_running_loop().time() + EVENT_TIMEOUT_S)
            return True

    try:
        if not await draw_if_still_current(
                _event_payload(
                    label, asset,
                    embedded_label=dynamic_signature is not None),
                asset, dynamic_signature is not None):
            return False
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            return False
        if asset is not None and _is_asset_path_failure(exc):
            # Keep the live event useful: fall back to the same native label
            # immediately, and repair the finite asset set in the background.
            if dynamic_signature is not None:
                await discard_scene_asset(
                    bb, state, dynamic_signature, asset)
            else:
                if effect is not None and state.event_assets.get(effect) == asset:
                    state.event_assets.pop(effect, None)
                try:
                    await bb.storage_remove(
                        f"/ext/user_assets/{APP_NAME}/{asset}")
                except Exception:  # noqa: BLE001 - it may already be absent
                    pass
                start_event_asset_warm(bb, state)
            try:
                if not await draw_if_still_current(
                        _event_payload(label, None), None, False):
                    return False
            except Exception as fallback_exc:  # noqa: BLE001
                logger.warning("event fallback draw failed: %s", fallback_exc)
                return False
        else:
            logger.warning("event draw failed: %s", exc)
            return False
    except Exception as exc:  # noqa: BLE001
        logger.warning("event draw failed: %s", exc)
        return False
    for index, queued in enumerate(state.event_queue):
        if queued is event:
            state.event_queue.pop(index)
            break
    return True


def _countdown_payload(deadline: float, x: int,
                       timeout: int = ELEMENT_TIMEOUT_S,
                       element_id: str = "dsncd") -> types.DisplayElements:
    """The live countdown, rendered and ticked BY THE DEVICE.

    The number was baked into the animation frames, so it only moved when the
    scene was re-pushed — every 21 seconds on Mars and every five minutes on
    Voyager, which is a clock that jumps five minutes at a time rather than
    counts. The firmware has a countdown element that takes a deadline and
    ticks itself, so the host stops being in the loop entirely.
    """
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        elements=[types.CountdownElement(
            id=element_id, type="countdown", timestamp=str(int(deadline)),
            direction="time_left", show_hours="when_non_zero",
            # y is the glyph's TOP and the firmware font is about 7 tall, so
            # H-6 ran off the bottom of the panel and clipped. H-7 fits in
            # rows 9..15, clear of both track rows at 6 and 8.
            color="#FFB43CFF", x=x, y=H - 7,
            display=types.DisplayName.FRONT, timeout=timeout)])


async def retire_countdown(bb, state: State) -> bool:
    """Expire the native timer, committing state only after the draw lands.

    A focused link can disappear without leaving another scene to replace it.
    In that case the status panel still needs to retire the old countdown.
    Keeping ``countdown_up`` true after a refusal makes the next retry finish
    the job instead of silently abandoning a real device element.
    """
    if not state.countdown_up:
        return True
    try:
        await asyncio.wait_for(
            bb.display_draw(
                _countdown_payload(time.time(), TRACK0 + 1, timeout=1,
                                   element_id=state.countdown_id or "dsncd")),
            INTERACTIVE_IO_TIMEOUT_S)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.debug("countdown retirement failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - cosmetic, and retryable
        logger.debug("countdown retirement failed: %s", exc)
        return False
    state.countdown_up = False
    state.countdown_id = None
    return True


def _scene_payload(filename: str, led: str | None = None,
                   timeout: int = ELEMENT_TIMEOUT_S) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        led_notification_color=led,
        elements=[types.AnimationElement(
            id="dsn", type="animation", path=filename, loop=True,
            x=0, y=0, display=types.DisplayName.FRONT,
            timeout=timeout)])


def _live_lease_payload(element_id: str = "dsnlive", y: int = 0,
                        timeout: int | None = None,
                        retire: tuple[tuple[str, int], ...] = (),
                        ) -> types.DisplayElements:
    """One moving source heartbeat; a new id is required to move geometry."""
    timeout = LIVE_LEASE_TIMEOUT_S if timeout is None else timeout
    elements = [types.RectangleElement(
        id=element_id, type="rectangle", x=FRESH_X, y=y,
        width=1, height=2, fill="solid", fill_colors=["#46DCEBFF"],
        border_width=0, display=types.DisplayName.FRONT, timeout=timeout)]
    for old_id, old_y in retire:
        if old_id == element_id:
            continue
        elements.append(types.RectangleElement(
            id=old_id, type="rectangle", x=FRESH_X, y=old_y,
            width=1, height=2, fill="solid", fill_colors=["#46DCEBFF"],
            border_width=0, display=types.DisplayName.FRONT, timeout=1))
    return types.DisplayElements(
        application_name=APP_NAME, priority=PRIORITY,
        elements=elements)


async def sync_live_lease(bb, state: State, freshness: str) -> bool:
    """Renew or retire the live claim, committing only after an accepted draw."""
    loop_now = asyncio.get_running_loop().time()
    # A possibly committed native element cannot outlive its own device
    # timeout. Forgetting it after that deadline is safe and prevents a long
    # device outage from growing the eventual recovery payload without bound.
    for element_id, deadline in list(state.heartbeat_uncertain_until.items()):
        if deadline <= loop_now:
            state.heartbeat_uncertain_until.pop(element_id, None)
            state.heartbeat_uncertain.pop(element_id, None)
    timestamp = state.feed_timestamp_ms
    # The point advances only with NASA's timestamp. It is not part of the
    # looping .anim, so a frozen source can never keep looking alive.
    should_live = (freshness == "fresh" and timestamp is not None
                   and state.view in {"instrument", "network"}
                   and bool(state.links))
    if should_live:
        assert timestamp is not None
        if (state.live_lease_up
                and timestamp == state.last_live_lease_timestamp_ms):
            return True
        if state.heartbeat_pending_timestamp_ms != timestamp:
            state.heartbeat_generation += 1
            state.heartbeat_pending_timestamp_ms = timestamp
            state.heartbeat_pending_id = (
                f"dsnlive{state.rt_nonce}{state.heartbeat_generation}")
            # Advance on every distinct source version. Keep the proposed
            # geometry until acceptance so a lost response retries the same
            # immutable id instead of leaking a second heartbeat lease.
            state.heartbeat_pending_y = (
                (state.heartbeat_y + 2) % (H - 1)
                if state.heartbeat_y is not None
                else int(timestamp // 1000) % (H - 1))
        new_id = state.heartbeat_pending_id or "dsnlive"
        new_y = (state.heartbeat_pending_y
                 if state.heartbeat_pending_y is not None else 0)
        retirements: dict[str, int] = {}
        if state.heartbeat_id is not None and state.heartbeat_y is not None:
            retirements[state.heartbeat_id] = state.heartbeat_y
        retirements.update(state.heartbeat_uncertain)
        retirements.pop(new_id, None)
        payload = _live_lease_payload(
            new_id, new_y, retire=tuple(retirements.items()))
    else:
        retirements = dict(state.heartbeat_uncertain)
        if (state.heartbeat_id is not None
                and state.heartbeat_y is not None):
            retirements[state.heartbeat_id] = state.heartbeat_y
        if (state.heartbeat_pending_id is not None
                and state.heartbeat_pending_y is not None):
            retirements[state.heartbeat_pending_id] = state.heartbeat_pending_y
        if not retirements:
            return True
        (target_id, target_y), *rest = retirements.items()
        payload = _live_lease_payload(
            target_id, target_y, timeout=1, retire=tuple(rest))
    try:
        await bb.display_draw(payload)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            logger.debug("live lease draw failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - retryable display state
        if should_live:
            # The transport can lose its response after committing. Preserve
            # this exact immutable id even if a newer source timestamp arrives;
            # the next accepted payload will retire it atomically.
            state.heartbeat_uncertain[new_id] = new_y
            state.heartbeat_uncertain_until[new_id] = (
                asyncio.get_running_loop().time() + LIVE_LEASE_TIMEOUT_S)
        logger.debug("live lease draw failed: %s", exc)
        return False
    state.live_lease_up = should_live
    if should_live:
        state.last_live_lease_timestamp_ms = timestamp
        state.heartbeat_id = new_id
        state.heartbeat_y = new_y
        state.heartbeat_pending_timestamp_ms = None
        state.heartbeat_pending_id = None
        state.heartbeat_pending_y = None
        state.heartbeat_uncertain.clear()
        state.heartbeat_uncertain_until.clear()
    else:
        state.heartbeat_id = None
        state.heartbeat_y = None
        state.heartbeat_pending_timestamp_ms = None
        state.heartbeat_pending_id = None
        state.heartbeat_pending_y = None
        state.heartbeat_uncertain.clear()
        state.heartbeat_uncertain_until.clear()
    return True


def watch_live_link(state: State) -> Link | None:
    """The newest source Link associated with a locally frozen watch."""
    if state.watch is None or not state.watch.on_air or not state.watch.live_key:
        return None
    return next((link for link in state.links
                 if link.key == state.watch.live_key), None)


def watch_contact_state(state: State) -> str:
    if state.watch is None:
        return "live"
    if watch_live_link(state) is None:
        return "off_air"
    if state.watch.live_key != state.watch.link.key:
        return "handoff"
    return "live"


def narration_target_link(state: State) -> Link | None:
    """Use current RF/dish identity during a frozen light-time watch."""
    if state.watch is not None and state.realtime_since is not None:
        return watch_live_link(state)
    return state.current()


def distance_display_link(state: State, fallback: Link) -> Link:
    """Current RF/pointing with the click-time journey distance frozen."""
    live = watch_live_link(state)
    if live is None or state.watch is None:
        return fallback
    frozen = state.watch.link
    return replace(live, range_km=frozen.range_km,
                   up_range_km=frozen.up_range_km,
                   down_range_km=frozen.down_range_km)


def distance_render_clock(
        state: State, link: Link, now: datetime,
        ) -> tuple[int, datetime]:
    """One cache-key clock and the exact timestamp that its pixels use.

    A coarse bucket is useful only if every render inside it is identical.
    Snapping the globe/daylight/creeping-head inputs to the loop-aligned bucket
    makes that invariant literal instead of merely hoping two close calls land
    on the same framebuffer cells.
    """
    cadence = max(1.0, scene_refresh_s(state, link))
    bucket = int(now.timestamp() / cadence)
    anchor = datetime.fromtimestamp(bucket * cadence, timezone.utc)
    return bucket, anchor


def scene_signature(state: State, link: Link,
                    now: datetime | None = None) -> tuple:
    """The pixels we intend to show, quantized to what the panel can resolve."""
    now = now or datetime.now(timezone.utc)
    if state.view == "network":
        fresh = feed_freshness(state, now.timestamp())
        focus = (network_focus_active(state)
                 and state.network_focus_key is not None)
        selected_key = (state.network_focus_key if focus else link.key)
        if focus:
            focus_links, focus_names, focus_trails = network_focus_inputs(state)
        else:
            focus_links, focus_names, focus_trails = (
                state.links, state.names, state.aim_trails)
        if DSN_NETWORK_STYLE == "dishes":
            if focus:
                return dish_focus_signature(
                    focus_links, fresh, focus_names, selected_key)
            return dish_network_signature(
                focus_links, fresh, selected_key)
        if DSN_NETWORK_STYLE == "skies":
            return three_skies_signature(
                focus_links, fresh, focus_names, selected_key,
                focus_trails, focus)
        return network_signature(
            state.links, fresh, state.names, page=state.network_page)
    if state.view == "instrument":
        fresh = feed_freshness(state, now.timestamp())
        visible = watch_live_link(state) if state.watch is not None else link
        visible = visible or link
        contact_state = watch_contact_state(state)
        return instrument_signature(
            visible, state.aim_trails.get(visible.key, []), fresh,
            state.names, contact_state)
    link = distance_display_link(state, link)
    fresh = feed_freshness(state, now.timestamp())
    # Use the same loop-aligned cadence the scheduler uses. A raw 21-second
    # signature bucket could otherwise wake on a feed poll and restart an
    # eight-second native loop off-seam even though the due timer was aligned.
    clock_bucket, render_anchor = distance_render_clock(state, link, now)
    light_s = link.light_s
    realtime_since = state.realtime_since
    live_distance = realtime_since is not None and light_s is not None
    if (realtime_since is not None and light_s is not None
            and light_s > RT_SEAMLESS_MAX_S):
        creeping = True
        progress_units = represented_progress(
            light_s, render_anchor.timestamp(), realtime_since)[0]
    else:
        creeping = False
        progress_units = None
    spacing = (None if creeping else
               represented_packet_spacing(light_s, live_distance))
    watch_state = (
        "off_air" if state.watch is not None and not state.watch.on_air else
        "handoff" if (state.watch is not None and state.watch.live_key
                       and state.watch.live_key != state.watch.link.key) else
        "live" if state.watch is not None else None
    )
    return ("distance", link.key, craft_label(link.craft, state.names),
            (dish_tilt(link.elevation,
                       dish_metres(link.dish, state.dish_types) == "70")
             if link.pointing_valid else ("unknown",)),
            band_key(link.band), pulse_span(link.down_bps),
            rate_label(link.down_bps), light_label(link.light_s),
            spacing, progress_units,
            link.up_active, link.down_active,
            link.arrayed, link.mspa, link.ddor,
            state.site_lons.get(_site_name(link.complex_name)),
            state.realtime_since,
            watch_state,
            fresh, clock_bucket)


def scene_needs_draw(state: State, intended: tuple, due: bool) -> bool:
    """Whether pixels, an event LED, or the animation timeout need a draw."""
    desired_countdown_id = (
        f"dsncd{state.rt_nonce}{state.rt_generation or 0}"
        if (state.view == "distance" and state.realtime_since is not None)
        else None
    )
    countdown_pending = (
        (desired_countdown_id is not None
         and (not state.countdown_up
              or state.countdown_id != desired_countdown_id))
        or (desired_countdown_id is None and state.countdown_up)
    )
    return (due or intended != state.last_scene_signature
            or state.led_blink is not None or countdown_pending)


def scene_refresh_s(state: State, link: Link) -> float:
    """Renew only on a native-loop seam; the matching timeout exceeds it."""
    if state.view == "distance" and state.realtime_since is not None:
        loop_s = distance_loop_s(link, state.names)
        raw = realtime_redraw_s(link.light_s)
        return math.ceil(raw / loop_s) * loop_s
    if state.view == "instrument":
        visible = watch_live_link(state) if state.watch is not None else link
        visible = visible or link
        loop_s = instrument_frame_count(
            visible, state.names, watch_contact_state(state)) / INSTRUMENT_FPS
    elif state.view == "network":
        if DSN_NETWORK_STYLE in NETWORK_FOCUS_STYLES:
            focus = (network_focus_active(state)
                     and state.network_focus_key is not None)
            selected_key = (state.network_focus_key if focus else link.key)
            focus_links, focus_names, _ = (
                network_focus_inputs(state) if focus else
                (state.links, state.names, state.aim_trails))
            if DSN_NETWORK_STYLE == "dishes":
                loop_s = (dish_focus_loop_s(
                    focus_links, focus_names, selected_key)
                    if focus else dish_network_loop_s(focus_links))
            else:
                loop_s = three_skies_loop_s(
                    focus_links, focus_names, selected_key, focus)
            if focus:
                # Focus is one deliberate native semantic cycle, not an
                # ambient carousel. Its deadline wakes the overview at seam.
                return loop_s
        else:
            # A row page boundary is content, not merely a lease renewal. The
            # next resident page replaces this one after its full marquee.
            loop_s = network_page_duration_s(
                state.links, state.network_page, state.names)
            if network_page_count(state.links) > 1:
                return loop_s
    else:
        loop_s = distance_loop_s(link, state.names)
    cycles = max(1, int(SCENE_RENEW_TARGET_S // max(loop_s, 0.1)))
    return loop_s * cycles


def scene_element_timeout(state: State, link: Link | None = None) -> int:
    if state.view == "distance" and state.realtime_since is not None:
        return REALTIME_ELEMENT_TIMEOUT_S
    if link is None:
        return ELEMENT_TIMEOUT_S
    return max(ELEMENT_TIMEOUT_S, math.ceil(scene_refresh_s(state, link) + 30))


def advance_network_page_if_due(state: State, due: bool) -> bool:
    """Choose the next page once; a refused draw retries that exact intent."""
    if (DSN_NETWORK_STYLE != "rows"
            or not due or state.view != "network" or state.network_page_pending
            or not state.last_scene_signature
            or state.last_scene_signature[0] != "network-page"):
        return False
    count = network_page_count(state.links)
    if count <= 1:
        return False
    state.network_page = (state.network_page + 1) % count
    state.network_page_pending = True
    return True


def arrival_due(state: State, link: Link, ts: float) -> bool:
    """Has one full light time elapsed since the user started the watch?

    This ENDS the watch: the lock releases and the rotation resumes. The
    countdown really does run out — 19 hours 48 minutes on Voyager, which
    completes tomorrow morning on a device that sits on a desk all day. That
    real elapsed-time boundary is what hands the display back; it does not
    claim the feed identified a particular packet at the spacecraft.
    """
    if state.realtime_since is None:
        return False
    light_s = state.watch.light_s if state.watch is not None else link.light_s
    deadline = (state.watch.deadline if state.watch is not None
                else state.realtime_since + light_s if light_s else None)
    if deadline is None or ts < deadline:
        return False
    completed_generation = (state.watch.generation if state.watch is not None
                            else state.rt_generation)
    completed_link = state.watch.link if state.watch is not None else link
    state.realtime_since = None      # the watch is over
    state.focus = None               # back to the live rotation
    state.completion_pending = completed_link.key  # until accepted arrival blink
    state.completion_link = completed_link
    state.completion_generation = completed_generation
    state.watch = None
    if state.view_before_lock is not None:
        state.view = state.view_before_lock
        state.view_before_lock = None
    return True


def complete_watch_if_due(state: State, link: Link, ts: float) -> bool:
    """Finish on the wall-clock boundary, independent of the active view."""
    if not arrival_due(state, link, ts):
        return False
    request_led(state, LED_ARRIVAL)
    state.dirty.set()
    logger.info("the %s light-time watch completed", link.craft)
    return True


def scene_intent_token(state: State) -> tuple:
    """Mutable selection/render state that must not cross an upload await."""
    current = state.current()
    return (id(current), current.key if current else None, state.view,
            state.focus, state.narration_focus, state.realtime_since,
            state.rt_generation, state.picking,
            state.network_page, state.network_focus_key,
            network_focus_active(state),
            ((state.watch.on_air, state.watch.live_key)
             if state.watch is not None else None),
            feed_freshness(state))


def remember_scene_asset(state: State, signature: tuple, filename: str) -> None:
    """Own an immutable uploaded asset before any ambiguous draw can fail."""
    previous = state.scene_cache.pop(signature, None)
    state.scene_cache[signature] = filename
    if previous and previous != filename and previous in state.scene_files:
        state.scene_files.remove(previous)
    if filename not in state.scene_files:
        state.scene_files.append(filename)


def next_scene_filename(state: State) -> str:
    """Immutable per-process generation; never overwrite firmware-owned data."""
    state.scene_gen += 1
    return (f"dsn_{state.rt_nonce}_{int(time.time()) % 100000:05d}_"
            f"{state.scene_gen}.anim")


def encode_native_frames(frames: list[Image.Image], fps: int, hold: int) -> bytes:
    """Let the encoder fold identical frames when one display tick is enough."""
    durations = None if hold == 1 else [hold] * len(frames)
    return anim.encode_anim(frames, fps=fps, durations=durations)


async def trim_scene_cache(bb, state: State) -> None:
    """Bound flash use while keeping the active rotation resident."""
    while len(state.scene_cache) > SCENE_CACHE_MAX:
        signature, filename = next(iter(state.scene_cache.items()))
        if filename == state.last_scene_filename and len(state.scene_cache) > 1:
            state.scene_cache.move_to_end(signature)
            continue
        try:
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
        except Exception as exc:  # noqa: BLE001 - retry on a later accepted draw
            logger.debug("scene cache cleanup deferred: %s", exc)
            break
        state.scene_cache.pop(signature, None)
        if filename in state.scene_files:
            state.scene_files.remove(filename)


async def scene_asset_exists(bb, filename: str, expected_size: int) -> bool:
    """Adopt an upload whose success response may have been lost."""
    try:
        entries = (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
    except Exception:  # noqa: BLE001 - the original upload error remains truth
        return False
    for entry in entries:
        if entry.name != filename:
            continue
        return _storage_file_matches(entry, expected_size)
    return False


async def discard_scene_asset(bb, state: State, signature: tuple,
                              filename: str) -> None:
    """Forget a definitely missing/corrupt path so retry mints a generation."""
    if state.scene_cache.get(signature) == filename:
        state.scene_cache.pop(signature, None)
    if state.last_scene_filename == filename:
        state.last_scene_filename = None
        state.last_scene_signature = None
    try:
        await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{filename}")
    except Exception:  # noqa: BLE001 - a missing file is already discarded
        return
    if filename in state.scene_files:
        state.scene_files.remove(filename)


async def prepare_network_page(bb, state: State, links: list[Link], page: int,
                               names: dict[str, str], freshness: str,
                               signature: tuple) -> None:
    """Warm one captured page during the current page's native dwell."""
    if signature in state.scene_cache:
        return
    frames, fps, hold = render_network_page_frames(
        links, page, freshness, names)
    blob = encode_native_frames(frames, fps, hold)
    filename = next_scene_filename(state)
    try:
        await bb.assets_upload(APP_NAME, filename, blob)
    except asyncio.CancelledError:
        raise
    except Exception:
        if not await scene_asset_exists(bb, filename, len(blob)):
            raise
    remember_scene_asset(state, signature, filename)
    await trim_scene_cache(bb, state)


def start_network_page_warm(bb, state: State) -> asyncio.Task | None:
    if DSN_NETWORK_STYLE != "rows":
        return None
    links = list(state.links)
    count = network_page_count(links)
    if count <= 1:
        return None
    if state.network_warm_task is not None and not state.network_warm_task.done():
        return state.network_warm_task
    page = (state.network_page + 1) % count
    names = dict(state.names)
    freshness = feed_freshness(state)
    signature = network_signature(links, freshness, names, page=page)
    if signature in state.scene_cache:
        state.network_warm_signature = None
        return None
    state.network_warm_signature = signature
    state.network_warm_task = asyncio.create_task(
        prepare_network_page(
            bb, state, links, page, names, freshness, signature))
    state.network_warm_task.add_done_callback(
        lambda task: logger.debug("network page warm failed: %s", task.exception())
        if not task.cancelled() and task.exception() is not None else None)
    return state.network_warm_task


async def push_scene(bb, state: State, link: Link,
                     signature: tuple | None = None,
                     rendered_at: datetime | None = None) -> bool:
    rendered_at = rendered_at or datetime.now(timezone.utc)
    if complete_watch_if_due(state, link, rendered_at.timestamp()):
        signature = None
    signature = signature or scene_signature(state, link, rendered_at)
    intent_token = scene_intent_token(state)
    intent_view = state.view
    intent_network_page = state.network_page
    intent_network_focus = (
        intent_view == "network"
        and DSN_NETWORK_STYLE in NETWORK_FOCUS_STYLES
        and network_focus_active(state)
        and state.network_focus_key is not None)
    intent_network_focus_key = (
        state.network_focus_key if intent_network_focus else link.key)
    if intent_network_focus:
        intent_links, intent_names, intent_trails = network_focus_inputs(state)
    else:
        intent_links = list(state.links)
        intent_names = dict(state.names)
        intent_trails = {key: list(points)
                         for key, points in state.aim_trails.items()}
    intent_freshness = feed_freshness(state, rendered_at.timestamp())
    intent_realtime_since = state.realtime_since
    intent_rt_generation = state.rt_generation
    intent_watch = state.watch
    intent_contact_state = watch_contact_state(state)
    live_watch_link = watch_live_link(state)
    instrument_link = (live_watch_link or link
                       if intent_view == "instrument" else link)
    distance_link = (distance_display_link(state, link)
                     if intent_view == "distance" else link)
    live_until = (
        state.watch.deadline if (intent_view == "distance"
                                 and state.watch is not None)
        else intent_realtime_since + link.light_s
        if (intent_view == "distance" and intent_realtime_since is not None
            and link.light_s) else None
    )
    element_timeout = (REALTIME_ELEMENT_TIMEOUT_S
                       if live_until is not None
                       else scene_element_timeout(state, link))
    filename = state.scene_cache.get(signature)
    if (filename is None and intent_view == "network"
            and DSN_NETWORK_STYLE == "rows"
            and state.network_warm_signature == signature
            and state.network_warm_task is not None
            and not state.network_warm_task.done()):
        # The page boundary caught its prewarm in flight. Share that exact
        # immutable build instead of racing a second upload/flash write.
        try:
            await state.network_warm_task
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - foreground can still build
            logger.debug("network page prewarm did not complete: %s", exc)
        filename = state.scene_cache.get(signature)
    if (filename is None and signature == state.last_scene_signature
            and state.last_scene_filename):
        filename = state.last_scene_filename
        remember_scene_asset(state, signature, filename)
    if filename is not None:
        state.scene_cache.move_to_end(signature)
    if filename is None:
        if intent_view == "network":
            if DSN_NETWORK_STYLE == "dishes":
                if intent_network_focus:
                    frames, fps, hold = render_dish_focus_frames(
                        intent_links, intent_freshness, intent_names,
                        intent_network_focus_key)
                else:
                    frames, fps, hold = render_dish_network_frames(
                        intent_links, intent_freshness,
                        intent_network_focus_key)
            elif DSN_NETWORK_STYLE == "skies":
                frames, fps, hold = render_three_skies_frames(
                    intent_links, intent_freshness, intent_names,
                    intent_network_focus_key, intent_trails,
                    intent_network_focus)
            else:
                frames, fps, hold = render_network_page_frames(
                    intent_links, intent_network_page,
                    intent_freshness, intent_names)
        elif intent_view == "instrument":
            frames, fps, hold = render_instrument_frames(
                instrument_link, state.aim_trails.get(instrument_link.key, []),
                intent_freshness, state.names, intent_contact_state)
        else:
            _, distance_anchor = distance_render_clock(
                state, distance_link, rendered_at)
            frames, fps, hold = render_frames(
                distance_link, distance_anchor, state.names,
                dish_types=state.dish_types,
                site_lons=state.site_lons,
                realtime_since=intent_realtime_since,
                on_air=(intent_watch.on_air if intent_watch is not None else None),
                handoff=(intent_watch is not None and intent_watch.on_air
                         and bool(intent_watch.live_key)
                         and intent_watch.live_key != intent_watch.link.key),
                freshness=intent_freshness)
        blob = encode_native_frames(frames, fps, hold)
        # Versioned, never reused: the firmware caches by path and may still
        # hold the file it is playing. Timestamp AND counter avoid collisions.
        filename = next_scene_filename(state)
        try:
            await bb.assets_upload(APP_NAME, filename, blob)
        except asyncio.CancelledError:
            raise
        except Exception:
            if not await scene_asset_exists(bb, filename, len(blob)):
                raise
            logger.info("adopted ambiguous scene upload: %s", filename)
        # Record ownership BEFORE display_draw. A lost response after upload
        # is ambiguous; retry this exact immutable path instead of leaking a
        # new generation or deleting a file the firmware may have opened.
        remember_scene_asset(state, signature, filename)
        # Ownership refusal happens after upload. Keep the cache bounded even
        # if BUSY/CUSTOM owns the panel for hours and every newer source aim
        # creates another immutable Network signature.
        await trim_scene_cache(bb, state)
    if scene_intent_token(state) != intent_token:
        state.dirty.set()
        return False
    led = state.led_blink                       # acknowledge only after success
    led_generation = state.led_generation
    try:
        async with state.interactive_draw:
            # Upload/encode stays outside the input lock. Only the opaque POST
            # is serialized, then revalidated: if it began first the picker
            # commits last; if input won, this stale scene never posts.
            if scene_intent_token(state) != intent_token:
                state.dirty.set()
                return False
            await asyncio.wait_for(
                bb.display_draw(
                    _scene_payload(filename, led, element_timeout)),
                INTERACTIVE_IO_TIMEOUT_S)
    except exceptions.BusyBarAPIError as exc:
        if _is_asset_path_failure(exc):
            await discard_scene_asset(bb, state, signature, filename)
            state.dirty.set()
        raise
    if state.led_generation == led_generation:
        state.led_blink = None
    state.last_scene_signature = signature
    state.last_scene_filename = filename
    if intent_view == "network" and DSN_NETWORK_STYLE == "rows":
        state.network_page_pending = False
    elif (intent_view == "network"
          and DSN_NETWORK_STYLE in NETWORK_FOCUS_STYLES
          and intent_network_focus
          and state.network_focus_key == intent_network_focus_key
          and math.isinf(state.network_focus_until)):
        # A refused draw leaves ``inf`` pending. Only an accepted semantic
        # zoom starts its complete native cycle. The retiring picker is an
        # opaque newer layer for up to one second, so count from when that
        # mask expires—not from a POST that may have landed behind it.
        visible_at = max(asyncio.get_running_loop().time(),
                         state.interactive_visible_until)
        focus_loop_s = (
            dish_focus_loop_s(
                intent_links, intent_names, intent_network_focus_key)
            if DSN_NETWORK_STYLE == "dishes" else
            three_skies_loop_s(
                intent_links, intent_names, intent_network_focus_key, True))
        state.network_focus_until = (
            visible_at + focus_loop_s)
    try:
        async with state.interactive_draw:
            if scene_intent_token(state) != intent_token:
                state.dirty.set()
            elif live_until is not None:
                countdown_id = f"dsncd{state.rt_nonce}{intent_rt_generation or 0}"
                if state.countdown_up and state.countdown_id != countdown_id:
                    if not await retire_countdown(bb, state):
                        raise RuntimeError("old countdown retirement was refused")
                await asyncio.wait_for(
                    bb.display_draw(_countdown_payload(
                        live_until, TRACK0 + 1, timeout=element_timeout,
                        element_id=countdown_id)),
                    INTERACTIVE_IO_TIMEOUT_S)
                state.countdown_up = True
                state.countdown_id = countdown_id
            elif state.countdown_up:
                if not await retire_countdown(bb, state):
                    raise RuntimeError("countdown retirement was refused")
    except Exception as exc:  # noqa: BLE001 - cosmetic
        logger.debug("countdown draw failed: %s", exc)
        state.dirty.set()
    crossing = (f"{crossing_seconds(link.light_s):.0f}s"
                if link.light_s else "unknown")
    logger.info("%s %s -> %s  %s  light %s  crossing %s  az %.0f el %.0f  %s",
                intent_view, link.dish, link.craft, link.band, light_label(link.light_s),
                crossing, link.azimuth, link.elevation,
                rate_label(link.down_bps))
    await trim_scene_cache(bb, state)
    if intent_view == "network" and DSN_NETWORK_STYLE == "rows":
        start_network_page_warm(bb, state)
    if led == LED_ARRIVAL and state.completion_pending == link.key:
        state.completion_pending = None
    return True


def dish_metres(dish: str,
                dish_types: dict[str, str] | None = None) -> str | None:
    """Published dish diameter, or None rather than an invented default."""
    kind = (dish_types or {}).get(dish, "")
    found = re.match(r"(\d+)", kind)
    if found:
        return found.group(1)
    return None

# Why each spacecraft exists or what it accomplished. NASA's feed gives us the
# full name live (see fetch_names) but never supplies mission phase or purpose.
# The review lease below prevents these curated facts from posing as permanent
# live status. Unknown or expired codes simply narrate without a blurb.
MISSIONS = {
    "vgr1": "Launched in 1977, it is the most distant object humans have ever "
            "made, and it is now flying through interstellar space",
    "vgr2": "Launched in 1977, it is the only spacecraft ever to visit Uranus "
            "and Neptune, and it is now in interstellar space",
    "jno": "It has been orbiting Jupiter since 2016, looking beneath the "
           "clouds to work out what the planet is made of",
    "mro": "It has been photographing Mars from orbit since 2006, and it "
           "relays data home for the rovers on the surface",
    "m01o": "In orbit since 2001, it is the longest-serving spacecraft at Mars",
    "mvn": "A Mars orbiter launched to trace how the planet's atmosphere "
           "escaped to space. NASA ended the mission in June 2026",
    "lro": "It has been mapping the Moon in fine detail since 2009, including "
           "the sites where people will land next",
    "soho": "It watches the Sun without pause from a point about a million "
            "miles sunward of Earth",
    "ace": "It samples the solar wind upstream of Earth, which buys us about "
           "an hour of warning before a solar storm arrives",
    "psyc": "It is on its way to a metal-rich asteroid, to see what may be the "
            "exposed core of an early planet",
    "nhpc": "It flew past Pluto in 2015 and is now out in the Kuiper Belt",
    "lucy": "It is on a twelve-year tour of the Trojan asteroids that share "
            "Jupiter's orbit",
    "eurc": "It is on its way to Europa, to survey the ocean beneath the ice "
            "of Jupiter's moon",
    "spp": "It flies closer to the Sun than anything ever built, through the "
           "outer atmosphere itself",
    "jwst": "It observes the early universe in infrared from a point beyond "
            "the Moon, about a million miles from Earth",
    "chdr": "It is an X-ray telescope, watching black holes and the remains "
            "of exploded stars",
    "dsco": "It watches the whole sunlit face of the Earth, and the solar "
            "wind arriving upstream of us",
    "tess": "It hunts for planets around other stars by watching for the dip "
            "as one crosses its star",
    "m20": "Perseverance landed in Jezero Crater in 2021 to explore ancient "
           "habitable environments and collect sealed rock cores for possible "
           "return to Earth",
    "msl": "Curiosity landed in Gale Crater in 2012 to read Mars's "
           "environmental history from layers of rock",
    "wind": "Launched in 1994 to measure the solar wind before it reaches "
            "Earth",
    "sta": "One of a pair sent to view the Sun from the side, so we could see "
           "storms coming rather than only head on",
    "orx": "It flew to an asteroid, took a sample and dropped it back to "
           "Earth by parachute, and is now on its way to another one",
    "tgo": "A European orbiter sniffing the Martian atmosphere for rare "
           "gases, and relaying data home for the rovers on the surface",
    "mex": "Europe's first Mars mission, in orbit since 2003 and still "
           "mapping the surface and sounding for water ice",
    "emm": "The United Arab Emirates' first mission to Mars, watching the "
           "whole planet's weather from a high orbit",
    "bepi": "A European and Japanese pair on their way to Mercury, braking "
            "against the Sun's gravity with flyby after flyby",
    "juice": "It is on its way to Jupiter to study three icy moons that may "
             "each hold an ocean under the shell",
    "gaia": "It charted the positions and motions of more than a billion "
            "stars, turning the Milky Way into a three-dimensional map",
    "imap": "It maps the boundary where the solar wind meets interstellar "
            "space, the bubble the Voyagers flew out of",
    "caps": "A microwave-oven-sized craft testing the odd looping orbit that "
            "the lunar Gateway station will use",
    "ltb": "A small lunar orbiter built to map water ice. NASA ended the "
           "mission in July 2025 after losing contact before science "
           "operations",
    # --- currently flying -------------------------------------------------
    "swfo": "It watches the solar wind from a million miles sunward, to give "
            "warning of a geomagnetic storm before it reaches us",
    "cgo": "It photographs the vast cloud of hydrogen that surrounds the "
           "Earth, far beyond the visible atmosphere",
    "escb": "One of a pair of small orbiters designed to measure how the "
            "solar wind strips material from Mars",
    "escg": "The other of the two ESCAPADE orbiters, designed to measure the "
            "same Martian space-weather event from a second location",
    "dart": "It was deliberately crashed into a small asteroid in 2022 to "
            "see whether the impact could shift its orbit. It could",
    "terr": "An Earth-observing satellite launched in 1999 to study the "
            "planet's land, atmosphere and oceans with five instruments",
    "hst": "The Hubble Space Telescope, in orbit since 1990 and still one of "
           "the sharpest eyes we have",
    "xmm": "A European X-ray observatory, watching the hottest and most "
           "violent objects in the sky",
    "intg": "A European observatory for gamma rays, the most energetic light "
            "there is",
    "hyb2": "It collected a sample from the asteroid Ryugu and dropped it "
            "into the Australian desert in 2020",
    "ch2o": "India's second lunar mission, still orbiting and mapping the "
            "Moon after its lander was lost",
    "ch2": "India's second lunar mission, mapping the Moon from orbit",
    "ch3": "India's third lunar mission, which landed near the Moon's south "
           "pole in 2023",
    "plc": "A Japanese orbiter that studied Venusian weather. JAXA ended "
           "operations in September 2025 after contact had been lost",
    "slim": "A Japanese lander that touched down within a hundred metres of "
            "its target in 2024, and came to rest upside down",
    "mom": "India's first mission to Mars, which reached orbit at the first "
           "attempt",
    "hera": "A European mission on its way to survey the asteroid that DART "
            "hit, to measure exactly what the impact did",

    # --- past, and mostly ended -------------------------------------------
    "cas": "It orbited Saturn for thirteen years, dropped a probe onto Titan, "
           "and was steered into the planet in 2017 so it could never "
           "contaminate a moon",
    "rose": "It escorted a comet around the Sun for two years and put a "
            "lander on its surface",
    "msgr": "The first spacecraft to orbit Mercury, which it mapped for four "
            "years before running out of fuel",
    "dawn": "The only spacecraft to orbit two different worlds: the asteroid "
            "Vesta, then the dwarf planet Ceres",
    "sdu": "It flew through a comet's tail, caught grains of it in aerogel, "
           "and parachuted them back to Earth",
    "dif": "It fired a copper slug into a comet to see what the inside of one "
           "is made of",
    "kepl": "It stared at one patch of sky for years and found thousands of "
            "planets around other stars",
    "stf": "An infrared telescope that spent sixteen years seeing the cold "
           "and the dust-hidden things visible light cannot reach",
    "ulys": "It used a Jupiter gravity assist to survey the Sun's polar "
            "regions from a steeply inclined orbit",
    "map": "It mapped the faint afterglow of the Big Bang and pinned down the "
           "age of the universe",
    "mer1": "Opportunity, a rover built for ninety days on Mars that kept "
            "going for fifteen years",
    "mer2": "Spirit, Opportunity's twin, which drove Mars for six years "
            "before its wheels bogged down for good",
    "phx": "A lander that dug into the northern plains of Mars and found "
           "water ice a few centimetres down",
    "nsyt": "It listened for marsquakes with a seismometer set directly on "
            "the ground, until dust covered its solar panels",
    "mgs": "It mapped Mars for nine years and found the gullies that argued "
           "for water having run there",
    "vex": "A European orbiter that studied the runaway greenhouse "
           "atmosphere of Venus for eight years",
    "grla": "One of a pair that flew in close formation around the Moon, "
            "measuring its gravity by the millimetre",
    "grlb": "The other of the pair, whose exact distance from its twin is "
            "what revealed the Moon's interior",
    "lade": "It measured the Moon's impossibly thin atmosphere and the dust "
            "that floats above the surface",
    "lcro": "It followed a spent rocket stage into a shadowed lunar crater "
            "and flew through the plume to look for water",
    "imag": "It made the first pictures of the invisible plasma trapped in "
            "Earth's magnetic field",
    "polr": "It watched the aurora from above, over the pole",
    "ice": "A 1978 spacecraft that visited a comet, was abandoned, and was "
           "briefly woken again by volunteers in 2014",
    "sele": "A Japanese lunar orbiter that mapped the Moon in high definition "
            "and filmed an Earthrise",
    "ch1": "India's first lunar mission, which found water bound into the "
           "soil of the Moon",
    "musc": "The first spacecraft to bring back a sample of an asteroid, "
            "limping home after nearly every system had failed",
    "spil": "A privately built Israeli lunar lander that reached the Moon in "
            "2019 and crashed on touchdown",
    "mcoa": "One of two briefcase-sized craft that flew to Mars alongside "
            "InSight and relayed its landing live",
    "mcob": "The other briefcase-sized relay, which sent home a parting "
            "photograph of Mars as it flew past",
    "lici": "An Italian cubesat that trailed DART and photographed the plume "
            "thrown off by the impact",

    # --- the cubesats that rode Artemis I ---------------------------------
    "argo": "A shoebox-sized Italian cubesat that photographed the rocket "
            "stage that carried it, as a test of autonomous imaging",
    "bios": "It carried yeast into deep space to measure what the radiation "
            "out there does to living cells",
    "equl": "A Japanese cubesat that steered itself to the far side of the "
            "Moon using water as propellant",
    "omot": "A Japanese attempt at the smallest ever lunar lander, which was "
            "lost before it could try",
    "hmap": "A cubesat built to map hydrogen, and so buried ice, at the "
            "Moon's south pole",
    "mlic": "A cubesat sent to look for water ice from lunar orbit",
    "neas": "It was to unfurl a solar sail and cruise to a near-Earth "
            "asteroid on sunlight alone",
    "cusp": "An Artemis I cubesat designed to measure solar particles and "
            "magnetic fields in deep space",
    "lfl": "It was to shine lasers into craters that never see sunlight, "
           "looking for ice in the dark",

    # --- fleets, and the flights ahead ------------------------------------
    "em1": "Artemis I: the uncrewed first flight of the Space Launch System "
           "and Orion, completed in 2022",
    "em2": "Artemis II: the first crewed flight of the Space Launch System "
           "and Orion, completed in April 2026",
    "em3": "Artemis III: the crewed low-Earth-orbit test flight in NASA's "
           "updated lunar architecture",
    "kplo": "South Korea's first mission beyond Earth orbit, photographing "
            "the Moon and scouting landing sites",
    "stab": "The second of the pair sent to view the Sun from the side. "
            "Contact was lost in 2014",
    "stb": "The second of the pair sent to view the Sun from the side. "
           "Contact was lost in 2014",
    "apm1": "A commercial lunar lander that reached space in 2024 but lost "
            "its propellant to a valve failure and never got there",
    "agm1": "A commercial lander built to carry cargo to the Moon's south "
            "pole",
    "ch2l": "Vikram, the lander India's second lunar mission carried. It was "
            "lost in the final minutes of its descent",
    "rsp": "A European rover built to drill two metres into Mars, deeper "
           "than anything has, looking for life that may be sheltered there",
    "icps": "Not a spacecraft: the upper stage that pushes an Artemis "
            "capsule out of Earth orbit and toward the Moon",
    "ltst": "Not a spacecraft: the upper stage that pushes an Artemis "
            "capsule out of Earth orbit and toward the Moon",
    "eus": "Not a spacecraft: an Exploration Upper Stage development effort "
           "that NASA terminated under its revised Artemis architecture",
    "jnsa": "One of a pair of small craft designed to fly past binary "
            "asteroids. The mission was shelved before launch",
    "jnsb": "The other of the pair built to study binary asteroids, shelved "
            "before it flew",
    "tm": "A cubesat deployed from Artemis I to test a plasma thruster. NASA "
          "detected brief downlink signals after deployment",
    "tmm": "A cubesat deployed from Artemis I to test a plasma thruster. NASA "
           "detected brief downlink signals after deployment",
    "cue3": "A student-built Cube Quest entrant designed to test deep-space "
            "radio navigation; it did not fly on Artemis I",
    "lunah-map": "A cubesat built to map hydrogen, and so buried ice, at the "
                 "Moon's south pole",
    "rd1": "A proposal to land a commercial capsule on Mars. It was never "
           "flown",
    "rp": "A rover designed to prospect for ice at the lunar poles. It was "
          "cancelled before it was built",
    "olin": "One of three small satellites designed to fly in formation and "
            "study structure in the upper atmosphere",
    "lnd1": "A small radio beacon intended for the lunar surface, to help "
            "later missions navigate",
    "m01s": "In orbit since 2001, it is the longest-serving spacecraft at "
            "Mars",
    "mros": "It has been photographing Mars from orbit since 2006, and it "
            "relays data home for the rovers on the surface",
    "gtl": "A long-running Japanese and American mission through the tail of "
           "Earth's magnetic field, streaming away from the Sun",
}


def spoken_name(code: str, names: dict[str, str]) -> str:
    """NASA's friendlyName exactly as written — it is already cased for
    reading aloud ('SOHO', 'Voyager 2', 'MAVEN'), which no amount of
    title-casing survives. Parentheticals are stripped: nobody says
    'Deep Space Climate Observatory open paren DSCOVR'.
    """
    full = names.get(code.lower(), "")
    full = re.sub(r"\s*\([^)]*\)", "", full)
    full = "".join(ch for ch in full if 32 <= ord(ch) <= 126).strip()
    return full or code


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


# Fleets. Near-identical craft share a purpose-level description rather than
# repeating mutable operational status a dozen times by hand.
for _n in (1, 3, 4, 5, 6, 7, 8, 9):
    MISSIONS[f"tdr{_n}"] = ("A Tracking and Data Relay Satellite built to "
                            "carry traffic between the ground and spacecraft "
                            "in Earth orbit")
MISSIONS["tdr1"] = ("The first Tracking and Data Relay Satellite, which "
                    "carried traffic for spacecraft in Earth orbit before "
                    "retirement")
MISSIONS["tdr4"] = ("A first-generation Tracking and Data Relay Satellite "
                    "that carried traffic for spacecraft in Earth orbit "
                    "before retirement")
for _n in (10, 11, 12, 13):
    MISSIONS[f"td{_n}"] = ("A Tracking and Data Relay Satellite built to "
                           "carry traffic between the ground and spacecraft "
                           "in Earth orbit")
for _n in range(10, 18):
    MISSIONS[f"go{_n}"] = ("A weather satellite built for geostationary "
                           "observation of the same face of Earth")
for _n in range(15, 19):
    MISSIONS[f"no{_n}"] = ("One of NOAA's polar-orbiting weather satellites, "
                           "which scanned Earth strip by strip before the "
                           "POES constellation was retired in August 2025")
for _n in range(1, 5):
    MISSIONS[f"mms{_n}"] = ("One of four flying in a pyramid, close enough "
                             "to catch the moment Earth's magnetic field "
                             "snaps and reconnects")
    MISSIONS[f"clu{_n}"] = ("One of a European quartet that flew in formation "
                            "through Earth's magnetic field. ESA ended the "
                            "Cluster mission in September 2024")
for _c in ("thb", "thc"):
    MISSIONS[_c] = ("One of two THEMIS probes moved into lunar orbit and "
                    "renamed ARTEMIS to study the Moon's space environment")

# Hyphenated aliases occur in historical/config records as well as future
# reservations, so a blanket "planned before it flies" is not truthful.
MISSIONS["em-1"] = MISSIONS["em1"]
MISSIONS["em-2"] = MISSIONS["em2"]
MISSIONS["em-3"] = MISSIONS["em3"]
for _n in range(4, 11):
    MISSIONS[f"em-{_n}"] = ("An Artemis flight designation in NASA's campaign "
                            "to return people to the Moon")


@dataclass(frozen=True)
class MissionReview:
    """Provenance policy for one spoken blurb.

    Stable history/purpose copy is reviewed once and does not pretend to be a
    live status.  Status-sensitive copy is publishable only while both its
    primary source and dated review lease are present and current.
    """

    source_url: str = ""
    reviewed_on: date | None = None
    review_by: date | None = None
    expired_fallback: str = ""
    stable: bool = False


MISSION_REVIEWED_ON = date(2026, 8, 8)
MISSION_REVIEW_BY = date(2026, 11, 8)

# Explicit is important here: a newly added blurb starts unverified and cannot
# be narrated merely because somebody forgot to classify it. These entries are
# historical facts or purpose-level descriptions whose truth does not depend on
# a craft still operating, flying a particular phase, or retaining a schedule.
STABLE_MISSION_BLURBS = frozenset({
    "mvn", "m20", "msl", "wind", "sta", "ltb", "escb", "escg",
    "dart", "terr", "gaia", "plc", "hyb2", "ch3", "slim", "mom",
    "cas", "rose", "msgr", "dawn", "sdu", "dif", "kepl", "stf",
    "ulys", "map", "mer1", "mer2", "phx", "nsyt", "mgs", "vex",
    "grla", "grlb", "lade", "lcro", "imag", "polr", "ice", "sele",
    "ch1", "musc", "spil", "mcoa", "mcob", "lici", "argo", "bios",
    "equl", "omot", "hmap", "mlic", "neas", "cusp", "lfl", "em1",
    "em2", "stab", "stb", "apm1", "agm1", "ch2l", "rsp", "icps",
    "ltst", "eus", "jnsa", "jnsb", "tm", "tmm", "cue3",
    "lunah-map", "rd1", "rp", "olin", "lnd1",
    *(f"tdr{n}" for n in (1, 3, 4, 5, 6, 7, 8, 9)),
    *(f"td{n}" for n in (10, 11, 12, 13)),
    *(f"go{n}" for n in range(10, 18)),
    *(f"no{n}" for n in range(15, 19)),
    *(f"clu{n}" for n in range(1, 5)),
    "thb", "thc", "em-1", "em-2",
    *(f"em-{n}" for n in range(4, 11)),
})

# Unclassified/status-sensitive copy fails closed. Stable entries remain
# available without a rolling deadline; source-backed status entries below get
# a finite lease. A blank URL is therefore never permission to voice mutable
# status.
MISSION_REVIEWS = {
    code: (MissionReview(reviewed_on=MISSION_REVIEWED_ON, stable=True)
           if code in STABLE_MISSION_BLURBS else MissionReview())
    for code in MISSIONS
}


def _source(codes: tuple[str, ...], url: str, *, fallback: str = "") -> None:
    for code in codes:
        stable = code in STABLE_MISSION_BLURBS
        MISSION_REVIEWS[code] = MissionReview(
            source_url=url,
            reviewed_on=MISSION_REVIEWED_ON,
            review_by=None if stable else MISSION_REVIEW_BY,
            expired_fallback=fallback,
            stable=stable,
        )


_source(("vgr1", "vgr2"), "https://science.nasa.gov/mission/voyager/")
_source(("mro",), "https://science.nasa.gov/mars/mars-relay-network/")
_source(("mvn",),
        "https://www.nasa.gov/news-release/"
        "nasa-says-farewell-to-maven-mars-mission-hosts-media-call-today/")
_source(("m20",), "https://science.nasa.gov/mission/mars-2020-perseverance/")
_source(("msl",), "https://science.nasa.gov/mission/msl-curiosity/")
_source(("wind",), "https://science.nasa.gov/mission/wind/")
_source(("ltb",), "https://science.nasa.gov/mission/lunar-trailblazer/")
_source(("escb", "escg"), "https://science.nasa.gov/mission/escapade/")
_source(("terr",), "https://terra.nasa.gov/about/terra-instrument-payload")
_source(("plc",),
        "https://global.jaxa.jp/press/2025/09/20250918-2_e.html")
_source(("ulys",),
        "https://www.esa.int/Science_Exploration/Space_Science/Ulysses_overview")
_source(("cusp", "tm", "tmm", "cue3"),
        "https://www.nasa.gov/directorates/stmd/"
        "prizes-challenges-crowdsourcing-program/centennial-challenges/"
        "cube-quest-concludes-wins-lessons-learned-from-centennial-challenge")
_source(("olin",),
        "https://www.colorado.edu/aerospace/research/cu-boulder-cubesats")
_source(tuple(f"tdr{n}" for n in (1, 3, 4, 5, 6, 7, 8, 9))
        + tuple(f"td{n}" for n in (10, 11, 12, 13)),
        "https://nssdc.gsfc.nasa.gov/nmc/spacecraft/display.action?id=1983-026B")
_source(tuple(f"go{n}" for n in range(10, 18)),
        "https://goes-r.noaa.gov/mission/history.html")
_source(tuple(f"no{n}" for n in range(15, 19)),
        "https://www.nesdis.noaa.gov/news/"
        "legacy-orbit-noaa-decommissions-the-poes-satellite-constellation")
_source(tuple(f"clu{n}" for n in range(1, 5)),
        "https://www.esa.int/Science_Exploration/Space_Science/Cluster")
_source(("thb", "thc"), "https://science.nasa.gov/mission/themis-artemis/")
_source(("em1", "em2", "em3") + tuple(f"em-{n}" for n in range(1, 11)),
        "https://www.nasa.gov/wp-content/uploads/2026/03/"
        "going-back-to-the-moon.pdf",
        fallback="An Artemis flight designation in NASA's lunar campaign")
_source(("eus",),
        "https://oig.nasa.gov/audits/"
        "nasas-management-of-programs-and-projects-after-mission-termination-"
        "canceled-or-repurposed-artemis-campaign-systems/")


def mission_blurb(code: str, on: date | None = None) -> str:
    """Return only stable or currently sourced-and-reviewed narration."""
    key = code.lower()
    text = MISSIONS.get(key, "")
    review = MISSION_REVIEWS.get(key)
    if not text or review is None:
        return ""
    if review.stable:
        return text
    if (not review.source_url or review.reviewed_on is None
            or review.review_by is None):
        return review.expired_fallback
    if (on or datetime.now(timezone.utc).date()) > review.review_by:
        return review.expired_fallback
    return text


def light_words(secs: float | None) -> str:
    """Light time, spoken. Coarse on purpose: the cache is keyed by the text,
    so a value that jitters would re-bake the line every poll."""
    if not secs:
        return ""
    if secs < 1:
        return "less than a second"          # Chandra said "0 seconds"
    if secs < 90:
        return _plural(round(secs), "second")
    mins = secs / 60
    if mins < 60:
        return _plural(round(mins), "minute")
    hours, rest = divmod(int(round(mins)), 60)
    return (_plural(hours, "hour") if rest == 0
            else f"{_plural(hours, 'hour')} and {_plural(rest, 'minute')}")


AU_KM = 149_597_870.7          # Earth to Sun
LIGHT_YEAR_KM = 9.4607e12


def distance_words(range_km: float | None) -> str:
    """How far away, in units that mean something at this scale.

    NOT light-years. Everything the DSN talks to is inside the solar system,
    so light-years give you 0.0022 for Voyager 2, 0.0001 for Juno and
    0.0000000 for Chandra — every craft reads "zero point zero zero zero".
    Kilometres carry the size; the Earth-Sun distance carries the meaning.

    Returns a phrase that already contains "away", so the comparison lands
    after it rather than stranding the preposition at the end of the clause.
    """
    if (range_km is None or not math.isfinite(range_km)
            or not 0 < range_km <= MAX_RANGE_KM):
        return ""
    if range_km >= 1e9:
        far = f"{range_km / 1e9:.0f} billion kilometres"
    elif range_km >= 1e6:
        far = f"{range_km / 1e6:.0f} million kilometres"
    elif range_km >= 1e3:
        far = f"{range_km / 1e3:.0f} thousand kilometres"
    else:
        far = f"{range_km:.0f} kilometres"
    au = range_km / AU_KM
    if au >= 1.5:
        return (f"{far} away, {au:.0f} times the Earth's distance "
                f"from the Sun")
    return f"{far} away"


def lightyear_words(range_km: float | None) -> str:
    """The light-year, used as the humbling comparison it actually is.

    Kept for genuinely deep space only. Saying a light year is ten thousand
    times further than Juno is technically true and rhetorically limp; saying
    it about Voyager, the most distant thing we have ever built, is the point.
    """
    if (range_km is None or not math.isfinite(range_km)
            or not 0 < range_km <= MAX_RANGE_KM
            or range_km / AU_KM < 50):
        return ""
    ratio = LIGHT_YEAR_KM / range_km
    return (f"A single light year is {ratio:.0f} times further than that, "
            f"and the nearest star is more than four of them away.")


def power_words(dbm: float | None) -> str:
    """Received power, said in units a person can feel.

    This is the most astonishing number the feed carries and we were throwing
    it away. Juno arrives at -140 dBm: ten billionths of a billionth of a
    watt, which a 34-metre dish pulls a clean data stream out of.
    """
    if (dbm is None or not math.isfinite(dbm)
            or not RECEIVE_POWER_MIN_DBM <= dbm < 0.0):
        return ""
    watts = 10 ** ((dbm - 30) / 10.0)
    for scale, unit in ((1e-18, "attowatt"), (1e-15, "femtowatt"),
                        (1e-12, "picowatt"), (1e-9, "nanowatt")):
        if watts < scale * 1000:
            count = watts / scale
            if count >= 1.5:
                return f"{count:.0f} {unit}s"
            return f"one {unit}" if count >= 0.8 else f"under one {unit}"
    return ""


def rate_words(bps: float | None) -> str:
    if bps is None or not math.isfinite(bps) or bps <= 0:
        return ""
    if bps > RATE_LABEL_MAX_GBPS * 1e9:
        return (f"more than {RATE_LABEL_MAX_GBPS:.0f} gigabits per second")
    if bps >= 1e9:
        return f"about {_plural(round(bps / 1e9), 'gigabit')} per second"
    if bps >= 1e6:
        return f"about {_plural(round(bps / 1e6), 'megabit')} per second"
    if bps >= 1e3:
        return f"about {_plural(round(bps / 1e3), 'kilobit')} per second"
    return f"{_plural(round(bps), 'bit')} per second"


def transmit_power_words(kw: float | None) -> str:
    """Spoken source power with the same honest ceiling as the panel."""
    if kw is None or not math.isfinite(kw) or kw < 0.05:
        return ""
    if kw > POWER_LABEL_MAX:
        return f"more than {POWER_LABEL_MAX:.0f} kilowatts"
    if kw >= 1:
        return f"{kw:.0f} kilowatts"
    watts = kw * 1000
    return (f"more than {POWER_LABEL_MAX:.0f} watts"
            if watts > POWER_LABEL_MAX else f"{watts:.0f} watts")


def receive_records_words(records: tuple[DownStream, ...], count: int) -> str:
    """Describe source records without inventing a contact-wide throughput.

    DSN receiver and telemetry-processing records may represent independent
    links or redundant processing of the same link. Their published rates are
    therefore retained and spoken per record, never summed.
    """
    if count <= 1:
        return ""
    opening = f"The source publishes {count} active receive signal records"
    if len(records) != count:
        return (f"{opening}. Their individual rates are not all available, "
                "and the records are not added into one contact throughput "
                "because they can include receiver redundancy.")
    if count > NARRATION_RECORD_DETAIL_MAX:
        return (f"{opening}. Their individual rates are not enumerated in "
                "speech, and the records are not added into one contact "
                "throughput because they can include receiver redundancy.")

    rates: list[str] = []
    for record in records:
        if record.bps is None or not math.isfinite(record.bps):
            rates.append("an unavailable rate")
        elif record.bps == 0:
            rates.append("zero bits per second")
        else:
            rates.append(rate_words(record.bps))
    joined = (rates[0] if len(rates) == 1 else
              f"{', '.join(rates[:-1])} and {rates[-1]}")
    return (f"{opening}, with per-record rates of {joined}. Those records "
            "are not added into one contact throughput because they can "
            "include receiver redundancy.")


def band_words(band: str) -> str:
    """Name the observed band without pretending it determines live rate."""
    kind = band_key(band)
    if kind == "KA":
        return ("This is Ka band, a high-frequency channel that can support "
                "wide bandwidth when the rest of the link allows it.")
    if kind == "K":
        return ("This is K band, the network's high-frequency near-Earth "
                "service. Deep-space high-rate service uses Ka band instead.")
    if kind == "S":
        return ("This is S band, a long-established channel valued for robust "
                "tracking, telemetry and command links.")
    if kind == "X":
        return "This is X band, the network's workhorse."
    return ""


def spoken(link: Link, names: dict[str, str] | None = None,
           dish_types: dict[str, str] | None = None) -> str:
    """The narration: which antenna, what the spacecraft is, how long the
    signal takes, and how fast it is arriving."""
    where = link.complex_name or "The Deep Space Network"
    number = link.dish.replace("DSS", "").lstrip("0") or link.dish
    size = dish_metres(link.dish, dish_types)
    craft = spoken_name(link.craft, names or {})
    listening = link.down_active
    published_downstreams = tuple(link.down_streams)
    down_record_count = max(link.streams, len(published_downstreams))
    # A contact scalar is meaningful only for one receive record. Multiple
    # records may be independent links or redundant receiver chains; neither
    # the XML nor DSN documentation makes their rates summable.
    single_down_bps = (
        published_downstreams[0].bps if down_record_count == 1
        and published_downstreams else
        link.down_bps if down_record_count <= 1 else None)

    if listening and link.up_active:
        action = f"receiving from and transmitting to {craft}"
    elif listening:
        action = f"receiving from {craft}"
    elif link.up_active:
        action = f"transmitting to {craft}"
    else:
        action = f"tracking {craft}"
    dish_clause = (f"on the {size} metre dish, number {number}"
                   if size else f"on dish number {number}")
    lines = [f"DSN Now reports {where} is {action}, {dish_clause}."]

    badge = activity_badge(link.activity)
    if badge in {"DEMO", "UPGRADE", "ENGINEER"}:
        source_activity = " ".join(
            re.findall(r"[A-Za-z0-9]+", link.activity))[:48]
        if source_activity:
            lines.append(f"The source labels this antenna activity as "
                         f"{source_activity}.")

    blurb = mission_blurb(link.craft)
    if blurb:
        lines.append(f"{blurb}.")

    how_far = distance_words(link.range_km)   # already ends "... away"
    if how_far:
        lines.append(f"It is {how_far}.")

    light = light_words(link.light_s)
    rate = rate_words(single_down_bps)
    if light and listening:
        if rate:
            lines.append(f"Its signal takes {light} to reach us, "
                         f"and arrives at {rate}.")
        elif down_record_count > 1:
            lines.append(f"Its signal takes {light} to reach us.")
        elif single_down_bps is None:
            lines.append(f"Its signal takes {light} to reach us. The carrier "
                         "is active, but this receive record has no usable "
                         "data rate.")
        else:
            lines.append(f"Its signal takes {light} to reach us, with a "
                         "published data rate of zero right now.")
    elif light:
        # The verb already said we are transmitting; don't say it twice.
        lines.append(f"It is {light} away at the speed of light.")
    elif rate:
        lines.append(f"Data is coming in at {rate}.")

    receive_records = receive_records_words(
        published_downstreams, down_record_count)
    if receive_records:
        lines.append(receive_records)

    # The number an operator actually lives by. This is light-time only, not
    # a promise about when a spacecraft will process or answer a command.
    if link.light_s and link.light_s > 60.0:
        # Preserve both source-published legs even when the represented link
        # is uplink-only. Falling back to the active-direction range is an
        # estimate; when both legs exist this is their actual published sum.
        return_light = link.down_light_s or link.light_s
        outbound_light = link.up_light_s or link.light_s
        lines.append(f"The light-time alone for an immediate round trip is "
                     f"about {light_words(outbound_light + return_light)}.")

    downstreams = link_streams(link)
    down_band_values = tuple(band_key(stream.band) for stream in downstreams)
    down_bands_complete = bool(down_band_values) and all(down_band_values)
    down_kinds = tuple(dict.fromkeys(
        kind if kind in BAND_PULSE else "unknown"
        for kind in down_band_values if kind))
    if len(down_kinds) > 1:
        joined = ", ".join(down_kinds[:-1]) + f" and {down_kinds[-1]}"
        lines.append(f"The source publishes active received carriers in "
                     f"{joined} bands for this contact.")
    elif down_bands_complete:
        band = band_words(down_kinds[0] if down_kinds else link.band)
        if band:
            lines.append(band)
    upstreams = link_upstreams(link)
    up_band_values = tuple(band_key(stream.band) for stream in upstreams)
    up_bands_complete = bool(up_band_values) and all(up_band_values)
    up_kinds = tuple(dict.fromkeys(
        kind if kind in BAND_PULSE else "unknown"
        for kind in up_band_values if kind))
    down_kind = (down_kinds[0]
                 if (down_bands_complete and len(down_kinds) == 1
                     and down_kinds[0] in BAND_PULSE) else "")
    if len(up_kinds) > 1:
        joined = ", ".join(up_kinds[:-1]) + f" and {up_kinds[-1]}"
        lines.append(f"The source publishes active uplink records in {joined} "
                     "bands for this contact.")
    elif (up_bands_complete and len(up_kinds) == 1
          and up_kinds[0] in BAND_PULSE and down_kind
          and up_kinds[0] != down_kind):
        lines.append(f"The uplink is {up_kinds[0]} band while the received "
                     f"carrier is {down_kind} band.")

    if len(upstreams) > 1:
        lines.append(f"The source publishes {len(upstreams)} active uplink "
                     "signal records for this contact.")

    faint = power_words(link.down_dbm) if listening else ""
    shout = transmit_power_words(link.up_kw)
    receive_clause = ""
    if faint:
        # _dbm() deliberately keeps the strongest usable record. With more
        # than one carrier, say so instead of attributing that value to the
        # contact as a whole.
        receive_subject = ("The strongest published receive record"
                           if len(downstreams) > 1 else "It")
        receive_clause = f"{receive_subject} reaches the dish at {faint}"
    if receive_clause and shout and len(upstreams) > 1:
        lines.append(f"{receive_clause}; the strongest published uplink "
                     f"record is {shout}.")
    elif receive_clause and shout:
        # The contrast is the whole point: we shout tens of kilowatts and
        # what comes back is around 10^-22 of it.
        lines.append(f"{receive_clause}, while Earth transmits at {shout}.")
    elif receive_clause:
        lines.append(f"{receive_clause}.")
    elif shout and len(upstreams) > 1:
        lines.append(f"The strongest published uplink record is {shout}.")
    elif shout:
        lines.append(f"Earth is transmitting at {shout}.")

    humbling = lightyear_words(link.range_km)
    if humbling:
        lines.append(humbling)

    # Rare, and the most interesting thing on the network when they happen.
    if link.arrayed:
        lines.append("More than one dish is being combined to improve the "
                     "receive margin or usable data rate.")
    if link.mspa:
        lines.append("This antenna is holding several spacecraft "
                     "in its beam at once.")
    if link.ddor:
        lines.append("They are taking a precision navigation fix, "
                     "using two complexes and usually a distant quasar as a "
                     "reference.")
    return " ".join(lines)


def describe_links(links: list[Link], names: dict[str, str],
                   dish_types: dict[str, str] | None = None,
                   view: str | None = None) -> list[str]:
    """What --dry-run prints: every live link, rendered but not drawn.

    A function rather than inline in main() so a test can run it — this path
    referenced an undefined name for two commits and crashed on the one
    command the README tells you to run first.
    """
    view = view or DEFAULT_VIEW
    out = [f"{len(links)} active link(s)"]
    network_blob: bytes | None = None
    if view == "network":
        if DSN_NETWORK_STYLE == "dishes":
            selected_key = links[0].key if links else None
            network_frames, fps, hold = render_dish_network_frames(
                links, selected_key=selected_key)
        elif DSN_NETWORK_STYLE == "skies":
            selected_key = links[0].key if links else None
            network_frames, fps, hold = render_three_skies_frames(
                links, names=names, selected_key=selected_key)
        else:
            network_frames, fps, hold = render_network_page_frames(
                links, 0, names=names)
        network_blob = encode_native_frames(network_frames, fps, hold)
    for link in links:
        if view == "instrument":
            frames, fps, hold = render_instrument_frames(link, names=names)
            blob = encode_native_frames(frames, fps, hold)
        elif view == "network":
            blob = network_blob or b""
        else:
            frames, fps, hold = render_frames(
                link, datetime.now(timezone.utc), names,
                dish_types=dish_types)
            blob = encode_native_frames(frames, fps, hold)
        lt = f"{link.light_s / 60:6.1f} min" if link.light_s else "    ?  "
        rate = rate_label(link.down_bps)
        crossing = (f"{crossing_seconds(link.light_s):.0f}s"
                    if link.light_s else "unknown")
        out.append(f"  {link.complex_name:10s} {link.dish:6s} -> {link.craft:5s} "
                   f"{link.band} {rate:>10s}  "
                   f"az {link.azimuth:3.0f} el {link.elevation:2.0f}  "
                   f"{len(link_streams(link))} receive record(s)  "
                   f"light {lt} -> crossing {crossing}  "
                   f"({len(blob) / 1024:.0f} kB)")
        out.append(f"      says: {spoken(link, names, dish_types)}")
    return out


def narration_ready(state: State, link: Link) -> bool:
    """Is this line worth baking yet?

    spoken() silently drops whole sentences when its inputs have not arrived:
    an empty name table gives the bare feed code instead of the full name, and
    an unresolved range removes BOTH the distance and the light-time
    sentences. prebake starts two seconds after launch, while fetch_names is
    still in flight and Horizons has not answered, so without this gate it
    caches a description of a spacecraft the app barely knows anything about
    -- and the content hash then keeps that stub forever. Measured on device:
    complete lines run 15-25 seconds, the stubs 1.4 to 4.6.
    """
    if not state.names:
        return False                       # no full name yet
    if ((link.range_km is None or link.range_km <= 0) and link.naif
            and link.naif not in state.range_unavailable):
        return False                       # Horizons may still answer
    return True


def observe_narration(state: State, link: Link) -> str | None:
    """Freeze one stable script for the lifetime of a dish/craft pass.

    Live telemetry still drives the pixels. Speech deliberately samples it
    only after two identical coarse scripts so a one-dB or rounding-boundary
    wobble cannot keep the Pi synthesising forever.
    """
    if not narration_ready(state, link):
        return None
    # The background worker wakes every two seconds, but NASA's snapshot does
    # not. Counting worker ticks would always freeze the very first eligible
    # feed sample; require two distinct source timestamps instead.
    observation = state.feed_timestamp_ms
    if observation is None:
        return None
    text = spoken(link, state.names, state.dish_types)
    frozen = state.narration_texts.get(link.key)
    # A pass gets one stable script.  Telemetry may keep changing after the
    # first two-source-snapshot freeze, but allowing a second pair to replace
    # it would churn TTS and its on-device cache for the entire pass.
    if frozen is not None:
        return frozen
    previous, count, last_observation = state.narration_candidates.get(
        link.key, ("", 0, -1))
    if previous != text:
        count = 1
    elif observation != last_observation:
        count += 1
    state.narration_candidates[link.key] = (text, count, observation)
    if count < 2:
        return frozen
    state.narration_texts[link.key] = text
    state.narration_frozen_at[link.key] = observation
    state.narration_candidates.pop(link.key, None)
    return text


def speech_name(text: str, voice: str | None = None, *, repair: int = 0) -> str:
    """Filename derived from the line AND the voice reading it.

    The firmware caches assets by path forever, which is usually a trap — here
    it is the whole point. Identical text in the same voice means an identical
    file, so a hit needs no upload at all and survives a restart. Nothing is
    ever overwritten, so the 508 'file is open' trap cannot fire either.

    The voice belongs in the key: without it, changing DSN_VOICE would keep
    serving lines an old narrator recorded, for as long as the cache held them.
    """
    voice = voice or VOICE
    keyed = f"{voice}\n{text}"
    if not 0 <= repair <= 0xff:
        raise ValueError(f"invalid speech repair generation: {repair}")
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    name = (f"v2_{_voice_tag(voice)}_"
            f"{hashlib.sha1(keyed.encode()).hexdigest()[:10]}{suffix}.snd")
    if len(name.encode("ascii")) > DEVICE_ASSET_FILENAME_MAX:
        raise ValueError(f"speech asset filename exceeds device limit: {name}")
    return name


def _voice_tag(voice: str) -> str:
    """The voice, flattened into something safe for a filename.

    It goes in the *name* and not just the hash so that a voice change is
    visible on the device: startup can spot lines the previous narrator
    recorded and reclaim the flash instead of stranding ten of them.
    """
    flat = re.sub(r"[^a-z0-9]", "", voice.lower()) or "default"
    # Configurable voice identifiers can exceed the bar's 31-byte filename
    # ceiling, so keep a readable prefix and hash the remainder. Reserve room for the
    # immutable corruption-repair suffix; short common voices retain their
    # established base cache paths exactly.
    fixed = len("v2__") + 10 + len("_rff.snd")
    tag_max = DEVICE_ASSET_FILENAME_MAX - fixed
    if len(flat) <= tag_max:
        return flat
    digest = hashlib.sha1(flat.encode()).hexdigest()[:6]
    return flat[:tag_max - len(digest)] + digest


# v2: the prefix is a cache generation. Bumping it makes every line baked by
# the older, ungated path unrecognisable, and the sweep below reclaims them.
VOICE_FILES = re.compile(
    r"^v2_(?P<voice>[a-z0-9]+)_(?P<digest>[0-9a-f]{10})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$")


def _speech_file_identity(name: str) -> tuple[str, int] | None:
    """Return the stable base path and immutable repair generation."""
    match = VOICE_FILES.fullmatch(name)
    if match is None:
        return None
    base = f"v2_{match.group('voice')}_{match.group('digest')}.snd"
    repair = int(match.group("repair") or "0", 16)
    return base, repair


def speech_asset_name(state: State, text: str) -> str:
    """Newest known immutable device path for this exact voice and text."""
    base = speech_name(text)
    return speech_name(text, repair=state.speech_repairs.get(base, 0))


def mark_speech_unplayable(state: State, name: str) -> None:
    """Quarantine a PLAY-404 path and mint its immutable successor.

    Returns nothing. It was annotated `-> str` and returned the repair
    generation, an int. The only caller discarded it, so nothing broke — but
    the name reads like "gives me the repaired path", and the next caller to
    believe the annotation would have got an integer where a filename goes.
    """
    identity = _speech_file_identity(name)
    if identity is None:
        raise ValueError(f"unrecognised speech asset: {name}")
    base, repair = identity
    next_repair = max(state.speech_repairs.get(base, 0), repair) + 1
    if next_repair > 0xff:
        raise RuntimeError(f"speech repair generations exhausted for {base}")
    state.speech_repairs[base] = next_repair
    state.speech.pop(name, None)
    state.speech_retire.add(name)


def _settle(fut: asyncio.Future, error: BaseException | None, value) -> None:
    if fut.done():                                    # cancelled while we ran
        return
    if error is not None:
        fut.set_exception(error)
    else:
        fut.set_result(value)


async def synth_off_loop(text: str) -> bytes:
    """Synthesise without pinning the app or its event loop.

    Linux is the always-on/Pi path. Kokoro retains roughly a gigabyte of model
    state after one call, so a rare cache miss runs in a disposable child and
    returns that memory to the OS when it exits. The child is also explicitly
    terminated on cancellation. Other platforms keep the daemon-thread path:
    macOS ``say`` is cheap, and a daemon avoids ``asyncio.run()`` waiting for a
    default-executor thread during shutdown.
    """
    if isolate_tts_process():
        return await synth_in_worker(text)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def work() -> None:
        try:
            value, error = synth_snd(text, VOICE), None
        except BaseException as exc:  # noqa: BLE001 - reported to the awaiter
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(_settle, fut, error, value)
        except RuntimeError:
            pass                                      # loop closed: shutting down

    threading.Thread(target=work, daemon=True, name="dsn-synth").start()
    return await fut


def isolate_tts_process() -> bool:
    """The resident neural-model cost matters on the Linux production host."""
    return sys.platform.startswith("linux")


async def _stop_synth_process(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        await proc.wait()  # let asyncio reap an exit that won the race
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def synth_in_worker(text: str) -> bytes:
    """Run one Linux bake in a cancellable process with a private output file."""
    with tempfile.NamedTemporaryFile(prefix="dsn-tts-", suffix=".snd",
                                     delete=False) as handle:
        output = Path(handle.name)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "busybar_dev.tts_worker",
            "--voice", VOICE, "--output", str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate(text.encode())
        except asyncio.CancelledError:
            await _stop_synth_process(proc)
            raise
        if proc.returncode:
            detail = stderr.decode(errors="replace").strip()
            if len(detail) > 1200:
                detail = detail[-1200:]
            raise RuntimeError(
                f"isolated TTS exited {proc.returncode}"
                + (f": {detail}" if detail else ""))
        pcm = output.read_bytes()
        if not pcm or len(pcm) % 2:
            raise RuntimeError("isolated TTS returned invalid s16 PCM")
        return pcm
    finally:
        if proc is not None and proc.returncode is None:
            await _stop_synth_process(proc)
        output.unlink(missing_ok=True)


def touch_speech(state: State, name: str) -> float:
    """Mark a cached line as just used, and return its duration.

    Dicts keep insertion order, so re-inserting at the end is what makes
    `next(iter(...))` in trim_speech_cache the *least recently used* entry
    rather than merely the oldest baked one.
    """
    seconds = state.speech.pop(name)
    state.speech[name] = seconds
    return seconds


async def ensure_speech(bb, state: State, text: str) -> tuple[str, float] | None:
    """Bake `text` to a device asset if it isn't already there.

    Serialised: kokoro runs at roughly 1x realtime, and two synths racing on a
    Pi just makes both late.
    """
    name = speech_asset_name(state, text)
    if name in state.speech:
        return name, touch_speech(state, name)
    async with state.synth:
        # A PLAY 404 can advance the repair generation while this bake waits.
        # Re-resolve under the same serialization used for uploads.
        name = speech_asset_name(state, text)
        if name in state.speech:                      # baked while we queued
            return name, touch_speech(state, name)
        try:
            pcm = await synth_off_loop(text)
        except Exception as exc:  # noqa: BLE001 - a missing voice is not fatal
            logger.warning("synth failed (%s): %s", VOICE, exc)
            return None
        seconds = len(pcm) / 2 / 44100                # s16le mono 44.1k
        try:
            await bb.assets_upload(APP_NAME, name, pcm)
        except Exception:
            # An upload timeout is ambiguous: the device may have committed
            # the deterministic path even though the response was lost. Adopt
            # only an exact-size file; otherwise re-raise instead of trying to
            # overwrite a path the firmware may still own.
            try:
                files = (await bb.storage_list(
                    f"/ext/user_assets/{APP_NAME}")).list
                existing = next((entry for entry in files if entry.name == name), None)
            except Exception:  # noqa: BLE001 - preserve the original failure
                existing = None
            if existing is None or not _storage_file_matches(existing, len(pcm)):
                raise
            logger.warning("adopting %s after an ambiguous upload result", name)
        state.speech[name] = seconds
        logger.info("baked %s in %s (%.1fs of audio)", name, VOICE, seconds)
        # Only after the successor is resident is it safe to retire paths that
        # the device reported missing or unplayable. A failed removal leaves
        # an immutable orphan, not a broken current cache entry; startup will
        # try the retirement again after rediscovering the repair generation.
        for old in list(state.speech_retire):
            try:
                await bb.storage_remove(
                    f"/ext/user_assets/{APP_NAME}/{old}")
            except exceptions.BusyBarAPIError as exc:
                if getattr(exc, "status_code", None) != 404:
                    logger.debug("speech retirement deferred for %s: %s", old, exc)
                    continue
            except Exception as exc:  # noqa: BLE001 - successor is usable
                logger.debug("speech retirement deferred for %s: %s", old, exc)
                continue
            state.speech_retire.discard(old)
        await trim_speech_cache(bb, state)
        return name, seconds


async def trim_speech_cache(bb, state: State) -> None:
    """Bounded, least recently used first — the device's flash is far smaller
    than this cache would like to be.

    It used to evict in insertion order on the grounds that this was "close
    enough" to least-used, and it is not: a spacecraft whose data rate churns
    mints a new line every pass, so the line you press START on most often was
    the oldest entry and went first, while the noise that displaced it stayed.
    `touch_speech` on every cache hit is the other half of this.
    """
    while len(state.speech) > SPEECH_CACHE_MAX:
        protected = {
            speech_asset_name(state, text)
            for text in state.narration_texts.values()
        }
        # If a corrupt ancestor could not be retired, its repaired successor
        # is the only durable record that the base path is poisoned. Evicting
        # that successor would make a restart adopt the corrupt base again.
        blocked_bases = {
            identity[0]
            for name in state.speech_retire
            if (identity := _speech_file_identity(name)) is not None
        }

        def evictable(name: str) -> bool:
            identity = _speech_file_identity(name)
            return identity is None or identity[0] not in blocked_bases  # noqa: B023 - consumed by the next() calls in this same iteration

        old = next((name for name in state.speech
                    if name not in protected and evictable(name)), None)
        if old is None:
            # Preserve the previous hard bound when only ordinary active
            # scripts are protected, but fail safe if every candidate is the
            # sole repaired successor of an unretired corrupt path.
            old = next((name for name in state.speech if evictable(name)), None)
        if old is None:
            logger.warning(
                "voice cache temporarily exceeds its bound while corrupt "
                "ancestors await retirement (%d entries, bound %d)",
                len(state.speech), SPEECH_CACHE_MAX)
            break
        try:
            await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{old}")
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) != 404:
                logger.warning(
                    "voice cache trim deferred (%d entries, bound %d): %s",
                    len(state.speech), SPEECH_CACHE_MAX, exc)
                break
        except Exception as exc:  # noqa: BLE001
            # Keep the mapping: removing it would make the next deterministic
            # bake try to overwrite a file that may still exist and be open.
            #
            # A live device was found holding 49 v2_ files against a bound of
            # 48. These deferral paths are the likeliest explanation and were
            # silent, so the next occurrence could not be told apart from the
            # documented corrupt-ancestor case above. Now it says which.
            logger.warning(
                "voice cache trim deferred (%d entries, bound %d): %s",
                len(state.speech), SPEECH_CACHE_MAX, exc)
            break
        state.speech.pop(old, None)


def request_narration(state: State, link: Link,
                      name: str | None) -> NarrationRequest:
    """Remember one cold START without confusing it with bake priority.

    The exact generation prevents an old worker completing after the user has
    moved from producing a stale READY/ERROR notice. Repeated START presses on
    the same unresolved line reuse the request and do not duplicate work.
    """
    current = state.narration_request
    if (current is not None and current.key == link.key
            and current.name == name and current.view == state.view):
        state.narration_priority = link.key
        return current
    state.narration_request_counter += 1
    request = NarrationRequest(
        state.narration_request_counter, link.key, name, state.view)
    state.narration_request = request
    state.narration_notice = None
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0
    state.narration_priority = link.key
    return request


def bind_narration_request(state: State, key: str,
                           name: str) -> NarrationRequest | None:
    """Bind a waiting START intent to the stable script chosen by prebake."""
    request = state.narration_request
    if request is None or request.key != key:
        return None
    if request.name != name:
        request = replace(request, name=name)
        state.narration_request = request
    return request


def clear_narration_request(state: State) -> None:
    """Invalidate UI intent; useful cache work may still finish silently."""
    state.narration_request_counter += 1
    state.narration_request = None
    state.narration_notice = None
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0


def finish_narration_request(state: State, request: NarrationRequest | None,
                             label: str) -> bool:
    """Queue terminal feedback only for the exact still-current START."""
    if request is None or state.narration_request != request:
        return False
    if label not in {NARRATION_READY, NARRATION_ERROR}:
        raise ValueError(f"invalid narration notice: {label}")
    state.narration_request = None
    state.narration_notice = NarrationNotice(
        request.generation, request.key, request.name, request.view, label)
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0
    state.dirty.set()                       # wake the scheduler; no scene upload
    return True


async def show_narration_notice(bb, state: State) -> bool:
    """Draw one exact terminal notice; retain it across an ordinary 409."""
    notice = state.narration_notice
    if notice is None:
        return False
    # A deferred error can outlive the worker's successful retry. Never tell
    # the user audio failed when this exact immutable asset is now resident.
    if (notice.label == NARRATION_ERROR and notice.name is not None
            and notice.name in state.speech):
        notice = replace(notice, label=NARRATION_READY)
        state.narration_notice = notice
        state.narration_notice_retry_at = 0.0
        state.narration_notice_failures = 0
    async with state.interactive_draw:
        # Recheck inside the same lock as the POST. A wheel/picker may have
        # invalidated this notice after the optimistic check above but before
        # we acquired display ownership; it must never physically land later.
        if state.narration_notice != notice:
            return False
        current = narration_target_link(state)
        invalid = (current is None or current.key != notice.key
                   or state.view != notice.view
                   or feed_freshness(state) != "fresh"
                   or (notice.label == NARRATION_READY
                       and (notice.name is None
                            or notice.name not in state.speech)))
        if invalid:
            state.narration_notice = None
            state.narration_notice_retry_at = 0.0
            state.narration_notice_failures = 0
            return False
        if (state.picking or state.speaking or state.ok_down_at is not None
                or state.speech_tasks):
            return False
        accepted = await _post_readout(bb, state, notice.label, timeout=3)
    if accepted and state.narration_notice == notice:
        state.narration_notice = None
        state.narration_notice_retry_at = 0.0
        state.narration_notice_failures = 0
    elif not accepted and state.narration_notice == notice:
        state.narration_notice_failures += 1
    return accepted


def narration_notice_backoff_s(failures: int) -> float:
    """Back off a refused toast without losing its exact user intent."""
    return 2.0 if failures <= 0 else min(30.0, 2.0 ** failures)


def narration_play_is_current(state: State, link: Link, generation: int,
                              view: str) -> bool:
    """Whether an in-flight PLAY result still belongs to this interaction."""
    return (state.narration_request_counter == generation
            and state.view == view
            and feed_freshness(state) == "fresh"
            and any(live.key == link.key for live in state.links))


def claim_audio_stop(state: State) -> int:
    """Synchronously invalidate older PLAY ownership and return STOP's token."""
    if state.audio_stop_pending and state.audio_stop_generation is not None:
        return state.audio_stop_generation
    state.audio_generation += 1
    generation = state.audio_generation
    state.audio_stop_generation = generation
    state.audio_stop_pending = True
    return generation


async def stop_audio_bounded(
        bb, state: State, reason: str, generation: int | None = None,
        ) -> None:
    """Neutralise a possibly accepted PLAY without delaying navigation.

    STOP and PLAY are opposite mutations of one device resource.  A lock
    prevents their requests from crossing, while the generation makes a
    queued retry harmless after a newer PLAY has taken ownership.
    """
    if generation is not None:
        # A deferred retry captures the intent it is retrying. If a newer
        # generation already won, it must not invent a fresh STOP on arrival.
        if (not state.audio_stop_pending
                or state.audio_stop_generation != generation):
            return
    elif state.audio_stop_pending and state.audio_stop_generation is not None:
        generation = state.audio_stop_generation
    else:
        generation = claim_audio_stop(state)
    async with state.audio_io:
        if (not state.audio_stop_pending
                or state.audio_stop_generation != generation):
            return
        try:
            await asyncio.wait_for(bb.audio_stop(), INTERACTIVE_IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - interaction still has to return
            if getattr(exc, "status_code", None) == 410:
                # DELETE /audio/play uses 410 for "no audio is playing". That
                # is already the exact postcondition STOP requested.
                state.audio_stop_pending = False
                state.audio_stop_retry_at = 0.0
                state.audio_stop_generation = None
            else:
                state.audio_stop_retry_at = (
                    asyncio.get_running_loop().time() + 30.0)
                logger.debug("%s audio stop failed: %s", reason, exc)
        else:
            state.audio_stop_pending = False
            state.audio_stop_retry_at = 0.0
            state.audio_stop_generation = None


async def shutdown_audio_bounded(
        bb, state: State, speech_tasks: list[asyncio.Task],
        ) -> set[asyncio.Task]:
    """Cancel narration and fence its final STOP behind every older PLAY.

    Claiming STOP is synchronous, so no already-started PLAY still owns the
    current generation once cancellation begins.  The final device mutation
    then uses the ordinary audio lock: a PLAY which is slow to acknowledge
    cancellation must finish (or release its request) before STOP can be
    issued.  If it never releases the lock, the bounded STOP attempt is
    cancelled instead of sending an unordered STOP which that PLAY could
    overtake later. If PLAY settles exactly as that attempt expires, retry
    the same STOP generation once after task settlement closes the boundary
    where a committed PLAY could otherwise escape without a final STOP.
    """
    needs_stop = bool(
        state.speaking or speech_tasks or state.audio_stop_pending)
    stop_generation = claim_audio_stop(state) if needs_stop else None

    for task in speech_tasks:
        task.cancel()
    pending: set[asyncio.Task] = set()
    if speech_tasks:
        done, pending = await asyncio.wait(
            speech_tasks, timeout=SHUTDOWN_TIMEOUT_S)
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    async def settle_stop(reason: str) -> bool:
        if stop_generation is None:
            return True
        try:
            await asyncio.wait_for(
                stop_audio_bounded(
                    bb, state, reason, stop_generation),
                SHUTDOWN_TIMEOUT_S)
        except TimeoutError:
            return False
        except Exception as exc:  # noqa: BLE001 - shutdown remains bounded
            logger.debug("shutdown audio stop failed: %s", exc)
            return False
        return (not state.audio_stop_pending
                or state.audio_stop_generation != stop_generation)

    stop_settled = await settle_stop("shutdown")

    if pending:
        # The fence above may have released whatever a cancelled task was
        # still blocked on, so give those a brief bounded window to land
        # before reporting them as unfinished.
        #
        # Sampling `task.done()` at this instant instead made the answer
        # depend on event-loop scheduling: a released task partway through
        # unwinding its own await points reads as unfinished on one platform
        # and finished on another. macOS happened to schedule the unwind
        # first and Linux -- the platform this deploys to -- did not, so the
        # suite was green on the laptop and red in CI for the same commit.
        settled, pending = await asyncio.wait(
            pending, timeout=SHUTDOWN_SETTLE_S)
        if settled:
            await asyncio.gather(*settled, return_exceptions=True)

    if not stop_settled and not pending:
        # The first STOP can hit its deadline on the same event-loop turn in
        # which a cancellation-resistant PLAY releases audio_io and commits.
        # Once every tracked PLAY task has settled, one same-generation retry
        # is ordered, safe, and closes that otherwise silent escape hatch.
        stop_settled = await settle_stop("shutdown retry")
    if not stop_settled:
        # Never bypass audio_io here. No STOP is safer than a STOP which an
        # older, cancellation-resistant PLAY can overtake after client close.
        logger.warning("audio shutdown fence missed the deadline")
    return pending


async def speak(bb, state: State, link: Link) -> None:
    """Play a resident line now, or prepare it without blocking the button."""
    if (state.speaking or feed_freshness(state) != "fresh"
            or not any(live.key == link.key for live in state.links)):
        if state.narration_return_view is not None:
            state.view = state.narration_return_view
            state.narration_return_view = None
            state.dirty.set()
        return

    if state.audio_stop_pending:
        # A previous ambiguous PLAY may still be audible. Resolve its bounded
        # STOP before starting another line, or the deferred retry could cut
        # off the new narration—or two clips could overlap.
        await stop_audio_bounded(bb, state, "before PLAY")
        if state.audio_stop_pending:
            await draw_readout(bb, state, NARRATION_BUSY)
            return

    # Interaction is cache-only. Kokoro and the subsequent upload together
    # take tens of seconds on the Pi, so a miss remains in the current view,
    # keeps browsing live and acknowledges the preparation immediately.
    text = (state.narration_texts.get(link.key)
            or spoken(link, state.names, state.dish_types))
    name = speech_asset_name(state, text)
    if name not in state.speech:
        if state.view != "network":
            # Keep the requested detail selected long enough for the usual Pi
            # synthesis/upload path to finish and acknowledge PRESS START.
            # The wheel remains live and cancels this intent immediately.
            note_manual_selection(state)
        request_narration(state, link, name)
        await draw_readout(
            bb, state, NARRATION_PREPARING if state.speech_cache_ready
            else NARRATION_STARTING)
        return

    # A new explicit press consumes any old completion notice. From here on
    # the line is resident: only actual playback owns the narration hold.
    # Serialize with a terminal notice already inside its display POST. That
    # older PRESS START must settle before PLAY, never land over audio after it
    # has begun. A wheel picker uses the same lock and likewise commits last.
    cache_still_ready = False
    async with state.interactive_draw:
        clear_narration_request(state)
        # Cache adoption/trim runs independently. Recheck and touch without an
        # intervening await so a file evicted while we waited on an older
        # display POST becomes PREPARING, never an uncaught KeyError.
        if name in state.speech:
            seconds = touch_speech(state, name)
            cache_still_ready = True
    if not cache_still_ready:
        if state.view != "network":
            note_manual_selection(state)
        request_narration(state, link, name)
        await draw_readout(bb, state, NARRATION_PREPARING)
        return
    if state.narration_priority == link.key:
        state.narration_priority = None
    play_generation = state.narration_request_counter
    started_view = state.view
    state.speaking = True
    took_hold = state.narration_focus is None
    state.narration_focus = link.key
    change_event = state.narration_changed
    state.dirty.set()
    try:
        if (feed_freshness(state) != "fresh"
                or not any(live.key == link.key for live in state.links)):
            return
        state.audio_generation += 1
        audio_play_generation = state.audio_generation
        try:
            async with state.audio_io:
                # A navigation STOP may have claimed a newer generation while
                # this task waited behind an older device request.
                if (state.audio_generation != audio_play_generation
                        or state.audio_stop_pending):
                    return
                await asyncio.wait_for(
                    bb.audio_play(application_name=APP_NAME, path=name),
                    INTERACTIVE_IO_TIMEOUT_S)
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) == 404:
                # The API uses 404 for both absent and unplayable audio. Never
                # overwrite or re-adopt the deterministic path: quarantine it
                # and let the background baker upload a new immutable repair
                # generation. A late result may invalidate storage state, but
                # it must not resurrect UI intent after the user moved.
                mark_speech_unplayable(state, name)
                repair_name = speech_asset_name(state, text)
                if not narration_play_is_current(
                        state, link, play_generation, started_view):
                    return
                if state.view != "network":
                    note_manual_selection(state)
                request_narration(state, link, repair_name)
                await draw_readout(bb, state, NARRATION_PREPARING)
                return
            if _is_refusal(exc):
                # Never turn one press into surprise playback later.
                if narration_play_is_current(
                        state, link, play_generation, started_view):
                    await draw_readout(bb, state, NARRATION_BUSY)
                return
            await stop_audio_bounded(bb, state, "ambiguous PLAY")
            if narration_play_is_current(
                    state, link, play_generation, started_view):
                await draw_readout(bb, state, NARRATION_ERROR)
            logger.warning("audio PLAY failed: %s", exc)
            return
        except TimeoutError:
            # busylib may otherwise spend roughly 30 seconds across transport
            # retries. Cancellation has settled before STOP, so even a PLAY
            # whose response was lost cannot begin later under an error card.
            await stop_audio_bounded(bb, state, "timed-out PLAY")
            if narration_play_is_current(
                    state, link, play_generation, started_view):
                await draw_readout(bb, state, NARRATION_ERROR)
            logger.warning("audio PLAY exceeded %.1fs interaction bound",
                           INTERACTIVE_IO_TIMEOUT_S)
            return
        except Exception as exc:  # noqa: BLE001 - ambiguous transport result
            await stop_audio_bounded(bb, state, "ambiguous PLAY")
            if narration_play_is_current(
                    state, link, play_generation, started_view):
                await draw_readout(bb, state, NARRATION_ERROR)
            logger.warning("audio PLAY failed: %s", exc)
            return

        if (state.audio_generation != audio_play_generation
                or not narration_play_is_current(
                    state, link, play_generation, started_view)):
            # Navigation/contact loss won while PLAY was in flight. It may
            # already have been accepted, so make the newer interaction win.
            await stop_audio_bounded(bb, state, "stale PLAY")
            return

        # Network is global, while narration is about one contact. Drill down
        # only after PLAY is accepted; a cold or refused press never changes
        # views. The instant craft readout bridges the scene swap.
        if started_view == "network" and state.view == "network":
            state.narration_return_view = "network"
            state.view = "instrument"
            state.dirty.set()
            await draw_readout(bb, state, link.craft.upper())

        hold = asyncio.create_task(asyncio.sleep(seconds + 0.5))
        changed = asyncio.create_task(change_event.wait())
        try:
            done, _ = await asyncio.wait(
                (hold, changed), return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in (hold, changed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(hold, changed, return_exceptions=True)
        if changed in done:
            await stop_audio_bounded(bb, state, "stale narration")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.warning("speak failed: %s", exc)
        await draw_readout(bb, state, NARRATION_ERROR)
    finally:
        state.speaking = False
        # Narration never owns the user's real-time lock. Its orthogonal hold
        # can be cleared even after a same-craft handoff without stranding a
        # permanent focus on the new dish.
        if took_hold:
            state.narration_focus = None
        if state.narration_return_view is not None:
            if state.view == "instrument" and state.realtime_since is None:
                state.view = state.narration_return_view
            state.narration_return_view = None
        state.dirty.set()


async def prebake(bb, state: State) -> None:
    """Serially warm stable pass scripts, prioritising an explicit request."""
    while True:
        await asyncio.sleep(2)
        if not state.links or state.speaking:
            continue
        current = state.current()
        priority = state.narration_priority
        ordered = sorted(state.links, key=lambda link: (
            0 if link.key == priority else 1 if current and link.key == current.key else 2,
            link.key))
        pending: tuple[Link, str] | None = None
        for link in ordered:
            text = observe_narration(state, link)
            if text is None:
                # A requested, otherwise-ready line gets the next real feed
                # observation before we spend 20-40 seconds on something
                # unrelated. Missing names/range do not block useful work.
                if link.key == priority and narration_ready(state, link):
                    break
                continue
            name = speech_asset_name(state, text)
            request = bind_narration_request(state, link.key, name)
            if name in state.speech:
                # Pin every active frozen line ahead of inactive historical
                # entries before a new bake can make the LRU choose a victim.
                touch_speech(state, name)
                if state.narration_priority == link.key:
                    state.narration_priority = None
                finish_narration_request(state, request, NARRATION_READY)
                continue
            pending = (link, text)
            break
        if pending is None:
            if priority and priority in state.narration_texts:
                state.narration_priority = None
            continue
        link, text = pending
        name = speech_asset_name(state, text)
        request = bind_narration_request(state, link.key, name)
        try:
            result = await ensure_speech(bb, state, text)
            if result is None:
                if state.narration_priority == link.key:
                    state.narration_priority = None
                finish_narration_request(state, request, NARRATION_ERROR)
                await asyncio.sleep(30)
            else:
                if state.narration_priority == link.key:
                    state.narration_priority = None
                finish_narration_request(state, request, NARRATION_READY)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning("prebake failed: %s", exc)
            if state.narration_priority == link.key:
                state.narration_priority = None
            finish_narration_request(state, request, NARRATION_ERROR)
            await asyncio.sleep(30)


async def load_speech_cache(bb, state: State) -> None:
    """Adopt voice lines a previous run left on the device.

    Durations are unknown until something plays, so they start at 0 and the
    hold falls back to a fixed guess; the text hash still saves the synth.

    Adopted lines start **cold**, in whatever order the device lists them: use
    order cannot be recovered, because StorageListElement carries only type,
    name and size — there is no mtime to seed
    it from. So the first rotation after a restart re-establishes the LRU
    order, and until it does an adopted line may be evicted ahead of a
    freshly-baked one. That costs one re-synth, not a wrong narration.
    """
    try:
        files = (await bb.storage_list(f"/ext/user_assets/{APP_NAME}")).list
        mine = _voice_tag(VOICE)
        strangers = 0
        versions: dict[str, list[tuple[int, str, int]]] = {}
        retire: list[str] = []
        for entry in files:
            kind = getattr(getattr(entry, "type", None), "value",
                           getattr(entry, "type", None))
            if str(kind).lower() != "file":
                continue
            if not (entry.name.startswith(("voice_", "v2_"))
                    and entry.name.endswith(".snd")):
                continue
            match = VOICE_FILES.fullmatch(entry.name)
            if match is None or match.group("voice") != mine:
                # A previous narrator's work, or a name from an older scheme.
                # Either way nothing will ever ask for it again, so it would
                # sit on flash until the end of time. Anything we cannot
                # recognise as ours is reclaimable — that keeps the next
                # change of naming from stranding a cache too.
                strangers += 1
                try:
                    await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{entry.name}")
                except Exception:  # noqa: BLE001
                    pass
                continue
            size = getattr(entry, "size", 0) or 0
            identity = _speech_file_identity(entry.name)
            if identity is None:
                retire.append(entry.name)
                continue
            base, repair = identity
            if size <= 0:
                if repair < 0xff:
                    state.speech_repairs[base] = max(
                        state.speech_repairs.get(base, 0), repair + 1)
                retire.append(entry.name)
                continue
            versions.setdefault(base, []).append((repair, entry.name, size))
        for base, candidates in versions.items():
            repair, name, size = max(candidates)
            if state.speech_repairs.get(base, 0) > repair:
                # A higher generation was present but invalid. Older valid
                # bytes cannot become current again after that quarantine.
                retire.extend(candidate_name
                              for _, candidate_name, _ in candidates)
                continue
            if repair:
                state.speech_repairs[base] = max(
                    state.speech_repairs.get(base, 0), repair)
            state.speech[name] = size / 2 / 44100
            retire.extend(candidate_name
                          for _, candidate_name, _ in candidates
                          if candidate_name != name)
        for name in retire:
            try:
                await bb.storage_remove(f"/ext/user_assets/{APP_NAME}/{name}")
            except Exception:  # noqa: BLE001 - newest generation remains usable
                state.speech_retire.add(name)
            else:
                state.speech_retire.discard(name)
        if state.speech:
            logger.info("adopted %d cached lines in %s", len(state.speech), VOICE)
        if strangers:
            logger.info("dropped %d lines from a previous voice", strangers)
        await trim_speech_cache(bb, state)
    except Exception as exc:  # noqa: BLE001 - an empty cache is fine
        logger.debug("voice cache scan failed: %s", exc)
    finally:
        state.speech_cache_ready = True
        request = state.narration_request
        if (request is not None
                and request.key in state.narration_texts):
            current_name = speech_asset_name(
                state, state.narration_texts[request.key])
            if current_name != request.name:
                request = replace(request, name=current_name)
                state.narration_request = request
        if (request is not None and request.name is not None
                and request.name in state.speech):
            if state.narration_priority == request.key:
                state.narration_priority = None
            finish_narration_request(state, request, NARRATION_READY)


async def prepare_narration_cache(bb, state: State) -> None:
    """Adopt device speech first, then run the indefinite low-priority baker."""
    await load_speech_cache(bb, state)
    await prebake(bb, state)


def encoder_delta(update: dict) -> int:
    """Wheel detents. Events are nested under `input` inside each update —
    reading them off the message itself silently never fires."""
    event = (update.get("input") or {}).get("encoder_event") or {}
    try:
        return int(event.get("delta") or 0)
    except (TypeError, ValueError):
        return 0


def is_ok_press(update: dict) -> bool:
    """OK is the wheel's down-click. Proto3 omits zero-valued fields, so an
    OK PRESS legitimately arrives as an EMPTY button_event."""
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return (event.get("button") in (None, 0, "OK")
            and event.get("action") in (None, 0, "PRESS"))


def is_ok_release(update: dict) -> bool:
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return (event.get("button") in (None, 0, "OK")
            and event.get("action") in (1, "RELEASE"))


def is_start_press(update: dict) -> bool:
    inp = update.get("input") or {}
    if "button_event" not in inp:
        return False
    event = inp.get("button_event") or {}
    return (event.get("button") in (2, "START")
            and event.get("action") in (None, 0, "PRESS"))


def release_realtime(state: State) -> None:
    state.focus = None
    state.realtime_since = None
    state.rt_generation = None
    state.watch = None
    if state.view_before_lock is not None:
        state.view = state.view_before_lock
        state.view_before_lock = None


def toggle_realtime(state: State, now: float | None = None) -> bool:
    """A tap keeps its old meaning and takes the user to the distance view."""
    clear_narration_request(state)
    state.narration_return_view = None
    clear_network_focus(state)
    link = state.current()
    if link is None:
        return False
    locking = state.realtime_since is None
    if locking:
        if not link.light_s:
            return False
        state.completion_pending = None
        state.completion_link = None
        state.completion_generation = None
        clear_led(state, LED_ARRIVAL)
        state.view_before_lock = state.view
        state.focus = link.key
        state.realtime_since = now if now is not None else time.time()
        state.rt_counter += 1
        state.rt_generation = state.rt_counter
        frozen = replace(link)
        state.watch = Watch(
            link=frozen,
            started_at=state.realtime_since,
            light_s=link.light_s,
            deadline=state.realtime_since + link.light_s,
            generation=state.rt_counter,
            return_view=state.view,
            live_key=link.key,
        )
        state.view = "distance"
        request_led(state, LED_LOCKED)
    else:
        release_realtime(state)
        request_led(state, LED_RELEASED)
    state.dirty.set()
    return locking


def toggle_view(state: State) -> str:
    clear_narration_request(state)
    state.narration_return_view = None
    clear_network_focus(state)
    if state.realtime_since is not None:
        # Preserve the established watch gesture: hold compares the literal
        # distance journey with its selected-link instrument and back.
        state.view = "distance" if state.view == "instrument" else "instrument"
    else:
        try:
            index = VIEW_ORDER.index(state.view)
        except ValueError:
            index = 0
        state.view = VIEW_ORDER[(index + 1) % len(VIEW_ORDER)]
    state.dirty.set()
    return state.view


async def _fire_ok_hold(bb, state: State, pressed_at: float) -> None:
    try:
        await asyncio.sleep(OK_HOLD_S)
        if state.ok_down_at != pressed_at:
            return
        state.ok_hold_fired = True
        view = toggle_view(state)
        await draw_readout(bb, state, view.upper())
        logger.info("view: %s", view)
    except asyncio.CancelledError:
        raise


def cancel_ok_hold(state: State) -> None:
    if state.ok_hold_task is not None:
        state.ok_hold_task.cancel()
    state.ok_hold_task = None
    state.ok_down_at = None
    state.ok_hold_fired = False


def note_manual_selection(state: State, now: float | None = None) -> None:
    """Keep a deliberately chosen contact visible long enough to observe it."""
    current = now if now is not None else asyncio.get_running_loop().time()
    state.manual_until = current + MANUAL_DWELL_S


async def cancel_narration(bb, state: State) -> None:
    """A deliberate wheel move interrupts audio and releases its private hold."""
    active = list(state.speech_tasks)
    if not active and not state.speaking and not state.audio_stop_pending:
        return
    for task in active:
        task.cancel()
    if active:
        await asyncio.gather(*active, return_exceptions=True)
    # STOP follows cancellation/gather so an in-flight PLAY cannot commit
    # after the stop and leave the old craft talking under the new picker.
    await stop_audio_bounded(bb, state, "navigation")
    state.speaking = False
    state.narration_focus = None
    state.dirty.set()


async def listen_input(bb, state: State) -> None:
    """Wheel cycles links; tap follows light time; hold switches view."""
    backoff = 1.0
    while True:
        try:
            connected = asyncio.get_running_loop().time()
            async for message in bb.stream_status_ws():
                backoff = 1.0
                if not isinstance(message, dict):
                    continue
                moved = False
                for update in message.get("updates", []):
                    delta = encoder_delta(update)
                    if delta:
                        note_manual_selection(state)
                        clear_narration_request(state)
                        # The first detent closes any prior Focus Lens.  A
                        # new one is armed only after the picker has rested.
                        clear_network_focus(state)
                        cancel_ok_hold(state)
                        # Gate the renderer before audio_stop or any other
                        # await can let the main loop commit under the picker.
                        state.picking = True
                        state.pick_at = asyncio.get_running_loop().time()
                        state.completion_pending = None
                        clear_led(state, LED_ARRIVAL)
                        await cancel_narration(bb, state)
                        state.enc_accum += delta
                        while abs(state.enc_accum) >= DETENT_COUNTS:
                            step = 1 if state.enc_accum > 0 else -1
                            state.enc_accum -= DETENT_COUNTS * step
                            release_realtime(state)  # scrolling releases a lock
                            if state.links:
                                state.cursor = (state.cursor + step) % len(state.links)
                            moved = True
                        continue
                    if is_ok_press(update):
                        if state.ok_down_at is None:
                            pressed_at = asyncio.get_running_loop().time()
                            state.ok_down_at = pressed_at
                            state.ok_hold_fired = False
                            state.ok_hold_task = asyncio.create_task(
                                _fire_ok_hold(bb, state, pressed_at))
                    elif is_ok_release(update):
                        hold_task = state.ok_hold_task
                        if hold_task is not None:
                            hold_task.cancel()
                        if state.ok_down_at is not None and not state.ok_hold_fired:
                            selected = state.current()
                            was_locked = state.realtime_since is not None
                            from_network = state.view == "network"
                            locking = toggle_realtime(state)
                            link = state.current()
                            if not was_locked and not locking:
                                await draw_readout(
                                    bb, state,
                                    "NO RANGE" if selected else "NO LINK")
                            elif locking and from_network and selected is not None:
                                # The global board's action target is made
                                # explicit before its Distance asset arrives.
                                await draw_readout(
                                    bb, state, selected.craft.upper())
                            logger.info("focus: %s", "real-time on " + link.key
                                        if locking and link else "auto-rotate")
                        state.ok_down_at = None
                        state.ok_hold_fired = False
                        state.ok_hold_task = None
                    elif is_start_press(update):
                        if state.speaking or state.speech_tasks:
                            await draw_readout(bb, state, NARRATION_BUSY)
                            continue
                        fresh = feed_freshness(state)
                        if fresh != "fresh":
                            await draw_readout(
                                bb, state, feed_status_label(fresh))
                            continue
                        link = narration_target_link(state)
                        if link is None:
                            await draw_readout(
                                bb, state,
                                "OFF AIR" if state.watch is not None
                                else "NO LINK")
                            continue
                        task = asyncio.create_task(speak(bb, state, link))
                        state.speech_tasks.add(task)
                        task.add_done_callback(state.speech_tasks.discard)
                if moved:
                    # Reveal-on-stop: the picker tracks the wheel with no lag,
                    # and the scene follows only once you rest. Rendering per
                    # detent would stall on an 80 KB upload. Drawn once per
                    # message, not once per detent, so a fast spin coalesces.
                    await draw_picker(bb, state)
            cancel_ok_hold(state)
            # A clean close ends the loop without raising; back off on a
            # short-lived session or this becomes a reconnect hot loop.
            if asyncio.get_running_loop().time() - connected < 5.0:
                logger.warning("input stream closed immediately, backing off")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                logger.info("input stream closed cleanly, reconnecting")
                backoff = 1.0
        except asyncio.CancelledError:
            cancel_ok_hold(state)
            raise
        except Exception as exc:  # noqa: BLE001
            cancel_ok_hold(state)
            logger.warning("input stream dropped (%s), retrying", exc)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


async def rotate(state: State) -> None:
    while True:
        await asyncio.sleep(ROTATE_S)
        if (state.view != "network"
                and not state.focus and not state.narration_focus
                and state.narration_request is None
                and state.narration_notice is None
                and not state.completion_pending
                and not state.picking
                and asyncio.get_running_loop().time() >= state.manual_until
                and len(state.links) > 1):
            state.cursor = (state.cursor + 1) % len(state.links)
            state.dirty.set()


async def await_or_stop(awaitable, stop: asyncio.Event):
    """Cancel one startup operation as soon as systemd asks us to stop."""
    operation = asyncio.ensure_future(awaitable)
    shutdown = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, shutdown), return_when=asyncio.FIRST_COMPLETED)
        if shutdown in done:
            operation.cancel()
            settled, _ = await asyncio.wait((operation,), timeout=SHUTDOWN_TIMEOUT_S)
            if operation in settled:
                await asyncio.gather(operation, return_exceptions=True)
            return None
        return await operation
    finally:
        shutdown.cancel()
        if not operation.done():
            operation.cancel()
        settled, _ = await asyncio.wait(
            (shutdown, operation), timeout=SHUTDOWN_TIMEOUT_S)
        if settled:
            await asyncio.gather(*settled, return_exceptions=True)


async def run(once: bool) -> None:
    state = State()
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Handlers first, then connect: connect_with_retry waits on `stop`, so a
    # SIGTERM while the bar is still absent has to be able to set it.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    bb = await connect_with_retry(aconnect, stop, log=logger)
    try:

        if once:
            # --once may run beside the Pi's copy; the sweep is app-scoped and
            # cannot tell whose files these are.
            logger.info("--once: skipping the asset sweep, another instance may be live")
        else:
            await await_or_stop(sweep_stale_assets(bb), stop)
            if stop.is_set():
                return

        load_ranges(state)
        load_history(state)
        # Feed acquisition is the only startup work that gates a live scene.
        # Config names and a potentially large speech-cache scan enrich it in
        # parallel; a slow cosmetic endpoint must not leave the strip blank.
        tasks = [asyncio.create_task(poll_names(state)),
                 asyncio.create_task(poll_feed(state)),
                 asyncio.create_task(poll_ranges(state))]
        if not once:
            tasks += [asyncio.create_task(listen_input(bb, state)),
                      asyncio.create_task(rotate(state)),
                      asyncio.create_task(prepare_narration_cache(bb, state))]

        for _ in range(60):                          # wait for the first feed
            if state.feed_seeded or stop.is_set():
                break
            await asyncio.sleep(0.5)
        if stop.is_set():
            return
        if not state.feed_seeded:
            logger.warning("no active links in the feed yet")
        elif not state.links:
            logger.info("the source currently reports no active links")

        next_draw = 0.0
        draw_retry_at = 0.0
        retry_intent: tuple | None = None
        once_scene_ready = False
        pushed_once = False
        once_timeout = 15
        event_warm_started = False
        while not stop.is_set():
            now = loop.time()
            if (state.network_focus_until > 0
                    and not math.isinf(state.network_focus_until)
                    and now >= state.network_focus_until):
                clear_network_focus(state)
                state.dirty.set()
            if (state.audio_stop_pending
                    and now >= state.audio_stop_retry_at):
                stop_generation = state.audio_stop_generation
                await await_or_stop(
                    stop_audio_bounded(
                        bb, state, "deferred", stop_generation), stop)
                if stop.is_set():
                    break
            if (state.dirty.is_set() and retry_intent is not None
                    and scene_intent_token(state) != retry_intent):
                # A rejected old scene must not make a new wheel/tap/hold feel
                # dead. Give genuinely new intent one immediate attempt.
                draw_retry_at = 0.0
                retry_intent = None
            new_freshness = feed_freshness(state)
            if new_freshness != state.freshness:
                old_freshness, state.freshness = state.freshness, new_freshness
                if (new_freshness != "fresh"
                        and state.network_focus_key is not None):
                    # Source truth outranks a semantic-zoom dwell. Return to
                    # ambient Network so delayed/stale is explicit immediately;
                    # never restart a frozen Focus asset with old geometry.
                    clear_network_focus(state)
                if state.speaking and new_freshness != "fresh":
                    note_narration_change(state)
                if (new_freshness != "fresh"
                        and (state.narration_request is not None
                             or state.narration_notice is not None)):
                    clear_narration_request(state)
                if new_freshness == "stale":
                    queue_events(state, [{"event": "stale", "t": time.time()}])
                elif (new_freshness == "fresh"
                      and old_freshness in {"delayed", "stale"}):
                    queue_events(state, [{"event": "recovered", "t": time.time()}])
                state.dirty.set()
            watched = state.current()
            if watched is not None:
                complete_watch_if_due(state, watched, time.time())
            if state.picking and now - state.pick_at >= PICK_REST_S:
                # The wheel has settled. Retire the pop-up and commit.
                commit_picker_selection(state, now)
                await await_or_stop(draw_picker(bb, state, timeout=1), stop)
                if stop.is_set():
                    break
                state.dirty.set()
            due = now >= next_draw
            advance_network_page_if_due(state, due)
            if (not state.picking and now >= draw_retry_at
                    and (due or state.dirty.is_set())):
                # An elapsed operation backoff is consumed even if the scene
                # pixels are unchanged; the live lease may be what needs retry.
                draw_retry_at = 0.0
                retry_intent = None
                state.dirty.clear()
                link = state.current()
                if link is not None:
                    rendered_at = datetime.now(timezone.utc)
                    signature = scene_signature(state, link, rendered_at)
                    needs_draw = scene_needs_draw(state, signature, due)
                    intended: tuple | None = signature if needs_draw else None
                    try:
                        if intended is not None:
                            accepted = await await_or_stop(
                                push_scene(
                                    bb, state, link, intended,
                                    rendered_at=rendered_at), stop)
                            if stop.is_set():
                                break
                            if accepted:
                                once_scene_ready = True
                                once_timeout = scene_element_timeout(state, link)
                                if state.status_up:
                                    await await_or_stop(
                                        draw_feed_status(bb, state, timeout=1), stop)
                                    if stop.is_set():
                                        break
                                next_draw = loop.time() + scene_refresh_s(state, link)
                                draw_retry_at = 0.0
                    except exceptions.BusyBarAPIError as exc:
                        if _is_refusal(exc):
                            logger.debug("yielding to a higher-priority app")
                            draw_retry_at = now + 30
                        elif _is_asset_path_failure(exc):
                            logger.warning("scene asset vanished; rebuilding")
                            draw_retry_at = now + 1
                        else:
                            logger.warning("draw rejected: %s", exc)
                            draw_retry_at = now + 30
                        retry_intent = scene_intent_token(state)
                        state.dirty.set()
                    except Exception as exc:  # noqa: BLE001 - offline is a state
                        logger.warning("draw failed: %s", exc)
                        draw_retry_at = now + 30
                        retry_intent = scene_intent_token(state)
                        state.dirty.set()
                else:
                    try:
                        retired = await await_or_stop(retire_countdown(bb, state), stop)
                        if stop.is_set():
                            break
                        if not retired:
                            raise RuntimeError("countdown retirement was refused")
                        await await_or_stop(draw_feed_status(bb, state), stop)
                        if stop.is_set():
                            break
                        next_draw = loop.time() + 10
                        draw_retry_at = 0.0
                        pushed_once = True
                        once_timeout = 15
                    except exceptions.BusyBarAPIError as exc:
                        if _is_refusal(exc):
                            draw_retry_at = now + 30
                        else:
                            logger.warning("status draw rejected: %s", exc)
                            draw_retry_at = now + 30
                        retry_intent = scene_intent_token(state)
                        state.dirty.set()
                    except Exception as exc:  # noqa: BLE001
                        logger.warning("status draw failed: %s", exc)
                        draw_retry_at = now + 30
                        retry_intent = scene_intent_token(state)
                        state.dirty.set()
                if draw_retry_at == 0.0:
                    lease_ok = await await_or_stop(
                        sync_live_lease(bb, state, new_freshness), stop)
                    if stop.is_set():
                        break
                    if not lease_ok:
                        state.dirty.set()
                        draw_retry_at = now + 30
                        retry_intent = scene_intent_token(state)
                    elif once_scene_ready:
                        pushed_once = True
            if (not once and not event_warm_started
                    and (once_scene_ready or pushed_once)):
                # The current state reaches the LEDs before background asset
                # work starts. Once warm, finite generic events only select a
                # resident path; a rare data-specific Three Skies handoff is
                # separately built/cached at event time, never on user input.
                event_warm_started = True
                warm_task = start_event_asset_warm(bb, state)
                tasks.append(warm_task)
            if (state.narration_notice is not None
                    and now >= state.narration_notice_retry_at
                    and now >= state.next_event_at):
                notice = state.narration_notice
                shown = await await_or_stop(
                    show_narration_notice(bb, state), stop)
                if stop.is_set():
                    break
                if shown or state.narration_notice is not notice:
                    state.narration_notice_retry_at = 0.0
                    if shown:
                        # Give the explicit user acknowledgement its native
                        # three-second dwell before queued feed events resume.
                        state.next_event_at = loop.time() + 4.0
                else:
                    # Picker/audio/event ownership is ordinary. Keep the exact
                    # terminal notice, but back off repeated device refusals:
                    # a BUSY session can own the bar for hours.
                    delay = narration_notice_backoff_s(
                        state.narration_notice_failures)
                    state.narration_notice_retry_at = loop.time() + delay
            if now >= state.next_event_at and state.event_queue:
                shown = await await_or_stop(show_next_event(bb, state), stop)
                if stop.is_set():
                    break
                state.next_event_at = loop.time() + (
                    EVENT_TIMEOUT_S + 1 if shown else 5)
            if once and pushed_once:
                logger.info("pushed one loop; it self-clears in %ds",
                            once_timeout)
                break
            try:
                # Tighter while the wheel is in play: at a 1s tick the picked
                # scene could take a whole extra second to appear after you
                # stopped turning, which reads as the wheel being slow.
                await asyncio.wait_for(
                    stop.wait(), timeout=0.15 if state.picking else 1.0)
            except asyncio.TimeoutError:
                pass
    finally:
        if state.ok_hold_task is not None:
            state.ok_hold_task.cancel()
        extra_tasks = [task for task in (
            state.event_warm_task, state.network_warm_task)
            if task is not None and task not in tasks]
        for t in [*tasks, *extra_tasks]:
            t.cancel()
        # Cancel every producer before taking the snapshot; no normal input
        # task can create a fresh narration after shutdown claims ownership.
        speech_tasks = list(state.speech_tasks)
        pending_speech = await shutdown_audio_bounded(
            bb, state, speech_tasks)
        settling = [*tasks, *extra_tasks,
                    *((state.ok_hold_task,) if state.ok_hold_task else ())]
        pending: set[asyncio.Task] = set()
        if settling:
            done, pending = await asyncio.wait(
                settling, timeout=SHUTDOWN_TIMEOUT_S)
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        pending.update(pending_speech)
        if pending:
            logger.warning("%d task(s) missed the shutdown deadline", len(pending))
        if not once:
            try:
                await asyncio.wait_for(
                    bb.display_clear(application_name=APP_NAME),
                    SHUTDOWN_TIMEOUT_S)
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(bb.aclose(), SHUTDOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="render and report without touching the device")
    parser.add_argument("--once", action="store_true",
                        help="push a single loop and exit")
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )
    configure_runtime()

    if args.dry_run:
        state = State()
        # Names too: without them the narration preview reads 'MRO' where the
        # real thing says 'Mars Reconnaissance Orbiter', which makes a dry run
        # a poor rehearsal of the one output it exists to rehearse.
        asyncio.run(fetch_names(state))
        with httpx.Client(headers=UA, timeout=20) as client:
            links = parse_feed(client.get(DSN_XML).content)
        print("\n".join(describe_links(links, state.names, state.dish_types)))
        return

    asyncio.run(run(args.once))


if __name__ == "__main__":
    main()

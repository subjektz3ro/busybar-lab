"""DSN limits."""

from __future__ import annotations

import logging

APP_NAME = "dsn"

PRIORITY = 30  # ambient foreground, same tier as skystrip

W, H = 72, 16

UA = {"User-Agent": "dsn (busybar hobby project)"}

DSN_XML = "https://eyes.nasa.gov/dsn/data/dsn.xml"

CONFIG_XML = "https://eyes.nasa.gov/dsn/config.xml"

HORIZONS = "https://ssd.jpl.nasa.gov/api/horizons.api"

AU_LIGHT_S = 499.004784  # seconds of light per astronomical unit

NARRATION_STARTING = "STARTING UP"

NARRATION_PREPARING = "PREPARING..."

NARRATION_READY = "PRESS START"

NARRATION_BUSY = "AUDIO BUSY"

NARRATION_ERROR = "AUDIO ERROR"

NETWORK_FOCUS_STYLES = frozenset({"dishes", "skies"})

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

RANGE_TTL_S = 6 * 3600  # maximum, for genuinely deep-space targets

RANGE_RETRY_S = 60  # failure backoff is not a successful zero range

RANGE_UNAVAILABLE_RETRY_S = RANGE_TTL_S  # Horizons has no record for this id

RANGE_CACHE_VERSION = 1

SHUTDOWN_TIMEOUT_S = 2.0

# A released task needs a moment to unwind its own await points. This is
# that moment, and it is bounded so a genuinely stuck task still gets
# reported rather than waited on.
SHUTDOWN_SETTLE_S = 0.25

INTERACTIVE_IO_TIMEOUT_S = 1.0

ELEMENT_TIMEOUT_S = 180  # self-clears if we stop pushing

REALTIME_ELEMENT_TIMEOUT_S = 360  # exceeds the 300s outer-planet redraw cap

REDRAW_S = 60

SCENE_RENEW_TARGET_S = 120

ANIM_FRAMES = 40

ANIM_FPS = 5

LOOP_S = ANIM_FRAMES / ANIM_FPS  # 8s, FIXED: text scroll must not depend

INSTRUMENT_FRAMES = 40

INSTRUMENT_FPS = 5

INSTRUMENT_LOOP_S = INSTRUMENT_FRAMES / INSTRUMENT_FPS

SCROLL_SPEED_PX_S = 20.0  # matches the established eight-second Distance feel

# A generated asset is built eagerly as PIL frames before it can be uploaded.
# Keep a hostile source value from turning one native scene into unbounded RAM
# and upload time.  Forty-eight seconds is long enough to expose every column
# of the longest accepted source label at the panel-readable scroll ceiling.
MAX_ANIMATION_FRAMES = 240

EVENT_TIMEOUT_S = 4

EVENT_QUEUE_MAX = 4

EVENT_MAX_AGE_S = 120

EVENT_FRAMES = 20

EVENT_FPS = 5

EVENT_EFFECTS = ("acquire", "loss", "handoff", "split", "merge", "array", "unarray")

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

RT_PACKETS = 12  # marks in a full chain, when locked to real time

# An 8-second loop can only tell the truth about a crossing it can fit. Below
# this the device animates a locked link at its real speed; above it, the
# chain is placed from the wall clock and re-pushed as it creeps.
RT_SEAMLESS_MAX_S = 120.0

DETENT_COUNTS = 1  # verified in skystrip: one detent IS one count.

# This was 4, which quietly demanded four physical
# clicks per step and made the wheel feel dead.
PICK_REST_S = 0.6  # stillness before the picked signal commits

MANUAL_DWELL_S = 120.0  # a chosen instrument remains long enough to observe

# The API's status-LED request can only blink one colour once on a draw; it has
# no addressing, patterns, or sustained-colour mode. So it
# is wired to genuine EVENTS only. Attaching it to the ordinary redraw would
# turn it into a metronome every time the scene refreshes.
LED_ARRIVAL = "#FFF4D0FF"  # a real-time light-crossing watch completed

LED_LOCKED = "#FFB300FF"  # real time engaged

LED_RELEASED = "#3355AAFF"  # back to browsing

logger = logging.getLogger(APP_NAME)

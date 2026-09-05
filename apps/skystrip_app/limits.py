"""Skystrip limits."""

from __future__ import annotations

import logging

from apps.skystrip_app import lightning as _lightning

logger = logging.getLogger("skystrip")

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

# Optional: pin an NWS station; blank = auto-discover from coordinates.
# NWS enhancement is available only when /points resolves the coordinate;
# elsewhere Open-Meteo carries modeled weather and the NWS layers fail soft.
FORECAST_INTERVAL_S = 1800

# NWS asks callers to identify themselves; set SKYSTRIP_CONTACT to an
# email/URL you own, or leave blank for an anonymous-but-named UA.
NEUTRAL_UA = {"User-Agent": "skystrip (hobby project)"}

STRIKE_RADIUS_KM = 60  # beyond this you can't see it from a city

STRIKE_NEAR_KM = 25  # inside this the storm is genuinely overhead

FAR_FLASH_GAP_S = 25  # distant flicker is occasional, not a strobe

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

FLASH_EVENT_TTL_S = _lightning.LIGHTNING_SOURCE_MAX_AGE_S

# The decoder and the eventual flash queue share this source-age contract, so
# a frame accepted at ingestion cannot outlive the runtime event lease.
FLASH_ANIM_FPS = 12

FLASH_ELEMENT_TIMEOUT_S = 2  # firmware leases are whole seconds

FLASH_ASSET_RETIRE_GRACE_S = 1.0  # let firmware release the expired file handle

AMBIENT_PERIOD_S = 20  # how often the top-strip mood re-evaluates

AMBIENT_LEVEL = 0.35  # ambient brightness scale (the strip is bright)

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

SCRUB_STEP_S = 1800  # one wheel detent = 30 minutes

SCRUB_MAX_S = 86400  # the Time Machine reaches a day each way

SCRUB_SNAP_S = 45  # idle this long and it drifts back to now

REVEAL_REST_S = 1.5  # stillness before the scene commits — longer than

# a deliberate click cadence, shorter than patience
TIMELINE_SLOTS = 97  # 48h of half-hour marks, yesterday to tomorrow

TIMELINE_STEP_S = 1800

ENC_COUNTS_PER_DETENT = 1  # verified: one detent = one count

BAKE_CHECK_S = 60  # how often to see if the report's words changed

ANIM_FRAMES = 40

ANIM_FPS = 5  # 8-second seamless loop, played by the device itself

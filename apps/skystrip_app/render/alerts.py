"""Skystrip render / alerts."""

from __future__ import annotations

import hashlib
from datetime import datetime

from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app.render import primitives as _render_primitives
from busybar_dev.pixel_text import (
    device_text,
    draw_marquee,
    marquee_frame_count,
    max_text_width,
    text_width,
)
from busybar_dev.weather_alerts import Alert


def _alert_deadline(alert: Alert) -> datetime:
    return min(alert.expires, alert.ends) if alert.ends is not None else alert.expires


def alert_expiry_label(alert: Alert, *, now: datetime | None = None) -> str:
    """A complete local deadline for the second row of the alert card."""
    local_now = (now or datetime.now(_settings.TZ)).astimezone(_settings.TZ)
    deadline = _alert_deadline(alert).astimezone(_settings.TZ)
    hour = deadline.hour % 12 or 12
    ampm = "AM" if deadline.hour < 12 else "PM"
    day = (
        ""
        if deadline.date() == local_now.date()
        else f" {deadline.month}/{deadline.day}"
    )
    return device_text(f"UNTIL{day} {hour}:{deadline.minute:02d} {ampm}")


# The widest event name this card can scroll at ALERT_SCROLL_SPEED_PX_S
# without the frame cap binding. Past it, draw_marquee's per-frame step grows
# and the label runs faster than the declared readability ceiling.
ALERT_EVENT_MAX_PX = max_text_width(
    fps=_limits.ALERT_ANIM_FPS, speed_px_s=_limits.ALERT_SCROLL_SPEED_PX_S
)


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
    _limits.logger.warning(
        "alert event name is too long to present (%d chars); showing %r instead",
        len(alert.event),
        f"WEATHER {kind}",
    )
    return f"WEATHER {kind}"


def alert_animation_frames(alert: Alert) -> list[Image.Image]:
    """Host-rendered, frame-provable alert marquee for the full 72×16 panel."""
    event = presentable_event(alert)
    expiry = alert_expiry_label(alert)
    box = (2, _limits.W - 3)
    box_width = box[1] - box[0] + 1
    frame_count = max(
        marquee_frame_count(
            event,
            box_width,
            fps=_limits.ALERT_ANIM_FPS,
            speed_px_s=_limits.ALERT_SCROLL_SPEED_PX_S,
        ),
        marquee_frame_count(
            expiry,
            box_width,
            fps=_limits.ALERT_ANIM_FPS,
            speed_px_s=_limits.ALERT_SCROLL_SPEED_PX_S,
        ),
    )
    frames: list[Image.Image] = []
    for index in range(frame_count):
        image = Image.new("RGB", (_limits.W, _limits.H), (0, 0, 0))
        pulse = 255 if (index // 3) % 2 == 0 else 120
        pixels = _render_primitives._rgb_pixels(image)
        # Sparse high-contrast brackets: the physical panel reads filled
        # backgrounds as haze, while these edges stay unmistakably urgent.
        for y in (0, 1, 5, 6, 8, 9, 13, 14):
            pixels[0, y] = (pulse, 18, 12)
            pixels[_limits.W - 1, y] = (pulse, 18, 12)
        for x in range(3, _limits.W - 3, 6):
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
    value = "\x1f".join(
        (
            alert.identifier,
            alert.event,
            alert.severity,
            _alert_deadline(alert).isoformat(),
        )
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]

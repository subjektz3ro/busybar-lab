"""Skystrip device / ambient."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

from astral.sun import elevation
from busylib import types

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art
from apps.skystrip_app.render import primitives as _render_primitives


# --- device side ----------------------------------------------------------
def _weather_led(wx: _weather.WeatherState) -> str | None:
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
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        led_notification_color=color,
        elements=[
            types.RectangleElement(
                id="ledping",
                type="rectangle",
                x=0,
                y=0,
                width=1,
                height=1,
                fill="solid",
                fill_colors=["#00000000"],
                border_width=0,
                display=types.DisplayName.FRONT,
                timeout=1,
            )
        ],
    )


def _ambient_mood(state: _model.SkyState) -> tuple[int, int, int]:
    """The top strip breathes the sky's current mood: sky color by sun
    position, weather-tinted, amber while time-traveling, dark during
    alarms so the red blinks own the strip."""
    if state.weather.severe and not state.alert_acked:
        return (0, 0, 0)
    if state.scrub_slot is not None:
        return _render_primitives._rgb_int(
            c * _limits.AMBIENT_LEVEL for c in (224, 160, 70)
        )
    now = datetime.now(timezone.utc)
    elev = elevation(_settings.OBSERVER, now)
    wx = state.weather
    horizon, zenith = _render_primitives._sky_colors(elev)
    c = _render_primitives._lerp_rgb(zenith, horizon, 0.35)
    if wx.stormy:
        c = _render_primitives._lerp_rgb(c, _render_art.STORM_ZENITH, 0.85)
    elif wx.snow:
        c = _render_primitives._lerp_rgb(c, (170, 175, 185), 0.6)
    elif wx.rain:
        c = _render_primitives._lerp_rgb(c, (30, 45, 80), 0.5)
    elif wx.cloud_frac > 0.3:
        lum = 0.3 * c[0] + 0.59 * c[1] + 0.11 * c[2]
        c = _render_primitives._lerp_rgb(c, (lum, lum, lum), 0.5 * wx.cloud_frac)
    scene = state.scene
    if scene == "skyline":
        c = _render_primitives._lerp_rgb(c, (255, 190, 90), 0.15)  # city glow
    elif scene == "lakefront":
        c = _render_primitives._lerp_rgb(c, (40, 120, 130), 0.20)  # lake teal
    elif scene == "forest":
        c = _render_primitives._lerp_rgb(c, (46, 110, 44), 0.22)  # deep woods green
    elif scene == "grove":
        c = _render_primitives._lerp_rgb(c, (96, 120, 36), 0.20)  # broadleaf green-gold
    elif scene == "backroads":
        c = _render_primitives._lerp_rgb(
            c, (120, 104, 76), 0.15
        )  # headlights on asphalt
    return (
        max(0, min(255, int(c[0] * _limits.AMBIENT_LEVEL))),
        max(0, min(255, int(c[1] * _limits.AMBIENT_LEVEL))),
        max(0, min(255, int(c[2] * _limits.AMBIENT_LEVEL))),
    )


async def ambient_lights(bb, state: _model.SkyState) -> None:
    """Steady mood color on the top strip via the firmware CLI (telnet
    over the USB link) — the StaticColor preset HTTP never exposed."""
    last = None
    warned = False
    while True:
        try:
            r, g, b = _ambient_mood(state)
            q = (r // 4, g // 4, b // 4)  # quantize: only send real changes
            if q != last:
                await bb.usb.send_command("status_lights", str(r), str(g), str(b))
                last = q
                warned = False
                _limits.logger.info("ambient: (%d,%d,%d)", r, g, b)
            await asyncio.sleep(_limits.AMBIENT_PERIOD_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - mood is best-effort
            if not warned:
                # The firmware CLI only answers on the USB interface —
                # over Wi-Fi the strip stays dark. Alarm blinks are HTTP
                # and unaffected.
                _limits.logger.info(
                    "ambient: CLI unreachable (USB-only feature); standing down: %s",
                    exc,
                )
                warned = True
            last = None
            await asyncio.sleep(300)

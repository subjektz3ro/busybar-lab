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
from busybar_dev.config import describe_exception


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


# The firmware's own vocabulary for GET /api/transport (OpenAPI 27.x: usb,
# wifi). Only a value we know means "not the USB link" stands the strip down
# without a probe; anything unfamiliar is probed, so a renamed interface can
# never silently kill the one transport where the strip works.
_NOT_USB = frozenset({"wifi", "ble", "bluetooth"})


def _api_major_minor(version: str) -> tuple[int, int] | None:
    parts = version.split(".")
    try:
        return int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return None


async def _transport_type(bb) -> str | None:
    """The bar's own account of how we reached it, or None when it cannot say.

    busylib declares the OpenAPI version each helper needs but does not
    guard the call; asking a bar that predates the endpoint would only add
    a 404 at ERROR to the log this feature is trying to keep quiet.
    """
    query = getattr(bb, "transport", None)
    if query is None:
        return None
    needed = (getattr(query, "__busy_openapi__", None) or {}).get("version")
    device = getattr(bb, "device_api_version", None)
    if isinstance(needed, str) and isinstance(device, str):
        floor, have = _api_major_minor(needed), _api_major_minor(device)
        if floor is not None and have is not None and have < floor:
            return None
    try:
        info = await query()
    except Exception:  # noqa: BLE001 - an older client or firmware; probe instead
        return None
    kind = getattr(info, "type", None)
    return kind.lower() if isinstance(kind, str) else None


def _stand_down(reason: str) -> None:
    _limits.logger.info(
        "ambient: the status strip is a USB-only CLI feature; standing down "
        "for this run (%s)",
        reason,
    )


async def ambient_lights(bb, state: _model.SkyState) -> None:
    """Steady mood color on the top strip via the firmware CLI (telnet
    over the USB link) — the StaticColor preset HTTP never exposed."""
    transport = await _transport_type(bb)
    if transport in _NOT_USB:
        _stand_down(f"bar reached over {transport}")
        return
    usb = transport == "usb"
    last = None
    failed: str | None = None
    while True:
        try:
            r, g, b = _ambient_mood(state)
            q = (r // 4, g // 4, b // 4)  # quantize: only send real changes
            if q != last:
                await bb.usb.send_command("status_lights", str(r), str(g), str(b))
                if failed is not None:
                    _limits.logger.info("ambient: strip command working again")
                    failed = None
                state.ambient_strip = True
                last = q
                _limits.logger.info("ambient: (%d,%d,%d)", r, g, b)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - mood is best-effort
            reason = describe_exception(exc)
            if not usb:
                # The bar could not tell us its transport and the CLI refused
                # the first command: over Wi-Fi the firmware resets the
                # socket, and busylib logs every attempt at WARNING. Retrying
                # every five minutes put 300 of those lines a day into the
                # service log for a strip that cannot light.
                _stand_down(reason)
                return
            # On the USB link a fault is transient (a reboot, a busy telnet
            # socket); going dark for the rest of the run would be the bug.
            if reason != failed:
                _limits.logger.info(
                    "ambient: strip command failed (%s); retrying in %ds",
                    reason, _limits.AMBIENT_RETRY_S,
                )
                failed = reason
            last = None
            await asyncio.sleep(_limits.AMBIENT_RETRY_S)
            continue
        await asyncio.sleep(_limits.AMBIENT_PERIOD_S)


async def reset_strip(bb, state: _model.SkyState) -> None:
    """Hand the strip back dark at shutdown, only if we ever lit it."""
    if not state.ambient_strip:
        return
    try:
        await bb.usb.send_command("status_lights", "0", "0", "0")
    except Exception:  # noqa: BLE001 - cleanup never masks the exit
        pass

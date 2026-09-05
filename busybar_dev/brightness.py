"""Opt-out, startup-only mitigation for firmware 1.2.3 Auto washout.

Brightness is persistent device-wide state, not a renderer property. Keep
this out of connect()/aconnect() so read-only tools never change the device.
See docs/known-issues.md for physical evidence, tradeoffs and reversal.
"""

from __future__ import annotations

import asyncio
import logging
import os

from busylib import AsyncBusyBar

AFFECTED_FIRMWARE = "1.2.3"
DEFAULT_FALLBACK = 35
STARTUP_TIMEOUT_S = 5.0
READBACK_INTERVAL_S = 0.25


def _fallback(log: logging.Logger) -> int | None:
    raw = os.environ.get("BUSYBAR_AUTO_BRIGHTNESS_FALLBACK", "").strip()
    if not raw:
        return DEFAULT_FALLBACK
    if raw == "off":
        return None
    if raw.isascii() and raw.isdecimal() and len(raw) <= 3:
        value = int(raw)
        if 1 <= value <= 100:
            return value
    # Do not echo arbitrary operator input into logs.
    log.warning(
        "invalid BUSYBAR_AUTO_BRIGHTNESS_FALLBACK; leaving brightness unchanged"
    )
    return None


async def _apply(
    bb: AsyncBusyBar,
    target: int,
    stop: asyncio.Event,
    log: logging.Logger,
) -> None:
    status = await bb.status()
    if (
        stop.is_set()
        or status.firmware is None
        or status.firmware.version != AFFECTED_FIRMWARE
    ):
        return
    current = await bb.display_brightness()
    if stop.is_set() or current.value != "auto":
        return
    await bb.display_brightness_set(target)
    # The setting is asynchronous. An accepted POST does not prove readback
    # has caught up; poll without repeating the write or fighting the user.
    while True:
        current = await bb.display_brightness()
        if current.value == str(target):
            log.warning(
                "firmware 1.2.3 Auto brightness workaround: fixed %d%% verified; "
                "ambient adjustment is disabled (see docs/known-issues.md)",
                target,
            )
            return
        if current.value not in (None, "auto"):
            log.warning(
                "brightness changed during workaround; leaving the new setting alone"
            )
            return
        await asyncio.sleep(READBACK_INTERVAL_S)


async def apply_brightness_workaround(
    bb: AsyncBusyBar,
    stop: asyncio.Event,
    *,
    log: logging.Logger,
) -> None:
    """Check once before drawing; preserve manual levels and unknown firmware.

    Cancel device I/O on shutdown or a bounded startup deadline. This helper
    does not repeat a failed setting call; busylib owns transport retries.
    A lost response may have committed the setting. Warn without exposing
    transport URLs/credentials, then let the app continue.
    """
    target = _fallback(log)
    if target is None or stop.is_set():
        return
    operation = asyncio.create_task(_apply(bb, target, stop, log))
    stopping = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, stopping),
            timeout=STARTUP_TIMEOUT_S,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if operation in done:
            operation.result()
        elif not stop.is_set():
            log.warning(
                "brightness workaround timed out; check the device setting manually"
            )
    except Exception as exc:  # noqa: BLE001 - a setting must not crash-loop the app
        log.warning(
            "brightness workaround could not be verified (%s); "
            "check the device setting manually",
            type(exc).__name__,
        )
    finally:
        for task in (operation, stopping):
            if not task.done():
                task.cancel()
        await asyncio.gather(operation, stopping, return_exceptions=True)

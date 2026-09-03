"""Shared helpers for BUSY Bar apps in this repo."""

from __future__ import annotations

import os

from busylib import AsyncBusyBar, BusyBar
from busylib.client import AsyncUsbController, UsbController

from .config import (
    load_env,
    parse_env_text,
    read_env_file,
    secret_file_warning,
)

USB_HOST = "10.0.4.20"
MDNS_HOST = "busybar.local"

# One .env parser lives in .config; these are re-exported because every caller
# in the repo, the installer and the tests reach them through busybar_dev.
__all__ = [
    "MDNS_HOST",
    "USB_HOST",
    "aconnect",
    "connect",
    "load_env",
    "parse_env_text",
    "read_env_file",
    "secret_file_warning",
]


def connect(host: str | None = None, token: str | None = None) -> BusyBar:
    """Return a connected BusyBar client.

    Resolution order: explicit ``host`` arg, ``BUSYBAR_HOST`` env var, then the
    fixed USB address ``10.0.4.20``, then ``busybar.local`` (mDNS).
    ``BUSYBAR_TOKEN`` supplies the Wi-Fi access PIN when needed; USB needs none.

    USB deliberately comes before mDNS: when the bar is on Wi-Fi with the HTTP
    API disabled (the default), ``busybar.local`` resolves to BOTH addresses and
    each request becomes address roulette — half of them hang until timeout.
    """
    load_env()
    host = host or os.environ.get("BUSYBAR_HOST")
    token = token or os.environ.get("BUSYBAR_TOKEN")
    candidates = [host] if host else [USB_HOST, MDNS_HOST]
    errors: list[str] = []
    for candidate in candidates:
        bb = BusyBar(candidate, token=token)
        try:
            bb.version()
            # busylib's lazy .usb telnet controller defaults to the USB
            # address; the CLI rides the same host as HTTP (port 23).
            bb._usb = UsbController(candidate)
            return bb
        except Exception as exc:  # noqa: BLE001 - report all hosts at the end
            errors.append(f"{candidate}: {exc}")
            bb.close()
    raise ConnectionError(
        "Could not reach the BUSY Bar. Is it plugged in over USB?\n"
        + "\n".join(errors)
    )


async def aconnect(host: str | None = None, token: str | None = None) -> AsyncBusyBar:
    """Async twin of connect() — same resolution order."""
    load_env()
    host = host or os.environ.get("BUSYBAR_HOST")
    token = token or os.environ.get("BUSYBAR_TOKEN")
    candidates = [host] if host else [USB_HOST, MDNS_HOST]
    errors: list[str] = []
    for candidate in candidates:
        bb = AsyncBusyBar(candidate, token=token)
        try:
            await bb.version()
            # Same telnet-host seeding as connect() above.
            bb._usb = AsyncUsbController(candidate)
            return bb
        except Exception as exc:  # noqa: BLE001 - report all hosts at the end
            errors.append(f"{candidate}: {exc}")
            await bb.aclose()
    raise ConnectionError(
        "Could not reach the BUSY Bar. Is it plugged in over USB?\n"
        + "\n".join(errors)
    )

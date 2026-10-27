"""The top-strip mood rides the firmware CLI, which only answers over USB.

On a Wi-Fi bar the strip stays dark whatever we do, so the feature must cost
nothing there: no command, no retry loop, no library warning every five
minutes for the life of the service.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.skystrip_app import model as sky_model
from apps.skystrip_app.device import ambient as sky_ambient

NO_TRANSPORT = object()


def _bar(transport_type, send_command=None):
    bar = SimpleNamespace(usb=SimpleNamespace(send_command=send_command or AsyncMock()))
    if transport_type is not NO_TRANSPORT:
        bar.transport = AsyncMock(return_value=SimpleNamespace(type=transport_type))
    return bar


def _notices(caplog):
    return [r for r in caplog.records if "USB-only" in r.getMessage()]


@pytest.mark.asyncio
async def test_a_wifi_bar_never_gets_a_single_cli_command(caplog):
    state = sky_model.SkyState()
    bar = _bar("wifi")

    with caplog.at_level(logging.INFO, logger="skystrip"):
        await asyncio.wait_for(sky_ambient.ambient_lights(bar, state), timeout=1)

    bar.usb.send_command.assert_not_awaited()
    assert state.ambient_strip is False
    assert [r.levelno for r in _notices(caplog)] == [logging.INFO]


@pytest.mark.parametrize("transport_type", ["usb", NO_TRANSPORT])
@pytest.mark.asyncio
async def test_a_refusing_cli_stands_the_strip_down_for_the_life_of_the_process(
    monkeypatch, caplog, transport_type,
):
    state = sky_model.SkyState()
    refused = AsyncMock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
    bar = _bar(transport_type, refused)
    sleeps: list[float] = []
    real_sleep = asyncio.sleep

    async def record_sleep(seconds):
        sleeps.append(seconds)
        await real_sleep(0)

    monkeypatch.setattr(sky_ambient.asyncio, "sleep", record_sleep)
    with caplog.at_level(logging.INFO, logger="skystrip"):
        await asyncio.wait_for(sky_ambient.ambient_lights(bar, state), timeout=1)

    assert refused.await_count == 1  # one probe, then silence
    assert state.ambient_strip is False
    assert sleeps == []  # no five-minute retry loop
    assert len(_notices(caplog)) == 1


@pytest.mark.asyncio
async def test_a_usb_bar_keeps_driving_the_mood(monkeypatch):
    state = sky_model.SkyState()
    bar = _bar("usb")

    async def stop_after_first_tick(_seconds):
        raise asyncio.CancelledError

    monkeypatch.setattr(sky_ambient.asyncio, "sleep", stop_after_first_tick)
    with pytest.raises(asyncio.CancelledError):
        await sky_ambient.ambient_lights(bar, state)

    assert bar.usb.send_command.await_count == 1
    assert bar.usb.send_command.await_args.args[0] == "status_lights"
    assert state.ambient_strip is True


@pytest.mark.asyncio
async def test_shutdown_only_resets_a_strip_that_was_ever_ours():
    lit = sky_model.SkyState()
    bar = _bar("usb")
    await sky_ambient.reset_strip(bar, lit)
    bar.usb.send_command.assert_awaited_once_with("status_lights", "0", "0", "0")

    dark = sky_model.SkyState()
    dark.ambient_strip = False
    wifi_bar = _bar("wifi")
    await sky_ambient.reset_strip(wifi_bar, dark)
    wifi_bar.usb.send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_reset_strip_swallows_a_lost_usb_link():
    state = sky_model.SkyState()
    bar = _bar("usb", AsyncMock(side_effect=OSError("USB gone")))
    await sky_ambient.reset_strip(bar, state)  # cleanup must never raise

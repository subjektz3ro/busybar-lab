"""The top-strip mood rides the firmware CLI, which only answers over USB.

On a Wi-Fi bar the strip stays dark whatever we do, so the feature must cost
nothing there: no command, no retry loop, no library warning every five
minutes for the life of the service. On the USB deployment it is the other
way round: a transient fault must not darken the strip for good, and a strip
we lit must be handed back dark at exit.
"""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app.device import ambient as sky_ambient

NO_TRANSPORT = object()


def _bar(transport_type, send_command=None, *, device_api="27.14.0", transport_fails=False):
    bar = SimpleNamespace(
        usb=SimpleNamespace(send_command=send_command or AsyncMock()),
        device_api_version=device_api,
    )
    if transport_type is not NO_TRANSPORT:
        if transport_fails:
            bar.transport = AsyncMock(side_effect=OSError("no route"))
        else:
            bar.transport = AsyncMock(return_value=SimpleNamespace(type=transport_type))
        bar.transport.__busy_openapi__ = {"version": "18.3.0"}
    return bar


def _notices(caplog):
    return [r for r in caplog.records if "USB-only" in r.getMessage()]


def _patch_sleep(monkeypatch, stop_after: int):
    """Record sleeps, yield through the real sleep, cancel after ``stop_after``."""
    real_sleep = asyncio.sleep
    sleeps: list[float] = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)
        if len(sleeps) >= stop_after:
            raise asyncio.CancelledError
        await real_sleep(0)

    monkeypatch.setattr(sky_ambient.asyncio, "sleep", fake_sleep)
    return sleeps


@pytest.mark.parametrize("reported", ["wifi", "WIFI", "ble"])
@pytest.mark.asyncio
async def test_a_bar_reached_over_the_air_never_gets_a_single_cli_command(caplog, reported):
    state = sky_model.SkyState()
    bar = _bar(reported)

    with caplog.at_level(logging.INFO, logger="skystrip"):
        await asyncio.wait_for(sky_ambient.ambient_lights(bar, state), timeout=1)

    bar.usb.send_command.assert_not_awaited()
    assert state.ambient_strip is False  # never lit, so never ours to reset
    assert [r.levelno for r in _notices(caplog)] == [logging.INFO]


@pytest.mark.parametrize("transport_type", [NO_TRANSPORT, "usb_ncm"])
@pytest.mark.asyncio
async def test_an_unknown_transport_is_probed_and_a_refusal_stands_down_for_good(
    monkeypatch, caplog, transport_type,
):
    state = sky_model.SkyState()
    refused = AsyncMock(side_effect=ConnectionResetError(104, "Connection reset by peer"))
    bar = _bar(transport_type, refused)
    sleeps = _patch_sleep(monkeypatch, stop_after=10)

    with caplog.at_level(logging.INFO, logger="skystrip"):
        await asyncio.wait_for(sky_ambient.ambient_lights(bar, state), timeout=1)

    assert refused.await_count == 1  # one probe, then silence
    assert state.ambient_strip is False
    assert sleeps == []  # no five-minute retry loop
    assert len(_notices(caplog)) == 1


@pytest.mark.asyncio
async def test_a_usb_bar_retries_a_transient_fault_instead_of_going_dark(monkeypatch, caplog):
    state = sky_model.SkyState()
    flaky = AsyncMock(side_effect=[OSError("USB device is not connected"), None])
    bar = _bar("usb", flaky)
    sleeps = _patch_sleep(monkeypatch, stop_after=2)

    with caplog.at_level(logging.INFO, logger="skystrip"):
        with pytest.raises(asyncio.CancelledError):
            await sky_ambient.ambient_lights(bar, state)

    assert flaky.await_count == 2
    assert sleeps[0] == sky_limits.AMBIENT_RETRY_S
    assert state.ambient_strip is True  # the second attempt lit it
    assert _notices(caplog) == []  # a USB fault is not the Wi-Fi story
    assert sum("retrying" in r.getMessage() for r in caplog.records) == 1


@pytest.mark.asyncio
async def test_a_usb_bar_keeps_driving_the_mood(monkeypatch):
    state = sky_model.SkyState()
    bar = _bar("usb")
    _patch_sleep(monkeypatch, stop_after=1)

    with pytest.raises(asyncio.CancelledError):
        await sky_ambient.ambient_lights(bar, state)

    assert bar.usb.send_command.await_count == 1
    assert bar.usb.send_command.await_args.args[0] == "status_lights"
    assert state.ambient_strip is True


@pytest.mark.asyncio
async def test_a_bar_too_old_for_the_transport_query_is_probed_without_asking():
    state = sky_model.SkyState()
    bar = _bar("usb", device_api="17.9.0")
    kind = await sky_ambient._transport_type(bar)

    assert kind is None  # asking would only earn a 404 in the log
    bar.transport.assert_not_awaited()
    assert state.ambient_strip is False


@pytest.mark.asyncio
async def test_a_failing_transport_query_falls_back_to_the_probe():
    bar = _bar("usb", transport_fails=True)
    assert await sky_ambient._transport_type(bar) is None


@pytest.mark.asyncio
async def test_shutdown_resets_only_a_strip_we_ever_lit():
    never_lit = sky_model.SkyState()
    bar = _bar("wifi")
    await sky_ambient.reset_strip(bar, never_lit)
    bar.usb.send_command.assert_not_awaited()

    lit_then_lost = sky_model.SkyState()
    lit_then_lost.ambient_strip = True
    usb_bar = _bar("usb")
    await sky_ambient.reset_strip(usb_bar, lit_then_lost)
    usb_bar.usb.send_command.assert_awaited_once_with("status_lights", "0", "0", "0")


@pytest.mark.asyncio
async def test_reset_strip_swallows_a_lost_usb_link():
    state = sky_model.SkyState()
    state.ambient_strip = True
    bar = _bar("usb", AsyncMock(side_effect=OSError("USB gone")))
    await sky_ambient.reset_strip(bar, state)  # cleanup must never raise

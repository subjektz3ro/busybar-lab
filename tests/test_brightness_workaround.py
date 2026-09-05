"""The firmware mitigation must run through the real app startup paths."""

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import httpx2
from busylib import AsyncBusyBar, types

from apps.dsn_app import runtime as dsn_runtime
from apps.skystrip_app import runtime as skystrip_runtime
from busybar_dev import brightness

LOG = logging.getLogger("test.brightness")


@pytest.fixture(autouse=True)
def clean_config(monkeypatch):
    monkeypatch.delenv("BUSYBAR_AUTO_BRIGHTNESS_FALLBACK", raising=False)
    monkeypatch.setattr(brightness, "READBACK_INTERVAL_S", 0)


def fake_bar(version="1.2.3", current="auto"):
    return SimpleNamespace(
        status=AsyncMock(
            return_value=types.Status(firmware=types.StatusFirmware(version=version))
        ),
        display_brightness=AsyncMock(
            return_value=types.DisplayBrightnessInfo(value=current)
        ),
        display_brightness_set=AsyncMock(),
    )


async def apply(bar, stop=None):
    await brightness.apply_brightness_workaround(bar, stop or asyncio.Event(), log=LOG)


@pytest.mark.parametrize("version", [None, "", "1.1.1", "1.2.2", "1.2.4", "1.2.3-dev"])
async def test_other_or_unknown_firmware_is_read_only(version):
    bar = fake_bar(version)
    await apply(bar)
    bar.display_brightness.assert_not_awaited()
    bar.display_brightness_set.assert_not_awaited()


async def test_missing_firmware_does_not_guess():
    bar = fake_bar()
    bar.status.return_value = types.Status()
    await apply(bar)
    bar.display_brightness.assert_not_awaited()


@pytest.mark.parametrize("current", ["0", "1", "35", "50", "100", None, "unknown"])
async def test_existing_manual_or_unknown_brightness_is_preserved(current):
    bar = fake_bar(current=current)
    await apply(bar)
    bar.display_brightness_set.assert_not_awaited()


@pytest.mark.parametrize(
    "configured, expected",
    [("", 35), ("35", 35), ("50", 50), (" 50 ", 50), ("1", 1), ("100", 100)],
)
async def test_delayed_readback_verified_without_repeated_writes(
    monkeypatch, caplog, configured, expected
):
    monkeypatch.setenv("BUSYBAR_AUTO_BRIGHTNESS_FALLBACK", configured)
    bar = fake_bar()
    bar.display_brightness.side_effect = [
        types.DisplayBrightnessInfo(value=v)
        for v in ["auto", "auto", None, str(expected)]
    ]
    await apply(bar)
    bar.display_brightness_set.assert_awaited_once_with(expected)
    assert bar.display_brightness.await_count == 4
    assert f"fixed {expected}% verified" in caplog.text


@pytest.mark.parametrize(
    "configured", ["off", "0", "101", "-1", "35.0", "nan", "AUTO", "３５", "1" * 1000]
)
async def test_disabled_or_invalid_config_never_touches_device(monkeypatch, configured):
    monkeypatch.setenv("BUSYBAR_AUTO_BRIGHTNESS_FALLBACK", configured)
    bar = fake_bar()
    await apply(bar)
    bar.status.assert_not_awaited()
    bar.display_brightness_set.assert_not_awaited()


async def test_an_operator_change_during_readback_is_not_overwritten(caplog):
    bar = fake_bar()
    bar.display_brightness.side_effect = [
        types.DisplayBrightnessInfo(value=v) for v in ["auto", "50"]
    ]
    await apply(bar)
    bar.display_brightness_set.assert_awaited_once_with(35)
    assert "leaving the new setting alone" in caplog.text
    assert "verified" not in caplog.text


@pytest.mark.parametrize(
    "endpoint", ["status", "display_brightness", "display_brightness_set"]
)
async def test_failures_do_not_crash_or_expose_transport_details(endpoint, caplog):
    bar = fake_bar()
    getattr(bar, endpoint).side_effect = OSError("sensitive transport detail")
    await apply(bar)
    assert "could not be verified (OSError)" in caplog.text
    assert "sensitive transport detail" not in caplog.text
    assert bar.display_brightness_set.await_count <= 1


async def test_lost_write_response_is_not_reissued(caplog):
    bar = fake_bar()

    async def committed_but_lost(_value):
        bar.display_brightness.return_value = types.DisplayBrightnessInfo(value="35")
        raise OSError("lost response")

    bar.display_brightness_set.side_effect = committed_but_lost
    await apply(bar)
    await apply(bar)  # The next startup adopts the manual setting.
    bar.display_brightness_set.assert_awaited_once_with(35)
    assert "fixed 35% verified" not in caplog.text


@pytest.mark.parametrize(
    "phase", ["status", "display_brightness", "display_brightness_set"]
)
@pytest.mark.parametrize("ending", ["stop", "cancel", "timeout"])
async def test_hung_io_is_cancelled_and_reaped(monkeypatch, phase, ending, caplog):
    bar, stop, entered, cancelled = (
        fake_bar(),
        asyncio.Event(),
        asyncio.Event(),
        asyncio.Event(),
    )

    async def hang(*_args):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cancelled.set()

    getattr(bar, phase).side_effect = hang
    if ending == "timeout":
        monkeypatch.setattr(brightness, "STARTUP_TIMEOUT_S", 0.02)
    task = asyncio.create_task(apply(bar, stop))
    await asyncio.wait_for(entered.wait(), 1)
    if ending == "cancel":
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        if ending == "stop":
            stop.set()
        await asyncio.wait_for(task, 1)
    assert cancelled.is_set()
    if ending == "timeout":
        assert "timed out" in caplog.text


async def test_readback_that_never_settles_has_a_deadline(monkeypatch, caplog):
    monkeypatch.setattr(brightness, "STARTUP_TIMEOUT_S", 0.02)
    bar = fake_bar()
    await apply(bar)
    bar.display_brightness_set.assert_awaited_once_with(35)
    assert "timed out" in caplog.text


async def test_already_stopping_never_contacts_device():
    bar, stop = fake_bar(), asyncio.Event()
    stop.set()
    await apply(bar, stop)
    bar.status.assert_not_awaited()


async def test_stop_between_read_and_write_prevents_mutation():
    bar, stop = fake_bar(), asyncio.Event()

    async def read_then_stop():
        stop.set()
        return types.DisplayBrightnessInfo(value="auto")

    bar.display_brightness.side_effect = read_then_stop
    await apply(bar, stop)
    bar.display_brightness_set.assert_not_awaited()


async def test_installed_client_wire_contract_and_second_start_are_idempotent():
    level, requests = "auto", []

    async def respond(request):
        nonlocal level
        requests.append(request)
        if request.url.path == "/api/status":
            return httpx2.Response(200, json={"firmware": {"version": "1.2.3"}})
        assert request.url.path == "/api/display/brightness"
        if request.method == "POST":
            assert dict(request.url.params) == {"value": "35"}
            assert request.content == b""
            level = request.url.params["value"]
            return httpx2.Response(200, json={"result": "ok"})
        return httpx2.Response(200, json={"value": level})

    async with AsyncBusyBar(
        "http://device.example",
        transport=httpx2.MockTransport(respond),
        compatibility_mode="none",
        max_retries=0,
    ) as bar:
        await apply(bar)
        await apply(bar)
    assert level == "35"
    assert [(r.method, r.url.path) for r in requests] == [
        ("GET", "/api/status"),
        ("GET", "/api/display/brightness"),
        ("POST", "/api/display/brightness"),
        ("GET", "/api/display/brightness"),
        ("GET", "/api/status"),
        ("GET", "/api/display/brightness"),
    ]


@pytest.mark.parametrize("runtime", [dsn_runtime, skystrip_runtime])
async def test_apps_mitigate_auto_before_asset_or_scene_effects(monkeypatch, runtime):
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_: None)
    monkeypatch.setattr(loop, "remove_signal_handler", lambda *_: None)
    monkeypatch.delenv("BUSYBAR_AUTO_BRIGHTNESS_FALLBACK", raising=False)
    bar = SimpleNamespace(
        status=AsyncMock(
            return_value=types.Status(firmware=types.StatusFirmware(version="1.2.3"))
        ),
        display_brightness=AsyncMock(
            side_effect=[
                types.DisplayBrightnessInfo(value="auto"),
                types.DisplayBrightnessInfo(value="35"),
            ]
        ),
        display_brightness_set=AsyncMock(),
        display_clear=AsyncMock(),
        aclose=AsyncMock(),
        usb=SimpleNamespace(send_command=AsyncMock()),
    )
    monkeypatch.setattr(runtime, "connect_with_retry", AsyncMock(return_value=bar))
    monkeypatch.setattr(
        runtime._device_assets,
        "sweep_stale_assets",
        AsyncMock(side_effect=RuntimeError("end startup")),
    )
    if runtime is dsn_runtime:
        monkeypatch.setattr(
            runtime._audio_output,
            "shutdown_audio_bounded",
            AsyncMock(return_value=set()),
        )
    with pytest.raises(RuntimeError, match="end startup"):
        await runtime.run(once=False)
    bar.display_brightness_set.assert_awaited_once_with(35)
    assert bar.display_brightness.await_count == 2
    bar.aclose.assert_awaited_once()

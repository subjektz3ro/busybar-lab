"""Exercise the real watcher coordinator with controlled provider/device edges."""

import asyncio
import signal
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from busylib import exceptions

from apps.skystrip_app import model, runtime, settings


@pytest.mark.parametrize(
    ("fresh", "lightning", "siren_failure", "draw_failure", "cleanup_failure"),
    [
        (False, False, False, None, False),
        (False, True, True, None, True),
        (True, False, False, None, False),
        (True, True, True, None, True),
        (True, False, False, 409, False),
        (True, False, False, 500, False),
        (True, True, False, "offline", False),
    ],
)
async def test_watcher_owns_one_state_and_reaps_all_tasks_on_sigterm(
    monkeypatch,
    fresh,
    lightning,
    siren_failure,
    draw_failure,
    cleanup_failure,
):
    loop = asyncio.get_running_loop()
    handlers, removed, workers, cancelled = {}, [], {}, set()
    monkeypatch.setattr(
        loop,
        "add_signal_handler",
        lambda sig, callback: handlers.__setitem__(sig, callback),
    )
    monkeypatch.setattr(loop, "remove_signal_handler", lambda sig: removed.append(sig))
    monkeypatch.setattr(
        settings,
        "LIGHTNING_WS",
        "wss://relay.example/authorized" if lightning else None,
    )
    monkeypatch.setattr(runtime._build_info, "_git_rev", lambda: "test")
    monkeypatch.setattr(runtime._selection, "load_scene_idx", lambda: 0)

    state = model.SkyState()
    if fresh:
        state.weather_ready.set()
        state.weather_updated_at = loop.time()
    monkeypatch.setattr(model, "SkyState", lambda: state)
    bar = SimpleNamespace(
        usb=SimpleNamespace(send_command=AsyncMock()),
        display_clear=AsyncMock(),
        aclose=AsyncMock(),
    )
    if cleanup_failure:
        bar.usb.send_command.side_effect = OSError("USB gone")
        bar.display_clear.side_effect = OSError("device gone")
    monkeypatch.setattr(runtime, "connect_with_retry", AsyncMock(return_value=bar))
    sweep = AsyncMock()
    siren = AsyncMock(side_effect=OSError("not provisioned") if siren_failure else None)
    monkeypatch.setattr(runtime._device_assets, "sweep_stale_assets", sweep)
    monkeypatch.setattr(runtime._audio_siren, "ensure_siren_asset", siren)

    owners = [
        (runtime._providers_alerts, "poll_alerts"),
        (runtime._providers_weather, "poll_nws"),
        (runtime._providers_radar, "poll_radar"),
        (runtime._input, "listen_buttons"),
        (runtime._device_alerts, "severe_alarm"),
        (runtime._audio_siren, "maintain_siren_asset"),
        (runtime._device_scrubber, "build_timeline"),
        (runtime._device_ambient, "ambient_lights"),
        (runtime._audio_report, "bake_report"),
        (runtime._device_effects, "watch_trains"),
        (runtime._device_effects, "watch_traffic"),
        (runtime._device_effects, "watch_meteors"),
    ]
    if lightning:
        owners.append((runtime._providers_lightning, "listen_lightning"))

    def worker(name):
        async def wait_for_shutdown(*args, **kwargs):
            workers[name] = (args[-1], kwargs)
            try:
                if len(workers) == len(owners):
                    handlers[signal.SIGTERM]()
                await asyncio.Event().wait()
            finally:
                cancelled.add(name)

        return wait_for_shutdown

    for owner, name in owners:
        monkeypatch.setattr(owner, name, worker(name))

    notices, pushes = [], []

    async def stale_notice(_bar, current):
        notices.append(current)
        await asyncio.sleep(0)

    sentinel_frames = object()

    async def push(_bar, current, _now, frames):
        pushes.append((current, frames))
        current.scene_change.set()
        await asyncio.sleep(0)
        if isinstance(draw_failure, int):
            raise exceptions.BusyBarAPIError("rejected", status_code=draw_failure)
        if draw_failure:
            raise OSError("offline")

    monkeypatch.setattr(runtime._device_display, "keep_stale_notice", stale_notice)
    monkeypatch.setattr(runtime._device_display, "push_scene", push)
    monkeypatch.setattr(
        runtime._render_scene, "render_loop_frames", lambda *_a, **_k: sentinel_frames
    )

    await asyncio.wait_for(runtime.run(once=False), timeout=2)

    assert set(workers) == cancelled == {name for _, name in owners}
    assert all(current is state for current, _ in workers.values())
    assert workers["poll_alerts"][1] == {"wait_for_point_check": True}
    assert state.shutting_down
    assert not state.detached_tasks
    assert set(handlers) == set(removed) == {signal.SIGINT, signal.SIGTERM}
    assert notices == ([] if fresh else [state])
    assert pushes == ([(state, sentinel_frames)] if fresh else [])
    sweep.assert_awaited_once_with(bar)
    siren.assert_awaited_once_with(bar, state)
    bar.usb.send_command.assert_awaited_once_with("status_lights", "0", "0", "0")
    bar.display_clear.assert_awaited_once_with(
        application_name=runtime._limits.APP_NAME
    )
    bar.aclose.assert_awaited_once_with()

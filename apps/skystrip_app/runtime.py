"""Own Skystrip's process, provider tasks, redraw schedule and shutdown.

Input and background work share one SkyState. Keep cancellation and final
device cleanup here; rendering and source interpretation have separate owners.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from datetime import datetime, timedelta, timezone

from busylib import exceptions, types

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import build_info as _build_info
from apps.skystrip_app import input as _input
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import selection as _selection
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather_state as _weather_state
from apps.skystrip_app.audio import output as _audio_output
from apps.skystrip_app.audio import report as _audio_report
from apps.skystrip_app.audio import siren as _audio_siren
from apps.skystrip_app.device import alerts as _device_alerts
from apps.skystrip_app.device import ambient as _device_ambient
from apps.skystrip_app.device import assets as _device_assets
from apps.skystrip_app.device import display as _device_display
from apps.skystrip_app.device import effects as _device_effects
from apps.skystrip_app.device import scrubber as _device_scrubber
from apps.skystrip_app.providers import alerts as _providers_alerts
from apps.skystrip_app.providers import lightning as _providers_lightning
from apps.skystrip_app.providers import radar as _providers_radar
from apps.skystrip_app.providers import weather as _providers_weather
from apps.skystrip_app.render import scene as _render_scene
from busybar_dev import aconnect
from busybar_dev.brightness import apply_brightness_workaround
from busybar_dev.config import describe_exception
from busybar_dev.device import connect_with_retry
from busybar_dev.device import is_refusal as _is_refusal


async def run(once: bool) -> None:
    """Run Skystrip after the caller has explicitly configured the process.

    CLI and Barkeep callers use :func:`main`, which calls
    :func:`configure_runtime` first. Programmatic callers must do the same when
    they want owner configuration rather than the public-safe import defaults.
    """
    _limits.logger.info("skystrip %s starting", _build_info._git_rev())
    if unlocated := _settings.warn_if_unlocated():
        _limits.logger.warning("%s", unlocated)
    state = _model.SkyState()
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)

    bb = await connect_with_retry(
        aconnect, stop, log=_limits.logger, describe=describe_exception
    )
    tasks: list[asyncio.Task] = []
    try:
        await apply_brightness_workaround(bb, stop, log=_limits.logger)
        if stop.is_set():
            return
        # Inside the try: a failure provisioning the siren used to propagate
        # out of run() with no cleanup at all — no aclose(), no display clear,
        # no signal handlers removed. A missing siren must not take the sky
        # down with it.
        if once:
            # --once is the audition path and may run beside a live instance
            # (barkeep's, on the Pi). The sweep is app-scoped with no instance
            # identity, so it would delete the running sky's assets.
            _limits.logger.info(
                "--once: skipping the asset sweep, another instance may be live"
            )
        else:
            await _device_assets.sweep_stale_assets(bb)
            try:
                await _audio_siren.ensure_siren_asset(bb, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - the sky outranks the siren
                _limits.logger.warning(
                    "siren provisioning failed (%s); continuing without it",
                    describe_exception(exc),
                )

        state.scene_idx = _selection.load_scene_idx()
        if once:
            now = datetime.now(timezone.utc)
            try:
                await _device_display.push_scene(
                    bb,
                    state,
                    now,
                    _render_scene.render_loop_frames(
                        now, state.weather, seed=0, scene=state.scene
                    ),
                )
            except exceptions.BusyBarAPIError as exc:
                if not _is_refusal(exc):
                    raise
                _limits.logger.info("a BUSY/CUSTOM session owns the display")
        else:
            _limits.logger.info(
                "waiting for a fresh weather snapshot before first draw"
            )
        if once:
            _limits.logger.info(
                "pushed one loop, exiting (element self-clears in %ss)",
                _limits.ELEMENT_TIMEOUT_S,
            )
            return

        tasks = [
            asyncio.create_task(
                _providers_alerts.poll_alerts(state, wait_for_point_check=True)
            ),
            asyncio.create_task(_providers_weather.poll_nws(state)),
            asyncio.create_task(_providers_radar.poll_radar(state)),
            asyncio.create_task(_input.listen_buttons(bb, state)),
            asyncio.create_task(_device_alerts.severe_alarm(bb, state)),
            asyncio.create_task(_audio_siren.maintain_siren_asset(bb, state)),
            asyncio.create_task(_device_scrubber.build_timeline(bb, state)),
            asyncio.create_task(_device_ambient.ambient_lights(bb, state)),
            asyncio.create_task(_audio_report.bake_report(bb, state)),
            asyncio.create_task(_device_effects.watch_trains(bb, state)),
            asyncio.create_task(_device_effects.watch_traffic(bb, state)),
            asyncio.create_task(_device_effects.watch_meteors(bb, state)),
        ]
        if _settings.LIGHTNING_WS is not None:
            tasks.append(
                asyncio.create_task(_providers_lightning.listen_lightning(state))
            )
        else:
            _limits.logger.info(
                "live lightning disabled; configure SKYSTRIP_LIGHTNING_WS "
                "with an authorized secure feed to enable it"
            )
        next_draw = 0.0
        device_backoff = 0.0
        while not stop.is_set():
            loop_now = loop.time()
            if loop_now >= next_draw:
                if not _weather_state.weather_is_fresh(state):
                    # Do not refresh a plausible-but-invented default or an
                    # expired last-good snapshot. The native lease will clear
                    # an old scene if sources remain unavailable — but it must
                    # clear to a stated reason, not to an unexplained black
                    # panel that reads as a dead app. Only while this app owns
                    # the view: an alert card and Time Machine both outrank it.
                    if (
                        state.visual_alert is None
                        and state.scrub_slot is None
                        and not state.shutting_down
                    ):
                        await _device_display.keep_stale_notice(bb, state)
                    next_draw = loop_now + 1.0
                    await asyncio.sleep(0)
                    continue
                # Fire just before each wall-clock minute so the baked clock
                # flips on the display as the minute actually changes
                wall = datetime.now(timezone.utc)
                to_boundary = 60.0 - (wall.second + wall.microsecond / 1e6) - 2.0
                if to_boundary < 5.0:
                    to_boundary += 60.0
                next_draw = loop_now + to_boundary
                seed = int(loop_now // 600)  # texture drifts every 10 min
                # Bake the minute this push will live through: ticks fire
                # just before the boundary, so back-half seconds mean the
                # upcoming minute; a late fire keeps the current one
                if wall.second >= 30:
                    now = wall + timedelta(seconds=61 - wall.second)
                else:
                    now = wall
                frames = _render_scene.render_loop_frames(
                    now, state.weather, seed, scene=state.scene
                )
                try:
                    await _device_display.push_scene(bb, state, now, frames)
                    device_backoff = 0.0
                except exceptions.BusyBarAPIError as exc:
                    if _is_refusal(exc):
                        # A BUSY/CUSTOM session owns the display; the sky yields
                        _limits.logger.debug("yielding to higher-priority app")
                    else:
                        _limits.logger.warning("draw rejected: %s", exc)
                except Exception as exc:  # noqa: BLE001 - offline is a state
                    device_backoff = min(max(device_backoff * 2, 5), 120)
                    next_draw = loop_now + device_backoff
                    _limits.logger.warning(
                        "scene push failed (%s); retry in %.0fs", exc, device_backoff
                    )
            if state.scene_change.is_set():
                state.scene_change.clear()
                next_draw = 0.0  # redraw with the new scene immediately
                continue
            if (
                state.scrub_slot is not None
                and not _alerts._unacknowledged_alert_active(state)
                and loop_now - state.scrub_touched > _limits.SCRUB_SNAP_S
            ):
                was_revealed = state.revealed
                state.scrub_slot = None
                state.revealed = False
                _limits.logger.info("time machine: back to now")
                try:
                    state.readout_gen = (state.readout_gen + 1) % 100
                    await _device_scrubber.draw_scrub_readout(
                        bb, state, "NOW", timeout=1
                    )
                    if was_revealed:
                        await _device_scrubber.retire_reveal(bb, state)
                except Exception:  # noqa: BLE001
                    pass
                continue
            if _device_scrubber._scrub_reveal_ready(state, loop_now):
                # The wheel rested: jump the scene to the chosen moment
                slot = state.scrub_slot
                meta = state.timeline_meta
                assert slot is not None and meta is not None
                intent = state.view_generation
                state.reveal_pending = True
                try:
                    if meta["scene"] != state.scene:
                        # Timeline still rebuilding for this scene: never
                        # serve the old scene's frame — hold the readout
                        # and jump straight to the animated render
                        await _device_scrubber.draw_scrub_readout(
                            bb,
                            state,
                            _device_scrubber._slot_label(meta, slot),
                            timeout=6,
                        )
                        _model.spawn_owned(
                            state,
                            _device_scrubber.animate_reveal(
                                bb, state, slot, initial=True, intent=intent
                            ),
                        )
                        continue
                    fname = meta["file"]
                    if (
                        state.last_reveal is not None
                        and state.last_reveal["slot"] == slot
                    ):
                        state.revealed = True
                        state.reveal_pending = False
                        continue  # already showing this slot
                    state.reveal_n += 1
                    eid = f"rv{state.reveal_n}"
                    prev = state.last_reveal
                    old_readout = state.last_readout
                    elements = _device_scrubber._retired_readout_elements(old_readout)
                    if prev is not None:
                        elements.append(
                            types.AnimationElement(
                                id=prev["eid"],
                                type="animation",
                                path=prev["fname"],
                                section=prev.get("section"),
                                loop=True,
                                x=0,
                                y=0,
                                display=types.DisplayName.FRONT,
                                timeout=1,
                            )
                        )
                    elements.append(
                        types.AnimationElement(
                            id=eid,
                            type="animation",
                            path=fname,
                            section=f"s{slot:02d}",
                            loop=True,
                            x=0,
                            y=0,
                            display=types.DisplayName.FRONT,
                            timeout=60,
                        )
                    )
                    async with state.display_lock:
                        if (
                            state.view_generation != intent
                            or state.scrub_slot != slot
                            or _alerts._unacknowledged_alert_active(state)
                        ):
                            state.reveal_pending = False
                            continue
                        await bb.display_draw(
                            types.DisplayElements(
                                application_name=_limits.APP_NAME,
                                priority=_limits.PRIORITY,
                                elements=elements,
                            )
                        )
                    state.last_reveal = {
                        "eid": eid,
                        "slot": slot,
                        "fname": fname,
                        "section": f"s{slot:02d}",
                    }
                    state.last_readout = None
                    state.revealed = True
                    state.reveal_pending = False
                    # And bring the moment to life in the background
                    _model.spawn_owned(
                        state,
                        _device_scrubber.animate_reveal(bb, state, slot, intent=intent),
                    )
                except Exception as exc:  # noqa: BLE001
                    state.reveal_pending = False
                    _limits.logger.warning("reveal failed: %s", exc)
                continue
            try:
                flash_event = await asyncio.wait_for(
                    state.flash_queue.get(),
                    timeout=min(1.0, max(0.05, next_draw - loop_now)),
                )
                flash_event = _providers_lightning._coalesce_fresh_flashes(
                    state.flash_queue,
                    flash_event,
                    now=loop.time(),
                )
                if flash_event is None:
                    _limits.logger.debug("discarded stale lightning burst")
                    continue
                dist = _providers_lightning._flash_distance(flash_event)
                try:
                    await _device_effects.flash(bb, state, dist)
                except Exception as exc:  # noqa: BLE001
                    _limits.logger.warning("flash draw failed: %s", exc)
                await asyncio.sleep(_limits.FLASH_MIN_GAP_S)
            except asyncio.TimeoutError:
                pass
    finally:
        state.shutting_down = True
        for task in tasks:
            task.cancel()
        for task in tuple(state.detached_tasks):
            task.cancel()
        if tasks or state.detached_tasks:
            await asyncio.gather(
                *tasks,
                *tuple(state.detached_tasks),
                return_exceptions=True,
            )
        if state.audio_owner is not None or state.audio_stop_pending:
            generation = _alerts._claim_audio_stop(state)
            await _audio_output.stop_audio(bb, state, generation)
        # Best-effort cleanup: never let it mask the exception that got us here
        try:
            await bb.usb.send_command("status_lights", "0", "0", "0")
        except Exception:  # noqa: BLE001
            pass
        try:
            if not once:
                await bb.display_clear(application_name=_limits.APP_NAME)
                _limits.logger.info("cleared")
        except Exception as exc:  # noqa: BLE001
            _limits.logger.warning("cleanup draw-clear failed: %s", exc)
        try:
            await bb.aclose()
        except Exception:  # noqa: BLE001
            pass
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(Exception):
                loop.remove_signal_handler(sig)

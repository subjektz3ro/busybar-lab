"""Own the DSN process: startup tasks, redraw scheduling and bounded shutdown.

Policy and rendering live in their owners. This loop sequences them against
one State and commits device ownership only after an accepted operation.
"""

from __future__ import annotations

import asyncio
import math
import signal
import time
from datetime import datetime, timezone

from busylib import exceptions

from apps.dsn_app import feed as _feed
from apps.dsn_app import history as _history
from apps.dsn_app import input as _input
from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import ranges as _ranges
from apps.dsn_app import reconcile as _reconcile
from apps.dsn_app import selection as _selection
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.audio import narration as _audio_narration
from apps.dsn_app.audio import output as _audio_output
from apps.dsn_app.audio import policy as _audio_policy
from apps.dsn_app.device import assets as _device_assets
from apps.dsn_app.device import display as _device_display
from apps.dsn_app.device import events as _device_events
from apps.dsn_app.device import scene_policy as _device_scene_policy
from apps.dsn_app.device import scenes as _device_scenes
from busybar_dev import aconnect
from busybar_dev.brightness import apply_brightness_workaround
from busybar_dev.device import connect_with_retry
from busybar_dev.device import is_refusal as _is_refusal


async def await_or_stop(awaitable, stop: asyncio.Event):
    """Cancel one startup operation as soon as systemd asks us to stop."""
    operation = asyncio.ensure_future(awaitable)
    shutdown = asyncio.create_task(stop.wait())
    try:
        done, _ = await asyncio.wait(
            (operation, shutdown), return_when=asyncio.FIRST_COMPLETED
        )
        if shutdown in done:
            operation.cancel()
            settled, _ = await asyncio.wait(
                (operation,), timeout=_limits.SHUTDOWN_TIMEOUT_S
            )
            if operation in settled:
                await asyncio.gather(operation, return_exceptions=True)
            return None
        return await operation
    finally:
        shutdown.cancel()
        if not operation.done():
            operation.cancel()
        settled, _ = await asyncio.wait(
            (shutdown, operation), timeout=_limits.SHUTDOWN_TIMEOUT_S
        )
        if settled:
            await asyncio.gather(*settled, return_exceptions=True)


async def run(once: bool) -> None:
    state = _model.State()
    tasks: list[asyncio.Task] = []
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    # Handlers first, then connect: connect_with_retry waits on `stop`, so a
    # SIGTERM while the bar is still absent has to be able to set it.
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:
            pass
    bb = await connect_with_retry(aconnect, stop, log=_limits.logger)
    try:
        await apply_brightness_workaround(bb, stop, log=_limits.logger)
        if stop.is_set():
            return
        if once:
            # --once may run beside the Pi's copy; the sweep is app-scoped and
            # cannot tell whose files these are.
            _limits.logger.info(
                "--once: skipping the asset sweep, another instance may be live"
            )
        else:
            await await_or_stop(_device_assets.sweep_stale_assets(bb), stop)
            if stop.is_set():
                return

        _ranges.load_ranges(state)
        _history.load_history(state)
        # Feed acquisition is the only startup work that gates a live scene.
        # Config names and a potentially large speech-cache scan enrich it in
        # parallel; a slow cosmetic endpoint must not leave the strip blank.
        tasks = [
            asyncio.create_task(_feed.poll_names(state)),
            asyncio.create_task(_feed.poll_feed(state)),
            asyncio.create_task(_ranges.poll_ranges(state)),
        ]
        if not once:
            tasks += [
                asyncio.create_task(_input.listen_input(bb, state)),
                asyncio.create_task(_selection.rotate(state)),
                asyncio.create_task(
                    _audio_narration.prepare_narration_cache(bb, state)
                ),
            ]

        for _ in range(60):  # wait for the first feed
            if state.feed_seeded or stop.is_set():
                break
            await asyncio.sleep(0.5)
        if stop.is_set():
            return
        if not state.feed_seeded:
            _limits.logger.warning("no active links in the feed yet")
        elif not state.links:
            _limits.logger.info("the source currently reports no active links")

        next_draw = 0.0
        draw_retry_at = 0.0
        retry_intent: tuple | None = None
        once_scene_ready = False
        pushed_once = False
        once_timeout = 15
        event_warm_started = False
        while not stop.is_set():
            now = loop.time()
            if (
                state.network_focus_until > 0
                and not math.isinf(state.network_focus_until)
                and now >= state.network_focus_until
            ):
                _selection.clear_network_focus(state)
                state.dirty.set()
            if state.audio_stop_pending and now >= state.audio_stop_retry_at:
                stop_generation = state.audio_stop_generation
                await await_or_stop(
                    _audio_output.stop_audio_bounded(
                        bb, state, "deferred", stop_generation
                    ),
                    stop,
                )
                if stop.is_set():
                    break
            if (
                state.dirty.is_set()
                and retry_intent is not None
                and _selection.scene_intent_token(state) != retry_intent
            ):
                # A rejected old scene must not make a new wheel/tap/hold feel
                # dead. Give genuinely new intent one immediate attempt.
                draw_retry_at = 0.0
                retry_intent = None
            new_freshness = _telemetry.feed_freshness(state)
            if new_freshness != state.freshness:
                old_freshness, state.freshness = state.freshness, new_freshness
                if new_freshness != "fresh" and state.network_focus_key is not None:
                    # Source truth outranks a semantic-zoom dwell. Return to
                    # ambient Network so delayed/stale is explicit immediately;
                    # never restart a frozen Focus asset with old geometry.
                    _selection.clear_network_focus(state)
                if state.speaking and new_freshness != "fresh":
                    _model.note_narration_change(state)
                if new_freshness != "fresh" and (
                    state.narration_request is not None
                    or state.narration_notice is not None
                ):
                    _audio_policy.clear_narration_request(state)
                if new_freshness == "stale":
                    _reconcile.queue_events(
                        state, [{"event": "stale", "t": time.time()}]
                    )
                elif new_freshness == "fresh" and old_freshness in {"delayed", "stale"}:
                    _reconcile.queue_events(
                        state, [{"event": "recovered", "t": time.time()}]
                    )
                state.dirty.set()
            watched = state.current()
            if watched is not None:
                _device_scene_policy.complete_watch_if_due(state, watched, time.time())
            if state.picking and now - state.pick_at >= _limits.PICK_REST_S:
                # The wheel has settled. Retire the pop-up and commit.
                _selection.commit_picker_selection(state, now)
                await await_or_stop(
                    _device_display.draw_picker(bb, state, timeout=1), stop
                )
                if stop.is_set():
                    break
                state.dirty.set()
            due = now >= next_draw
            _device_scene_policy.advance_network_page_if_due(state, due)
            if (
                not state.picking
                and now >= draw_retry_at
                and (due or state.dirty.is_set())
            ):
                # An elapsed operation backoff is consumed even if the scene
                # pixels are unchanged; the live lease may be what needs retry.
                draw_retry_at = 0.0
                retry_intent = None
                state.dirty.clear()
                link = state.current()
                if link is not None:
                    rendered_at = datetime.now(timezone.utc)
                    signature = _device_scene_policy.scene_signature(
                        state, link, rendered_at
                    )
                    needs_draw = _device_scene_policy.scene_needs_draw(
                        state, signature, due
                    )
                    intended: tuple | None = signature if needs_draw else None
                    try:
                        if intended is not None:
                            accepted = await await_or_stop(
                                _device_scenes.push_scene(
                                    bb, state, link, intended, rendered_at=rendered_at
                                ),
                                stop,
                            )
                            if stop.is_set():
                                break
                            if accepted:
                                once_scene_ready = True
                                once_timeout = (
                                    _device_scene_policy.scene_element_timeout(
                                        state, link
                                    )
                                )
                                if state.status_up:
                                    await await_or_stop(
                                        _device_display.draw_feed_status(
                                            bb, state, timeout=1
                                        ),
                                        stop,
                                    )
                                    if stop.is_set():
                                        break
                                next_draw = (
                                    loop.time()
                                    + _device_scene_policy.scene_refresh_s(state, link)
                                )
                                draw_retry_at = 0.0
                    except exceptions.BusyBarAPIError as exc:
                        if _is_refusal(exc):
                            _limits.logger.debug("yielding to a higher-priority app")
                            draw_retry_at = now + 30
                        elif _device_assets._is_asset_path_failure(exc):
                            _limits.logger.warning("scene asset vanished; rebuilding")
                            draw_retry_at = now + 1
                        else:
                            _limits.logger.warning("draw rejected: %s", exc)
                            draw_retry_at = now + 30
                        retry_intent = _selection.scene_intent_token(state)
                        state.dirty.set()
                    except Exception as exc:  # noqa: BLE001 - offline is a state
                        _limits.logger.warning("draw failed: %s", exc)
                        draw_retry_at = now + 30
                        retry_intent = _selection.scene_intent_token(state)
                        state.dirty.set()
                else:
                    try:
                        retired = await await_or_stop(
                            _device_display.retire_countdown(bb, state), stop
                        )
                        if stop.is_set():
                            break
                        if not retired:
                            raise RuntimeError("countdown retirement was refused")
                        await await_or_stop(
                            _device_display.draw_feed_status(bb, state), stop
                        )
                        if stop.is_set():
                            break
                        next_draw = loop.time() + 10
                        draw_retry_at = 0.0
                        pushed_once = True
                        once_timeout = 15
                    except exceptions.BusyBarAPIError as exc:
                        if _is_refusal(exc):
                            draw_retry_at = now + 30
                        else:
                            _limits.logger.warning("status draw rejected: %s", exc)
                            draw_retry_at = now + 30
                        retry_intent = _selection.scene_intent_token(state)
                        state.dirty.set()
                    except Exception as exc:  # noqa: BLE001
                        _limits.logger.warning("status draw failed: %s", exc)
                        draw_retry_at = now + 30
                        retry_intent = _selection.scene_intent_token(state)
                        state.dirty.set()
                if draw_retry_at == 0.0:
                    lease_ok = await await_or_stop(
                        _device_display.sync_live_lease(bb, state, new_freshness), stop
                    )
                    if stop.is_set():
                        break
                    if not lease_ok:
                        state.dirty.set()
                        draw_retry_at = now + 30
                        retry_intent = _selection.scene_intent_token(state)
                    elif once_scene_ready:
                        pushed_once = True
            if (
                not once
                and not event_warm_started
                and (once_scene_ready or pushed_once)
            ):
                # The current state reaches the LEDs before background asset
                # work starts. Once warm, finite generic events only select a
                # resident path; a rare data-specific Three Skies handoff is
                # separately built/cached at event time, never on user input.
                event_warm_started = True
                warm_task = _device_events.start_event_asset_warm(bb, state)
                tasks.append(warm_task)
            if (
                state.narration_notice is not None
                and now >= state.narration_notice_retry_at
                and now >= state.next_event_at
            ):
                notice = state.narration_notice
                shown = await await_or_stop(
                    _audio_narration.show_narration_notice(bb, state), stop
                )
                if stop.is_set():
                    break
                if shown or state.narration_notice is not notice:
                    state.narration_notice_retry_at = 0.0
                    if shown:
                        # Give the explicit user acknowledgement its native
                        # three-second dwell before queued feed events resume.
                        state.next_event_at = loop.time() + 4.0
                else:
                    # Picker/audio/event ownership is ordinary. Keep the exact
                    # terminal notice, but back off repeated device refusals:
                    # a BUSY session can own the bar for hours.
                    delay = _audio_policy.narration_notice_backoff_s(
                        state.narration_notice_failures
                    )
                    state.narration_notice_retry_at = loop.time() + delay
            if now >= state.next_event_at and state.event_queue:
                shown = await await_or_stop(
                    _device_events.show_next_event(bb, state), stop
                )
                if stop.is_set():
                    break
                state.next_event_at = loop.time() + (
                    _limits.EVENT_TIMEOUT_S + 1 if shown else 5
                )
            if once and pushed_once:
                _limits.logger.info(
                    "pushed one loop; it self-clears in %ds", once_timeout
                )
                break
            try:
                # Tighter while the wheel is in play: at a 1s tick the picked
                # scene could take a whole extra second to appear after you
                # stopped turning, which reads as the wheel being slow.
                await asyncio.wait_for(
                    stop.wait(), timeout=0.15 if state.picking else 1.0
                )
            except asyncio.TimeoutError:
                pass
    finally:
        if state.ok_hold_task is not None:
            state.ok_hold_task.cancel()
        extra_tasks = [
            task
            for task in (state.event_warm_task, state.network_warm_task)
            if task is not None and task not in tasks
        ]
        for t in [*tasks, *extra_tasks]:
            t.cancel()
        # Cancel every producer before taking the snapshot; no normal input
        # task can create a fresh narration after shutdown claims ownership.
        speech_tasks = list(state.speech_tasks)
        pending_speech = await _audio_output.shutdown_audio_bounded(
            bb, state, speech_tasks
        )
        settling = [
            *tasks,
            *extra_tasks,
            *((state.ok_hold_task,) if state.ok_hold_task else ()),
        ]
        pending: set[asyncio.Task] = set()
        if settling:
            done, pending = await asyncio.wait(
                settling, timeout=_limits.SHUTDOWN_TIMEOUT_S
            )
            if done:
                await asyncio.gather(*done, return_exceptions=True)
        pending.update(pending_speech)
        if pending:
            _limits.logger.warning(
                "%d task(s) missed the shutdown deadline", len(pending)
            )
        if not once:
            try:
                await asyncio.wait_for(
                    bb.display_clear(application_name=_limits.APP_NAME),
                    _limits.SHUTDOWN_TIMEOUT_S,
                )
            except Exception:  # noqa: BLE001
                pass
        try:
            await asyncio.wait_for(bb.aclose(), _limits.SHUTDOWN_TIMEOUT_S)
        except Exception:  # noqa: BLE001
            pass

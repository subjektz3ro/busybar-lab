"""DSN audio / narration."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from busylib import exceptions

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.audio import assets as _audio_assets
from apps.dsn_app.audio import output as _audio_output
from apps.dsn_app.audio import policy as _audio_policy
from apps.dsn_app.audio import words as _audio_words
from apps.dsn_app.device import display as _device_display
from busybar_dev.device import is_refusal as _is_refusal


async def show_narration_notice(bb, state: _model.State) -> bool:
    """Draw one exact terminal notice; retain it across an ordinary 409."""
    notice = state.narration_notice
    if notice is None:
        return False
    # A deferred error can outlive the worker's successful retry. Never tell
    # the user audio failed when this exact immutable asset is now resident.
    if (
        notice.label == _limits.NARRATION_ERROR
        and notice.name is not None
        and notice.name in state.speech
    ):
        notice = replace(notice, label=_limits.NARRATION_READY)
        state.narration_notice = notice
        state.narration_notice_retry_at = 0.0
        state.narration_notice_failures = 0
    async with state.interactive_draw:
        # Recheck inside the same lock as the POST. A wheel/picker may have
        # invalidated this notice after the optimistic check above but before
        # we acquired display ownership; it must never physically land later.
        if state.narration_notice != notice:
            return False
        current = _selection.narration_target_link(state)
        invalid = (
            current is None
            or current.key != notice.key
            or state.view != notice.view
            or _telemetry.feed_freshness(state) != "fresh"
            or (
                notice.label == _limits.NARRATION_READY
                and (notice.name is None or notice.name not in state.speech)
            )
        )
        if invalid:
            state.narration_notice = None
            state.narration_notice_retry_at = 0.0
            state.narration_notice_failures = 0
            return False
        if (
            state.picking
            or state.speaking
            or state.ok_down_at is not None
            or state.speech_tasks
        ):
            return False
        accepted = await _device_display._post_readout(
            bb, state, notice.label, timeout=3
        )
    if accepted and state.narration_notice == notice:
        state.narration_notice = None
        state.narration_notice_retry_at = 0.0
        state.narration_notice_failures = 0
    elif not accepted and state.narration_notice == notice:
        state.narration_notice_failures += 1
    return accepted


async def speak(bb, state: _model.State, link: _source.Link) -> None:
    """Play a resident line now, or prepare it without blocking the button."""
    if (
        state.speaking
        or _telemetry.feed_freshness(state) != "fresh"
        or not any(live.key == link.key for live in state.links)
    ):
        if state.narration_return_view is not None:
            state.view = state.narration_return_view
            state.narration_return_view = None
            state.dirty.set()
        return

    if state.audio_stop_pending:
        # A previous ambiguous PLAY may still be audible. Resolve its bounded
        # STOP before starting another line, or the deferred retry could cut
        # off the new narration—or two clips could overlap.
        await _audio_output.stop_audio_bounded(bb, state, "before PLAY")
        if state.audio_stop_pending:
            await _device_display.draw_readout(bb, state, _limits.NARRATION_BUSY)
            return

    # Interaction is cache-only. Kokoro and the subsequent upload together
    # take tens of seconds on the Pi, so a miss remains in the current view,
    # keeps browsing live and acknowledges the preparation immediately.
    text = state.narration_texts.get(link.key) or _audio_words.spoken(
        link, state.names, state.dish_types
    )
    name = _audio_assets.speech_asset_name(state, text)
    if name not in state.speech:
        if state.view != "network":
            # Keep the requested detail selected long enough for the usual Pi
            # synthesis/upload path to finish and acknowledge PRESS START.
            # The wheel remains live and cancels this intent immediately.
            _selection.note_manual_selection(state)
        _audio_policy.request_narration(state, link, name)
        await _device_display.draw_readout(
            bb,
            state,
            _limits.NARRATION_PREPARING
            if state.speech_cache_ready
            else _limits.NARRATION_STARTING,
        )
        return

    # A new explicit press consumes any old completion notice. From here on
    # the line is resident: only actual playback owns the narration hold.
    # Serialize with a terminal notice already inside its display POST. That
    # older PRESS START must settle before PLAY, never land over audio after it
    # has begun. A wheel picker uses the same lock and likewise commits last.
    cache_still_ready = False
    async with state.interactive_draw:
        _audio_policy.clear_narration_request(state)
        # Cache adoption/trim runs independently. Recheck and touch without an
        # intervening await so a file evicted while we waited on an older
        # display POST becomes PREPARING, never an uncaught KeyError.
        if name in state.speech:
            seconds = _audio_assets.touch_speech(state, name)
            cache_still_ready = True
    if not cache_still_ready:
        if state.view != "network":
            _selection.note_manual_selection(state)
        _audio_policy.request_narration(state, link, name)
        await _device_display.draw_readout(bb, state, _limits.NARRATION_PREPARING)
        return
    if state.narration_priority == link.key:
        state.narration_priority = None
    play_generation = state.narration_request_counter
    started_view = state.view
    state.speaking = True
    took_hold = state.narration_focus is None
    state.narration_focus = link.key
    change_event = state.narration_changed
    state.dirty.set()
    try:
        if _telemetry.feed_freshness(state) != "fresh" or not any(
            live.key == link.key for live in state.links
        ):
            return
        state.audio_generation += 1
        audio_play_generation = state.audio_generation
        try:
            async with state.audio_io:
                # A navigation STOP may have claimed a newer generation while
                # this task waited behind an older device request.
                if (
                    state.audio_generation != audio_play_generation
                    or state.audio_stop_pending
                ):
                    return
                await asyncio.wait_for(
                    bb.audio_play(application_name=_limits.APP_NAME, path=name),
                    _limits.INTERACTIVE_IO_TIMEOUT_S,
                )
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) == 404:
                # The API uses 404 for both absent and unplayable audio. Never
                # overwrite or re-adopt the deterministic path: quarantine it
                # and let the background baker upload a new immutable repair
                # generation. A late result may invalidate storage state, but
                # it must not resurrect UI intent after the user moved.
                _audio_assets.mark_speech_unplayable(state, name)
                repair_name = _audio_assets.speech_asset_name(state, text)
                if not _audio_output.narration_play_is_current(
                    state, link, play_generation, started_view
                ):
                    return
                if state.view != "network":
                    _selection.note_manual_selection(state)
                _audio_policy.request_narration(state, link, repair_name)
                await _device_display.draw_readout(
                    bb, state, _limits.NARRATION_PREPARING
                )
                return
            if _is_refusal(exc):
                # Never turn one press into surprise playback later.
                if _audio_output.narration_play_is_current(
                    state, link, play_generation, started_view
                ):
                    await _device_display.draw_readout(
                        bb, state, _limits.NARRATION_BUSY
                    )
                return
            await _audio_output.stop_audio_bounded(bb, state, "ambiguous PLAY")
            if _audio_output.narration_play_is_current(
                state, link, play_generation, started_view
            ):
                await _device_display.draw_readout(bb, state, _limits.NARRATION_ERROR)
            _limits.logger.warning("audio PLAY failed: %s", exc)
            return
        except TimeoutError:
            # busylib may otherwise spend roughly 30 seconds across transport
            # retries. Cancellation has settled before STOP, so even a PLAY
            # whose response was lost cannot begin later under an error card.
            await _audio_output.stop_audio_bounded(bb, state, "timed-out PLAY")
            if _audio_output.narration_play_is_current(
                state, link, play_generation, started_view
            ):
                await _device_display.draw_readout(bb, state, _limits.NARRATION_ERROR)
            _limits.logger.warning(
                "audio PLAY exceeded %.1fs interaction bound",
                _limits.INTERACTIVE_IO_TIMEOUT_S,
            )
            return
        except Exception as exc:  # noqa: BLE001 - ambiguous transport result
            await _audio_output.stop_audio_bounded(bb, state, "ambiguous PLAY")
            if _audio_output.narration_play_is_current(
                state, link, play_generation, started_view
            ):
                await _device_display.draw_readout(bb, state, _limits.NARRATION_ERROR)
            _limits.logger.warning("audio PLAY failed: %s", exc)
            return

        if (
            state.audio_generation != audio_play_generation
            or not _audio_output.narration_play_is_current(
                state, link, play_generation, started_view
            )
        ):
            # Navigation/contact loss won while PLAY was in flight. It may
            # already have been accepted, so make the newer interaction win.
            await _audio_output.stop_audio_bounded(bb, state, "stale PLAY")
            return

        # Network is global, while narration is about one contact. Drill down
        # only after PLAY is accepted; a cold or refused press never changes
        # views. The instant craft readout bridges the scene swap.
        if started_view == "network" and state.view == "network":
            state.narration_return_view = "network"
            state.view = "instrument"
            state.dirty.set()
            await _device_display.draw_readout(bb, state, link.craft.upper())

        hold = asyncio.create_task(asyncio.sleep(seconds + 0.5))
        changed = asyncio.create_task(change_event.wait())
        try:
            done, _ = await asyncio.wait(
                (hold, changed), return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            for task in (hold, changed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(hold, changed, return_exceptions=True)
        if changed in done:
            await _audio_output.stop_audio_bounded(bb, state, "stale narration")
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _limits.logger.warning("speak failed: %s", exc)
        await _device_display.draw_readout(bb, state, _limits.NARRATION_ERROR)
    finally:
        state.speaking = False
        # Narration never owns the user's real-time lock. Its orthogonal hold
        # can be cleared even after a same-craft handoff without stranding a
        # permanent focus on the new dish.
        if took_hold:
            state.narration_focus = None
        if state.narration_return_view is not None:
            if state.view == "instrument" and state.realtime_since is None:
                state.view = state.narration_return_view
            state.narration_return_view = None
        state.dirty.set()


async def prebake(bb, state: _model.State) -> None:
    """Serially warm stable pass scripts, prioritising an explicit request."""
    while True:
        await asyncio.sleep(2)
        if not state.links or state.speaking:
            continue
        current = state.current()
        priority = state.narration_priority
        ordered = sorted(
            state.links,
            key=lambda link: (
                0
                if link.key == priority
                else 1
                if current and link.key == current.key
                else 2,
                link.key,
            ),
        )
        pending: tuple[_source.Link, str] | None = None
        for link in ordered:
            text = _audio_policy.observe_narration(state, link)
            if text is None:
                # A requested, otherwise-ready line gets the next real feed
                # observation before we spend 20-40 seconds on something
                # unrelated. Missing names/range do not block useful work.
                if link.key == priority and _audio_policy.narration_ready(state, link):
                    break
                continue
            name = _audio_assets.speech_asset_name(state, text)
            request = _audio_policy.bind_narration_request(state, link.key, name)
            if name in state.speech:
                # Pin every active frozen line ahead of inactive historical
                # entries before a new bake can make the LRU choose a victim.
                _audio_assets.touch_speech(state, name)
                if state.narration_priority == link.key:
                    state.narration_priority = None
                _audio_policy.finish_narration_request(
                    state, request, _limits.NARRATION_READY
                )
                continue
            pending = (link, text)
            break
        if pending is None:
            if priority and priority in state.narration_texts:
                state.narration_priority = None
            continue
        link, text = pending
        name = _audio_assets.speech_asset_name(state, text)
        request = _audio_policy.bind_narration_request(state, link.key, name)
        try:
            result = await _audio_assets.ensure_speech(bb, state, text)
            if result is None:
                if state.narration_priority == link.key:
                    state.narration_priority = None
                _audio_policy.finish_narration_request(
                    state, request, _limits.NARRATION_ERROR
                )
                await asyncio.sleep(30)
            else:
                if state.narration_priority == link.key:
                    state.narration_priority = None
                _audio_policy.finish_narration_request(
                    state, request, _limits.NARRATION_READY
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _limits.logger.warning("prebake failed: %s", exc)
            if state.narration_priority == link.key:
                state.narration_priority = None
            _audio_policy.finish_narration_request(
                state, request, _limits.NARRATION_ERROR
            )
            await asyncio.sleep(30)


async def prepare_narration_cache(bb, state: _model.State) -> None:
    """Adopt device speech first, then run the indefinite low-priority baker."""
    await _audio_assets.load_speech_cache(bb, state)
    await prebake(bb, state)

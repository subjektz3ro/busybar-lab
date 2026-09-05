"""Skystrip audio / report."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from busylib import exceptions

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app.audio import output as _audio_output
from apps.skystrip_app.audio import report_assets as _audio_report_assets
from apps.skystrip_app.audio import report_policy as _audio_report_policy
from apps.skystrip_app.device import report_status as _device_report_status
from busybar_dev.device import is_refusal as _is_refusal
from busybar_dev.tts import synth_snd_async


async def _finish_report_ready(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest,
) -> None:
    if _audio_report_policy._report_status_is_current(
        state, request, _limits.REPORT_READY
    ):
        await _device_report_status._show_report_status(
            bb, state, request, _limits.REPORT_READY
        )
    else:
        await _device_report_status._retire_report_statuses(bb, state, request)
    _audio_report_policy._finish_report_request(state, request)


async def _prepare_report_take(bb, state: _model.SkyState, text: str) -> str:
    """Adopt or fully prepare one exact take (background/CLI path only)."""
    resident = await _audio_report_assets._adopt_report_take(bb, state, text)
    if resident is not None:
        return resident
    snd = await synth_snd_async(text)
    fname, _ = await _audio_report_assets._ensure_report_take(bb, state, text, snd)
    return fname


async def _report_prepare_worker(bb, state: _model.SkyState) -> None:
    """One managed synth+upload lane, with the newest requested text queued."""
    owner = asyncio.current_task()
    try:
        while state.report_prepare_text is not None:
            text = state.report_prepare_text
            try:
                await _prepare_report_take(bb, state, text)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                _limits.logger.warning("report bake failed: %s", exc)
                request = state.report_request
                if request is not None and request.text == text:
                    await _device_report_status._finish_report_failure(
                        bb, state, request
                    )
            else:
                _limits.logger.info("report baked: %r", text)
                request = state.report_request
                if request is not None and request.text == text:
                    latest = _audio_report_policy._current_report_text(state)
                    if (
                        latest != text
                        and _audio_report_policy._report_request_is_current(
                            state, request
                        )
                    ):
                        # Weather changed while the Pi was speaking. Keep the
                        # same fenced user intent, prepare only the newest
                        # truthful line, and never post a non-actionable READY.
                        state.report_request = replace(request, text=latest)
                        state.report_prepare_pending = latest
                        state.report_prepare_pending_priority = True
                    else:
                        await _finish_report_ready(bb, state, request)

            pending = state.report_prepare_pending
            state.report_prepare_pending = None
            state.report_prepare_pending_priority = False
            if pending is None:
                break
            state.report_prepare_text = pending
    finally:
        if state.report_prepare_task is owner:
            state.report_prepare_task = None
            state.report_prepare_text = None
        request = state.report_request
        if request is not None:
            # Cancellation/shutdown cannot leave a logical intent alive. Any
            # accepted card still has only its native three-second lease.
            _audio_report_policy._finish_report_request(state, request)


def _queue_report_prepare(
    bb,
    state: _model.SkyState,
    text: str,
    *,
    priority: bool,
    force: bool = False,
) -> asyncio.Task:
    """Wake or reprioritize the single background report preparation lane."""
    task = state.report_prepare_task
    if task is not None and not task.done():
        if state.report_prepare_text == text and not force:
            if priority:
                # This exact user request makes unrelated background work that
                # had been queued behind it obsolete.
                state.report_prepare_pending = None
                state.report_prepare_pending_priority = False
            return task
        if priority or not state.report_prepare_pending_priority:
            state.report_prepare_pending = text
            state.report_prepare_pending_priority = priority
        return task

    state.report_prepare_text = text
    state.report_prepare_pending = None
    state.report_prepare_pending_priority = False
    task = _model.spawn_owned(state, _report_prepare_worker(bb, state))
    state.report_prepare_task = task
    return task


async def bake_report(bb, state: _model.SkyState) -> None:
    """Keep a freshly voiced report waiting on the bar. Whenever live
    data changes the report's WORDS, re-synthesize in the background —
    a double press then plays a file that already exists (Kokoro on
    the Pi runs ~1x realtime, too slow to synth at press time)."""
    # Deterministic report paths survive the transient-asset startup sweep and
    # are adopted before synthesis; old timestamp generations are still swept.
    await asyncio.sleep(10)  # let the first observations land
    while True:
        try:
            if state.forecast or state.hourly:
                text = _audio_report_policy._current_report_text(state)
                if text != state.report_text:
                    _queue_report_prepare(bb, state, text, priority=False)
            await asyncio.sleep(_limits.BAKE_CHECK_S)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _limits.logger.warning("report bake failed: %s", exc)
            await asyncio.sleep(_limits.BAKE_CHECK_S)


async def _settle_report_audio(bb, state: _model.SkyState) -> bool:
    """Resolve an ambiguous older PLAY within the interaction budget."""
    if not state.audio_stop_pending:
        return True
    generation = state.audio_generation
    try:
        await asyncio.wait_for(
            _audio_output.stop_audio(bb, state, generation), _limits.REPORT_IO_TIMEOUT_S
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - BUSY is the safe outcome
        _limits.logger.warning("report audio STOP did not settle: %s", exc)
    return not state.audio_stop_pending


async def weather_report(bb, state: _model.SkyState) -> None:
    """Play only a resident report; prepare a cache miss in the background.

    A miss posts PREPARING, prioritizes one managed synth+upload worker, and
    returns. Completion posts START TWICE for the exact still-current view;
    it never auto-plays thirty seconds after the initiating button press.
    """
    text = _audio_report_policy._current_report_text(state)
    request = _audio_report_policy._begin_report_request(state, text)
    if not _audio_report_policy._report_request_is_current(state, request):
        _audio_report_policy._finish_report_request(state, request)
        return

    cached = state.report_file if state.report_text == text else None
    repair = False
    if cached:  # the baked take: instant playback
        _limits.logger.info(
            "double press — playing baked report: %r", state.report_text
        )
        try:
            if not await _device_report_status._retire_report_statuses(bb, state):
                _audio_report_policy._finish_report_request(state, request)
                return
            if not await _settle_report_audio(bb, state):
                await _device_report_status._finish_report_failure(
                    bb, state, request, _limits.REPORT_AUDIO_BUSY
                )
                return
            played = await asyncio.wait_for(
                _audio_output._play_audio(
                    bb,
                    state,
                    cached,
                    "report",
                    lambda: _audio_report_policy._report_play_is_current(
                        state, request, cached
                    ),
                ),
                _limits.REPORT_IO_TIMEOUT_S,
            )
            if played:
                _audio_report_policy._finish_report_request(state, request)
                return
            if _audio_report_policy._report_request_is_current(
                state, request
            ) and not _audio_report_policy._report_play_is_current(
                state, request, cached
            ):
                # The report changed while retirement/PLAY was in flight.
                # Re-enter as a cache miss for the newest exact text; never
                # describe a stale take as merely AUDIO BUSY.
                _audio_report_policy._finish_report_request(state, request)
                await weather_report(bb, state)
                return
            if _audio_report_policy._report_request_is_current(state, request):
                await _device_report_status._finish_report_failure(
                    bb, state, request, _limits.REPORT_AUDIO_BUSY
                )
            else:
                _audio_report_policy._finish_report_request(state, request)
            return
        except asyncio.CancelledError:
            _audio_report_policy._finish_report_request(state, request)
            raise
        except exceptions.BusyBarAPIError as exc:
            if _is_refusal(exc):
                _limits.logger.debug(
                    "baked report yielded to the active device session"
                )
                await _device_report_status._finish_report_failure(
                    bb, state, request, _limits.REPORT_AUDIO_BUSY
                )
                return
            if getattr(exc, "status_code", None) != 404:
                _limits.logger.warning("baked report failed: %s", exc)
                await _settle_report_audio(bb, state)
                await _device_report_status._finish_report_failure(bb, state, request)
                return
            # A definite missing/unplayable path never opened. Invalidate it
            # before queuing a new immutable generation.
            _audio_report_assets._mark_report_unplayable(state, cached, text)
            repair = True
        except Exception as exc:  # noqa: BLE001
            # A lost/timed-out PLAY response may already be audible. Ordered
            # STOP settles before the error card; never synthesize another take.
            _limits.logger.warning("baked report failed: %s", exc)
            await _settle_report_audio(bb, state)
            await _device_report_status._finish_report_failure(bb, state, request)
            return

    try:
        acknowledged = await _device_report_status._show_report_status(
            bb,
            state,
            request,
            _limits.REPORT_PREPARING,
        )
    except asyncio.CancelledError:
        _audio_report_policy._finish_report_request(state, request)
        raise
    if not acknowledged:
        _audio_report_policy._finish_report_request(state, request)
        return

    # Cache adoption/publication can win while the status POST is in flight.
    # It is still a miss interaction: acknowledge readiness, never autoplay.
    if state.report_file is not None and state.report_text == text:
        await _finish_report_ready(bb, state, request)
        return

    _limits.logger.info("double press — preparing weather report: %r", text)
    _queue_report_prepare(bb, state, text, priority=True, force=repair)

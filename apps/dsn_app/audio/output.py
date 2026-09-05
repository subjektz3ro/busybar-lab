"""DSN audio / output."""

from __future__ import annotations

import asyncio

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry


def narration_play_is_current(
    state: _model.State, link: _source.Link, generation: int, view: str
) -> bool:
    """Whether an in-flight PLAY result still belongs to this interaction."""
    return (
        state.narration_request_counter == generation
        and state.view == view
        and _telemetry.feed_freshness(state) == "fresh"
        and any(live.key == link.key for live in state.links)
    )


def claim_audio_stop(state: _model.State) -> int:
    """Synchronously invalidate older PLAY ownership and return STOP's token."""
    if state.audio_stop_pending and state.audio_stop_generation is not None:
        return state.audio_stop_generation
    state.audio_generation += 1
    generation = state.audio_generation
    state.audio_stop_generation = generation
    state.audio_stop_pending = True
    return generation


async def stop_audio_bounded(
    bb,
    state: _model.State,
    reason: str,
    generation: int | None = None,
) -> None:
    """Neutralise a possibly accepted PLAY without delaying navigation.

    STOP and PLAY are opposite mutations of one device resource.  A lock
    prevents their requests from crossing, while the generation makes a
    queued retry harmless after a newer PLAY has taken ownership.
    """
    if generation is not None:
        # A deferred retry captures the intent it is retrying. If a newer
        # generation already won, it must not invent a fresh STOP on arrival.
        if not state.audio_stop_pending or state.audio_stop_generation != generation:
            return
    elif state.audio_stop_pending and state.audio_stop_generation is not None:
        generation = state.audio_stop_generation
    else:
        generation = claim_audio_stop(state)
    async with state.audio_io:
        if not state.audio_stop_pending or state.audio_stop_generation != generation:
            return
        try:
            await asyncio.wait_for(bb.audio_stop(), _limits.INTERACTIVE_IO_TIMEOUT_S)
        except Exception as exc:  # noqa: BLE001 - interaction still has to return
            if getattr(exc, "status_code", None) == 410:
                # DELETE /audio/play uses 410 for "no audio is playing". That
                # is already the exact postcondition STOP requested.
                state.audio_stop_pending = False
                state.audio_stop_retry_at = 0.0
                state.audio_stop_generation = None
            else:
                state.audio_stop_retry_at = asyncio.get_running_loop().time() + 30.0
                _limits.logger.debug("%s audio stop failed: %s", reason, exc)
        else:
            state.audio_stop_pending = False
            state.audio_stop_retry_at = 0.0
            state.audio_stop_generation = None


async def shutdown_audio_bounded(
    bb,
    state: _model.State,
    speech_tasks: list[asyncio.Task],
) -> set[asyncio.Task]:
    """Cancel narration and fence its final STOP behind every older PLAY.

    Claiming STOP is synchronous, so no already-started PLAY still owns the
    current generation once cancellation begins.  The final device mutation
    then uses the ordinary audio lock: a PLAY which is slow to acknowledge
    cancellation must finish (or release its request) before STOP can be
    issued.  If it never releases the lock, the bounded STOP attempt is
    cancelled instead of sending an unordered STOP which that PLAY could
    overtake later. If PLAY settles exactly as that attempt expires, retry
    the same STOP generation once after task settlement closes the boundary
    where a committed PLAY could otherwise escape without a final STOP.
    """
    needs_stop = bool(state.speaking or speech_tasks or state.audio_stop_pending)
    stop_generation = claim_audio_stop(state) if needs_stop else None

    for task in speech_tasks:
        task.cancel()
    pending: set[asyncio.Task] = set()
    if speech_tasks:
        done, pending = await asyncio.wait(
            speech_tasks, timeout=_limits.SHUTDOWN_TIMEOUT_S
        )
        if done:
            await asyncio.gather(*done, return_exceptions=True)

    async def settle_stop(reason: str) -> bool:
        if stop_generation is None:
            return True
        try:
            await asyncio.wait_for(
                stop_audio_bounded(bb, state, reason, stop_generation),
                _limits.SHUTDOWN_TIMEOUT_S,
            )
        except TimeoutError:
            return False
        except Exception as exc:  # noqa: BLE001 - shutdown remains bounded
            _limits.logger.debug("shutdown audio stop failed: %s", exc)
            return False
        return (
            not state.audio_stop_pending
            or state.audio_stop_generation != stop_generation
        )

    stop_settled = await settle_stop("shutdown")

    if pending:
        # The fence above may have released whatever a cancelled task was
        # still blocked on, so give those a brief bounded window to land
        # before reporting them as unfinished.
        #
        # Sampling `task.done()` at this instant instead made the answer
        # depend on event-loop scheduling: a released task partway through
        # unwinding its own await points reads as unfinished on one platform
        # and finished on another. macOS happened to schedule the unwind
        # first and Linux -- the platform this deploys to -- did not, so the
        # suite was green on the laptop and red in CI for the same commit.
        settled, pending = await asyncio.wait(
            pending, timeout=_limits.SHUTDOWN_SETTLE_S
        )
        if settled:
            await asyncio.gather(*settled, return_exceptions=True)

    if not stop_settled and not pending:
        # The first STOP can hit its deadline on the same event-loop turn in
        # which a cancellation-resistant PLAY releases audio_io and commits.
        # Once every tracked PLAY task has settled, one same-generation retry
        # is ordered, safe, and closes that otherwise silent escape hatch.
        stop_settled = await settle_stop("shutdown retry")
    if not stop_settled:
        # Never bypass audio_io here. No STOP is safer than a STOP which an
        # older, cancellation-resistant PLAY can overtake after client close.
        _limits.logger.warning("audio shutdown fence missed the deadline")
    return pending

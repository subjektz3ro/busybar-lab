"""Skystrip audio / output."""

from __future__ import annotations

from busylib import types

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from busybar_dev.device import is_refusal as _is_refusal


def _alert_payload(filename: str, prefix: tuple = ()) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        led_notification_color="#FF2222FF",
        elements=[
            *prefix,
            types.AnimationElement(
                id="alert",
                type="animation",
                path=filename,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=_limits.ALERT_ELEMENT_TIMEOUT_S,
            ),
        ],
    )


def _audio_already_stopped(exc: Exception) -> bool:
    return (
        getattr(exc, "status_code", None) == 410
        or "already stopped" in str(exc).lower()
    )


async def _stop_audio_locked(bb, state: _model.SkyState, generation: int) -> bool:
    """Generation-owned STOP; caller holds ``state.audio_lock``."""
    if generation != state.audio_generation:
        return False
    try:
        await bb.audio_stop()
    except Exception as exc:  # noqa: BLE001
        if not _audio_already_stopped(exc):
            state.audio_stop_pending = True
            _limits.logger.warning("audio stop failed; will retry: %s", exc)
            return False
    if generation == state.audio_generation:
        state.audio_owner = None
        state.audio_path = None
        state.audio_stop_pending = False
        return True
    return False


async def stop_audio(bb, state: _model.SkyState, generation: int) -> bool:
    """Run the generation-owned STOP after every older PLAY has settled."""
    async with state.audio_lock:
        return await _stop_audio_locked(bb, state, generation)


async def _play_audio(
    bb,
    state: _model.SkyState,
    path: str,
    owner: str,
    still_valid,
) -> bool:
    """Serialize PLAY and revalidate its intent while holding the audio lane."""
    if state.audio_stop_pending:
        return False
    state.audio_generation += 1
    generation = state.audio_generation
    async with state.audio_lock:
        if (
            generation != state.audio_generation
            or state.audio_stop_pending
            or state.shutting_down
            or not still_valid()
        ):
            return False
        state.audio_owner = f"{owner}-pending"
        state.audio_path = path
        try:
            await bb.audio_play(path=path, application_name=_limits.APP_NAME)
        except Exception as exc:  # noqa: BLE001
            if _is_refusal(exc) or getattr(exc, "status_code", None) == 404:
                # 409 is a definite refusal; PLAY 404 is a definite missing or
                # unplayable asset. Neither committed audio, so answering with
                # the device-global STOP could silence somebody else's app.
                if generation == state.audio_generation:
                    state.audio_owner = None
                    state.audio_path = None
                    state.audio_stop_pending = False
                raise
            if generation == state.audio_generation:
                state.audio_stop_pending = True
            raise
        except BaseException:
            if generation == state.audio_generation:
                # A transport/cancellation may have committed PLAY remotely.
                state.audio_stop_pending = True
            raise
        if (
            generation == state.audio_generation
            and not state.shutting_down
            and still_valid()
        ):
            state.audio_owner = owner
            return True
        if generation == state.audio_generation:
            # PLAY was accepted after its view/alert intent changed. Claim and
            # issue the newer STOP in this same serialized lane, so no later
            # navigation draw can be followed by stale report audio.
            stop_generation = _alerts._claim_audio_stop(state)
            await _stop_audio_locked(bb, state, stop_generation)
        return False

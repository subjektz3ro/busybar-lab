"""Skystrip device / alerts."""

from __future__ import annotations

import asyncio

from busylib import exceptions

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app.audio import output as _audio_output
from apps.skystrip_app.audio import siren as _audio_siren
from apps.skystrip_app.device import ambient as _device_ambient
from apps.skystrip_app.device import assets as _device_assets
from apps.skystrip_app.device import display as _device_display
from apps.skystrip_app.device import report_status as _device_report_status


async def acknowledge_alert(bb, state: _model.SkyState, reason: str) -> bool:
    """Consume one input as acknowledgement, STOP, and restore the chosen view."""
    active = state.visual_alert is not None or state.weather.severe
    if not active and not state.alert_dismiss_pending:
        return False
    if not state.alert_acked:
        state.alert_acked = True
        state.alert_generation += 1
        state.alert_drawn_generation = -1
        if state.scrub_slot is not None:
            # An alert can hold the selected Time Machine view longer than
            # its ordinary idle lease. Acknowledgement restores that exact
            # view, so give it a fresh lease instead of snapping to NOW on
            # the very next main-loop tick.
            state.scrub_touched = asyncio.get_running_loop().time()
        _limits.logger.warning("weather alert acknowledged by %s", reason)
    state.alert_dismiss_pending = True
    _alerts._signal_alert_change(state)
    generation = _alerts._claim_audio_stop(state)
    if state.audio_stop_pending:
        await _audio_output.stop_audio(bb, state, generation)
    await _device_display.restore_current_view(bb, state)
    return True


async def _wait_for_alert_change(
    state: _model.SkyState,
    timeout: float,
    observed_generation: int,
) -> None:
    """Wait without clearing a transition that lands at the timeout edge."""
    if state.alert_wake_generation != observed_generation:
        return
    # Clear only the level that existed when the caller captured its state,
    # then re-check the monotonic generation. A change racing this clear still
    # returns immediately even though Event.clear() erased its level bit.
    state.alert_changed.clear()
    if state.alert_wake_generation != observed_generation:
        return
    try:
        await asyncio.wait_for(state.alert_changed.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass


async def severe_alarm(bb, state: _model.SkyState) -> None:
    """Present Severe/Extreme CAP alerts; sound only exact Extreme severity."""
    last_siren = -1e9
    last_siren_generation = -1
    try:
        while True:
            try:
                wake_generation = state.alert_wake_generation
                if state.audio_stop_pending:
                    await _audio_output.stop_audio(bb, state, state.audio_generation)

                alert = state.visual_alert
                generation = state.alert_generation
                if generation != last_siren_generation:
                    last_siren = -1e9
                    last_siren_generation = generation
                if alert is not None and not state.alert_acked:
                    filename = await _device_assets.ensure_alert_asset(
                        bb, state, alert, generation
                    )
                    if (
                        filename is None
                        or state.alert_generation != generation
                        or state.alert_acked
                        or state.visual_alert is None
                    ):
                        await _wait_for_alert_change(
                            state, _limits.ALERT_ASSET_RETRY_S, wake_generation
                        )
                        continue
                    async with state.display_lock:
                        report_statuses = _device_report_status._live_report_statuses(
                            state
                        )
                        await bb.display_draw(
                            _audio_output._alert_payload(
                                filename,
                                tuple(
                                    _device_report_status._retired_report_status_elements(
                                        report_statuses
                                    )
                                ),
                            )
                        )
                        _device_report_status._forget_report_statuses(
                            state, report_statuses
                        )
                    state.alert_drawn_generation = generation

                    # Revalidate after the awaited draw.  This is the race that
                    # previously allowed PLAY to cross an acknowledgement STOP.
                    now = asyncio.get_running_loop().time()
                    siren = state.siren_alert
                    if (
                        siren is not None
                        and state.siren_file is not None
                        and now - last_siren >= _limits.SIREN_RETRIGGER_S
                        and state.alert_generation == generation
                        and not state.alert_acked
                    ):
                        siren_path = state.siren_file
                        try:
                            played = await _audio_output._play_audio(
                                bb,
                                state,
                                siren_path,
                                "alert",
                                # The three loop values are bound as defaults
                                # rather than captured: this predicate decides
                                # whether an already-accepted PLAY still belongs
                                # to the intent that asked for it, so it must
                                # compare against the generation, alert and path
                                # that were current when it was built — never
                                # whatever the next loop pass has moved on to.
                                # `state` stays late-bound on purpose; the whole
                                # point is to read it fresh after the await.
                                lambda gen=generation, armed=siren, path=siren_path: (
                                    state.alert_generation == gen
                                    and not state.alert_acked
                                    and state.siren_alert is not None
                                    and state.siren_alert.identifier == armed.identifier
                                    and state.siren_file == path
                                ),
                            )
                        except exceptions.BusyBarAPIError as exc:
                            if getattr(exc, "status_code", None) != 404:
                                raise
                            _audio_siren.mark_siren_unplayable(state, siren_path)
                            _limits.logger.error(
                                "extreme-weather siren %s is missing or "
                                "unplayable; provisioning a repair",
                                siren_path,
                            )
                            played = False
                        if played:
                            last_siren = now
                    await _wait_for_alert_change(
                        state, _limits.ALERT_REDRAW_S, wake_generation
                    )
                elif alert is not None:  # acknowledged: view stays, red pulse stays
                    if state.alert_dismiss_pending:
                        await _device_display.restore_current_view(bb, state)
                    async with state.display_lock:
                        await bb.display_draw(
                            _device_ambient._led_ping_payload("#FF2222FF")
                        )
                    last_siren = -1e9
                    await _wait_for_alert_change(
                        state, _limits.ALERT_REDRAW_S, wake_generation
                    )
                else:
                    if state.audio_owner in {"alert", "alert-pending"}:
                        stop_generation = _alerts._claim_audio_stop(state)
                        await _audio_output.stop_audio(bb, state, stop_generation)
                    if state.alert_dismiss_pending:
                        await _device_display.restore_current_view(bb, state)
                    last_siren = -1e9
                    await _wait_for_alert_change(state, 1, wake_generation)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - busy session refuses draws
                _limits.logger.debug("alarm tick failed: %s", exc)
                await _wait_for_alert_change(state, 2, wake_generation)
    finally:
        if state.audio_owner in {"alert", "alert-pending"} or state.audio_stop_pending:
            generation = _alerts._claim_audio_stop(state)
            await _audio_output.stop_audio(bb, state, generation)

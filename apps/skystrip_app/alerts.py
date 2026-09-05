"""Skystrip alerts."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from busybar_dev.weather_alerts import Alert, preserve_acknowledgement


def _alert_signature(alert: Alert | None) -> tuple | None:
    if alert is None:
        return None
    return (
        alert.identifier,
        alert.references,
        alert.event,
        alert.headline,
        alert.severity,
        alert.urgency,
        alert.certainty,
        alert.effective,
        alert.expires,
        alert.ends,
    )


def _claim_audio_stop(state: _model.SkyState, *, force: bool = False) -> int:
    """Invalidate every older PLAY before yielding to the device.

    This synchronous generation bump is the important half of STOP.  A siren
    task blocked in a display POST can resume later, but it can no longer pass
    the generation check immediately before AUDIO PLAY.
    """
    owned = state.audio_owner is not None or state.audio_stop_pending
    state.audio_generation += 1
    # audio_stop is global on the device.  Invalidating a not-yet-started PLAY
    # must not silence audio owned by BUSY or another app.
    state.audio_stop_pending = force or owned
    return state.audio_generation


def _apply_alert_selection(
    state: _model.SkyState,
    visual: Alert | None,
    siren: Alert | None,
    alerts: tuple[Alert, ...] = (),
) -> bool:
    """Atomically install one authoritative CAP selection.

    Returns whether presentation/audio work is needed.  Acknowledgement follows
    CAP identifier/reference lineage only for routine updates; a new episode or
    a material escalation always re-arms.
    """
    previous_visual = state.visual_alert
    previous_siren = state.siren_alert
    old_visual_sig = _alert_signature(previous_visual)
    old_siren_sig = _alert_signature(previous_siren)
    new_visual_sig = _alert_signature(visual)
    new_siren_sig = _alert_signature(siren)
    changed = (old_visual_sig, old_siren_sig) != (new_visual_sig, new_siren_sig)

    was_acked = state.alert_acked
    if visual is None:
        acknowledged = False
    elif previous_visual is not None and old_visual_sig == new_visual_sig:
        acknowledged = was_acked
    else:
        acknowledged = was_acked and preserve_acknowledgement(previous_visual, visual)

    state.active_alerts = alerts
    state.visual_alert = visual
    state.siren_alert = siren
    state.alert_acked = acknowledged
    state.weather = replace(
        state.weather,
        severe=visual is not None,
        severe_event=visual.event if visual is not None else "",
    )

    if changed:
        state.alert_generation += 1
        state.alert_drawn_generation = -1
        if visual is None and previous_visual is not None:
            state.alert_dismiss_pending = True
        elif visual is not None:
            state.alert_dismiss_pending = False

        rearmed = (
            visual is not None
            and not acknowledged
            and (
                previous_visual is None
                or not preserve_acknowledgement(previous_visual, visual)
            )
        )
        if previous_siren != siren or rearmed or visual is None:
            _claim_audio_stop(state)
        _signal_alert_change(state)

        if visual is None:
            _limits.logger.info("weather alert all-clear")
        else:
            _limits.logger.warning(
                "weather alert active: %s (CAP severity %s; siren %s)",
                visual.event,
                visual.severity,
                "armed" if siren is not None and not acknowledged else "off",
            )
    return changed


def _locally_active_alerts(
    alerts: tuple[Alert, ...],
    now: datetime,
) -> tuple[Alert, ...]:
    """Expire last-good CAP state on its own source deadlines during outage."""
    return tuple(
        alert
        for alert in alerts
        if alert.expires > now and (alert.ends is None or alert.ends > now)
    )


def _signal_alert_change(state: _model.SkyState) -> None:
    """Publish a level wake plus a generation that cannot be cleared away."""
    state.alert_wake_generation += 1
    state.alert_changed.set()


def _stand_down_nws_alerts(state: _model.SkyState) -> None:
    """Clear every NWS alert claim at the confirmed `/points` boundary."""
    if (
        state.active_alerts
        or state.visual_alert is not None
        or state.siren_alert is not None
    ):
        _apply_alert_selection(state, None, None, ())
    state.alert_known = True


def _unacknowledged_alert_active(state: _model.SkyState) -> bool:
    """Whether an alert still owns the display and the next user gesture."""
    return (
        state.visual_alert is not None or state.weather.severe
    ) and not state.alert_acked

"""Skystrip audio / report policy."""

from __future__ import annotations

from datetime import datetime

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app.audio import report_plain as _audio_report_plain


def _current_report_text(state: _model.SkyState) -> str:
    return _audio_report_plain._compose_report(
        state.weather, state.forecast, datetime.now(_settings.TZ), state.hourly
    )


def _begin_report_request(state: _model.SkyState, text: str) -> _model.ReportRequest:
    """Claim one explicit report request and invalidate any older worker."""
    state.report_generation += 1
    request = _model.ReportRequest(
        state.report_generation,
        state.view_generation,
        state.alert_generation,
        text,
    )
    state.report_request = request
    return request


def _report_request_is_current(
    state: _model.SkyState,
    request: _model.ReportRequest,
) -> bool:
    """A slow synthesis may act only for the exact view that requested it."""
    return (
        state.report_request == request
        and state.report_generation == request.generation
        and state.view_generation == request.view_generation
        and state.alert_generation == request.alert_generation
        and not state.shutting_down
        and not _alerts._unacknowledged_alert_active(state)
    )


def _report_status_is_current(
    state: _model.SkyState,
    request: _model.ReportRequest,
    label: str,
) -> bool:
    if not _report_request_is_current(state, request):
        return False
    if label != _limits.REPORT_READY:
        return True
    return (
        state.report_file is not None
        and state.report_text == request.text
        and _current_report_text(state) == request.text
    )


def _report_play_is_current(
    state: _model.SkyState,
    request: _model.ReportRequest,
    path: str,
) -> bool:
    """A cached PLAY remains bound to the exact resident/current words."""
    return (
        _report_request_is_current(state, request)
        and state.report_file == path
        and state.report_text == request.text
        and _current_report_text(state) == request.text
    )


def _finish_report_request(
    state: _model.SkyState, request: _model.ReportRequest
) -> None:
    if state.report_request == request:
        state.report_request = None

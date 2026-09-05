"""Skystrip device / report status."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from busylib import exceptions, types

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app.audio import report_policy as _audio_report_policy
from busybar_dev.device import is_refusal as _is_refusal
from busybar_dev.pixel_text import device_text


def _live_report_statuses(state: _model.SkyState) -> list[_model.ReportStatus]:
    """Forget cards whose native whole-second timeout has certainly elapsed."""
    now = asyncio.get_running_loop().time()
    state.report_statuses[:] = [
        status for status in state.report_statuses if status.expires_at > now
    ]
    return list(state.report_statuses)


def _report_status_elements(status: _model.ReportStatus, timeout: int) -> list:
    """Stable geometry for one native status generation and its retirement."""
    suffix = status.element_generation
    return [
        types.RectangleElement(
            id=f"reportbg{suffix}",
            type="rectangle",
            x=0,
            y=0,
            width=_limits.W,
            height=_limits.H,
            fill="solid",
            fill_colors=["#000000FF"],
            border_width=0,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
        types.TextElement(
            id=f"reporttx{suffix}",
            type="text",
            text=device_text(status.label),
            font="condensed",
            color="#E3B15DFF",
            align="center",
            x=36,
            y=8,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
    ]


def _retired_report_status_elements(statuses: list[_model.ReportStatus]) -> list:
    return [
        element
        for status in statuses
        for element in _report_status_elements(status, timeout=1)
    ]


def _stale_report_statuses(state: _model.SkyState) -> list[_model.ReportStatus]:
    """Cards no longer owned by the exact still-current report request."""
    live = _live_report_statuses(state)
    request = state.report_request
    current_generation = (
        request.generation
        if request is not None
        and _audio_report_policy._report_request_is_current(state, request)
        else None
    )
    return [
        status
        for status in live
        if not (
            status.request_generation == current_generation
            or (
                status.terminal
                and status.view_generation == state.view_generation
                and status.alert_generation == state.alert_generation
                and not state.shutting_down
                and not _alerts._unacknowledged_alert_active(state)
            )
        )
    ]


def _forget_report_statuses(
    state: _model.SkyState,
    statuses: list[_model.ReportStatus],
) -> None:
    retired = set(statuses)
    state.report_statuses[:] = [
        status for status in state.report_statuses if status not in retired
    ]


async def _post_report_retirement_locked(
    bb,
    state: _model.SkyState,
    statuses: list[_model.ReportStatus],
) -> bool:
    """Retire exact possibly-live ids; ``state.display_lock`` is held."""
    if not statuses:
        return True
    try:
        await asyncio.wait_for(
            bb.display_draw(
                types.DisplayElements(
                    application_name=_limits.APP_NAME,
                    priority=_limits.PRIORITY,
                    elements=_retired_report_status_elements(statuses),
                )
            ),
            _limits.REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            _limits.logger.debug("report status retirement yielded to device owner")
        else:
            _limits.logger.warning("report status retirement rejected: %s", exc)
        return False
    except TimeoutError:
        _limits.logger.warning(
            "report status retirement exceeded %.1fs", _limits.REPORT_IO_TIMEOUT_S
        )
        return False
    except Exception as exc:  # noqa: BLE001 - a lost response may have committed
        _limits.logger.warning("report status retirement failed: %s", exc)
        return False
    _forget_report_statuses(state, statuses)
    return True


async def _retire_report_statuses_serialized(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest | None = None,
) -> bool:
    """Exact-id retirement while owning the one display lane."""
    async with state.display_lock:
        live = _live_report_statuses(state)
        targets = (
            live
            if request is None
            else [
                status
                for status in live
                if status.request_generation == request.generation
            ]
        )
        return await _post_report_retirement_locked(bb, state, targets)


async def _retire_report_statuses(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest | None = None,
) -> bool:
    """Bound lock acquisition plus POST for an interactive retirement."""
    try:
        return await asyncio.wait_for(
            _retire_report_statuses_serialized(bb, state, request),
            _limits.REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        _limits.logger.warning(
            "report status retirement missed %.1fs interaction bound",
            _limits.REPORT_IO_TIMEOUT_S,
        )
        return False


async def _show_report_status_serialized(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest,
    label: str,
    timeout: int = _limits.REPORT_STATUS_TIMEOUT_S,
) -> bool:
    """Replace prior report cards and draw truthful feedback immediately.

    The candidate enters the registry before POST because a transport timeout
    can mean the device committed it. A definite API refusal removes only the
    candidate; an accepted draw retires every older card in the same payload.
    """
    async with state.display_lock:
        if not _audio_report_policy._report_status_is_current(state, request, label):
            return False
        previous = _live_report_statuses(state)
        state.report_status_generation += 1
        started = asyncio.get_running_loop().time()
        candidate = _model.ReportStatus(
            request.generation,
            state.report_status_generation,
            label,
            started + _limits.REPORT_IO_TIMEOUT_S + timeout,
            request.view_generation,
            request.alert_generation,
            label
            in {
                _limits.REPORT_READY,
                _limits.REPORT_AUDIO_BUSY,
                _limits.REPORT_AUDIO_ERROR,
            },
        )
        state.report_statuses.append(candidate)
        payload = types.DisplayElements(
            application_name=_limits.APP_NAME,
            priority=_limits.PRIORITY,
            elements=[
                *_retired_report_status_elements(previous),
                *_report_status_elements(candidate, timeout),
            ],
        )
        try:
            await asyncio.wait_for(
                bb.display_draw(payload),
                _limits.REPORT_IO_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except exceptions.BusyBarAPIError as exc:
            # An API response is a definite rejection, unlike a lost transport
            # response. Keep older cards because their retirement also failed.
            _forget_report_statuses(state, [candidate])
            if _is_refusal(exc):
                _limits.logger.debug("report status yielded to device owner")
            else:
                _limits.logger.warning("report status rejected: %s", exc)
            return False
        except TimeoutError:
            _limits.logger.warning(
                "report status draw exceeded %.1fs", _limits.REPORT_IO_TIMEOUT_S
            )
            return False
        except Exception as exc:  # noqa: BLE001 - POST may have committed
            _limits.logger.warning("report status draw failed: %s", exc)
            return False

        committed = replace(
            candidate,
            expires_at=asyncio.get_running_loop().time() + timeout,
        )
        _forget_report_statuses(state, [*previous, candidate])
        state.report_statuses.append(committed)
        if not _audio_report_policy._report_status_is_current(state, request, label):
            # A selector/alert can change while the POST is in flight. Keep
            # physical ordering deterministic: retirement lands before the
            # queued newer view can acquire this same display lane.
            await _post_report_retirement_locked(bb, state, [committed])
            return False
        return True


async def _show_report_status(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest,
    label: str,
    timeout: int = _limits.REPORT_STATUS_TIMEOUT_S,
) -> bool:
    """Bound the complete acknowledgement, including display-lock waiting."""
    try:
        return await asyncio.wait_for(
            _show_report_status_serialized(bb, state, request, label, timeout),
            _limits.REPORT_IO_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except TimeoutError:
        # If cancellation interrupted POST, its pre-registered ids remain
        # quarantined until their native lease expires or a later draw retires
        # them. If it interrupted lock acquisition, no card was registered.
        _limits.logger.warning(
            "report status missed %.1fs interaction bound", _limits.REPORT_IO_TIMEOUT_S
        )
        return False


async def _finish_report_failure(
    bb,
    state: _model.SkyState,
    request: _model.ReportRequest,
    label: str = _limits.REPORT_AUDIO_ERROR,
) -> None:
    """Atomically retire PREPARING and, when still relevant, show the error."""
    if _audio_report_policy._report_request_is_current(state, request):
        await _show_report_status(bb, state, request, label)
    else:
        await _retire_report_statuses(bb, state, request)
    _audio_report_policy._finish_report_request(state, request)

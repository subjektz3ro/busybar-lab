"""DSN device / events."""

from __future__ import annotations

import asyncio
import time

from busylib import exceptions, types

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import settings as _settings
from apps.dsn_app.device import assets as _device_assets
from apps.dsn_app.device import display as _device_display
from apps.dsn_app.render import events as _render_events
from apps.dsn_app.render import labels as _render_labels
from busybar_dev.device import is_refusal as _is_refusal


def start_event_asset_warm(bb, state: _model.State) -> asyncio.Task:
    """One tracked repair/prewarm worker for the finite event vocabulary."""
    if state.event_warm_task is not None and not state.event_warm_task.done():
        return state.event_warm_task
    state.event_warm_task = asyncio.create_task(
        _device_assets.prepare_event_assets(bb, state)
    )
    state.event_warm_task.add_done_callback(
        lambda task: (
            _limits.logger.debug("event asset warm failed: %s", task.exception())
            if not task.cancelled() and task.exception() is not None
            else None
        )
    )
    return state.event_warm_task


async def show_next_event(bb, state: _model.State) -> bool:
    """Show and acknowledge one queued transition only after an accepted draw."""
    cutoff = time.time() - _limits.EVENT_MAX_AGE_S
    state.event_queue[:] = [
        event
        for event in state.event_queue
        if not event.get("t") or event["t"] >= cutoff
    ]
    if (
        not state.event_queue
        or state.picking
        or state.speaking
        or state.ok_down_at is not None
        or state.realtime_since is not None
        or _selection.network_focus_active(state)
        or asyncio.get_running_loop().time() < state.interactive_visible_until
    ):
        return False
    event = state.event_queue[0]
    label = _render_labels.event_label(event)
    effect = _render_events.event_effect(event)
    dynamic_signature: tuple | None = None
    if _settings.DSN_NETWORK_STYLE == "skies" and event.get("event") == "handoff":
        # Generic handoff art cannot place two observations from independent
        # local coordinate frames. Generate the rare event-specific composed
        # card; if either aim or complete label is unavailable, exact native
        # scrolling text is the honest fallback.
        try:
            prepared = await _device_assets.prepare_handoff_echo_asset(bb, state, event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - text remains available
            _limits.logger.warning("handoff echo preparation failed: %s", exc)
            prepared = None
        if prepared is None:
            asset = None
        else:
            dynamic_signature, asset = prepared
    else:
        asset = state.event_assets.get(effect) if effect is not None else None

    async def draw_if_still_current(
        payload: types.DisplayElements,
        shown_asset: str | None,
        shown_embedded_label: bool = False,
    ) -> bool:
        """Serialize opaque event cards with newer wheel/readout feedback.

        The wheel marks ``picking`` before waiting on this lock. If this event
        POST began first, the picker therefore commits last; if the picker won
        the lock, this second gate suppresses the now-obsolete event card.
        """
        async with state.interactive_draw:
            if (
                not state.event_queue
                or state.event_queue[0] is not event
                or state.picking
                or state.speaking
                or state.ok_down_at is not None
                or state.realtime_since is not None
                or _selection.network_focus_active(state)
                or asyncio.get_running_loop().time() < state.interactive_visible_until
            ):
                return False
            await asyncio.wait_for(
                bb.display_draw(payload), _limits.INTERACTIVE_IO_TIMEOUT_S
            )
            state.active_event_label = label
            state.active_event_asset = shown_asset
            state.active_event_embedded_label = shown_embedded_label
            state.active_event_until = (
                asyncio.get_running_loop().time() + _limits.EVENT_TIMEOUT_S
            )
            return True

    try:
        if not await draw_if_still_current(
            _device_display._event_payload(
                label, asset, embedded_label=dynamic_signature is not None
            ),
            asset,
            dynamic_signature is not None,
        ):
            return False
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            return False
        if asset is not None and _device_assets._is_asset_path_failure(exc):
            # Keep the live event useful: fall back to the same native label
            # immediately, and repair the finite asset set in the background.
            if dynamic_signature is not None:
                await _device_assets.discard_scene_asset(
                    bb, state, dynamic_signature, asset
                )
            else:
                if effect is not None and state.event_assets.get(effect) == asset:
                    state.event_assets.pop(effect, None)
                try:
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{asset}"
                    )
                except Exception:  # noqa: BLE001 - it may already be absent
                    pass
                start_event_asset_warm(bb, state)
            try:
                if not await draw_if_still_current(
                    _device_display._event_payload(label, None), None, False
                ):
                    return False
            except Exception as fallback_exc:  # noqa: BLE001
                _limits.logger.warning("event fallback draw failed: %s", fallback_exc)
                return False
        else:
            _limits.logger.warning("event draw failed: %s", exc)
            return False
    except Exception as exc:  # noqa: BLE001
        _limits.logger.warning("event draw failed: %s", exc)
        return False
    for index, queued in enumerate(state.event_queue):
        if queued is event:
            state.event_queue.pop(index)
            break
    return True

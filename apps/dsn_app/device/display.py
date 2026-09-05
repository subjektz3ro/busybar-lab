"""DSN device / display."""

from __future__ import annotations

import asyncio
import time

from busylib import exceptions, types

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import text as _render_text
from busybar_dev.device import is_refusal as _is_refusal


def picker_label(state: _model.State) -> str:
    """What the picker shows: which signal, and where it sits in the list.

    The device font fits about 12 characters, so this is the feed's short
    code rather than the full name — 'MARS RECONNAISSANCE ORBITER' would
    overflow, and the point here is to move quickly, not to read.
    """
    total = len(state.links)
    if not total:
        return "NO SIGNAL"
    link = state.current()
    return _render_text.device_text(
        f"{link.craft if link else '?'} {state.cursor % total + 1}/{total}"
    )


def _picker_payload(
    label: str, timeout: int = 3, id_suffix: str = "", prefix: tuple = ()
) -> types.DisplayElements:
    """Native interaction layer with immutable geometry for each stable id.

    Runtime normally reuses one suffix. If it must interrupt a later-created
    opaque event card, the caller advances that finite layer generation once;
    this creates the picker above the event, whose own ids are retired in the
    same draw. Timed elements prevent abandoned generations from stacking.
    """
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        elements=[
            *prefix,
            types.RectangleElement(
                id=f"pickbg{id_suffix}",
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
                id=f"picktx{id_suffix}",
                type="text",
                text=_render_text.device_text(label),
                font="condensed",
                color="#FFD98CFF",
                align="center",
                x=36,
                y=8,
                scroll_rate=1400,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            ),
        ],
    )


def _interactive_payload(
    state: _model.State, label: str, timeout: int
) -> types.DisplayElements:
    """Build a picker/readout that also retires a visible event atomically."""
    now = asyncio.get_running_loop().time()
    prefix: tuple = ()
    if state.active_event_label is not None:
        if now < state.active_event_until:
            # Event ids may sit above the current picker layer. Retire them
            # and mint exactly one newer stable interaction layer in this
            # same accepted draw; no four-second card can resurface later.
            retire = _event_payload(
                state.active_event_label,
                state.active_event_asset,
                timeout=1,
                embedded_label=state.active_event_embedded_label,
            )
            prefix = tuple(retire.elements)
            state.interactive_layer += 1
        else:
            state.active_event_label = None
            state.active_event_asset = None
            state.active_event_embedded_label = False
            state.active_event_until = 0.0
    suffix = f"{state.rt_nonce}{state.interactive_layer}"
    return _picker_payload(label, timeout, suffix, prefix)


async def draw_picker(bb, state: _model.State, timeout: int = 3) -> None:
    """The pop-up that rides the wheel.

    Device text elements, not a re-rendered animation: a scene costs an 80 KB
    upload and about a second, which is far too slow to keep up with a wheel
    and would make every detent feel stuck.
    """
    try:
        async with state.interactive_draw:
            await asyncio.wait_for(
                bb.display_draw(
                    _interactive_payload(state, picker_label(state), timeout)
                ),
                _limits.INTERACTIVE_IO_TIMEOUT_S,
            )
            state.active_event_label = None
            state.active_event_asset = None
            state.active_event_embedded_label = False
            state.active_event_until = 0.0
            state.interactive_visible_until = (
                asyncio.get_running_loop().time() + timeout
            )
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.warning("picker draw failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _limits.logger.warning("picker draw failed: %s", exc)


async def _post_readout(bb, state: _model.State, label: str, timeout: int) -> bool:
    """Post native text; caller owns ``state.interactive_draw`` ordering."""
    try:
        await asyncio.wait_for(
            bb.display_draw(_interactive_payload(state, label, timeout)),
            _limits.INTERACTIVE_IO_TIMEOUT_S,
        )
        state.active_event_label = None
        state.active_event_asset = None
        state.active_event_embedded_label = False
        state.active_event_until = 0.0
        state.interactive_visible_until = asyncio.get_running_loop().time() + timeout
        return True
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.warning("readout draw failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _limits.logger.warning("readout draw failed: %s", exc)
    return False


async def draw_readout(bb, state: _model.State, label: str, timeout: int = 2) -> bool:
    """An instant native-text acknowledgement while the next asset prepares."""
    async with state.interactive_draw:
        return await _post_readout(bb, state, label, timeout)


def _event_payload(
    label: str,
    asset: str | None = None,
    timeout: int = _limits.EVENT_TIMEOUT_S,
    embedded_label: bool = False,
) -> types.DisplayElements:
    elements: list = [
        types.RectangleElement(
            id="eventbg",
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
    ]
    if asset is not None:
        elements.append(
            types.AnimationElement(
                id="eventanim",
                type="animation",
                path=asset,
                loop=False,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            )
        )
    if not embedded_label:
        elements.append(
            types.TextElement(
                id="eventtx",
                type="text",
                text=_render_text.device_text(label),
                font="condensed",
                color="#9FE8FFFF",
                align="center",
                x=36,
                y=8,
                scroll_rate=1400,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            )
        )
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        led_notification_color="#73DDEBFF",
        elements=elements,
    )


def _status_payload(label: str, timeout: int = 15) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        elements=[
            types.RectangleElement(
                id="statusbg",
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
                id="statustx",
                type="text",
                text=_render_text.device_text(label),
                font="condensed",
                color="#E3B15DFF",
                align="center",
                x=36,
                y=8,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            ),
        ],
    )


def feed_status_label(freshness: str, fresh_label: str = "NO LINK DATA") -> str:
    """One vocabulary for ambient and START feed-state acknowledgements."""
    return (
        fresh_label
        if freshness == "fresh"
        else "FEED DELAY"
        if freshness == "delayed"
        else "FEED STALE"
        if freshness == "stale"
        else "DSN OFFLINE"
    )


async def draw_feed_status(bb, state: _model.State, timeout: int = 15) -> None:
    fresh = _telemetry.feed_freshness(state)
    await bb.display_draw(_status_payload(feed_status_label(fresh), timeout))
    state.status_up = timeout > 1


def _countdown_payload(
    deadline: float,
    x: int,
    timeout: int = _limits.ELEMENT_TIMEOUT_S,
    element_id: str = "dsncd",
) -> types.DisplayElements:
    """The live countdown, rendered and ticked BY THE DEVICE.

    The number was baked into the animation frames, so it only moved when the
    scene was re-pushed — every 21 seconds on Mars and every five minutes on
    Voyager, which is a clock that jumps five minutes at a time rather than
    counts. The firmware has a countdown element that takes a deadline and
    ticks itself, so the host stops being in the loop entirely.
    """
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        elements=[
            types.CountdownElement(
                id=element_id,
                type="countdown",
                timestamp=str(int(deadline)),
                direction="time_left",
                show_hours="when_non_zero",
                # y is the glyph's TOP and the firmware font is about 7 tall, so
                # H-6 ran off the bottom of the panel and clipped. H-7 fits in
                # rows 9..15, clear of both track rows at 6 and 8.
                color="#FFB43CFF",
                x=x,
                y=_limits.H - 7,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            )
        ],
    )


async def retire_countdown(bb, state: _model.State) -> bool:
    """Expire the native timer, committing state only after the draw lands.

    A focused link can disappear without leaving another scene to replace it.
    In that case the status panel still needs to retire the old countdown.
    Keeping ``countdown_up`` true after a refusal makes the next retry finish
    the job instead of silently abandoning a real device element.
    """
    if not state.countdown_up:
        return True
    try:
        await asyncio.wait_for(
            bb.display_draw(
                _countdown_payload(
                    time.time(),
                    _limits.TRACK0 + 1,
                    timeout=1,
                    element_id=state.countdown_id or "dsncd",
                )
            ),
            _limits.INTERACTIVE_IO_TIMEOUT_S,
        )
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.debug("countdown retirement failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - cosmetic, and retryable
        _limits.logger.debug("countdown retirement failed: %s", exc)
        return False
    state.countdown_up = False
    state.countdown_id = None
    return True


def _scene_payload(
    filename: str, led: str | None = None, timeout: int = _limits.ELEMENT_TIMEOUT_S
) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        led_notification_color=led,
        elements=[
            types.AnimationElement(
                id="dsn",
                type="animation",
                path=filename,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            )
        ],
    )


def _live_lease_payload(
    element_id: str = "dsnlive",
    y: int = 0,
    timeout: int | None = None,
    retire: tuple[tuple[str, int], ...] = (),
) -> types.DisplayElements:
    """One moving source heartbeat; a new id is required to move geometry."""
    timeout = _settings.LIVE_LEASE_TIMEOUT_S if timeout is None else timeout
    elements = [
        types.RectangleElement(
            id=element_id,
            type="rectangle",
            x=_render_palette.FRESH_X,
            y=y,
            width=1,
            height=2,
            fill="solid",
            fill_colors=["#46DCEBFF"],
            border_width=0,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        )
    ]
    for old_id, old_y in retire:
        if old_id == element_id:
            continue
        elements.append(
            types.RectangleElement(
                id=old_id,
                type="rectangle",
                x=_render_palette.FRESH_X,
                y=old_y,
                width=1,
                height=2,
                fill="solid",
                fill_colors=["#46DCEBFF"],
                border_width=0,
                display=types.DisplayName.FRONT,
                timeout=1,
            )
        )
    return types.DisplayElements(
        application_name=_limits.APP_NAME, priority=_limits.PRIORITY, elements=elements
    )


async def sync_live_lease(bb, state: _model.State, freshness: str) -> bool:
    """Renew or retire the live claim, committing only after an accepted draw."""
    loop_now = asyncio.get_running_loop().time()
    # A possibly committed native element cannot outlive its own device
    # timeout. Forgetting it after that deadline is safe and prevents a long
    # device outage from growing the eventual recovery payload without bound.
    for element_id, deadline in list(state.heartbeat_uncertain_until.items()):
        if deadline <= loop_now:
            state.heartbeat_uncertain_until.pop(element_id, None)
            state.heartbeat_uncertain.pop(element_id, None)
    timestamp = state.feed_timestamp_ms
    # The point advances only with NASA's timestamp. It is not part of the
    # looping .anim, so a frozen source can never keep looking alive.
    should_live = (
        freshness == "fresh"
        and timestamp is not None
        and state.view in {"instrument", "network"}
        and bool(state.links)
    )
    if should_live:
        assert timestamp is not None
        if state.live_lease_up and timestamp == state.last_live_lease_timestamp_ms:
            return True
        if state.heartbeat_pending_timestamp_ms != timestamp:
            state.heartbeat_generation += 1
            state.heartbeat_pending_timestamp_ms = timestamp
            state.heartbeat_pending_id = (
                f"dsnlive{state.rt_nonce}{state.heartbeat_generation}"
            )
            # Advance on every distinct source version. Keep the proposed
            # geometry until acceptance so a lost response retries the same
            # immutable id instead of leaking a second heartbeat lease.
            state.heartbeat_pending_y = (
                (state.heartbeat_y + 2) % (_limits.H - 1)
                if state.heartbeat_y is not None
                else int(timestamp // 1000) % (_limits.H - 1)
            )
        new_id = state.heartbeat_pending_id or "dsnlive"
        new_y = (
            state.heartbeat_pending_y if state.heartbeat_pending_y is not None else 0
        )
        retirements: dict[str, int] = {}
        if state.heartbeat_id is not None and state.heartbeat_y is not None:
            retirements[state.heartbeat_id] = state.heartbeat_y
        retirements.update(state.heartbeat_uncertain)
        retirements.pop(new_id, None)
        payload = _live_lease_payload(new_id, new_y, retire=tuple(retirements.items()))
    else:
        retirements = dict(state.heartbeat_uncertain)
        if state.heartbeat_id is not None and state.heartbeat_y is not None:
            retirements[state.heartbeat_id] = state.heartbeat_y
        if (
            state.heartbeat_pending_id is not None
            and state.heartbeat_pending_y is not None
        ):
            retirements[state.heartbeat_pending_id] = state.heartbeat_pending_y
        if not retirements:
            return True
        (target_id, target_y), *rest = retirements.items()
        payload = _live_lease_payload(
            target_id, target_y, timeout=1, retire=tuple(rest)
        )
    try:
        await bb.display_draw(payload)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.debug("live lease draw failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 - retryable display state
        if should_live:
            # The transport can lose its response after committing. Preserve
            # this exact immutable id even if a newer source timestamp arrives;
            # the next accepted payload will retire it atomically.
            state.heartbeat_uncertain[new_id] = new_y
            state.heartbeat_uncertain_until[new_id] = (
                asyncio.get_running_loop().time() + _settings.LIVE_LEASE_TIMEOUT_S
            )
        _limits.logger.debug("live lease draw failed: %s", exc)
        return False
    state.live_lease_up = should_live
    if should_live:
        state.last_live_lease_timestamp_ms = timestamp
        state.heartbeat_id = new_id
        state.heartbeat_y = new_y
        state.heartbeat_pending_timestamp_ms = None
        state.heartbeat_pending_id = None
        state.heartbeat_pending_y = None
        state.heartbeat_uncertain.clear()
        state.heartbeat_uncertain_until.clear()
    else:
        state.heartbeat_id = None
        state.heartbeat_y = None
        state.heartbeat_pending_timestamp_ms = None
        state.heartbeat_pending_id = None
        state.heartbeat_pending_y = None
        state.heartbeat_uncertain.clear()
        state.heartbeat_uncertain_until.clear()
    return True

"""Skystrip providers / lightning."""

from __future__ import annotations

import asyncio
import logging
import random
import time

from apps.skystrip_app import lightning as _lightning
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather_state as _weather_state

# A configured lightning URL may carry relay credentials. The WebSocket
# library's debug records can include the request target, so give that one
# transport a disabled private logger and keep our value-free lifecycle logs
# on the normal app logger.
LIGHTNING_TRANSPORT_LOGGER = logging.Logger(
    "skystrip.lightning.transport", level=logging.CRITICAL + 1
)

LIGHTNING_TRANSPORT_LOGGER.disabled = True


async def listen_lightning(state: _model.SkyState) -> None:
    """Queue ambient flashes only; alerts and sirens come solely from CAP."""
    import websockets

    endpoint = _settings.LIGHTNING_WS
    if endpoint is None:
        return
    backoff = 1.0
    invalid_frames = 0
    while True:
        try:
            async with websockets.connect(
                endpoint,
                open_timeout=10,
                max_size=_lightning.LIGHTNING_FRAME_MAX_BYTES,
                max_queue=_limits.LIGHTNING_WS_MAX_QUEUE,
                logger=LIGHTNING_TRANSPORT_LOGGER,
            ) as ws:
                await ws.send(_limits.LIGHTNING_SUBSCRIPTION)
                _limits.logger.info(
                    "lightning: connected to configured secure endpoint"
                )
                connected_at = asyncio.get_running_loop().time()
                async for raw in ws:
                    backoff = 1.0  # a delivered frame proves the session works
                    try:
                        loop = asyncio.get_running_loop()
                        strike = _lightning.parse_lightning_strike(
                            raw,
                            wall_now=time.time(),
                            monotonic_now=loop.time(),
                        )
                        dist = _weather_state._km(
                            _settings.LAT,
                            _settings.LON,
                            strike.latitude,
                            strike.longitude,
                        )
                    except Exception as exc:  # noqa: BLE001 - bad frame: drop
                        invalid_frames += 1
                        if invalid_frames == 1 or invalid_frames % 100 == 0:
                            # Never log the payload or exception text: either
                            # may contain source-specific or sensitive fields.
                            _limits.logger.warning(
                                "lightning: discarded invalid frame (%s; %d total)",
                                type(exc).__name__,
                                invalid_frames,
                            )
                        continue
                    observed_at = strike.observed_at
                    if dist <= _limits.STRIKE_NEAR_KM:
                        _enqueue_flash(
                            state.flash_queue,
                            dist,
                            observed_at=observed_at,
                        )
                    elif dist <= _limits.STRIKE_RADIUS_KM:
                        # far flicker on the horizon: rare by design
                        if (
                            observed_at
                            - getattr(
                                listen_lightning,
                                "_far_at",
                                0.0,
                            )
                            > _limits.FAR_FLASH_GAP_S
                        ):
                            listen_lightning.__dict__["_far_at"] = observed_at
                            _enqueue_flash(
                                state.flash_queue,
                                dist,
                                observed_at=observed_at,
                            )
            # A protocol-legal close ends `async for` without raising. Without
            # this, a server that accepts then closes gives a reconnect hot
            # loop — thousands per second at the configured relay/source.
            if asyncio.get_running_loop().time() - connected_at < 5.0:
                _limits.logger.warning(
                    "lightning: endpoint closed immediately, backing off"
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
            else:
                _limits.logger.info("lightning: endpoint closed cleanly, reconnecting")
                backoff = 1.0
                # 370 clean-close reconnects were observed in one host's logs.
                # Blitzortung is community-run and non-commercial (see
                # NOTICE.md); a floor under the rate costs nothing here and is
                # the difference between reconnecting and hammering.
                await asyncio.sleep(
                    random.uniform(
                        _limits.RECONNECT_FLOOR_S, _limits.RECONNECT_FLOOR_S * 2
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Exception messages from a WebSocket library may echo the URL.
            # Log only the class so query tokens and userinfo stay private.
            _limits.logger.warning(
                "lightning: endpoint dropped (%s), retrying",
                type(exc).__name__,
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)


def _flash_distance(event: float | _model._FlashEvent) -> float:
    if isinstance(event, _model._FlashEvent):
        return event.distance_km
    return float(event)


def _coalesce_flashes(
    queue: asyncio.Queue,
    event: float | _model._FlashEvent,
) -> float | _model._FlashEvent:
    """Drain the backlog, keeping the nearest strike.

    A cell can outrun FLASH_MIN_GAP_S, and buffered strikes would keep
    strobing the bar long after the storm cleared — half an hour of queued
    lightning under a sky that has gone quiet. One flash stands for the
    burst, at its nearest (so brightest) distance.
    """
    while True:
        try:
            candidate = queue.get_nowait()
        except asyncio.QueueEmpty:
            return event
        if _flash_distance(candidate) < _flash_distance(event):
            event = candidate


def _coalesce_fresh_flashes(
    queue: asyncio.Queue,
    event: float | _model._FlashEvent,
    *,
    now: float,
) -> float | _model._FlashEvent | None:
    """Drain a burst and keep its nearest still-current strike.

    Plain floats remain accepted for the small host-side helper contracts;
    live listener events are timestamped, so a stalled weather feed cannot
    replay old lightning when the scene becomes eligible again.
    """
    nearest: float | _model._FlashEvent | None = None
    while True:
        if not isinstance(event, _model._FlashEvent):
            fresh = True
        else:
            age = now - event.observed_at
            fresh = 0.0 <= age <= _limits.FLASH_EVENT_TTL_S
        if fresh and (
            nearest is None or _flash_distance(event) < _flash_distance(nearest)
        ):
            nearest = event
        try:
            event = queue.get_nowait()
        except asyncio.QueueEmpty:
            return nearest


def _enqueue_flash(
    queue: asyncio.Queue,
    dist: float,
    *,
    observed_at: float | None = None,
) -> None:
    """Bound a detector burst and retain its nearest (brightest) strike."""
    event: float | _model._FlashEvent
    if observed_at is None:
        event = dist
    else:
        event = _model._FlashEvent(distance_km=dist, observed_at=observed_at)
    try:
        queue.put_nowait(event)
    except asyncio.QueueFull:
        nearest: float | _model._FlashEvent | None
        if observed_at is None:
            nearest = _coalesce_flashes(queue, event)
        else:
            nearest = _coalesce_fresh_flashes(
                queue,
                event,
                now=observed_at,
            )
        if nearest is not None:
            queue.put_nowait(nearest)

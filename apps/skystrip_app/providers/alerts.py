"""Skystrip providers / alerts."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import httpx

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from busybar_dev.config import describe_exception
from busybar_dev.weather_alerts import (
    parse_active_alerts,
    select_siren_alert,
    select_visual_alert,
)


async def poll_alerts(
    state: _model.SkyState,
    *,
    wait_for_point_check: bool = False,
) -> None:
    """Poll CAP independently of the five-minute observation pipeline.

    A valid empty response is an authoritative all-clear.  A malformed or
    failed response preserves last-good alerts only until their own CAP expiry
    times, never forever.
    """
    if wait_for_point_check:
        # The managed runtime starts point discovery and CAP concurrently.
        # Wait for that first bounded attempt so an outside-coverage install
        # never briefly arms an alert before /points establishes the boundary.
        await state.nws_point_checked.wait()
    async with httpx.AsyncClient(headers=_settings.NWS_UA, timeout=20) as client:
        while True:
            now = datetime.now(timezone.utc)
            # CAP's own deadline wins even if the next HTTP request stalls for
            # the full transport timeout.
            active = _alerts._locally_active_alerts(state.active_alerts, now)
            if active != state.active_alerts:
                _alerts._apply_alert_selection(
                    state,
                    select_visual_alert(active),
                    select_siren_alert(active),
                    active,
                )
            if state.nws_point_covered is False:
                # `/points` is the product's locality boundary for every NWS
                # enhancement. Do not query or present point-filtered CAP when
                # the configured coordinate is confirmed outside it.
                _alerts._stand_down_nws_alerts(state)
                await asyncio.sleep(_limits.ALERTS_INTERVAL_S)
                continue
            try:
                request = client.get(
                    "https://api.weather.gov/alerts/active",
                    params={"point": f"{_settings.LAT:.4f},{_settings.LON:.4f}"},
                )
                if state.active_alerts:
                    nearest = min(
                        min(alert.expires, alert.ends)
                        if alert.ends is not None
                        else alert.expires
                        for alert in state.active_alerts
                    )
                    deadline_s = max(0.01, (nearest - now).total_seconds())
                    response = await asyncio.wait_for(request, deadline_s)
                else:
                    response = await request
                response.raise_for_status()
                # `/points` can establish the unsupported boundary while this
                # independent CAP request is in flight. Recheck before an old
                # response can arm a card or siren for an unsupported point.
                if state.nws_point_covered is False:
                    _alerts._stand_down_nws_alerts(state)
                    continue
                received_at = datetime.now(timezone.utc)
                alerts = parse_active_alerts(response.json(), now=received_at)
                visual = select_visual_alert(alerts)
                siren = select_siren_alert(alerts)
                state.alert_known = True
                _alerts._apply_alert_selection(state, visual, siren, alerts)
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                # The request was bounded by CAP's nearest local deadline.
                # Expire first, then immediately retry without extending the
                # siren through httpx's ordinary 20-second transport timeout.
                expired_at = datetime.now(timezone.utc)
                active = _alerts._locally_active_alerts(state.active_alerts, expired_at)
                if active != state.active_alerts:
                    _alerts._apply_alert_selection(
                        state,
                        select_visual_alert(active),
                        select_siren_alert(active),
                        active,
                    )
                continue
            except Exception as exc:  # noqa: BLE001
                # Envelope failure is UNKNOWN, not an all-clear.  The local
                # CAP deadlines remain authoritative even while NWS is down.
                failed_at = datetime.now(timezone.utc)
                active = _alerts._locally_active_alerts(state.active_alerts, failed_at)
                if active != state.active_alerts:
                    _alerts._apply_alert_selection(
                        state,
                        select_visual_alert(active),
                        select_siren_alert(active),
                        active,
                    )
                _limits.logger.warning(
                    "alert poll failed; retaining unexpired state: %s",
                    describe_exception(exc),
                )

            delay = float(_limits.ALERTS_INTERVAL_S)
            if state.active_alerts:
                nearest = min(
                    min(a.expires, a.ends) if a.ends is not None else a.expires
                    for a in state.active_alerts
                )
                remaining = (nearest - datetime.now(timezone.utc)).total_seconds()
                delay = min(delay, max(0.25, remaining))
            await asyncio.sleep(delay)

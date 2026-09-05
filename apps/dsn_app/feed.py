"""DSN feed."""

from __future__ import annotations

import asyncio
import time

import httpx

from apps.dsn_app import history as _history
from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import ranges as _ranges
from apps.dsn_app import reconcile as _reconcile
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source


async def poll_names(state: _model.State) -> None:
    """Keep the name map fresh, and KEEP TRYING if it never arrived.

    This ran exactly once at startup. A transient failure — the Pi booting
    before DHCP settles, a 500 — once left names, dish sizes and site
    longitudes empty for the life of the process. Unknown truth-bearing facts
    now stay unknown rather than becoming a 34-metre dish or longitude zero,
    but retries still recover the richer scene. A craft added to the network
    mid-run must likewise acquire its name without a restart.
    """
    delay = 5.0
    while True:
        if await fetch_names(state):
            delay = 5.0
            await asyncio.sleep(3600)  # new craft appear; refresh hourly
        else:
            await asyncio.sleep(delay)
            delay = min(delay * 2, 300)


async def fetch_names(state: _model.State) -> bool:
    """Atomically refresh NASA's names, dish facts and site longitudes."""
    try:
        async with httpx.AsyncClient(headers=_limits.UA, timeout=20) as client:
            r = await client.get(_limits.CONFIG_XML)
            r.raise_for_status()
        names, dish_types, site_lons = _source.parse_config(r.content)
        changed = (names, dish_types, site_lons) != (
            state.names,
            state.dish_types,
            state.site_lons,
        )
        state.names, state.dish_types, state.site_lons = (names, dish_types, site_lons)
        if changed:
            state.dirty.set()
        _limits.logger.info(
            "names: %d spacecraft, %d dishes", len(state.names), len(state.dish_types)
        )
        return True
    except Exception as exc:  # noqa: BLE001 - retain the last valid snapshot
        _limits.logger.warning("DSN config unavailable/invalid (%s); retrying", exc)
        return False


async def poll_feed(state: _model.State) -> None:
    async with httpx.AsyncClient(headers=_limits.UA, timeout=20) as client:
        while True:
            try:
                r = await client.get(_limits.DSN_XML)
                r.raise_for_status()
                received_at = time.time()
                source_timestamp = _source.feed_timestamp_ms(r.content)
                if source_timestamp is None:
                    _limits.logger.warning(
                        "ignoring DSN snapshot without a source timestamp"
                    )
                    await asyncio.sleep(_settings.POLL_S)
                    continue
                if not _source.source_timestamp_valid(source_timestamp, received_at):
                    _limits.logger.warning(
                        "ignoring DSN snapshot with implausible source timestamp: %s",
                        source_timestamp,
                    )
                    await asyncio.sleep(_settings.POLL_S)
                    continue
                if (
                    source_timestamp is not None
                    and state.feed_timestamp_ms is not None
                    and source_timestamp < state.feed_timestamp_ms
                ):
                    _limits.logger.warning(
                        "ignoring older DSN snapshot: %s < %s",
                        source_timestamp,
                        state.feed_timestamp_ms,
                    )
                    await asyncio.sleep(_settings.POLL_S)
                    continue
                if (
                    state.feed_timestamp_ms is not None
                    and source_timestamp == state.feed_timestamp_ms
                ):
                    # The source timestamp is the snapshot version. Do not
                    # accept different-looking transport bytes under an old
                    # lease; simply let freshness age toward delayed/stale.
                    await asyncio.sleep(_settings.POLL_S)
                    continue
                links = _source.parse_feed(r.content)
                for link in links:  # fill gaps from Horizons
                    if link.range_km is not None and link.naif:
                        state.range_unavailable.discard(link.naif)
                    if link.range_km is None and link.naif:
                        link.range_km = _ranges.cached_range(
                            state, link.naif, received_at
                        )
                seeded = state.feed_seeded
                history_events = (
                    _history.link_events(state.links, links, received_at)
                    if seeded
                    else []
                )
                changed = [l.key for l in links] != [l.key for l in state.links]
                events = _reconcile.reconcile_links(state, links, received_at)
                _history.append_history(history_events)
                _history.note_seen(state, history_events)
                _reconcile.queue_events(state, events)
                state.feed_advanced_at = received_at
                state.feed_timestamp_ms = source_timestamp
                if changed:
                    _limits.logger.info(
                        "links: %s",
                        ", ".join(f"{l.dish}->{l.craft}" for l in links) or "(none)",
                    )
                # Waking is cheap. The main loop compares a device-resolution
                # signature before it renders or uploads anything.
                state.dirty.set()
            except Exception as exc:  # noqa: BLE001 - a feed outage is a state
                _limits.logger.warning("dsn feed failed: %s", exc)
            await asyncio.sleep(_settings.POLL_S)

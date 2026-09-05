"""Skystrip providers / radar."""

from __future__ import annotations

import asyncio
import time

import httpx

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather_state as _weather_state
from busybar_dev.config import describe_exception
from busybar_dev.radar import (
    RADAR_MAX_ZOOM,
    RAINVIEWER_TILE_SIZE,
    decode_coverage_mask,
    decode_radar_tile,
    rainviewer_frame_age,
    sample_dbz,
    tile_pixel,
    web_mercator_contains,
)


async def poll_radar(state: _model.SkyState) -> None:
    """The watch-on-your-wrist treatment for rain: sample the live radar
    mosaic at OUR coordinates instead of trusting an airport 10 miles off.
    RainViewer serves a global composite, keyless; fails soft down the
    resolve chain (Open-Meteo nowcast, then a fresh NWS station or last-good)."""
    if not web_mercator_contains(_settings.LAT):
        # RainViewer's slippy-map products cannot represent the polar caps.
        # Never clamp a pole onto the edge tile and pretend that edge radar is
        # local evidence; keep the global nowcast/station chain authoritative.
        state.radar_dbz = None
        state.radar_at = 0.0
        state.radar_covered = False
        _weather_state.apply_rain(state)
        _limits.logger.info(
            "radar coverage unavailable outside Web Mercator; using fallback"
        )
        # Park forever rather than waking every RADAR_INTERVAL_S to do nothing.
        # Coverage is a property of the configured coordinates, so it cannot
        # become available without a restart; an Event that is never set is
        # both cancellable and free.
        await asyncio.Event().wait()

    tx, ty, px, py = tile_pixel(
        _settings.LAT, _settings.LON, RADAR_MAX_ZOOM, RAINVIEWER_TILE_SIZE
    )
    async with httpx.AsyncClient(headers=_limits.NEUTRAL_UA, timeout=20) as client:
        while True:
            try:
                r = await client.get(
                    "https://api.rainviewer.com/public/weather-maps.json"
                )
                r.raise_for_status()
                idx = r.json()
                frame = idx["radar"]["past"][-1]
                frame_time = frame.get("time")
                # Validate before spending two more requests on a cached frame.
                # It is checked again at commit because those requests consume
                # part of the frame's real freshness window.
                rainviewer_frame_age(frame_time, now_unix=time.time())
                host = idx["host"].rstrip("/")
                r = await client.get(
                    f"{host}/v2/coverage/0/{RAINVIEWER_TILE_SIZE}"
                    f"/{RADAR_MAX_ZOOM}/{tx}/{ty}/0/0_0.png"
                )
                r.raise_for_status()
                covered = decode_coverage_mask(
                    r.content, px, py, tile_size=RAINVIEWER_TILE_SIZE
                )
                previous_coverage = state.radar_covered
                state.radar_covered = covered
                if not covered:
                    # A transparent radar tile is ambiguous by itself: it can
                    # mean "covered and clear" or "there is no radar here".
                    # The official mask resolves that ambiguity. Invalidate the
                    # old radar timestamp so resolve_rain falls through now,
                    # rather than declaring a model-reported storm dry for 15m.
                    state.radar_dbz = None
                    state.radar_at = 0.0
                    _weather_state.apply_rain(state)
                    if previous_coverage is not False:
                        _limits.logger.info(
                            "radar coverage unavailable; using global fallback"
                        )
                    await asyncio.sleep(_weather_state.RADAR_INTERVAL_S)
                    continue
                if previous_coverage is False:
                    _limits.logger.info("radar coverage available again")
                r = await client.get(
                    f"{host}{frame['path']}/{RAINVIEWER_TILE_SIZE}"
                    f"/{RADAR_MAX_ZOOM}"
                    f"/{tx}/{ty}/0/0_0.png"
                )
                r.raise_for_status()
                img = decode_radar_tile(r.content, tile_size=RAINVIEWER_TILE_SIZE)
                dbz = sample_dbz(img, px, py)
                source_age = rainviewer_frame_age(frame_time, now_unix=time.time())
                # Keep source time on the monotonic axis used by resolve_rain.
                # Receipt time would make an old cached mosaic look brand new.
                monotonic_now = asyncio.get_running_loop().time()
                state.radar_dbz = dbz
                state.radar_at = monotonic_now - source_age
                _weather_state.apply_rain(state)
            except Exception as exc:  # noqa: BLE001 - feed is best-effort
                _limits.logger.warning("radar poll failed: %s", describe_exception(exc))
                # Preserve the last sample and its original source-mapped time,
                # but re-resolve now so an aged-out sample immediately yields to
                # the current Open-Meteo/station chain.
                _weather_state.apply_rain(state)
            await asyncio.sleep(_weather_state.RADAR_INTERVAL_S)

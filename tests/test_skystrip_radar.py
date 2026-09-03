"""RainViewer coverage is authority metadata, not a decorative overlay."""

from __future__ import annotations

import asyncio
import io
import sys
from pathlib import Path

import httpx
import pytest
from PIL import Image

from busybar_dev.radar import OM_FRESH_S, RADAR_FRESH_S, STATION_FRESH_S

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
skystrip = pytest.importorskip("skystrip")
WALL_NOW = 1_800_000_000.0


def _png_bytes(image: Image.Image) -> bytes:
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _client_for(handler):
    class MockClient(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx.MockTransport(handler)
            super().__init__(*args, **kwargs)

    return MockClient


async def _wait_until(predicate) -> None:
    while not predicate():
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_absent_station_preserves_last_good_until_rain_lease_expires():
    state = skystrip.SkyState()
    now = asyncio.get_running_loop().time()
    state.weather = skystrip.WeatherState(rain=True, rain_tier=2)
    state.weather_ready.set()
    state.weather_updated_at = now
    state.rain_known = True
    state.rain_at = now

    skystrip.apply_rain(state)

    assert state.station_rain is None
    assert state.station_at is None
    assert state.weather.rain is True
    assert state.weather.rain_tier == 2
    assert state.rain_src == "last-good"

    # A newer temperature/cloud refresh does not renew precipitation evidence.
    state.weather_updated_at = now
    state.rain_at = now - skystrip.WEATHER_LEASE_S - 1.0
    skystrip.apply_rain(state)

    # Suppress the stale visual but label it unavailable, never provider-clear.
    assert state.weather.rain is False
    assert state.weather.rain_tier == 1
    assert state.rain_src == "unavailable"


@pytest.mark.asyncio
async def test_stale_station_cannot_reassert_over_global_or_last_good_state():
    state = skystrip.SkyState()
    now = asyncio.get_running_loop().time()
    state.weather = skystrip.WeatherState(rain=True, rain_tier=2)
    state.weather_ready.set()
    state.weather_updated_at = now
    state.station_rain = True
    state.station_at = now - STATION_FRESH_S
    state.om_rain = False
    state.om_at = now

    skystrip.apply_rain(state)

    assert state.weather.rain is False
    assert state.weather.rain_tier == 1
    assert state.rain_src == "nowcast"

    state.om_at = now - OM_FRESH_S
    skystrip.apply_rain(state)

    assert state.weather.rain is False
    assert state.weather.rain_tier == 1
    assert state.rain_src == "last-good"


@pytest.mark.asyncio
async def test_uncovered_mask_invalidates_radar_and_skips_echo_tile(monkeypatch):
    requests: list[str] = []
    uncovered = _png_bytes(Image.new("RGBA", (256, 256), (0, 0, 0, 255)))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.rainviewer.com":
            return httpx.Response(200, json={
                "host": "https://tiles.example",
                "radar": {"past": [{
                    "path": "/v2/radar/current", "time": WALL_NOW,
                }]},
            })
        if request.url.path.startswith("/v2/coverage/"):
            return httpx.Response(200, content=uncovered)
        raise AssertionError(f"radar tile must be skipped without coverage: {request.url}")

    monkeypatch.setattr(skystrip, "LAT", 0.0)
    monkeypatch.setattr(skystrip, "LON", 0.0)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(
        skystrip.httpx, "AsyncClient", _client_for(handler))

    state = skystrip.SkyState()
    now = asyncio.get_running_loop().time()
    state.radar_dbz = 50.0
    state.radar_at = now
    state.om_rain = True
    state.om_at = now

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(
                lambda: state.radar_at == 0.0
                and state.rain_src == "nowcast"),
            timeout=1.0,
        )
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_covered is False
    assert state.radar_dbz is None
    assert state.weather.rain is True
    assert any("/v2/coverage/0/256/7/64/64/0/0_0.png" in url
               for url in requests)
    assert not any("/v2/radar/current" in url for url in requests)


@pytest.mark.asyncio
async def test_covered_mask_allows_echo_tile_to_own_rain(monkeypatch):
    requests: list[str] = []
    covered = _png_bytes(Image.new("RGBA", (256, 256), (0, 0, 0, 0)))
    echo = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
    echo.putpixel((0, 0), (255, 238, 0, 255))  # official 35-dBZ anchor
    echo_png = _png_bytes(echo)

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.rainviewer.com":
            return httpx.Response(200, json={
                "host": "https://tiles.example/",
                "radar": {"past": [{
                    "path": "/v2/radar/current", "time": WALL_NOW - 120.0,
                }]},
            })
        if request.url.path.startswith("/v2/coverage/"):
            return httpx.Response(200, content=covered)
        if request.url.path.startswith("/v2/radar/current"):
            return httpx.Response(200, content=echo_png)
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(skystrip, "LAT", 0.0)
    monkeypatch.setattr(skystrip, "LON", 0.0)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(
        skystrip.httpx, "AsyncClient", _client_for(handler))

    state = skystrip.SkyState()
    state.om_rain = False
    state.om_at = asyncio.get_running_loop().time()

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(lambda: state.rain_src == "radar"),
            timeout=1.0,
        )
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_covered is True
    assert state.radar_dbz == 35.0
    assert state.radar_at != 0.0
    source_age = asyncio.get_running_loop().time() - state.radar_at
    assert source_age == pytest.approx(120.0, abs=0.1)
    assert state.weather.rain is True
    assert state.weather.rain_tier == 1
    assert any("/v2/radar/current/256/7/64/64/0/0_0.png" in url
               for url in requests)


@pytest.mark.asyncio
async def test_wrong_size_echo_tile_never_gains_radar_authority(monkeypatch):
    requests: list[str] = []
    covered = _png_bytes(Image.new("RGBA", (256, 256), (0, 0, 0, 0)))
    wrong_size = _png_bytes(Image.new("RGBA", (1, 1), (255, 170, 0, 255)))

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.rainviewer.com":
            return httpx.Response(200, json={
                "host": "https://tiles.example",
                "radar": {"past": [{
                    "path": "/v2/radar/wrong-size", "time": WALL_NOW,
                }]},
            })
        if request.url.path.startswith("/v2/coverage/"):
            return httpx.Response(200, content=covered)
        if request.url.path.startswith("/v2/radar/wrong-size"):
            return httpx.Response(200, content=wrong_size)
        raise AssertionError(f"unexpected request: {request.url}")

    monkeypatch.setattr(skystrip, "LAT", 0.0)
    monkeypatch.setattr(skystrip, "LON", 0.0)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(
        skystrip.httpx, "AsyncClient", _client_for(handler))

    state = skystrip.SkyState()
    state.om_rain = True
    state.om_at = asyncio.get_running_loop().time()

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(lambda: state.rain_src == "nowcast"), timeout=1.0)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_covered is True
    assert state.radar_dbz is None
    assert state.radar_at == 0.0
    assert state.weather.rain is True
    assert any("/v2/radar/wrong-size" in url for url in requests)


@pytest.mark.asyncio
async def test_stale_cached_frame_preserves_sample_but_falls_through_to_nowcast(
    monkeypatch,
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.rainviewer.com":
            return httpx.Response(200, json={
                "host": "https://tiles.example",
                "radar": {"past": [{
                    "path": "/v2/radar/stale",
                    "time": WALL_NOW - RADAR_FRESH_S,
                }]},
            })
        raise AssertionError("stale frame must not request mask or echo tiles")

    monkeypatch.setattr(skystrip, "LAT", 0.0)
    monkeypatch.setattr(skystrip, "LON", 0.0)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(
        skystrip.httpx, "AsyncClient", _client_for(handler))

    state = skystrip.SkyState()
    loop_now = asyncio.get_running_loop().time()
    old_radar_at = loop_now - RADAR_FRESH_S - 10.0
    state.radar_dbz = 50.0
    state.radar_at = old_radar_at
    state.om_rain = True
    state.om_at = loop_now

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(lambda: state.rain_src == "nowcast"), timeout=1.0)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_dbz == 50.0
    assert state.radar_at == old_radar_at
    assert state.weather.rain is True
    assert len(requests) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "frame_time",
    [None, "not-a-timestamp", WALL_NOW + 301.0],
)
async def test_invalid_frame_time_never_requests_tiles_or_gains_authority(
    monkeypatch, frame_time,
):
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.host == "api.rainviewer.com":
            return httpx.Response(200, json={
                "host": "https://tiles.example",
                "radar": {"past": [{
                    "path": "/v2/radar/invalid", "time": frame_time,
                }]},
            })
        raise AssertionError("invalid frame must not request mask or echo tiles")

    monkeypatch.setattr(skystrip, "LAT", 0.0)
    monkeypatch.setattr(skystrip, "LON", 0.0)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(
        skystrip.httpx, "AsyncClient", _client_for(handler))

    state = skystrip.SkyState()
    state.om_rain = True
    state.om_at = asyncio.get_running_loop().time()

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(lambda: state.rain_src == "nowcast"), timeout=1.0)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_at == 0.0
    assert state.radar_dbz is None
    assert state.weather.rain is True
    assert len(requests) == 1


@pytest.mark.asyncio
async def test_polar_coordinate_stands_down_without_requesting_edge_tile(
    monkeypatch,
):
    class ForbiddenClient:
        def __init__(self, *args, **kwargs):
            raise AssertionError("polar coordinates must not request radar")

    monkeypatch.setattr(skystrip, "LAT", 90.0)
    monkeypatch.setattr(skystrip, "LON", 180.0)
    monkeypatch.setattr(skystrip.httpx, "AsyncClient", ForbiddenClient)

    state = skystrip.SkyState()
    now = asyncio.get_running_loop().time()
    state.radar_dbz = 50.0
    state.radar_at = now
    state.om_rain = True
    state.om_at = now

    poller = asyncio.create_task(skystrip.poll_radar(state))
    try:
        await asyncio.wait_for(
            _wait_until(
                lambda: state.radar_covered is False
                and state.rain_src == "nowcast"),
            timeout=1.0,
        )
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.radar_dbz is None
    assert state.radar_at == 0.0
    assert state.weather.rain is True

"""Host-only characterization contracts for DSN range enrichment.

The range worker is an optional NASA/JPL enrichment boundary.  These tests use
deterministic clocks and transports so they never contact Horizons or sleep.
"""

from __future__ import annotations

import asyncio
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def _link(
    *,
    craft: str = "VGR2",
    dish: str = "DSS14",
    naif: int = -32,
    range_km: float | None = None,
) -> dsn.Link:
    return dsn.Link(
        complex_name="Goldstone",
        dish=dish,
        craft=craft,
        elevation=30.0,
        azimuth=120.0,
        band="X",
        down_bps=160.0,
        up_active=False,
        range_km=range_km,
        naif=naif,
        down_streams=(dsn.DownStream("X", 160.0, -140.0),),
        streams=1,
    )


def _horizons_response(au: float) -> str:
    return (
        "API VERSION: 1.2\n"
        "$$SOE\n"
        f"2026-Sep-03 12:00:00.000  {au}  -0.1\n"
        "$$EOE\n"
    )


class _CountingDirty:
    def __init__(self) -> None:
        self.set_calls = 0

    def set(self) -> None:
        self.set_calls += 1


def test_horizons_request_contract_is_exact(monkeypatch):
    state = dsn.State(links=[_link()])
    trace: list[tuple] = []

    class Response:
        text = _horizons_response(1.0)

        def raise_for_status(self):
            trace.append(("raise_for_status",))

    class Client:
        def __init__(self, *args, **kwargs):
            trace.append(("client", args, dict(kwargs)))

        async def __aenter__(self):
            trace.append(("enter",))
            return self

        async def __aexit__(self, *_args):
            trace.append(("exit",))

        async def get(self, endpoint, *, params):
            trace.append(("get", endpoint, dict(params)))
            return Response()

    times = iter((1_800_000_000.0, 1_800_000_000.0, 1_800_000_001.0))

    async def stop_after_attempt(delay):
        trace.append(("sleep", delay))
        raise asyncio.CancelledError

    def persist(saved_state):
        trace.append((
            "persist", tuple(sorted(saved_state.range_state.values)),
        ))

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: next(times))
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
    monkeypatch.setattr(dsn, "save_ranges", persist)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    expected_params = {
        "format": "text",
        "COMMAND": "'-32'",
        "OBJ_DATA": "NO",
        "MAKE_EPHEM": "YES",
        "EPHEM_TYPE": "OBSERVER",
        "CENTER": "'500@399'",
        "QUANTITIES": "'20'",
        "TLIST_TYPE": "JD",
        "TLIST": "'2461420.83333'",
    }
    assert trace == [
        ("client", (), {
            "headers": {"User-Agent": "dsn (busybar hobby project)"},
            "timeout": 30,
        }),
        ("enter",),
        ("get", "https://ssd.jpl.nasa.gov/api/horizons.api", expected_params),
        ("raise_for_status",),
        ("persist", (-32,)),
        ("sleep", 2),
        ("exit",),
    ]


def test_success_records_and_persists_once_then_fills_current_aliases(monkeypatch):
    first = _link(craft="VGR2", dish="DSS14")
    alias = _link(craft="VGR2-ALIAS", dish="DSS43")
    native_range = 987_654.0
    native = _link(craft="NATIVE", dish="DSS63", range_km=native_range)
    dirty = _CountingDirty()
    state = dsn.State(
        links=[first, alias, native],
        range_state=dsn.RangeState(
            retry_at={-32: 1_700_000_000.0},
            unavailable={-32},
        ),
    )
    state.dirty = dirty
    persisted: list[dict[str, object]] = []
    au = 1.25

    class Response:
        text = _horizons_response(au)

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            return Response()

    times = iter((1_800_000_000.0, 1_800_000_100.0, 1_800_000_200.0))

    async def stop_after_attempt(_delay):
        raise asyncio.CancelledError

    def persist(saved_state):
        persisted.append({
            "ranges": dict(saved_state.range_state.values),
            "retry": dict(saved_state.range_state.retry_at),
            "unavailable": set(saved_state.range_state.unavailable),
        })

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: next(times))
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
    monkeypatch.setattr(dsn, "save_ranges", persist)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    km = au * dsn.AU_LIGHT_S * dsn.C_KM_S
    expected_ranges = {-32: (km, 1_800_000_200.0)}
    assert state.range_state.values == expected_ranges
    assert state.range_state.retry_at == {}
    assert state.range_state.unavailable == set()
    assert persisted == [{
        "ranges": expected_ranges,
        "retry": {},
        "unavailable": set(),
    }]
    assert first.range_km == pytest.approx(km)
    assert alias.range_km == pytest.approx(km)
    assert native.range_km == native_range
    assert dirty.set_calls == 1


@pytest.mark.parametrize(("km", "ttl"), [
    (1_999_999.0, dsn.RANGE_NEAR_EARTH_TTL_S),
    (2_000_000.0, dsn.RANGE_INTERMEDIATE_TTL_S),
    (50_000_000.0, dsn.RANGE_TTL_S),
])
def test_cached_range_is_fresh_only_before_its_class_ttl_and_evicts_at_boundary(
    km, ttl,
):
    naif = -32
    observed_at = 10_000.0
    state = dsn.State(
        range_state=dsn.RangeState(values={naif: (km, observed_at)}),
    )
    expiry = observed_at + ttl

    assert dsn.range_ttl_s(km) == ttl
    assert dsn.cached_range(
        state, naif, now=math.nextafter(expiry, -math.inf),
    ) == km
    assert state.range_state.values == {naif: (km, observed_at)}

    assert dsn.cached_range(state, naif, now=expiry) is None
    assert naif not in state.range_state.values


def test_feed_native_range_clears_unavailable_but_retains_retry(monkeypatch):
    now = 1_800_000_000.0
    retry_at = now + 12_345.0
    state = dsn.State(
        range_state=dsn.RangeState(
            retry_at={-32: retry_at},
            unavailable={-32},
        ),
    )
    feed = b"""<dsn>
      <station name="gdscc" friendlyName="Goldstone"/>
      <dish name="DSS14" elevationAngle="30" azimuthAngle="120">
        <downSignal active="true" spacecraft="VGR2" spacecraftID="-32"
                    band="X" dataRate="160" power="-140"/>
        <target name="VGR2" id="32" downlegRange="21000000000"/>
      </dish>
      <timestamp>1800000000000</timestamp>
    </dsn>"""
    calls: list[tuple] = []

    class Response:
        content = feed

        def raise_for_status(self):
            calls.append(("raise_for_status",))

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, endpoint):
            calls.append(("get", endpoint))
            return Response()

    async def stop_after_poll(delay):
        calls.append(("sleep", delay))
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: now)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_poll)
    monkeypatch.setattr(dsn, "append_history", lambda _events: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert calls == [
        ("get", "https://eyes.nasa.gov/dsn/data/dsn.xml"),
        ("raise_for_status",),
        ("sleep", dsn.POLL_S),
    ]
    assert len(state.links) == 1
    assert state.links[0].naif == -32
    assert state.links[0].range_km == 21_000_000_000.0
    assert -32 not in state.range_state.unavailable
    assert state.range_state.retry_at == {-32: retry_at}


def test_inflight_result_only_updates_current_links(monkeypatch):
    old = _link(craft="OLD", dish="DSS14")
    replacement = _link(craft="OLD", dish="DSS14")
    new_alias = _link(craft="NEW-ALIAS", dish="DSS43")
    native_range = 456_789.0
    native = _link(craft="NATIVE", dish="DSS63", range_km=native_range)
    state = dsn.State(links=[old])
    trace: list[object] = []
    au = 2.0

    class Response:
        text = _horizons_response(au)

        def raise_for_status(self):
            return None

    async def scenario():
        request_started = asyncio.Event()
        release_response = asyncio.Event()

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                trace.append("request-started")
                request_started.set()
                await release_response.wait()
                trace.append("response-released")
                return Response()

        async def stop_after_attempt(delay):
            trace.append(("sleep", delay))
            raise asyncio.CancelledError

        monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
        monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
        task = asyncio.create_task(dsn.poll_ranges(state))
        await request_started.wait()

        state.links = [replacement, new_alias, native]
        trace.append("links-replaced")
        release_response.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(dsn.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(dsn, "save_ranges", lambda _state: None)
    asyncio.run(scenario())

    km = au * dsn.AU_LIGHT_S * dsn.C_KM_S
    assert trace == [
        "request-started",
        "links-replaced",
        "response-released",
        ("sleep", 2),
    ]
    assert all(link is not old for link in state.links)
    assert old.range_km is None
    assert replacement.range_km == pytest.approx(km)
    assert new_alias.range_km == pytest.approx(km)
    assert native.range_km == native_range


def test_cancellation_during_request_mutates_nothing(monkeypatch):
    link = _link()
    range_state = dsn.RangeState(
        values={-61: (820_000_000.0, 1_799_999_000.0)},
        retry_at={-60: 1_800_001_000.0},
        unavailable={-60},
    )
    state = dsn.State(links=[link], range_state=range_state)
    dirty = _CountingDirty()
    state.dirty = dirty
    persisted = []

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            raise asyncio.CancelledError

    async def unexpected_sleep(_delay):
        pytest.fail("a cancelled request must leave the worker immediately")

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(dsn.asyncio, "sleep", unexpected_sleep)
    monkeypatch.setattr(dsn, "save_ranges", persisted.append)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert link.range_km is None
    assert state.range_state.values == {
        -61: (820_000_000.0, 1_799_999_000.0),
    }
    assert state.range_state.retry_at == {-60: 1_800_001_000.0}
    assert state.range_state.unavailable == {-60}
    assert persisted == []
    assert dirty.set_calls == 0


@pytest.mark.parametrize(
    ("now", "expected_requests", "expected_sleep"),
    [
        (math.nextafter(10_000.0, -math.inf), 0, 10),
        (10_000.0, 1, 2),
    ],
)
def test_retry_deadline_is_inclusive(
    monkeypatch, now, expected_requests, expected_sleep,
):
    link = _link()
    state = dsn.State(
        links=[link],
        range_state=dsn.RangeState(retry_at={-32: 10_000.0}),
    )
    requests = []
    sleeps = []

    class Response:
        text = _horizons_response(1.0)

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *_args, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def get(self, *_args, **_kwargs):
            requests.append(1)
            return Response()

    async def stop_at_first_sleep(delay):
        sleeps.append(delay)
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: now)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_at_first_sleep)
    monkeypatch.setattr(dsn, "save_ranges", lambda _state: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert len(requests) == expected_requests
    assert sleeps == [expected_sleep]


def test_success_after_naif_disappears_keeps_only_the_cache(monkeypatch):
    departed = _link(craft="DEPARTED")
    state = dsn.State(links=[departed])
    dirty = _CountingDirty()
    state.dirty = dirty
    persisted = []
    au = 2.5

    class Response:
        text = _horizons_response(au)

        def raise_for_status(self):
            return None

    async def scenario():
        request_started = asyncio.Event()
        release_response = asyncio.Event()

        class Client:
            def __init__(self, *_args, **_kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return None

            async def get(self, *_args, **_kwargs):
                request_started.set()
                await release_response.wait()
                return Response()

        async def stop_after_attempt(_delay):
            raise asyncio.CancelledError

        monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
        monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
        task = asyncio.create_task(dsn.poll_ranges(state))
        await request_started.wait()
        state.links = []
        release_response.set()

        with pytest.raises(asyncio.CancelledError):
            await task

    monkeypatch.setattr(dsn.time, "time", lambda: 1_800_000_000.0)
    monkeypatch.setattr(
        dsn,
        "save_ranges",
        lambda saved: persisted.append(dict(saved.range_state.values)),
    )
    asyncio.run(scenario())

    km = au * dsn.AU_LIGHT_S * dsn.C_KM_S
    assert state.links == []
    assert departed.range_km is None
    assert state.range_state.values == {-32: (km, 1_800_000_000.0)}
    assert persisted == [{-32: (km, 1_800_000_000.0)}]
    assert dirty.set_calls == 1

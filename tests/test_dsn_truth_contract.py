"""Truth contracts for the live DSN source and light-time representation.

These tests are intentionally host-only.  They pin what the application may
claim from NASA's feed without requiring a network connection or a BUSY Bar.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def _duplex_feed(rate: str, craft: str = "ORX") -> bytes:
    return f"""<dsn>
      <station name="gdscc" friendlyName="Goldstone"/>
      <dish name="DSS25" azimuthAngle="156" elevationAngle="28"
            activity="Spacecraft Telemetry, Tracking, and Command">
        <upSignal active="true" signalType="data" dataRate="0"
                  frequency="0" band="X" power="18"
                  spacecraft="{craft}" spacecraftID="-64"/>
        <downSignal active="true" signalType="data" dataRate="{rate}"
                    frequency="0" band="X" power="-110"
                    spacecraft="{craft}" spacecraftID="-64"/>
        <target name="{craft}" id="64" uplegRange="63900000"
                downlegRange="63900000" rtlt="-1"/>
      </dish>
      <timestamp>1786153687000</timestamp>
    </dsn>""".encode()


def _partial_multistream_feed() -> bytes:
    """A real contact shape with useful and missing fields side by side."""
    return b"""<dsn>
      <station name="gdscc" friendlyName="Goldstone"/>
      <dish name="DSS25" azimuthAngle="156" elevationAngle="28">
        <upSignal active="true" signalType="data" band="X" power="18"
                  spacecraft="ORX" spacecraftID="-64"/>
        <upSignal active="true" signalType="ranging" band="" power="5"
                  spacecraft="ORX" spacecraftID="-64"/>
        <downSignal active="true" signalType="data" dataRate="1000"
                    band="X" power="-120" spacecraft="ORX" spacecraftID="-64"/>
        <downSignal active="true" signalType="ranging" dataRate="NaN"
                    band="" power="-140" spacecraft="ORX" spacecraftID="-64"/>
        <target name="ORX" id="64" uplegRange="90000000"
                downlegRange="90000000" rtlt="-1"/>
      </dish>
      <timestamp>1786153687000</timestamp>
    </dsn>"""


def _link(**changes) -> dsn.Link:
    base = dsn.Link(
        complex_name="Canberra",
        dish="DSS43",
        craft="VGR2",
        elevation=30.0,
        band="X",
        down_bps=160.0,
        up_active=True,
        range_km=dsn.C_KM_S * 2.0,
        naif=-32,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        azimuth=120.0,
        down_streams=(dsn.DownStream("X", 160.0, -140.0),),
        up_band="X",
    )
    return replace(base, **changes)


def _seeded_state(link: dsn.Link) -> dsn.State:
    state = dsn.State()
    state.links = [link]
    state.feed_seeded = True
    return state


def test_unknown_rate_is_not_the_same_observation_as_explicit_zero_rate():
    unknown = dsn.parse_feed(_duplex_feed("NaN"))[0]
    zero = dsn.parse_feed(_duplex_feed("0"))[0]

    # ``active`` says a receive signal exists.  Rate validity is a separate
    # fact: unavailable must not silently become a measured zero.
    assert unknown.down_streams != zero.down_streams
    assert dsn._instrument_metrics(unknown) != dsn._instrument_metrics(zero)
    assert dsn._instrument_metrics(unknown)[0][1] != "0BPS"
    assert dsn._instrument_metrics(zero)[0][1] == "0BPS"


@pytest.mark.parametrize("rate", ["NaN", "0"])
def test_active_downsignal_is_narrated_and_drawn_as_receive_even_without_rate(rate):
    link = dsn.parse_feed(_duplex_feed(rate))[0]

    words = dsn.spoken(link, {"orx": "OSIRIS-APEX"}, {"DSS25": "34M"}).lower()
    assert "listening" in words or "receiv" in words
    assert "femtowatt" in words, "published receive power must not be discarded"

    frames, _, _ = dsn.render_frames(
        link, datetime(2026, 8, 8, tzinfo=timezone.utc), {"orx": "OSIRIS-APEX"})
    downlink_colour = dsn.BAND_PULSE["X"]
    assert any(
        frame.getpixel((x, dsn.DOWN_Y)) == downlink_colour
        for frame in frames
        for x in range(dsn.TRACK0, dsn.TRACK1 + 1)
    ), "an active receive signal must not look like an uplink-only pass"


def test_partial_multistream_rate_is_not_narrated_as_no_usable_rate():
    link = dsn.parse_feed(_partial_multistream_feed())[0]
    assert {stream.bps for stream in link.down_streams} == {1000.0, None}
    assert link.down_bps is None, "an incomplete set has no defensible total"

    words = dsn.spoken(
        link, {"orx": "OSIRIS-APEX"}, {"DSS25": "34M"}).lower()
    assert "no usable data rate" not in words
    assert "per-record rates" in words
    assert "receiver redundancy" in words
    assert "contact throughput" in words


def test_multistream_receive_power_is_attributed_to_the_strongest_record():
    link = dsn.parse_feed(_partial_multistream_feed())[0]
    assert link.down_dbm == -120.0

    words = dsn.spoken(
        link, {"orx": "OSIRIS-APEX"}, {"DSS25": "34M"}).lower()
    assert "strongest published receive" in words


def test_missing_band_record_prevents_contact_wide_band_claims():
    link = dsn.parse_feed(_partial_multistream_feed())[0]

    assert link.band == ""
    assert link.up_band == ""
    words = dsn.spoken(
        link, {"orx": "OSIRIS-APEX"}, {"DSS25": "34M"}).lower()
    assert "this is x band" not in words
    assert "the uplink is x band" not in words


def test_watch_completion_uses_the_light_time_frozen_at_the_click():
    original = _link(range_km=dsn.C_KM_S * 2.0)
    state = _seeded_state(original)
    assert dsn.toggle_realtime(state, now=100.0) is True

    # A later feed sample may update the geometric range, but it must not move
    # the deadline of the traversal the user already started.
    refreshed = replace(original, range_km=dsn.C_KM_S * 20.0)
    dsn.reconcile_links(state, [refreshed], now=101.0)

    assert dsn.arrival_due(state, refreshed, 102.01) is True


def test_watch_keeps_its_frozen_scene_when_the_live_pass_ends():
    original = _link(range_km=dsn.C_KM_S * 20.0)
    state = _seeded_state(original)
    assert dsn.toggle_realtime(state, now=100.0) is True

    dsn.reconcile_links(state, [], now=101.0)

    assert state.realtime_since == 100.0
    assert state.view == "distance"
    watched = state.current()
    assert watched is not None, "a locally timed watch must outlive source visibility"
    assert watched.key == original.key
    assert watched.light_s == pytest.approx(20.0)


def test_watch_recovers_its_live_annotation_when_the_contact_returns():
    original = _link(range_km=dsn.C_KM_S * 20.0)
    state = _seeded_state(original)
    assert dsn.toggle_realtime(state, now=100.0) is True

    dsn.reconcile_links(state, [], now=101.0)
    assert state.watch is not None
    assert state.watch.on_air is False
    assert state.watch.live_key is None

    refreshed = replace(original, down_bps=320.0)
    dsn.reconcile_links(state, [refreshed], now=102.0)

    assert state.watch.on_air is True
    assert state.watch.live_key == refreshed.key
    assert state.focus == refreshed.key
    # The source annotation recovers without changing the frozen journey.
    assert state.current() is state.watch.link
    assert state.watch.deadline == pytest.approx(120.0)


def test_watch_narration_targets_the_live_handoff_not_the_frozen_old_dish():
    original = _link(dish="DSS43", range_km=dsn.C_KM_S * 20.0)
    state = _seeded_state(original)
    assert dsn.toggle_realtime(state, now=100.0) is True

    handoff = replace(original, dish="DSS14", down_bps=320.0)
    dsn.reconcile_links(state, [handoff], now=101.0)

    assert state.current() is state.watch.link  # journey stays click-time frozen
    assert dsn.narration_target_link(state) is handoff
    assert dsn.narration_target_link(state).key == "DSS14/VGR2"


def test_just_received_but_old_source_snapshot_is_not_fresh():
    now = 1_800_000_000.0
    state = dsn.State()
    state.feed_timestamp_ms = int((now - dsn.FEED_STALE_S - 1.0) * 1000)
    state.feed_advanced_at = now

    assert dsn.feed_freshness(state, now=now) != "fresh"


def test_seeded_state_quarantines_a_snapshot_without_a_source_timestamp(monkeypatch):
    original = _link()
    state = _seeded_state(original)
    state.feed_timestamp_ms = 2_000
    state.feed_advanced_at = 100.0

    timestamp_less = _duplex_feed("160", craft="JNO").replace(
        b"<timestamp>1786153687000</timestamp>", b"")

    class Response:
        content = timestamp_less

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response()

    async def stop_after_poll(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_poll)
    monkeypatch.setattr(dsn, "append_history", lambda _events: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert [link.key for link in state.links] == [original.key]
    assert state.feed_timestamp_ms == 2_000
    assert state.event_queue == []


def test_horizons_failure_does_not_enter_the_success_range_cache(monkeypatch):
    unresolved = _link(craft="SPP", naif=-96, range_km=None)
    state = _seeded_state(unresolved)

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            raise OSError("temporary Horizons outage")

    async def stop_after_attempt(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
    monkeypatch.setattr(dsn, "save_ranges", lambda _state: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert unresolved.naif not in state.ranges


def test_horizons_unavailable_target_gets_a_long_negative_backoff(monkeypatch):
    unresolved = _link(craft="XMM", naif=-60, range_km=None)
    state = _seeded_state(unresolved)

    class Response:
        text = ("API VERSION: 1.2\nAPI SOURCE: NASA/JPL Horizons API\n\n"
                "No such record, positive values only\n")

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response()

    async def stop_after_attempt(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
    before = time.time()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert unresolved.naif not in state.ranges
    assert state.range_retry_at[unresolved.naif] >= (
        before + dsn.RANGE_UNAVAILABLE_RETRY_S)
    assert unresolved.naif in state.range_unavailable
    with pytest.raises(dsn.HorizonsUnavailable,
                       match="No such record, positive values only"):
        dsn.horizons_au(Response.text)

    state.names = {"xmm": "X-ray Multi-Mirror Mission"}
    assert dsn.narration_ready(state, unresolved)
    words = dsn.spoken(unresolved, state.names, state.dish_types)
    assert "kilometres away" not in words
    assert "signal takes" not in words


def test_malformed_horizons_success_keeps_the_short_transient_retry(monkeypatch):
    unresolved = _link(craft="SPP", naif=-96, range_km=None)
    state = _seeded_state(unresolved)

    class Response:
        text = "<html>temporary upstream response</html>"

        def raise_for_status(self):
            return None

    class Client:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def get(self, *args, **kwargs):
            return Response()

    async def stop_after_attempt(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_attempt)
    before = time.time()

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert unresolved.naif not in state.ranges
    assert unresolved.naif not in state.range_unavailable
    assert before + dsn.RANGE_RETRY_S <= state.range_retry_at[unresolved.naif]
    assert state.range_retry_at[unresolved.naif] < (
        before + dsn.RANGE_UNAVAILABLE_RETRY_S)
    with pytest.raises(ValueError, match="missing Horizons ephemeris table"):
        dsn.horizons_au(Response.text)


def test_horizons_range_parser_accepts_the_official_observer_table():
    body = """API VERSION: 1.2
$$SOE
 2026-Aug-08 06:52:17.184     6.29489242661185  -4.6392252
$$EOE
"""
    assert dsn.horizons_au(body) == pytest.approx(6.29489242661185)


@pytest.mark.parametrize("band", ["S", "K", "Ka"])
def test_band_narration_does_not_claim_band_alone_explains_live_rate(band):
    words = dsn.band_words(band).lower()
    assert f"{band.lower()} band" in words
    for unsupported_claim in ("the reason", "most of why", "slowest"):
        assert unsupported_claim not in words


def test_narration_attributes_spacecraft_identity_to_the_source():
    opening = dsn.spoken(
        _link(), {"vgr2": "Voyager 2"}, {"DSS43": "70M"}).split(".", 1)[0].lower()

    assert any(marker in opening for marker in (
        "dsn now", "feed", "reports", "reported", "identifies", "appears"
    )), "NASA warns that a live lock can occasionally be misidentified"


@pytest.mark.parametrize("test_name", ["DOUG", "SHAN"])
def test_nasa_test_targets_are_not_presented_as_spacecraft(test_name):
    links = dsn.parse_feed(_duplex_feed("160", craft=test_name))
    assert links == []


def test_directional_ranges_wind_and_every_uplink_record_survive_parsing():
    feed = b"""<dsn>
      <station friendlyName="Canberra"/>
      <dish name="DSS43" azimuthAngle="42" elevationAngle="23"
            windSpeed="9" activity="Engineering DEMO">
        <upSignal active="true" signalType="data" band="S" power="18"
                  spacecraft="VGR2" spacecraftID="-32"/>
        <upSignal active="true" signalType="ranging" band="X" power="5"
                  spacecraft="VGR2" spacecraftID="-32"/>
        <target name="VGR2" id="32" uplegRange="21000000001"
                downlegRange="21000000099"/>
      </dish>
      <timestamp>1786153687000</timestamp>
    </dsn>"""
    link = dsn.parse_feed(feed)[0]

    assert link.range_km == 21_000_000_001, "uplink-only must use uplegRange"
    assert link.up_range_km == 21_000_000_001
    assert link.wind_kmh == 9
    assert link.activity == "Engineering DEMO"
    assert [(stream.band, stream.kw, stream.signal_type)
            for stream in link.up_streams] == [
                ("S", 18.0, "data"), ("X", 5.0, "ranging")]
    assert link.up_kw == 18.0, "scalar power is strongest record, never a sum"

    with_down = feed.replace(
        b'<target name="VGR2"',
        b'<downSignal active="true" signalType="data" band="X" '
        b'dataRate="160" power="-140" spacecraft="VGR2" spacecraftID="-32"/>'
        b'<target name="VGR2"')
    duplex = dsn.parse_feed(with_down)[0]
    assert duplex.range_km == 21_000_000_099, "receive must use downlegRange"
    assert duplex.down_streams[0].signal_type == "data"


def test_round_trip_narration_adds_published_legs_without_promising_an_answer():
    link = _link(
        range_km=dsn.C_KM_S * 120,
        up_range_km=dsn.C_KM_S * 180,
    )
    words = dsn.spoken(link).lower()
    assert "light-time alone for an immediate round trip" in words
    assert "5 minutes" in words
    assert "answered" not in words

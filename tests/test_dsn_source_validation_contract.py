"""Adversarial contracts for DSN source, config, and range-cache boundaries."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


def _feed(timestamp: int, *, station: str = "gdscc", craft: str = "VGR2",
          dish: str = "DSS14", rate: str = "160") -> bytes:
    return f"""<dsn>
      <station name="{station}"/>
      <dish name="{dish}" elevationAngle="30" azimuthAngle="120">
        <downSignal active="true" spacecraft="{craft}" spacecraftID="-32"
                    band="X" dataRate="{rate}" power="-140"/>
        <target name="{craft}" id="32" downlegRange="21000000000"/>
      </dish>
      <timestamp>{timestamp}</timestamp>
    </dsn>""".encode()


def _link(*, craft: str = "VGR2", dish: str = "DSS14",
          naif: int = -32) -> dsn.Link:
    return dsn.Link(
        complex_name="Goldstone", dish=dish, craft=craft,
        elevation=30.0, azimuth=120.0, band="X", down_bps=160.0,
        up_active=False, range_km=None, naif=naif,
        down_streams=(dsn.DownStream("X", 160.0, -140.0),), streams=1,
    )


def _dense_feed(dishes: int, links_per_dish: int, timestamp: int = 1_000) -> bytes:
    parts = ['<dsn><station name="gdscc"/>']
    for dish_index in range(dishes):
        dish = f"DSS{dish_index:09d}"  # maximum accepted dish-code width
        parts.append(
            f'<dish name="{dish}" elevationAngle="30" azimuthAngle="120">')
        for link_index in range(links_per_dish):
            craft = f"C{dish_index:02d}{link_index:02d}"
            parts.append(
                f'<downSignal active="true" spacecraft="{craft}" '
                f'spacecraftID="-{1000 + dish_index * 10 + link_index}" '
                'band="X" dataRate="160" power="-140"/>')
        parts.append('</dish>')
    parts.append(f'<timestamp>{timestamp}</timestamp></dsn>')
    return "".join(parts).encode()


def test_timestamp_parser_and_freshness_reject_unbounded_epochs():
    huge = b"<dsn><timestamp>" + b"9" * 4000 + b"</timestamp></dsn>"
    assert dsn.feed_timestamp_ms(huge) is None

    state = dsn.State(
        feed_timestamp_ms=int("9" * 4000), feed_advanced_at=1_800_000_000.0)
    assert dsn.feed_freshness(state, now=1_800_000_000.0) == "stale"


def test_fixture_counter_cannot_drive_a_fresh_production_scene():
    now = 1_800_000_000.0
    assert dsn.source_timestamp_valid(1, now) is False
    state = dsn.State(feed_timestamp_ms=1, feed_advanced_at=now)
    assert dsn.feed_freshness(state, now=now) == "stale"


def test_future_snapshot_cannot_poison_the_feed_watermark(monkeypatch):
    now = 1_800_000_000.0
    future = int((now + 365 * 86400) * 1000)
    current = int(now * 1000)
    responses = iter((_feed(future), _feed(current)))

    class Response:
        def __init__(self, content):
            self.content = content

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
            return Response(next(responses))

    observed = []
    state = dsn.State()

    async def after_poll(_delay):
        observed.append(state.feed_timestamp_ms)
        if len(observed) == 2:
            raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.time, "time", lambda: now)
    monkeypatch.setattr(dsn.asyncio, "sleep", after_poll)
    monkeypatch.setattr(dsn, "append_history", lambda _events: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert observed == [None, current]
    assert state.feed_timestamp_ms == current
    assert [link.key for link in state.links] == ["DSS14/VGR2"]


def test_site_codes_are_canonical_without_friendly_names():
    assert dsn.parse_feed(_feed(1_000, station="gdscc"))[0].complex_name == \
        "Goldstone"
    assert dsn.parse_feed(_feed(1_000, station="mdscc"))[0].complex_name == \
        "Madrid"
    assert dsn.parse_feed(_feed(1_000, station="cdscc"))[0].complex_name == \
        "Canberra"


def test_config_is_complete_finite_and_range_checked():
    fixture = Path("tests/fixtures/dsn_config.xml").read_bytes()
    names, dishes, sites = dsn.parse_config(fixture)
    assert names["vgr2"] == "Voyager 2"
    assert dishes["DSS43"] == "70M"
    assert set(sites) == {"Goldstone", "Madrid", "Canberra"}

    for invalid in (b'longitude="NaN"', b'longitude="Infinity"',
                    b'longitude="181"'):
        poisoned = fixture.replace(b'longitude="-4.2480085"', invalid, 1)
        with pytest.raises(ValueError, match="longitude"):
            dsn.parse_config(poisoned)

    start = fixture.index(b'<site name="gdscc"')
    end = fixture.index(b'</site>', start) + len(b'</site>')
    partial = fixture[:start] + fixture[end:]
    with pytest.raises(ValueError, match="missing DSN sites"):
        dsn.parse_config(partial)


def test_invalid_config_does_not_partially_replace_last_known_good(monkeypatch):
    fixture = Path("tests/fixtures/dsn_config.xml").read_bytes()
    poisoned = fixture.replace(
        b'longitude="-4.2480085"', b'longitude="NaN"', 1)

    class Response:
        content = poisoned

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

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    state = dsn.State(
        names={"vgr2": "Voyager 2"}, dish_types={"DSS14": "70M"},
        site_lons={"Goldstone": -116.9})
    before = (state.names.copy(), state.dish_types.copy(), state.site_lons.copy())

    assert asyncio.run(dsn.fetch_names(state)) is False
    assert (state.names, state.dish_types, state.site_lons) == before


def test_source_identity_limits_reject_instead_of_truncate():
    long_code = "X" * (dsn.SOURCE_CODE_MAX + 1)
    with pytest.raises(dsn.SourceValidationError, match="oversized.*identity"):
        dsn.parse_feed(_feed(1_000, craft=long_code))
    with pytest.raises(ValueError, match="bounded document size"):
        dsn.parse_feed(b" " * (dsn.FEED_XML_MAX_BYTES + 1))

    fixture = Path("tests/fixtures/dsn_config.xml").read_bytes()
    extra = (f'<spacecraft name="long" friendlyName="'
             f'{"X" * (dsn.SOURCE_NAME_MAX + 1)}"/>').encode()
    bounded = fixture.replace(b"</spacecraftMap>", extra + b"</spacecraftMap>")
    with pytest.raises(dsn.SourceValidationError,
                       match="oversized config spacecraft friendly name"):
        dsn.parse_config(bounded)

    enormous_id = "9" * 4000
    hostile_id = _feed(1_000).replace(
        b'spacecraftID="-32"', f'spacecraftID="{enormous_id}"'.encode()).replace(
        b'id="32"', f'id="{enormous_id}"'.encode())
    assert dsn.parse_feed(hostile_id)[0].naif is None

    # The renderer's future-ID policy is usable through ingestion too; the
    # old ten-digit suffix crash must not be "fixed" by rejecting that ID.
    future_dish = "DSS1234567890"
    assert len(future_dish) <= dsn.SOURCE_DISH_CODE_MAX
    assert dsn.parse_feed(_feed(1_000, dish=future_dish))[0].dish == future_dish


def test_extreme_source_ranges_remain_unknown_and_cannot_expand_narration():
    hostile = _feed(1_000).replace(
        b'downlegRange="21000000000"', b'downlegRange="1e300"')
    parsed = dsn.parse_feed(hostile)[0]

    assert parsed.range_km is None
    assert parsed.light_s is None
    assert dsn.distance_words(float("inf")) == ""
    assert dsn.distance_words(1e300) == ""
    with pytest.raises(ValueError, match="invalid Horizons observer range"):
        dsn.horizons_au("$$SOE\n2026-Aug-08 00:00 1e300\n$$EOE")


def test_rejected_identity_snapshot_cannot_manufacture_a_link_loss(monkeypatch):
    now = 1_800_000_000.0
    original = _link()
    state = dsn.State(
        links=[original], feed_seeded=True,
        feed_timestamp_ms=int((now - 1) * 1000), feed_advanced_at=now - 1)
    hostile = _feed(
        int(now * 1000), craft="X" * (dsn.SOURCE_CODE_MAX + 1))

    class Response:
        content = hostile

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
    monkeypatch.setattr(dsn.time, "time", lambda: now)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_poll)
    monkeypatch.setattr(dsn, "append_history", lambda _events: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_feed(state))

    assert state.links == [original]
    assert state.feed_timestamp_ms == int((now - 1) * 1000)
    assert state.event_queue == []


def test_maximum_accepted_collection_has_a_complete_bounded_roster_plan():
    feed = _dense_feed(
        dsn.FEED_DISHES_PER_SITE_MAX, dsn.FEED_LINKS_PER_DISH_MAX)
    assert len(feed) < dsn.FEED_XML_MAX_BYTES
    links = dsn.parse_feed(feed)
    assert len(links) == (
        dsn.FEED_DISHES_PER_SITE_MAX * dsn.FEED_LINKS_PER_DISH_MAX)

    groups = dsn.group_links_by_dish(links, "Goldstone")
    width = dsn._dish_roster_width(groups)
    travel_capacity = (
        dsn.SCROLL_SPEED_PX_S * dsn.MAX_ANIMATION_FRAMES
        / dsn.INSTRUMENT_FPS)
    assert width + dsn.SCROLL_GAP_PX <= travel_capacity
    assert dsn.dish_network_frame_count(links) <= dsn.MAX_ANIMATION_FRAMES

    longest = links[0]
    names = {longest.craft.lower(): "W" * dsn.SOURCE_NAME_MAX}
    moving = dsn.distance_header_layout(longest, names)[3]
    assert dsn.text_width(moving) + dsn.SCROLL_GAP_PX <= travel_capacity
    assert dsn.distance_frame_count(longest, names) <= dsn.MAX_ANIMATION_FRAMES


@pytest.mark.parametrize(("dishes", "links_per_dish", "message"), [
    (dsn.FEED_DISHES_PER_SITE_MAX + 1, 1, "too many dishes"),
    (1, dsn.FEED_LINKS_PER_DISH_MAX + 1, "too many active links"),
])
def test_over_budget_valid_feed_is_rejected_as_one_snapshot(
        dishes, links_per_dish, message):
    with pytest.raises(dsn.SourceValidationError, match=message):
        dsn.parse_feed(_dense_feed(dishes, links_per_dish))


def test_config_collection_limit_is_fail_closed():
    fixture = Path("tests/fixtures/dsn_config.xml").read_bytes()
    count = dsn.CONFIG_SPACECRAFT_MAX - 168 + 1
    extras = b"".join(
        f'<spacecraft name="z{index}" friendlyName="Z {index}"/>'.encode()
        for index in range(count))
    oversized = fixture.replace(
        b"</spacecraftMap>", extras + b"</spacecraftMap>")
    assert len(oversized) < dsn.CONFIG_XML_MAX_BYTES
    with pytest.raises(dsn.SourceValidationError, match="too many spacecraft"):
        dsn.parse_config(oversized)


def test_unknown_dish_size_remains_unknown():
    assert dsn.dish_metres("DSS14", {}) is None
    assert dsn.dish_metres("DSS999", {"DSS14": "70M"}) is None
    assert dsn.dish_metres("DSS14", {"DSS14": "70M"}) == "70"


def test_range_cache_is_versioned_validated_and_range_sensitive(
        tmp_path, monkeypatch):
    cache = tmp_path / "ranges.json"
    monkeypatch.setattr(dsn, "RANGE_CACHE", cache)
    now = 1_800_000_000.0
    monkeypatch.setattr(dsn.time, "time", lambda: now)

    cache.write_text(json.dumps({
        "version": dsn.RANGE_CACHE_VERSION,
        "ranges": {
            "-151": [70_000.0, now - dsn.RANGE_NEAR_EARTH_TTL_S - 1],
            "-32": [21_000_000_000.0, now - 3600],
            "-61": [820_000_000.0, now + 1],
        },
    }))
    state = dsn.State()
    dsn.load_ranges(state)
    assert state.range_state.values == {
        -32: (21_000_000_000.0, now - 3600),
    }

    dsn.save_ranges(state)
    saved = json.loads(cache.read_text())
    assert saved["version"] == dsn.RANGE_CACHE_VERSION
    assert set(saved["ranges"]) == {"-32"}
    assert dsn.range_ttl_s(70_000.0) == dsn.RANGE_NEAR_EARTH_TTL_S
    assert dsn.range_ttl_s(21_000_000_000.0) == dsn.RANGE_TTL_S


@pytest.mark.parametrize("payload", [
    [],
    {"version": 1, "ranges": {"-32": [1]}},
    {"version": 1, "ranges": {"bad": [1.0, 2.0]}},
    {"version": 1, "ranges": {"-32": ["NaN", 2.0]}},
    {"version": 2, "ranges": {}},
])
def test_malformed_valid_json_cache_is_ignored_atomically(
        payload, tmp_path, monkeypatch):
    cache = tmp_path / "ranges.json"
    cache.write_text(json.dumps(payload))
    monkeypatch.setattr(dsn, "RANGE_CACHE", cache)
    original = {-32: (21_000_000_000.0, 1_800_000_000.0)}
    state = dsn.State(range_state=dsn.RangeState(values=original.copy()))

    dsn.load_ranges(state)

    assert state.range_state.values == original


def test_one_horizons_query_fills_every_link_with_the_same_naif(monkeypatch):
    first = _link(craft="ALIAS1", dish="DSS14", naif=-32)
    second = _link(craft="ALIAS2", dish="DSS43", naif=-32)
    state = dsn.State(links=[first, second], feed_seeded=True)
    calls = []

    class Response:
        text = "$$SOE\n2026-Aug-08 00:00:00.000  1.0  0.0\n$$EOE"

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
            calls.append(1)
            return Response()

    async def stop_after_query(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(dsn.httpx, "AsyncClient", Client)
    monkeypatch.setattr(dsn.asyncio, "sleep", stop_after_query)
    monkeypatch.setattr(dsn, "save_ranges", lambda _state: None)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(dsn.poll_ranges(state))

    assert len(calls) == 1
    assert first.range_km == second.range_km
    assert first.range_km is not None


def test_signal_and_link_duplicates_are_canonical_not_aggregated():
    single = _feed(1_000)
    signal_start = single.index(b"        <downSignal")
    signal_end = single.index(b"/>", signal_start) + len(b"/>\n")
    signal = single[signal_start:signal_end]
    duplicate_signal = single[:signal_end] + signal + single[signal_end:]
    link = dsn.parse_feed(duplicate_signal)[0]
    assert link.streams == 1
    assert link.down_bps == 160.0

    two_rates = single.replace(
        b"        <target",
        b'''        <downSignal active="true" spacecraft="VGR2"
                    spacecraftID="-32" band="X" dataRate="320"
                    power="-141"/>
        <target''', 1)
    link = dsn.parse_feed(two_rates)[0]
    assert link.streams == 2
    assert link.down_bps is None
    assert {stream.bps for stream in link.down_streams} == {160.0, 320.0}

    block = single.split(b"      <dish", 1)[1].split(b"      </dish>", 1)[0]
    duplicate_block = single.replace(
        b"      </dish>", b"      </dish>\n      <dish" + block
        + b"      </dish>", 1)
    assert len(dsn.parse_feed(duplicate_block)) == 1

    contradiction = duplicate_block.replace(b'dataRate="160"',
                                            b'dataRate="161"', 1)
    with pytest.raises(dsn.SourceValidationError,
                       match="contradictory duplicate DSN link"):
        dsn.parse_feed(contradiction)


def test_band_and_native_text_normalization_cover_source_variants():
    for variant in ("K", " k ", "K BAND", "K-band", "k_band"):
        assert dsn.band_key(variant) == "K"

    state = dsn.State(links=[_link(craft="ZÜRICH")])
    assert dsn.picker_label(state) == "ZURICH 1/1"
    handoff = dsn.event_label({
        "event": "handoff", "craft": "ZÜRICH",
        "from_dish": "DSS14", "dish": "DSS43",
    })
    assert handoff == "ZURICH 14>43"

    payloads = (dsn._picker_payload("MÜNCHEN"),
                dsn._event_payload("MÜNCHEN"),
                dsn._status_payload("MÜNCHEN"))
    for payload in payloads:
        for element in payload.elements:
            if getattr(element, "type", None) == "text":
                assert element.text == "MUNCHEN"
                assert all(0x20 <= ord(ch) <= 0x7E for ch in element.text)

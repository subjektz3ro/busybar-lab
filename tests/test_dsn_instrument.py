"""Host-side contracts for the DSN live-instrument view.

These tests deliberately exercise only source data and rendered pixels.  They
need neither a BUSY Bar nor network access.
"""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import source as dsn_source
from apps.dsn_app import telemetry as dsn_telemetry
from apps.dsn_app.render import dish as dsn_render_dish
from apps.dsn_app.render import instrument as dsn_render_instrument
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import scope as dsn_render_scope
from apps.dsn_app.render import text as dsn_render_text


FEED = b"""<dsn>
 <station name="cdscc" friendlyName="Canberra" timeUTC="1786145549000"/>
 <dish name="DSS34" azimuthAngle="331" elevationAngle="21"
       activity="Spacecraft Telemetry, Tracking, and Command"
       isMSPA="true" isArray="true" isDDOR="true">
  <upSignal active="true" signalType="data" dataRate="0" frequency="0"
            band="X" power="18" spacecraft="LRO" spacecraftID="-85"/>
  <downSignal active="true" signalType="data" dataRate="28590000"
              frequency="0" band="K" power="-88" spacecraft="LRO"
              spacecraftID="-85"/>
  <downSignal active="true" signalType="data" dataRate="146400"
              frequency="0" band="S" power="-120" spacecraft="LRO"
              spacecraftID="-85"/>
  <target name="LRO" id="85" uplegRange="366000"
          downlegRange="366000" rtlt="-1"/>
 </dish>
 <timestamp>1786145549000</timestamp>
</dsn>"""


def instrument_link(**changes) -> dsn_source.Link:
    base = dsn_source.Link(
        complex_name="Canberra",
        dish="DSS34",
        craft="LRO",
        elevation=21.0,
        band="K",
        down_bps=28_736_400.0,
        up_active=True,
        range_km=366_000.0,
        down_dbm=-88.0,
        up_kw=18.0,
        streams=2,
        azimuth=331.0,
        down_streams=(
            dsn_source.DownStream("K", 28_590_000.0, -88.0),
            dsn_source.DownStream("S", 146_400.0, -120.0),
        ),
        up_band="X",
    )
    return replace(base, **changes)


def _row_positions(frame, y: int, colour: tuple[int, int, int]) -> list[int]:
    px = frame.load()
    # Keep endpoint power flares and wraparound out of direction assertions.
    return [x for x in range(dsn_render_palette.INSTRUMENT_X0 + 5, dsn_render_palette.INSTRUMENT_X1 - 3)
            if px[x, y] == colour]


def _carrier_region(frame) -> tuple[tuple[tuple[int, int, int], ...], ...]:
    px = frame.load()
    return tuple(tuple(px[x, y] for x in range(dsn_render_palette.INSTRUMENT_X0,
                                                dsn_render_palette.INSTRUMENT_X1 + 1))
                 for y in range(6, 11))


def _glyph_points(x: int, rows: tuple[str, ...]) -> set[tuple[int, int]]:
    return {(x + dx, 6 + dy)
            for dy, row in enumerate(rows)
            for dx, bit in enumerate(row) if bit == "1"}


def test_feed_retains_source_time_pointing_and_each_real_stream():
    assert dsn_source.feed_timestamp_ms(FEED) == 1_786_145_549_000
    link = dsn_source.parse_feed(FEED)[0]

    assert (link.complex_name, link.dish, link.craft) == (
        "Canberra", "DSS34", "LRO")
    assert (link.azimuth, link.elevation) == (331.0, 21.0)
    assert link.activity == "Spacecraft Telemetry, Tracking, and Command"
    assert (link.up_active, link.up_band, link.up_kw) == (True, "X", 18.0)
    assert link.down_bps is None  # receive records may be redundant, not summable
    assert {stream.bps for stream in link.down_streams} == {
        28_590_000.0, 146_400.0}
    assert link.down_dbm == -88.0
    assert link.streams == 2
    assert [(stream.band, stream.bps, stream.dbm)
            for stream in link.down_streams] == [
                ("K", 28_590_000.0, -88.0),
                ("S", 146_400.0, -120.0),
            ]
    assert (link.arrayed, link.mspa, link.ddor) == (True, True, True)


@pytest.mark.parametrize(
    ("azimuth", "expected"),
    [(0.0, (dsn_render_palette.SCOPE_CX, dsn_render_palette.SCOPE_CY - dsn_render_palette.SCOPE_R)),
     (90.0, (dsn_render_palette.SCOPE_CX + dsn_render_palette.SCOPE_R, dsn_render_palette.SCOPE_CY)),
     (180.0, (dsn_render_palette.SCOPE_CX, dsn_render_palette.SCOPE_CY + dsn_render_palette.SCOPE_R)),
     (270.0, (dsn_render_palette.SCOPE_CX - dsn_render_palette.SCOPE_R, dsn_render_palette.SCOPE_CY))],
)
def test_pointing_pixel_puts_horizon_on_the_true_cardinal(azimuth, expected):
    assert dsn_render_scope.pointing_pixel(azimuth, 0.0) == expected


def test_pointing_pixel_puts_zenith_at_the_centre_and_clamps_elevation():
    centre = (dsn_render_palette.SCOPE_CX, dsn_render_palette.SCOPE_CY)
    for azimuth in (0.0, 73.0, 180.0, 359.0):
        assert dsn_render_scope.pointing_pixel(azimuth, 90.0) == centre
        assert dsn_render_scope.pointing_pixel(azimuth, 1000.0) == centre
    assert dsn_render_scope.pointing_pixel(360.0, -10.0) == dsn_render_scope.pointing_pixel(0.0, 0.0)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0, 0), (1, 1), (999, 1), (1_000, 2), (99_999, 2),
     (100_000, 3), (999_999, 3), (1_000_000, 4),
     (9_999_999, 4), (10_000_000, 5)],
)
def test_rate_buckets_change_only_at_declared_thresholds(value, expected):
    assert dsn_telemetry.rate_bucket(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0), (-151.0, 1), (-150.0, 2), (-136.0, 2),
     (-135.0, 3), (-88.0, 3)],
)
def test_receive_power_buckets_change_only_at_declared_thresholds(value, expected):
    assert dsn_telemetry.receive_power_bucket(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [(0.0, 0), (0.1, 1), (0.999, 1), (1.0, 2), (9.999, 2), (10.0, 3)],
)
def test_transmit_power_buckets_change_only_at_declared_thresholds(value, expected):
    assert dsn_telemetry.transmit_power_bucket(value) == expected


def test_instrument_signature_ignores_jitter_that_changes_no_physical_led():
    link = instrument_link(
        azimuth=331.0,
        up_kw=18.10,
        down_dbm=-88.10,
        down_streams=(
            dsn_source.DownStream("K", 28_590_010.0, -88.10),
            dsn_source.DownStream("S", 146_410.0, -120.10),
        ),
    )
    jittered = replace(
        link,
        azimuth=331.20,
        up_kw=18.20,
        down_dbm=-88.20,
        down_streams=(
            dsn_source.DownStream("K", 28_590_020.0, -88.20),
            dsn_source.DownStream("S", 146_420.0, -120.20),
        ),
    )
    assert dsn_render_scope.pointing_pixel(link.azimuth, link.elevation) == \
        dsn_render_scope.pointing_pixel(jittered.azimuth, jittered.elevation)
    assert dsn_render_instrument.instrument_signature(link, [], "fresh") == \
        dsn_render_instrument.instrument_signature(jittered, [], "fresh")


def test_instrument_signature_changes_for_visible_or_semantic_changes():
    base = instrument_link(
        azimuth=0.0,
        elevation=0.0,
        down_bps=999.0,
        down_dbm=-151.0,
        up_kw=0.9,
        streams=1,
        down_streams=(dsn_source.DownStream("S", 999.0, -151.0),),
    )
    signature = dsn_render_instrument.instrument_signature(base, [], "fresh")
    variants = [
        replace(base, azimuth=90.0),
        replace(base, down_bps=1_000.0,
                down_streams=(dsn_source.DownStream("S", 1_000.0, -151.0),)),
        replace(base, down_dbm=-149.0,
                down_streams=(dsn_source.DownStream("S", 999.0, -149.0),)),
        replace(base, up_kw=1.0),
        replace(base, down_streams=(dsn_source.DownStream("X", 999.0, -151.0),)),
        replace(base, arrayed=True),
    ]
    for changed in variants:
        assert dsn_render_instrument.instrument_signature(changed, [], "fresh") != signature
    assert dsn_render_instrument.instrument_signature(base, [(1, 1)], "fresh") != signature
    assert dsn_render_instrument.instrument_signature(base, [], "delayed") != signature


def test_instrument_animation_has_one_fixed_device_native_clock():
    frames, fps, hold = dsn_render_instrument.render_instrument_frames(instrument_link())
    assert len(frames) == dsn_limits.INSTRUMENT_FRAMES == 40
    assert fps == dsn_limits.INSTRUMENT_FPS == 5
    assert hold == 1
    assert len(frames) / fps == pytest.approx(8.0)
    assert {frame.size for frame in frames} == {(72, 16)}


def test_only_fresh_carriers_move():
    link = instrument_link()
    fresh, _, _ = dsn_render_instrument.render_instrument_frames(link, freshness="fresh")
    delayed, _, _ = dsn_render_instrument.render_instrument_frames(link, freshness="delayed")
    stale, _, _ = dsn_render_instrument.render_instrument_frames(link, freshness="stale")

    assert len({_carrier_region(frame) for frame in fresh}) > 1
    assert len({_carrier_region(frame) for frame in delayed}) == 1
    assert len({_carrier_region(frame) for frame in stale}) == 1


def test_fresh_animation_leaves_the_rail_to_the_native_live_lease():
    frames, _, _ = dsn_render_instrument.render_instrument_frames(instrument_link(), freshness="fresh")
    assert all(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))


def test_uplink_and_downlink_move_in_their_literal_directions():
    link = instrument_link(
        band="S",
        down_bps=100.0,
        down_dbm=-140.0,
        streams=1,
        down_streams=(dsn_source.DownStream("S", 100.0, -140.0),),
    )
    frames, _, _ = dsn_render_instrument.render_instrument_frames(link, freshness="fresh")

    up0 = _row_positions(frames[0], 6, dsn_render_palette.UPLINK)
    up1 = _row_positions(frames[1], 6, dsn_render_palette.UPLINK)
    down0 = _row_positions(frames[0], 9, dsn_render_palette.BAND_PULSE["S"])
    down1 = _row_positions(frames[1], 9, dsn_render_palette.BAND_PULSE["S"])
    assert up0 and up1 and down0 and down1
    assert min(up1) > min(up0), "uplink must move from the dish toward the craft"
    assert min(down1) < min(down0), "downlink must move from the craft toward Earth"


def test_each_downsignal_gets_its_own_band_coloured_lane():
    frames, _, _ = dsn_render_instrument.render_instrument_frames(instrument_link())
    assert any(dsn_render_palette.BAND_PULSE["K"] in [frame.getpixel((x, 8))
                                      for x in range(dsn_render_palette.INSTRUMENT_X0,
                                                     dsn_render_palette.INSTRUMENT_X1 + 1)]
               for frame in frames)
    assert any(dsn_render_palette.BAND_PULSE["S"] in [frame.getpixel((x, 10))
                                      for x in range(dsn_render_palette.INSTRUMENT_X0,
                                                     dsn_render_palette.INSTRUMENT_X1 + 1)]
               for frame in frames)


def test_zero_rate_stream_is_a_silent_tether_not_a_fake_carrier():
    link = instrument_link(
        band="X",
        down_bps=0.0,
        down_dbm=-140.0,
        streams=1,
        down_streams=(dsn_source.DownStream("X", 0.0, -140.0),),
    )
    frames, _, _ = dsn_render_instrument.render_instrument_frames(link, freshness="fresh")
    for frame in frames:
        interior = [frame.getpixel((x, 9))
                    for x in range(dsn_render_palette.INSTRUMENT_X0 + 5,
                                   dsn_render_palette.INSTRUMENT_X1 + 1)]
        assert dsn_render_palette.BAND_PULSE["X"] not in interior


def test_missing_live_fields_stay_unknown_instead_of_becoming_strong_x_band():
    feed = b"""<dsn>
      <station friendlyName="Canberra"/>
      <dish name="DSS34">
        <downSignal active="true" spacecraft="LRO" dataRate="0" power="NaN"/>
        <target name="LRO" id="85" downlegRange="366000"/>
      </dish>
      <timestamp>1786145549000</timestamp>
    </dsn>"""
    link = dsn_source.parse_feed(feed)[0]

    assert link.pointing_valid is False
    assert dsn_render_scope.link_pointing_pixel(link) is None
    assert link.band == ""
    assert link.down_streams[0].band == ""
    assert link.down_dbm is None
    assert dsn_telemetry.receive_power_bucket(link.down_streams[0].dbm) == 0
    assert dsn_render_instrument._instrument_metrics(link)[0] == ("?", "0BPS")

    frame = dsn_render_instrument.render_instrument_frames(link)[0][0]
    assert all(frame.getpixel((x, y)) != dsn_render_palette.SCOPE_HEAD
               for x in range(dsn_limits.W) for y in range(dsn_limits.H))
    # Unknown power gets no invented weak flare; the endpoint is only tether.
    assert frame.getpixel((dsn_render_palette.INSTRUMENT_X0, 9)) == dsn_render_palette.INSTRUMENT_TETHER


def test_nonfinite_numeric_fields_are_missing_not_maximal():
    assert dsn_render_text._f("NaN", 7.0) == 7.0
    assert dsn_render_text._f("inf", 7.0) == 7.0
    assert dsn_telemetry.receive_power_bucket(float("nan")) == 0


def test_instrument_contrast_clears_the_measured_physical_panel_floor():
    assert max(dsn_render_palette.INSTRUMENT_TETHER) >= 77
    assert max(dsn_render_palette.SCOPE_TRAIL) - max(dsn_render_palette.SCOPE_RING) >= 77
    weak_flare = (175, 105, 35)
    assert max(weak_flare) - max(dsn_render_palette.INSTRUMENT_TETHER) >= 77


def test_a_black_gutter_separates_metrics_and_modes_from_freshness():
    frames, _, _ = dsn_render_instrument.render_instrument_frames(instrument_link())
    assert all(frame.getpixel((dsn_render_palette.FRESH_GUTTER_X, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))


def test_array_mspa_and_ddor_glyphs_have_distinct_composable_geometry():
    array_rows = ("101", "111", "010", "010", "010")
    mspa_rows = ("010", "010", "111", "101", "101")
    ddor_rows = ("010", "101", "010", "101", "010")
    expected = {
        dsn_render_dish.ANTENNA: _glyph_points(61, array_rows),
        dsn_render_palette.NAME: _glyph_points(64, mspa_rows),
        dsn_render_dish.DDOR_MARK: _glyph_points(67, ddor_rows),
    }
    link = instrument_link(arrayed=True, mspa=True, ddor=True)
    frame = dsn_render_instrument.render_instrument_frames(link)[0][0]
    px = frame.load()

    for colour, points in expected.items():
        assert {point for point in points if px[point] == colour} == points
    all_points = list(expected.values())
    assert all(a.isdisjoint(b) for i, a in enumerate(all_points)
               for b in all_points[i + 1:])

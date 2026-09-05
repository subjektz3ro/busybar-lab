"""Extreme finite telemetry must never become a false exact display value."""

from dataclasses import replace

import pytest

from apps.dsn_app import cli as dsn_cli
from apps.dsn_app import source as dsn_source
from apps.dsn_app.audio import words as dsn_audio_words
from apps.dsn_app.render import instrument as dsn_render_instrument
from apps.dsn_app.render import labels as dsn_render_labels


def _link(**changes) -> dsn_source.Link:
    base = dsn_source.Link(
        complex_name="Canberra",
        dish="DSS43",
        craft="VGR2",
        elevation=30.0,
        band="X",
        down_bps=160.0,
        up_active=True,
        range_km=dsn_source.C_KM_S * 60.0,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        down_streams=(dsn_source.DownStream("X", 160.0, -140.0),),
        up_streams=(dsn_source.UpStream("X", 18.0, "data"),),
    )
    return replace(base, **changes)


@pytest.mark.parametrize(
    ("bps", "expected"),
    [
        (None, "RATE?"),
        (float("inf"), "RATE?"),
        (-1.0, "RATE?"),
        (0.0, "0BPS"),
        (999_000_000_000.0, "999GBPS"),
        (999_000_000_001.0, ">999GBPS"),
        (1e300, ">999GBPS"),
    ],
)
def test_rate_label_has_explicit_unknown_and_overflow_states(bps, expected):
    assert dsn_render_labels.rate_label(bps) == expected


def test_spoken_rate_uses_gigabits_and_fails_to_an_inequality():
    assert dsn_audio_words.rate_words(1_000_000_000.0) == "about 1 gigabit per second"
    assert dsn_audio_words.rate_words(999_000_000_000.0) == \
        "about 999 gigabits per second"
    assert dsn_audio_words.rate_words(999_000_000_001.0) == \
        "more than 999 gigabits per second"
    assert dsn_audio_words.rate_words(1e300) == "more than 999 gigabits per second"


def test_power_metric_boundaries_do_not_clamp_to_false_exact_values():
    assert dsn_render_instrument.receive_power_label(-250.0) == "-250DBM"
    assert dsn_render_instrument.receive_power_label(-250.01) == "RANGE?"
    assert dsn_render_instrument.receive_power_label(0.0) == "RANGE?"
    assert dsn_render_instrument.receive_power_label(999.0) == "RANGE?"
    assert dsn_render_instrument.receive_power_label(float("inf")) == "POWER?"

    assert dsn_render_instrument.transmit_power_label(999.0) == "999KW"
    assert dsn_render_instrument.transmit_power_label(999.01) == ">999KW"
    assert dsn_render_instrument.transmit_power_label(0.999) == "999W"
    assert dsn_render_instrument.transmit_power_label(0.99901) == ">999W"
    assert dsn_render_instrument.transmit_power_label(-1.0) == "POWER?"


def test_instrument_pages_expose_extreme_power_as_range_or_overflow():
    extreme = _link(
        down_dbm=-1e300,
        up_kw=1e300,
        up_streams=(dsn_source.UpStream("X", 1e300, "data"),),
    )
    pages = dsn_render_instrument._instrument_metrics(extreme)
    assert ("RX", "RANGE?") in pages
    assert ("TX", ">999KW") in pages
    assert ("RX", "-999DBM") not in pages
    assert ("TX", "999KW") not in pages


def test_parser_rejects_positive_receive_power_as_missing_telemetry():
    class Signal:
        def get(self, key):
            return "999" if key == "power" else None

    assert dsn_source._signal_dbm(Signal()) is None


def test_multiple_receive_records_never_reuse_a_legacy_aggregate_rate():
    records = (
        dsn_source.DownStream("Ka", 500_000.0, -140.0),
        dsn_source.DownStream("S", 499_000.0, -141.0),
    )
    pages = dsn_render_instrument._instrument_metrics(_link(
        streams=2, down_streams=records, down_bps=999_000.0))
    assert ("KA/S", "RATE?") in pages
    assert ("999KBPS", "") not in pages


def test_active_uplink_without_published_power_is_unknown_not_none():
    feed = b"""<dsn><station name="gdscc"/><dish name="DSS25"
        azimuthAngle="1" elevationAngle="1"><upSignal active="true"
        spacecraft="JNO" spacecraftID="-61" band="X"/></dish></dsn>"""
    active_unknown = dsn_source.parse_feed(feed)[0]
    inactive = _link(up_active=False, up_kw=0.0, up_streams=())

    assert active_unknown.up_kw is None
    assert active_unknown.up_streams[0].kw is None
    assert ("TX", "POWER?") in dsn_render_instrument._instrument_metrics(active_unknown)
    assert ("TX", "NONE") not in dsn_render_instrument._instrument_metrics(active_unknown)
    assert ("TX", "NONE") in dsn_render_instrument._instrument_metrics(inactive)


def test_record_counts_use_an_explicit_overflow_instead_of_false_99():
    assert dsn_render_instrument.signal_count_label(99, "RX") == "99RX"
    assert dsn_render_instrument.signal_count_label(100, "RX") == ">99RX"
    assert dsn_render_instrument.signal_count_label(1_000_000, "SIG") == ">99SIG"


def test_extreme_narration_and_dry_run_remain_bounded_and_truthful():
    extreme = _link(
        down_bps=1e300,
        down_dbm=-1e300,
        up_kw=1e300,
        down_streams=(dsn_source.DownStream("X", 1e300, -1e300),),
        up_streams=(dsn_source.UpStream("X", 1e300, "data"),),
    )
    words = dsn_audio_words.spoken(
        extreme, {"vgr2": "Voyager 2"}, {"DSS43": "70M"}).lower()
    assert "more than 999 gigabits per second" in words
    assert "more than 999 kilowatts" in words
    assert "attowatt" not in words
    assert "100000000000000000000" not in words

    dry_run = " ".join(dsn_cli.describe_links(
        [extreme], {"vgr2": "Voyager 2"}, {"DSS43": "70M"},
        view="instrument"))
    assert ">999GBPS" in dry_run
    assert "100000000000000000000" not in dry_run

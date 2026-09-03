"""Regression gates for sourced narration and nonmetric visual motion."""

from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from apps import dsn


def _link(**changes) -> dsn.Link:
    base = dsn.Link(
        complex_name="Canberra",
        dish="DSS43",
        craft="VGR2",
        elevation=30.0,
        band="X",
        down_bps=160.0,
        up_active=True,
        range_km=dsn.C_KM_S * 60.0,
        naif=-32,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        azimuth=120.0,
        down_streams=(dsn.DownStream("X", 160.0, -140.0),),
        up_band="X",
        up_streams=(dsn.UpStream("X", 18.0, "data"),),
    )
    return replace(base, **changes)


def test_every_publishable_mission_blurb_is_stable_or_has_a_sourced_lease():
    assert set(dsn.MISSIONS) == set(dsn.MISSION_REVIEWS)
    assert dsn.STABLE_MISSION_BLURBS <= set(dsn.MISSIONS)
    for code, review in dsn.MISSION_REVIEWS.items():
        current = dsn.mission_blurb(code, dsn.MISSION_REVIEWED_ON)
        if review.stable:
            assert review.reviewed_on is not None, code
            assert review.review_by is None, code
            assert current == dsn.MISSIONS[code]
            assert dsn.mission_blurb(code, date(2100, 1, 1)) == current
        elif current:
            assert review.source_url.startswith("https://"), code
            assert review.reviewed_on is not None, code
            assert review.review_by is not None, code
            assert review.reviewed_on <= review.review_by, code
            expired = dsn.mission_blurb(code, date(2100, 1, 1))
            assert expired in {"", review.expired_fallback}, code
        else:
            # Blank-source mutable copy is data awaiting review, not narration.
            assert not review.source_url, code


def test_unsourced_status_is_silent_but_sourced_status_retains_a_lease():
    unreviewed = dsn.MISSION_REVIEWS["tgo"]
    assert not unreviewed.stable
    assert not unreviewed.source_url
    assert dsn.mission_blurb("tgo", dsn.MISSION_REVIEWED_ON) == ""

    voyager = dsn.MISSION_REVIEWS["vgr2"]
    assert not voyager.stable
    assert voyager.source_url == "https://science.nasa.gov/mission/voyager/"
    assert voyager.reviewed_on == dsn.MISSION_REVIEWED_ON
    assert voyager.review_by == dsn.MISSION_REVIEW_BY
    assert "interstellar space" in dsn.mission_blurb(
        "vgr2", dsn.MISSION_REVIEWED_ON)
    assert dsn.mission_blurb("vgr2", date(2100, 1, 1)) == ""

    mro = dsn.MISSION_REVIEWS["mro"]
    assert mro.source_url == "https://science.nasa.gov/mars/mars-relay-network/"
    assert "relays data home" in dsn.mission_blurb(
        "mro", dsn.MISSION_REVIEWED_ON)
    assert dsn.mission_blurb("mro", date(2100, 1, 1)) == ""


@pytest.mark.parametrize(
    ("code", "must_include", "must_exclude"),
    [
        ("mvn", "ended the mission in june 2026", "it studies"),
        ("m20", "landed in jezero crater in 2021", "drilling core"),
        ("msl", "landed in gale crater in 2012", "has been climbing"),
        ("wind", "launched in 1994 to measure", "thirty years"),
        ("ltb", "ended the mission in july 2025", "hunting"),
        ("escg", "designed to measure", "at mars"),
        ("plc", "ended operations in september 2025", "watching"),
        ("terr", "launched in 1999 to study", "has been photographing"),
        ("ulys", "steeply inclined orbit", "only spacecraft"),
        ("cusp", "designed to measure", "measuring the particles"),
        ("em3", "low-earth-orbit test flight", "south pole"),
        ("eus", "terminated", "meant to replace"),
        ("tm", "detected brief downlink signals", "never heard"),
        ("tmm", "detected brief downlink signals", "never heard"),
        ("cue3", "did not fly", "testing whether"),
        ("olin", "designed to fly", "satellites flying"),
    ],
)
def test_confirmed_stale_aliases_are_phase_truthful(
        code: str, must_include: str, must_exclude: str):
    words = dsn.MISSIONS[code].lower()
    assert must_include in words
    assert must_exclude not in words
    assert dsn.MISSION_REVIEWS[code].source_url.startswith("https://")


def test_generated_fleet_aliases_do_not_generalise_status():
    reviewed_codes = set()
    for code in ("go10", "go11", "go12", "go14", "go16", "go17"):
        assert "watching" not in dsn.MISSIONS[code].lower()
        reviewed_codes.add(code)
    for code in ("go13", "go15"):
        assert "retir" not in dsn.MISSIONS[code].lower()
        reviewed_codes.add(code)
    for code in ("no15", "no16", "no17", "no18"):
        assert "retired in august 2025" in dsn.MISSIONS[code].lower()
        reviewed_codes.add(code)
    for code in ("clu1", "clu2", "clu3", "clu4"):
        assert "ended the cluster mission" in dsn.MISSIONS[code].lower()
        reviewed_codes.add(code)
    for code in ("thb", "thc"):
        assert "moved into lunar orbit" in dsn.MISSIONS[code].lower()
        assert "renamed artemis" in dsn.MISSIONS[code].lower()
        reviewed_codes.add(code)

    assert "retirement" in dsn.MISSIONS["tdr1"].lower()
    assert "retirement" in dsn.MISSIONS["tdr4"].lower()
    assert "retir" not in dsn.MISSIONS["tdr3"].lower()
    assert "completed in 2022" in dsn.MISSIONS["em-1"].lower()
    assert "completed in april 2026" in dsn.MISSIONS["em-2"].lower()
    assert "low-earth-orbit" in dsn.MISSIONS["em-3"].lower()
    assert "flight designation" in dsn.MISSIONS["em-4"].lower()
    reviewed_codes.update(("tdr1", "tdr3", "tdr4", "em-1", "em-2",
                           "em-3", "em-4"))
    assert all(dsn.MISSION_REVIEWS[code].source_url.startswith("https://")
               for code in reviewed_codes)


def test_expired_artemis_status_falls_back_to_phase_neutral_copy():
    words = dsn.mission_blurb("em3", date(2100, 1, 1)).lower()
    assert words == "an artemis flight designation in nasa's lunar campaign"
    assert "low-earth-orbit" not in words


def test_k_and_ka_services_are_not_conflated():
    k_words = dsn.band_words("K").lower()
    ka_words = dsn.band_words("Ka").lower()
    assert "near-earth" in k_words
    assert "deep-space" in k_words and "ka band" in k_words
    assert "ka band" in ka_words
    assert "near-earth and deep-space" not in k_words


def test_receive_records_are_spoken_per_record_not_as_summed_throughput():
    records = (
        dsn.DownStream("X", 1_000.0, -140.0, "data"),
        dsn.DownStream("X", 2_000.0, -141.0, "data"),
    )
    # Deliberately poison the legacy scalar: narration must ignore it whenever
    # more than one raw record exists.
    link = _link(streams=2, down_streams=records, down_bps=999_000_000.0)
    words = dsn.spoken(
        link, {"vgr2": "Voyager 2"}, {"DSS43": "70M"}).lower()

    assert "2 active receive signal records" in words
    assert "about 1 kilobit per second" in words
    assert "about 2 kilobits per second" in words
    assert "receiver redundancy" in words
    assert "contact throughput" in words
    assert "999 megabits" not in words
    assert "about 3 kilobits" not in words
    assert "total data rate" not in words
    assert "separate streams" not in words


def test_many_receive_records_have_bounded_exact_nonaggregating_speech():
    records = tuple(
        dsn.DownStream("X", float(index + 1) * 1_000.0, -140.0, "data")
        for index in range(dsn.FEED_SIGNAL_RECORDS_PER_DISH_MAX))
    words = dsn.receive_records_words(records, len(records))

    assert f"{len(records)} active receive signal records" in words
    assert "not enumerated in speech" in words
    assert "not added into one contact throughput" in words
    assert len(words) < 300


def test_unknown_source_band_tokens_are_not_read_as_supported_services():
    hostile = "NOT-A-REAL-BAND-WITH-SOURCE-TEXT"
    records = (
        dsn.DownStream("X", 1_000.0, -140.0, "data"),
        dsn.DownStream(hostile, 2_000.0, -141.0, "data"),
    )
    words = dsn.spoken(
        _link(streams=2, down_streams=records, down_bps=None),
        {"vgr2": "Voyager 2"}, {"DSS43": "70M"}).lower()

    assert ("x and unknown bands" in words
            or "unknown and x bands" in words)
    assert hostile.lower() not in words


def test_unknown_dish_size_is_omitted_instead_of_invented():
    words = dsn.spoken(
        _link(dish="DSS99"), {"vgr2": "Voyager 2"}, {}).split(".", 1)[0]
    assert "dish number 99" in words
    assert "metre dish" not in words


def test_missing_range_draws_stationary_nonmetric_carriers():
    when = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)
    unknown, _, _ = dsn.render_frames(
        _link(craft="LUCY", range_km=None), when, {"lucy": "Lucy"})
    known, _, _ = dsn.render_frames(
        _link(craft="LUCY"), when, {"lucy": "Lucy"})

    def rf_rows(frame):
        return tuple(
            frame.getpixel((x, y))
            for y in (dsn.UP_Y, dsn.DOWN_Y)
            for x in range(dsn.TRACK0, dsn.TRACK1 + 1)
        )

    assert len({rf_rows(frame) for frame in unknown}) == 1
    assert len({rf_rows(frame) for frame in known}) > 1
    dry_run = " ".join(dsn.describe_links(
        [_link(craft="LUCY", range_km=None)], {"lucy": "Lucy"},
        {"DSS43": "70M"}, view="distance"))
    assert "crossing unknown" in dry_run
    assert "crosses in 8" not in dry_run


def test_controlled_attitude_spacecraft_do_not_receive_invented_glints():
    assert "lucy" not in dsn.CRAFT_GLINT
    assert "clipper" not in dsn.CRAFT_GLINT

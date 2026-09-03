"""The DSN source trust boundary stays pure and import-compatible."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

import apps.dsn as dsn
import apps.dsn_source as source


ROOT = Path(__file__).resolve().parents[1]


def test_entrypoint_reexports_the_source_contract() -> None:
    """Existing callers keep one API while ingestion has a distinct owner."""
    for name in (
        "DownStream",
        "UpStream",
        "Link",
        "SourceValidationError",
        "parse_feed",
        "parse_config",
        "feed_timestamp_ms",
        "source_timestamp_valid",
        "canonical_site_name",
        "band_key",
        "_signal_dbm",
    ):
        assert getattr(dsn, name) is getattr(source, name)

    for name in (
        "FEED_XML_MAX_BYTES",
        "CONFIG_XML_MAX_BYTES",
        "FEED_DISH_ELEMENTS_MAX",
        "FEED_SIGNAL_RECORDS_PER_DISH_MAX",
        "MAX_RANGE_KM",
        "NOT_SPACECRAFT",
    ):
        assert getattr(dsn, name) == getattr(source, name)


def test_source_parser_builds_the_domain_model_directly() -> None:
    feed = b"""
    <dsn>
      <timestamp>1780000000000</timestamp>
      <station name="gdscc" />
      <dish name="DSS14" elevationAngle="42" azimuthAngle="120">
        <target name="MRO" id="74" downlegRange="21000000" />
        <downSignal active="true" spacecraft="MRO" spacecraftID="-74"
                    band="X" dataRate="160" power="-130" />
      </dish>
    </dsn>
    """

    links = source.parse_feed(feed)

    assert len(links) == 1
    assert type(links[0]) is source.Link
    assert links[0].key == "DSS14/MRO"
    assert links[0].complex_name == "Goldstone"
    assert links[0].down_streams == (source.DownStream("X", 160.0, -130.0),)


def test_standalone_entrypoint_import_uses_the_same_source_objects() -> None:
    """``python apps/dsn.py`` follows the same seam as ``import apps.dsn``."""
    code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(ROOT / "apps")!r})
import dsn
import dsn_source
assert dsn.Link is dsn_source.Link
assert dsn.parse_feed is dsn_source.parse_feed
assert dsn.band_key is dsn_source.band_key
"""

    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

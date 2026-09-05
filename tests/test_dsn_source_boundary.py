"""DSN has one source-model owner, separate from launchers and runtime I/O."""

from __future__ import annotations

from pathlib import Path
import subprocess
import sys

from apps.dsn_app import feed, ranges, source
from apps.dsn_app.render import distance


ROOT = Path(__file__).resolve().parents[1]


def test_ingestion_ranges_and_rendering_share_the_source_contract() -> None:
    """Consumers must not create local imitations of the validated model."""
    assert feed._source is source
    assert ranges._source is source
    assert distance._source is source
    assert source.Link.__module__ == "apps.dsn_app.source"


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


def test_standalone_and_package_import_share_offline_renderers() -> None:
    """The stable renderer entry points must not pull in runtime I/O."""
    code = f"""
import sys
sys.path.insert(0, {str(ROOT)!r})
sys.path.insert(0, {str(ROOT / "apps")!r})
import dsn
from apps import dsn as packaged
from apps.dsn_app.render import examples
assert dsn.render_visual is packaged.render_visual is examples.render_visual
assert dsn.render_distance_visual is examples.render_distance_visual
assert dsn.render_instrument_visual is examples.render_instrument_visual
assert "apps.dsn_app.runtime" not in sys.modules
assert "apps.dsn_app.audio.narration" not in sys.modules
assert "apps.dsn_app.feed" not in sys.modules
"""

    subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

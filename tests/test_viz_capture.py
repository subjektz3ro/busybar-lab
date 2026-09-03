"""Framebuffer captures are honest, host-free, read-only evidence."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

import busybar_dev
from busybar_dev import screen
from busybar_viz import cli
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.artifacts import _validate_segment
from busybar_viz.capture import load_capture_segment
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    EvidenceLevel,
    RenderedSegment,
)

REPO = Path(__file__).resolve().parents[1]


class _FakeBar:
    def __init__(self):
        self.entered = False
        self.closed = False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, _exc_type, _exc, _traceback):
        self.closed = True


@pytest.fixture
def fake_device(monkeypatch):
    bar = _FakeBar()
    monkeypatch.setattr(busybar_dev, "connect", lambda: bar)
    monkeypatch.setattr(
        screen, "front_image",
        lambda _bb: Image.new("RGB", (72, 16), (200, 120, 0)),
    )
    monkeypatch.setattr(
        screen, "back_image",
        lambda _bb: Image.new("L", (160, 80), 40),
    )
    return bar


def test_capture_wraps_framebuffers_with_honest_provenance(fake_device):
    request, segment = load_capture_segment(("front", "back"))

    assert [track.id for track in segment.displays] == ["front", "back"]
    assert all(
        track.confidence is Confidence.FRAMEBUFFER_OBSERVED
        for track in segment.displays
    )
    assert segment.displays[1].frames[0].mode == "RGB"
    assert segment.evidence_level is EvidenceLevel.FRAMEBUFFER_CAPTURED
    assert request.parameters == {"displays": ["front", "back"]}
    assert any(".anim" in note for note in segment.notes)
    assert required_checks_pass(analyze(segment))
    assert fake_device.entered is True
    assert fake_device.closed is True


def test_capture_rejects_wrong_sizes_and_bad_display_lists(fake_device, monkeypatch):
    monkeypatch.setattr(
        screen, "front_image", lambda _bb: Image.new("RGB", (10, 10)),
    )
    with pytest.raises(ValueError, match="framebuffer"):
        load_capture_segment(("front",))
    with pytest.raises(ValueError, match="unique"):
        load_capture_segment(("front", "front"))
    with pytest.raises(ValueError, match="unique"):
        load_capture_segment(())


def test_automatic_framebuffer_level_requires_framebuffer_tracks():
    track = DisplayTrack(
        "front", (Image.new("RGB", (72, 16)),), 1, Confidence.SOURCE_EXACT,
    )
    segment = RenderedSegment(
        displays=(track,),
        evidence_level=EvidenceLevel.FRAMEBUFFER_CAPTURED,
        checks=(CheckSpec.create(
            "front-dimensions", "frame.dimensions",
            display="front", size=(72, 16),
        ),),
    )
    with pytest.raises(ValueError, match="framebuffer-observed"):
        _validate_segment(segment)

    reviewed = RenderedSegment(
        displays=(track,),
        evidence_level=EvidenceLevel.HARDWARE_OBSERVED,
        checks=segment.checks,
    )
    with pytest.raises(ValueError, match="may only claim"):
        _validate_segment(reviewed)


def test_capture_cli_publishes_deterministic_artifacts(
    fake_device, tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    argv = ("--data-dir", str(tmp_path / "viz"), "capture", "--json")

    assert cli.main(argv) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["evidence_level"] == "framebuffer-captured"
    assert result["passed"] is True
    assert Path(result["previews"]["front"]["gap_contact_sheet"]).is_file()

    assert cli.main(argv) == 0
    rerun = json.loads(capsys.readouterr().out)
    assert rerun["artifact_id"] == result["artifact_id"]

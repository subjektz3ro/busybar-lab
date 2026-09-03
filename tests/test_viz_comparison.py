"""Cross-artifact comparisons expose exact metrics and visible derived diffs."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image

from busybar_viz.artifacts import ArtifactStore
from busybar_viz.comparison import COMPARISON_SCHEMA, ComparisonStore
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    RenderedSegment,
    RenderRequest,
)

ROOT = Path(__file__).resolve().parents[1]


def _publish(root: Path, name: str, color: tuple[int, int, int]):
    frame = Image.new("RGB", (72, 16), (0, 0, 0))
    frame.putpixel((7, 4), color)
    return ArtifactStore(root, ROOT).publish(
        RenderRequest.from_values(f"test/{name}"),
        RenderedSegment(
            displays=(DisplayTrack(
                "front", (frame,), 1, Confidence.SOURCE_EXACT,
            ),),
            checks=(CheckSpec.create(
                "front-dimensions",
                "frame.dimensions",
                display="front",
                size=(72, 16),
            ),),
            source_paths=("tests/test_viz_comparison.py",),
        ),
    )


def test_comparison_records_exact_pixel_metrics_and_model_visible_heatmap(tmp_path):
    before = _publish(tmp_path, "before", (10, 10, 10))
    after = _publish(tmp_path, "after", (20, 40, 60))

    result = ComparisonStore(tmp_path).publish(before.path, after.path)

    assert result.summary["schema"] == COMPARISON_SCHEMA
    assert result.summary["changed"] is True
    frame = result.summary["displays"]["front"]["frames"][0]
    assert frame["changed_pixels"] == 1
    assert frame["changed_bbox"] == [7, 4, 8, 5]
    assert frame["max_channel_delta"] == 50
    sheet = Image.open(result.path / "front-diff-contact-sheet.png").convert("RGB")
    assert max(max(pixel) for pixel in sheet.get_flattened_data()) >= 200


def test_comparison_of_different_artifacts_can_have_identical_pixels(tmp_path):
    before = _publish(tmp_path, "one", (20, 20, 20))
    after = _publish(tmp_path, "two", (20, 20, 20))

    result = ComparisonStore(tmp_path).publish(before.path, after.path)

    assert before.artifact_id != after.artifact_id
    assert result.summary["changed"] is False
    assert result.summary["displays"]["front"]["state"] == "identical"


def test_comparison_refuses_a_tampered_authoritative_frame(tmp_path):
    before = _publish(tmp_path, "before", (10, 10, 10))
    after = _publish(tmp_path, "after", (20, 20, 20))
    raw = after.path / "frames/front/frame-000.rgb"
    raw.write_bytes(b"\0" * (72 * 16 * 3))

    with pytest.raises(ValueError, match="hash mismatch"):
        ComparisonStore(tmp_path).publish(before.path, after.path)


def test_comparison_refuses_tampered_manifest_metadata(tmp_path):
    before = _publish(tmp_path, "before", (10, 10, 10))
    after = _publish(tmp_path, "after", (20, 20, 20))
    manifest = after.path / "manifest.json"
    value = json.loads(manifest.read_text())
    value["passed"] = not value["passed"]
    manifest.write_text(json.dumps(value))

    with pytest.raises(ValueError, match="payload does not match"):
        ComparisonStore(tmp_path).publish(before.path, after.path)


def test_comparison_refuses_tampered_cached_output(tmp_path):
    before = _publish(tmp_path, "before", (10, 10, 10))
    after = _publish(tmp_path, "after", (20, 20, 20))
    store = ComparisonStore(tmp_path)
    first = store.publish(before.path, after.path)
    (first.path / "comparison.json").write_text("{}\n")

    with pytest.raises(ValueError, match="cached comparison content"):
        store.publish(before.path, after.path)

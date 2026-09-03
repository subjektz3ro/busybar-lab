"""Repository PNG and native animation inputs enter the same generic pipeline."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from PIL import Image

from busybar_dev.anim import (
    DEFAULT_DECODE_LIMITS,
    encode_anim,
)
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.assets import load_asset_segment
from busybar_viz.limits import MAX_FRAMES, LimitError
from busybar_viz.models import Confidence

REPO = Path(__file__).resolve().parents[1]


def test_front_png_loads_as_one_exact_renderer_track(tmp_path):
    asset = tmp_path / "fixtures" / "status.png"
    asset.parent.mkdir()
    source = Image.new("RGBA", (72, 16), (10, 20, 30, 128))
    source.save(asset)

    request, segment = load_asset_segment(
        asset, repo_root=tmp_path, display_id="front",
    )

    track = segment.displays[0]
    assert (track.id, track.size, len(track.frames), track.fps) == (
        "front", (72, 16), 1, 1,
    )
    assert track.confidence is Confidence.SOURCE_EXACT
    assert track.frames[0].mode == "RGB"
    assert request.parameters == {
        "path": "fixtures/status.png",
        "display": "front",
        "source_sha256": hashlib.sha256(asset.read_bytes()).hexdigest(),
    }
    assert required_checks_pass(analyze(segment))


def test_named_anim_section_preserves_decoded_pixels_timing_and_duplicates(tmp_path):
    red = Image.new("RGB", (160, 80), (200, 0, 0))
    blue = Image.new("RGB", (160, 80), (0, 0, 200))
    asset = tmp_path / "loop.anim"
    asset.write_bytes(encode_anim(
        [red, red.copy(), blue],
        fps=6,
        sections=[("middle", 1, 2)],
    ))

    request, segment = load_asset_segment(
        asset,
        repo_root=tmp_path,
        display_id="back",
        section="middle",
    )

    track = segment.displays[0]
    assert (track.id, track.size, track.fps) == ("back", (160, 80), 6)
    assert [frame.getpixel((0, 0)) for frame in track.frames] == [
        (200, 0, 0), (0, 0, 200),
    ]
    assert request.parameters["section"] == "middle"
    assert request.parameters["source_sha256"] == hashlib.sha256(
        asset.read_bytes()
    ).hexdigest()
    assert "busybar_dev/anim.py" in segment.source_paths
    assert required_checks_pass(analyze(segment))


def test_asset_loader_rejects_wrong_profiles_unknown_types_and_path_escape(tmp_path):
    wrong = tmp_path / "wrong.png"
    Image.new("RGB", (10, 10)).save(wrong)
    text = tmp_path / "readme.txt"
    text.write_text("not pixels")
    outside = tmp_path.parent / "outside.png"
    Image.new("RGB", (72, 16)).save(outside)

    with pytest.raises(ValueError, match="not the front display"):
        load_asset_segment(wrong, repo_root=tmp_path, display_id="front")
    with pytest.raises(ValueError, match="only .png or .anim"):
        load_asset_segment(text, repo_root=tmp_path, display_id="front")
    with pytest.raises(ValueError, match="inside the repository"):
        load_asset_segment(outside, repo_root=tmp_path, display_id="front")


def test_asset_loader_enforces_expanded_frame_and_timing_budgets(tmp_path):
    frame = Image.new("RGB", (72, 16), (1, 2, 3))
    too_many = tmp_path / "too-many.anim"
    too_many.write_bytes(encode_anim(
        [frame], fps=5, durations=[MAX_FRAMES + 1],
    ))
    too_fast = tmp_path / "too-fast.anim"
    too_fast.write_bytes(encode_anim([frame], fps=21))

    with pytest.raises(ValueError, match="expands to"):
        load_asset_segment(
            too_many, repo_root=tmp_path, display_id="front",
        )
    with pytest.raises(LimitError, match="fps"):
        load_asset_segment(
            too_fast, repo_root=tmp_path, display_id="front",
        )


def test_asset_loader_rejects_unknown_native_sections(tmp_path):
    asset = tmp_path / "loop.anim"
    asset.write_bytes(encode_anim([
        Image.new("RGB", (72, 16), (1, 2, 3)),
    ], fps=5))

    with pytest.raises(KeyError):
        load_asset_segment(
            asset,
            repo_root=tmp_path,
            display_id="front",
            section="missing",
        )


def test_asset_loader_rejects_oversized_anim_before_allocating_it(tmp_path):
    asset = tmp_path / "oversized.anim"
    with asset.open("wb") as handle:
        handle.truncate(DEFAULT_DECODE_LIMITS.max_source_bytes + 1)

    with pytest.raises(LimitError, match="decode budget"):
        load_asset_segment(asset, repo_root=tmp_path, display_id="front")


def test_asset_loader_rejects_oversized_png_before_decoding_it(tmp_path):
    asset = tmp_path / "oversized.png"
    with asset.open("wb") as handle:
        handle.truncate(DEFAULT_DECODE_LIMITS.max_source_bytes + 1)

    with pytest.raises(LimitError, match="decode budget"):
        load_asset_segment(asset, repo_root=tmp_path, display_id="front")


def test_asset_loader_rejects_wrong_png_dimensions_before_conversion(
    tmp_path, monkeypatch,
):
    asset = tmp_path / "wrong.png"
    Image.new("RGB", (10, 10), (1, 2, 3)).save(asset)

    def unexpected_convert(self, *args, **kwargs):
        raise AssertionError("wrong-size PNG should not be decoded to RGB")

    monkeypatch.setattr(Image.Image, "convert", unexpected_convert)
    with pytest.raises(ValueError, match="not the front display"):
        load_asset_segment(asset, repo_root=tmp_path, display_id="front")


def test_asset_loader_rejects_a_non_png_with_a_png_suffix(tmp_path):
    asset = tmp_path / "renamed.png"
    Image.new("RGB", (72, 16), (1, 2, 3)).save(asset, format="JPEG")

    with pytest.raises(ValueError, match="valid PNG"):
        load_asset_segment(asset, repo_root=tmp_path, display_id="front")

    corrupt = tmp_path / "corrupt.png"
    corrupt.write_bytes(b"not an image")
    with pytest.raises(ValueError, match="valid PNG"):
        load_asset_segment(corrupt, repo_root=tmp_path, display_id="front")


def test_asset_snapshot_identity_survives_source_replacement(tmp_path):
    from busybar_viz.artifacts import ArtifactStore

    asset = tmp_path / "status.png"
    Image.new("RGB", (72, 16), (200, 0, 0)).save(asset)
    loaded_bytes = asset.read_bytes()
    request, segment = load_asset_segment(
        asset, repo_root=tmp_path, display_id="front",
    )

    Image.new("RGB", (72, 16), (0, 0, 200)).save(asset)
    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(request, segment)

    expected = hashlib.sha256(loaded_bytes).hexdigest()
    assert request.parameters["source_sha256"] == expected
    assert artifact.manifest["scenario"]["parameters"]["source_sha256"] == expected
    assert artifact.manifest["displays"]["front"]["frame_hashes"][0] == hashlib.sha256(
        Image.new("RGB", (72, 16), (200, 0, 0)).tobytes()
    ).hexdigest()
    assert "status.png" not in artifact.manifest["source"]["files"]

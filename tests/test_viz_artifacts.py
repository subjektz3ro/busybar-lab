"""Evidence bundles preserve every authoritative, app-neutral audit input."""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import replace
from pathlib import Path

import pytest
from PIL import Image

from busybar_viz.artifacts import (
    ArtifactStore,
    _gif_bytes,
    _sample_indices,
    canonical_json,
    verify_artifact,
)
from busybar_viz.limits import LimitError
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    EvidenceLevel,
    InkReference,
    InputEvent,
    RegionSpec,
    RenderedSegment,
    RenderRequest,
    SignalEvent,
)


REPO = Path(__file__).resolve().parents[1]


def _solid(color, size=(72, 16)) -> Image.Image:
    return Image.new("RGB", size, color)


def _segment(*, include_back=False, baseline=(0, 0, 0)) -> RenderedSegment:
    front_frames = (_solid((0, 0, 0)), _solid((1, 2, 3)))
    tracks = [DisplayTrack(
        "front",
        front_frames,
        5,
        Confidence.SOURCE_EXACT,
        (_solid(baseline),),
    )]
    checks = [CheckSpec.create(
        "front-size", "frame.dimensions", size=(72, 16),
    )]
    if include_back:
        tracks.append(DisplayTrack(
            "back",
            (_solid((4, 5, 6), (160, 80)),),
            2,
            Confidence.EMULATED_CONFORMANT,
        ))
        checks.append(CheckSpec.create(
            "back-size", "frame.dimensions", display="back", size=(160, 80),
        ))
    return RenderedSegment(
        displays=tuple(tracks),
        signals=(SignalEvent(100_000, "top_led.pulse", "#010203FF"),),
        regions=(RegionSpec("corner", rect=(0, 0, 1, 1)),),
        checks=tuple(checks),
        notes=("Renderer pixels only; no physical observation.",),
    )


def _request() -> RenderRequest:
    return RenderRequest.from_values(
        "fixture/two-displays",
        {"variant": "calm"},
        (InputEvent(50_000, "button.press", "ok"),),
    )


def _gif_duration_ms(path: Path) -> int:
    total = 0
    with Image.open(path) as image:
        for index in range(image.n_frames):
            image.seek(index)
            total += int(image.info["duration"])
    return total


def test_dual_display_publication_preserves_frames_baselines_trace_and_previews(
    tmp_path,
):
    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(
        _request(), _segment(include_back=True),
    )

    assert artifact.passed
    assert artifact.manifest["scenario"]["inputs"] == [{
        "t_us": 50_000,
        "kind": "button.press",
        "control": "ok",
        "value": True,
    }]
    assert set(artifact.manifest["displays"]) == {"front", "back"}
    assert artifact.manifest["displays"]["front"]["frame_count"] == 2
    assert artifact.manifest["displays"]["back"]["width"] == 160
    assert artifact.manifest["displays"]["front"]["baseline_hashes"]

    source = _segment(include_back=True)
    for track in source.displays:
        for index, frame in enumerate(track.frames):
            raw = artifact.path / f"frames/{track.id}/frame-{index:03d}.rgb"
            assert raw.read_bytes() == frame.tobytes()
        for expected in (
            f"{track.id}.gif",
            f"{track.id}-gap.gif",
            f"{track.id}-contact-sheet.png",
            f"{track.id}-gap-contact-sheet.png",
            f"{track.id}-change-heatmap.png",
        ):
            assert (artifact.path / expected).is_file()

    baseline_path = artifact.path / "baselines/front/baseline-000.rgb"
    assert baseline_path.read_bytes() == source.displays[0].baselines[0].tobytes()
    assert artifact.manifest["files"][
        "baselines/front/baseline-000.rgb"
    ]["role"] == "authoritative-audit-baseline"

    trace = [json.loads(line) for line in (
        artifact.path / "trace.jsonl"
    ).read_text().splitlines()]
    assert [event["seq"] for event in trace] == list(range(1, len(trace) + 1))
    assert [event["t_us"] for event in trace] == sorted(
        event["t_us"] for event in trace
    )
    assert {
        event["body"]["display"]
        for event in trace if event["kind"] == "display.frame"
    } == {"front", "back"}

    for logical, metadata in artifact.manifest["files"].items():
        assert hashlib.sha256((artifact.path / logical).read_bytes()).hexdigest() == (
            metadata["sha256"]
        )
    assert {
        "trace.jsonl",
        "scenario.normalized.json",
        "audit.json",
        "signals.json",
        "summary.md",
    } <= artifact.manifest["files"].keys()


def test_publication_is_content_addressed_and_baselines_are_part_of_identity(
    tmp_path,
):
    store = ArtifactStore(tmp_path / "viz", REPO)
    first = store.publish(_request(), _segment())
    repeated = store.publish(_request(), _segment())
    changed_baseline = store.publish(
        _request(), _segment(baseline=(9, 8, 7)),
    )

    assert repeated.artifact_id == first.artifact_id
    assert repeated.path == first.path
    assert changed_baseline.artifact_id != first.artifact_id


def test_manifest_payload_and_complete_inventory_are_authenticated(tmp_path):
    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(
        _request(), _segment(include_back=True),
    )
    payload = dict(artifact.manifest)
    claimed = payload.pop("artifact_id")

    assert hashlib.sha256(canonical_json(payload)).hexdigest() == claimed
    verified = verify_artifact(
        artifact.path,
        full=True,
        expected_artifact_id=artifact.artifact_id,
    )
    assert verified.artifact_id == artifact.artifact_id
    assert verified.path == artifact.path.resolve()
    assert "manifest.json" not in artifact.manifest["files"]
    assert set(artifact.manifest["files"]) == {
        path.relative_to(artifact.path).as_posix()
        for path in artifact.path.rglob("*")
        if path.is_file() and path.name != "manifest.json"
    }


def test_manifest_metadata_tampering_is_rejected(tmp_path):
    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(
        _request(), _segment(),
    )
    manifest_path = artifact.path / "manifest.json"
    changed = json.loads(manifest_path.read_text())
    changed["passed"] = not changed["passed"]
    manifest_path.write_text(json.dumps(changed))

    with pytest.raises(ValueError, match="payload does not match"):
        verify_artifact(artifact.path)


def test_existing_artifact_is_fully_verified_before_reuse(tmp_path):
    store = ArtifactStore(tmp_path / "viz", REPO)
    artifact = store.publish(_request(), _segment())
    (artifact.path / "summary.md").write_text("tampered\n")

    with pytest.raises(ValueError, match="file hash mismatch"):
        verify_artifact(artifact.path)
    with pytest.raises(ValueError, match="file hash mismatch"):
        store.publish(_request(), _segment())


def test_full_verifier_rejects_unlisted_and_symlinked_files(tmp_path):
    first = ArtifactStore(tmp_path / "first", REPO).publish(_request(), _segment())
    (first.path / "unlisted.txt").write_text("not in the manifest")
    with pytest.raises(ValueError, match="inventory does not match"):
        verify_artifact(first.path)

    second = ArtifactStore(tmp_path / "second", REPO).publish(_request(), _segment())
    (second.path / "linked").symlink_to(second.path / "summary.md")
    with pytest.raises(ValueError, match="symlink"):
        verify_artifact(second.path)

    parent_alias = tmp_path / "artifact-parent-link"
    parent_alias.symlink_to(second.path.parent, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        verify_artifact(parent_alias / second.path.name)


def test_authoritative_frame_hashes_are_cross_checked_with_inventory(tmp_path):
    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(_request(), _segment())
    manifest_path = artifact.path / "manifest.json"
    changed = json.loads(manifest_path.read_text())
    changed["displays"]["front"]["frame_hashes"][0] = "0" * 64
    payload = dict(changed)
    payload.pop("artifact_id")
    changed["artifact_id"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    manifest_path.write_bytes(canonical_json(changed) + b"\n")

    with pytest.raises(ValueError, match="disagrees with display metadata"):
        verify_artifact(manifest_path)


def test_automatic_evidence_never_claims_generated_gap_files_were_reviewed(
    tmp_path,
):
    segment = replace(
        _segment(), evidence_level=EvidenceLevel.RENDERER_VERIFIED,
    )

    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(_request(), segment)

    assert artifact.manifest["evidence"] == {
        "automatic_level": "renderer-verified",
        "reviewed_level": None,
        "available_previews": ["native-raster", "led-gap-simulation"],
        "notice": (
            "Generating a gap preview does not mean it was inspected. "
            "Review evidence belongs in the session journal."
        ),
    }
    summary = (artifact.path / "summary.md").read_text()
    assert "Automatic evidence level: `renderer-verified`" in summary
    assert "Gap preview generated: yes; inspected: not recorded" in summary


def test_failed_audits_remove_automatic_renderer_evidence(tmp_path):
    segment = replace(
        _segment(),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        checks=(CheckSpec.create(
            "must-move", "animation.unique_frames", minimum=3,
        ),),
    )

    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(_request(), segment)

    assert not artifact.passed
    assert artifact.manifest["evidence"]["automatic_level"] is None
    assert "Automatic evidence level: none" in (
        artifact.path / "summary.md"
    ).read_text()


def test_named_display_tuple_order_does_not_change_the_artifact(tmp_path):
    store = ArtifactStore(tmp_path / "viz", REPO)
    normal = _segment(include_back=True)
    reversed_tracks = replace(normal, displays=tuple(reversed(normal.displays)))

    first = store.publish(_request(), normal)
    reordered = store.publish(_request(), reversed_tracks)

    assert reordered.artifact_id == first.artifact_id
    assert reordered.path == first.path


def test_sampled_gap_animation_keeps_the_source_loop_duration():
    frames = []
    for index in range(80):
        frame = _solid((0, 0, 0))
        frame.putpixel((index % frame.width, index % frame.height), (255, 80, 20))
        frames.append(frame)
    indices = _sample_indices(len(frames), 60)
    sampled = tuple(frames[index] for index in indices)

    blob = _gif_bytes(
        sampled,
        10,
        gaps=True,
        display_id="front",
        source_indices=indices,
        source_frame_count=len(frames),
    )

    total = 0
    with Image.open(io.BytesIO(blob)) as image:
        assert image.n_frames == len(sampled)
        for index in range(image.n_frames):
            image.seek(index)
            total += int(image.info["duration"])
    assert total == 8_000


def test_failed_required_check_is_published_as_reviewable_failed_evidence(tmp_path):
    segment = replace(
        _segment(),
        checks=(CheckSpec.create(
            "must-move", "animation.unique_frames", minimum=3,
        ),),
    )

    artifact = ArtifactStore(tmp_path / "viz", REPO).publish(_request(), segment)

    assert not artifact.passed
    assert artifact.manifest["passed"] is False
    assert artifact.manifest["checks"][0]["status"] == "fail"
    assert "Result: **FAIL**" in (artifact.path / "summary.md").read_text()


@pytest.mark.parametrize(
    ("segment", "message"),
    (
        (RenderedSegment(displays=()), "no display tracks"),
        (RenderedSegment(displays=(
            DisplayTrack("side", (_solid((0, 0, 0)),), 5, Confidence.SOURCE_EXACT),
        )), "unknown BUSY Bar display"),
        (RenderedSegment(displays=(
            DisplayTrack("front", (_solid((0, 0, 0), (71, 16)),), 5,
                         Confidence.SOURCE_EXACT),
        )), "front frames"),
        (RenderedSegment(displays=(
            DisplayTrack("front", (_solid((0, 0, 0)),), 5,
                         Confidence.SOURCE_EXACT),
            DisplayTrack("front", (_solid((0, 0, 0)),), 5,
                         Confidence.SOURCE_EXACT),
        )), "ids must be unique"),
        (replace(_segment(), regions=(
            RegionSpec("outside", points=((72, 0),)),
        )), "out-of-bounds"),
        (replace(_segment(), signals=(
            SignalEvent(10_000_000, "late", True),
        )), "signal timestamps"),
        (replace(_segment(), signals=(
            SignalEvent(True, "pulse", True),
        )), "integer microseconds"),
        (replace(_segment(), signals=(
            SignalEvent(0, "", True),
        )), "signal kinds"),
        (replace(_segment(), signals=(
            SignalEvent(0, "metric", float("nan")),
        )), "finite JSON"),
        (replace(_segment(), evidence_level="renderer-verified"),
         "EvidenceLevel"),
    ),
)
def test_invalid_adapter_output_is_rejected_before_artifact_writes(
    tmp_path, segment, message,
):
    store = ArtifactStore(tmp_path / "viz", REPO)

    with pytest.raises((ValueError, LimitError), match=message):
        store.publish(_request(), segment)
    assert not store.artifacts_dir.exists()


def test_audit_contract_rejects_unsafe_or_nonblocking_required_checks(tmp_path):
    with pytest.raises(ValueError, match="safe 1-64"):
        CheckSpec.create("bad/check", "frame.dimensions")
    with pytest.raises(ValueError, match="error, warning, or info"):
        CheckSpec.create("severity", "frame.dimensions", severity="errror")

    store = ArtifactStore(tmp_path / "viz", REPO)
    with pytest.raises(ValueError, match="no automated checks"):
        store.publish(_request(), replace(_segment(), checks=()))
    with pytest.raises(ValueError, match="at least one error"):
        store.publish(_request(), replace(
            _segment(),
            checks=(CheckSpec.create(
                "metrics", "frame.summary_metrics", severity="info",
            ),),
        ))
    duplicate = CheckSpec.create("same", "frame.dimensions", size=(72, 16))
    with pytest.raises(ValueError, match="duplicate automated check"):
        store.publish(_request(), replace(_segment(), checks=(duplicate, duplicate)))


def test_evidence_and_track_confidence_fail_closed(tmp_path):
    store = ArtifactStore(tmp_path / "viz", REPO)
    with pytest.raises(ValueError, match="only claim renderer-verified"):
        store.publish(_request(), replace(
            _segment(), evidence_level=EvidenceLevel.HARDWARE_OBSERVED,
        ))
    bad_track = replace(_segment().displays[0], confidence="source_exact")
    with pytest.raises(ValueError, match="known confidence"):
        store.publish(_request(), replace(_segment(), displays=(bad_track,)))
    approximate = replace(
        _segment().displays[0], confidence=Confidence.APPROXIMATE,
    )
    with pytest.raises(ValueError, match="source-exact"):
        store.publish(_request(), replace(
            _segment(),
            displays=(approximate,),
            evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        ))


def test_library_callers_receive_the_same_request_validation_as_the_cli(tmp_path):
    store = ArtifactStore(tmp_path / "viz", REPO)

    with pytest.raises(ValueError, match="finite JSON"):
        RenderRequest.from_values("fixture", {"bad": float("nan")})
    with pytest.raises(LimitError, match="ordered"):
        store.publish(
            RenderRequest.from_values("fixture", inputs=(
                InputEvent(2, "button.press", "ok"),
                InputEvent(1, "button.press", "ok"),
            )),
            _segment(),
        )


def test_source_paths_cannot_escape_the_checkout(tmp_path):
    segment = replace(_segment(), source_paths=("../private.txt",))

    with pytest.raises(ValueError, match="escapes the repository"):
        ArtifactStore(tmp_path / "viz", REPO).publish(_request(), segment)


def _ink_segment(reference: InkReference) -> RenderedSegment:
    frames = []
    for _index in range(2):
        frame = _solid((0, 0, 0))
        frame.putpixel((2, 3), (240, 200, 40))
        frames.append(frame)
    return RenderedSegment(
        displays=(DisplayTrack(
            "front", tuple(frames), 5, Confidence.SOURCE_EXACT,
        ),),
        ink_references=(reference,),
        checks=(CheckSpec.create(
            "full-label", "text.full_ink_preserved", reference=reference.id,
        ),),
    )


def test_full_ink_references_are_identity_bound_and_published_for_review(tmp_path):
    store = ArtifactStore(tmp_path / "viz", REPO)
    reference = InkReference(
        "status", "STATUS", "front", ((2, 3, (240, 200, 40)),),
    )

    artifact = store.publish(_request(), _ink_segment(reference))
    changed = store.publish(_request(), _ink_segment(replace(
        reference, label="STATUS LABEL",
    )))

    assert artifact.passed
    assert artifact.artifact_id != changed.artifact_id
    assert artifact.manifest["ink_references"][0]["id"] == "status"
    serialized = artifact.path / "references/status.json"
    preview = artifact.path / "references/status.png"
    assert json.loads(serialized.read_text())["pixels"] == [
        [2, 3, [240, 200, 40]],
    ]
    assert preview.is_file()
    assert artifact.manifest["files"]["references/status.json"]["role"] == (
        "authoritative-full-ink-reference"
    )


@pytest.mark.parametrize(
    "reference",
    (
        InkReference(
            "outside", "TOO LONG", "front", ((72, 3, (240, 200, 40)),),
        ),
        InkReference(
            "wrong-frame", "STATUS", "front",
            ((2, 3, (240, 200, 40)),), frame_indices=(5,),
        ),
    ),
)
def test_failed_fit_references_publish_failed_evidence_instead_of_disappearing(
    tmp_path, reference,
):
    artifact = ArtifactStore(tmp_path / reference.id, REPO).publish(
        _request(), _ink_segment(reference),
    )

    assert not artifact.passed
    check = artifact.manifest["checks"][0]
    assert check["status"] == "fail"
    assert (
        check["observed"]["outside_sample_count"]
        or check["observed"]["invalid_frame_indices"]
    )


@pytest.mark.parametrize(
    ("reference", "message"),
    (
        (InkReference("empty", "EMPTY", "front", ()), "has no samples"),
        (InkReference("bad/id", "BAD", "front", ((0, 0, (1, 2, 3)),)),
         "safe 1-64"),
        (InkReference("label", "", "front", ((0, 0, (1, 2, 3)),)),
         "labels"),
        (InkReference("side", "SIDE", "side", ((0, 0, (1, 2, 3)),)),
         "missing display"),
        (InkReference(
            "repeat", "REPEAT", "front",
            ((0, 0, (1, 2, 3)), (0, 0, (1, 2, 3))),
        ), "repeats a coordinate"),
        (InkReference(
            "color", "COLOR", "front",
            ((0, 0, (1, 2, 256)),),
        ), "RGB byte triples"),
        (InkReference(
            "index", "INDEX", "front",
            ((0, 0, (1, 2, 3)),), frame_indices=(True,),
        ), "unique integers"),
    ),
)
def test_malformed_ink_references_are_rejected_before_publication(
    tmp_path, reference, message,
):
    with pytest.raises(ValueError, match=message):
        ArtifactStore(tmp_path / "viz", REPO).publish(
            _request(), _ink_segment(reference),
        )


def test_duplicate_ink_reference_ids_are_rejected(tmp_path):
    reference = InkReference(
        "status", "STATUS", "front", ((2, 3, (240, 200, 40)),),
    )
    segment = replace(
        _ink_segment(reference), ink_references=(reference, reference),
    )

    with pytest.raises(ValueError, match="duplicate ink reference"):
        ArtifactStore(tmp_path / "viz", REPO).publish(_request(), segment)

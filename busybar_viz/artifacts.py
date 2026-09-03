"""Atomic, content-addressed evidence bundles for one or both displays."""

from __future__ import annotations

import errno
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

from .analysis import analyze, required_checks_pass
from .limits import (
    MAX_ARTIFACT_BYTES,
    LimitError,
    validate_inputs,
    validate_parameters,
    validate_signals,
    validate_timing,
)
from .models import (
    EVIDENCE_SCHEMA,
    TRACE_SCHEMA,
    Confidence,
    EvidenceLevel,
    RenderedSegment,
    RenderRequest,
)
from .panel import change_heatmap, contact_sheet, panelise, upscale
from .profiles import profile_for

_CORE_SOURCE_PATHS = (
    "busybar_viz/analysis.py",
    "busybar_viz/artifacts.py",
    "busybar_viz/models.py",
    "busybar_viz/offline.py",
    "busybar_viz/panel.py",
    "busybar_viz/profiles.py",
)
_ARTIFACT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")
_SAFE_CHECK_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_CHECK_SEVERITIES = frozenset({"error", "warning", "info"})


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True,
        allow_nan=False,
    ).encode("ascii")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    """Hash a source file without allocating it as one unbounded bytes object."""

    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, value: object) -> None:
    path.write_bytes(canonical_json(value) + b"\n")


def _png_bytes(frame: Image.Image) -> bytes:
    buffer = io.BytesIO()
    frame.save(buffer, format="PNG", optimize=False)
    return buffer.getvalue()


def _gif_bytes(
    frames: tuple[Image.Image, ...],
    fps: int,
    *,
    gaps: bool,
    display_id: str,
    source_indices: tuple[int, ...] | None = None,
    source_frame_count: int | None = None,
) -> bytes:
    # The back-panel gap view is 160x80 packages.  A half-scale package model
    # preserves the measured 10:8 light/gap ratio without creating a 4K frame.
    if gaps:
        if display_id == "front":
            rendered = tuple(panelise(frame) for frame in frames)
        else:
            rendered = tuple(
                panelise(frame, led_size=5, gap_size=4) for frame in frames
            )
    else:
        scale = 8 if display_id == "front" else 4
        rendered = tuple(upscale(frame, scale=scale) for frame in frames)
    buffer = io.BytesIO()
    duration: int | list[int] = max(1, round(1000 / fps))
    if source_indices is not None:
        if len(source_indices) != len(rendered) or source_frame_count is None:
            raise ValueError("sampled GIF timing metadata is inconsistent")
        boundaries = (*source_indices[1:], source_frame_count)
        duration = [
            max(1, round((right - left) * 1000 / fps))
            for left, right in zip(source_indices, boundaries)
        ]
    rendered[0].save(
        buffer,
        format="GIF",
        save_all=True,
        append_images=list(rendered[1:]),
        duration=duration,
        loop=0,
        optimize=False,
        disposal=2,
    )
    return buffer.getvalue()


def _sample_indices(count: int, maximum: int) -> tuple[int, ...]:
    if count <= maximum:
        return tuple(range(count))
    return tuple(sorted({round(index * (count - 1) / (maximum - 1))
                         for index in range(maximum)}))


def _sample_frames(
    frames: tuple[Image.Image, ...], maximum: int,
) -> tuple[Image.Image, ...]:
    return tuple(frames[index] for index in _sample_indices(len(frames), maximum))


def _source_fingerprint(repo_root: Path, paths: Iterable[str]) -> dict[str, Any]:
    files: dict[str, str] = {}
    for logical in sorted(set((*_CORE_SOURCE_PATHS, *paths))):
        candidate = (repo_root / logical).resolve()
        try:
            candidate.relative_to(repo_root.resolve())
        except ValueError as exc:
            raise ValueError(f"source path escapes the repository: {logical}") from exc
        if not candidate.is_file():
            raise ValueError(f"source path does not exist: {logical}")
        files[logical] = _sha256_file(candidate)
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
        commit = completed.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        commit = None
    return {"git_commit": commit, "files": files}


def _trace(
    segment: RenderedSegment,
    results,
    frame_hashes: dict[str, list[str]],
) -> list[dict[str, Any]]:
    tracks = tuple(sorted(segment.displays, key=lambda track: track.id))
    pending: list[tuple[int, int, str, str, dict[str, Any]]] = []
    order = 0

    def queue(t_us: int, kind: str, actor: str, body: dict[str, Any]) -> None:
        nonlocal order
        order += 1
        pending.append((t_us, order, kind, actor, body))

    queue(0, "run.start", "runner", {
        "displays": [track.id for track in tracks],
    })
    for track in tracks:
        for index, digest in enumerate(frame_hashes[track.id]):
            queue(round(index * 1_000_000 / track.fps), "display.frame", "app", {
                "display": track.id,
                "frame_index": index,
                "rgb_sha256": digest,
            })
    for signal in segment.signals:
        queue(signal.t_us, signal.kind, "app", {
            "value": signal.value,
            "confidence": signal.confidence.value,
        })
    for result in results:
        queue(segment.duration_us, "assert.result", "audit", result.as_dict())
    queue(segment.duration_us, "run.end", "runner", {
        "passed": required_checks_pass(results),
    })
    events: list[dict[str, Any]] = []
    for seq, (t_us, _order, kind, actor, body) in enumerate(
        sorted(pending, key=lambda item: (item[0], item[1])), start=1,
    ):
        events.append({
            "schema": TRACE_SCHEMA,
            "seq": seq,
            "t_us": t_us,
            "kind": kind,
            "actor": actor,
            "body": body,
        })
    return events


def _validate_segment(segment: RenderedSegment) -> None:
    if (
        segment.evidence_level is not None
        and not isinstance(segment.evidence_level, EvidenceLevel)
    ):
        raise ValueError("automatic evidence level must use EvidenceLevel or None")
    if segment.evidence_level not in {
        None,
        EvidenceLevel.RENDERER_VERIFIED,
        EvidenceLevel.FRAMEBUFFER_CAPTURED,
    }:
        # gap-previewed and hardware-observed are reviewed assertions made by
        # a person or agent in the session journal; a segment can only claim
        # what the tool itself performed.
        raise ValueError(
            "automatic EvidenceLevel may only claim renderer-verified "
            "or framebuffer-captured"
        )
    if not segment.displays:
        raise ValueError("adapter returned no display tracks")
    ids = [track.id for track in segment.displays]
    if len(ids) != len(set(ids)):
        raise ValueError("display track ids must be unique")
    for track in segment.displays:
        if not isinstance(track.confidence, Confidence):
            raise ValueError("display confidence must use a known confidence level")
        validate_timing(frame_count=len(track.frames), fps=track.fps)
        profile = profile_for(track.id)
        if not track.frames:
            raise ValueError(f"display track {track.id!r} has no frames")
        if any(frame.mode != "RGB" or frame.size != profile.size
               for frame in track.frames):
            raise ValueError(
                f"{track.id} frames must all be RGB {profile.width}x{profile.height}"
            )
        if track.baselines:
            if len(track.baselines) not in {1, len(track.frames)}:
                raise ValueError(
                    f"{track.id} baselines must contain one or one per frame"
                )
            if any(frame.mode != "RGB" or frame.size != profile.size
                   for frame in track.baselines):
                raise ValueError(f"{track.id} baselines do not match its profile")
    if (
        segment.evidence_level is EvidenceLevel.RENDERER_VERIFIED
        and any(track.confidence is not Confidence.SOURCE_EXACT
                for track in segment.displays)
    ):
        raise ValueError(
            "renderer-verified evidence requires source-exact display tracks"
        )
    if (
        segment.evidence_level is EvidenceLevel.FRAMEBUFFER_CAPTURED
        and any(track.confidence is not Confidence.FRAMEBUFFER_OBSERVED
                for track in segment.displays)
    ):
        raise ValueError(
            "framebuffer-captured evidence requires framebuffer-observed tracks"
        )
    if not segment.checks:
        raise ValueError("adapter returned no automated checks")
    seen_checks: set[str] = set()
    for check in segment.checks:
        if not isinstance(check.id, str) or not _SAFE_CHECK_ID_RE.fullmatch(check.id):
            raise ValueError("check ids must be safe 1-64 character names")
        if check.id in seen_checks:
            raise ValueError(f"duplicate automated check: {check.id}")
        seen_checks.add(check.id)
        if check.severity not in _CHECK_SEVERITIES:
            raise ValueError("check severity must be error, warning, or info")
    if not any(check.severity == "error" for check in segment.checks):
        raise ValueError("adapter must declare at least one error-severity check")
    seen_regions: set[str] = set()
    for region in segment.regions:
        if region.id in seen_regions:
            raise ValueError(f"duplicate semantic region: {region.id}")
        seen_regions.add(region.id)
        region_track = segment.display(region.display)
        if region_track is None:
            raise ValueError(
                f"region {region.id!r} references missing display {region.display!r}"
            )
        width, height = region_track.size
        if not region.points and region.rect is None:
            raise ValueError(f"region {region.id!r} is empty")
        if any(not (0 <= x < width and 0 <= y < height) for x, y in region.points):
            raise ValueError(f"region {region.id!r} contains out-of-bounds points")
        if region.rect is not None:
            x0, y0, x1, y1 = region.rect
            if not (0 <= x0 < x1 <= width and 0 <= y0 < y1 <= height):
                raise ValueError(f"region {region.id!r} has an invalid rectangle")
    seen_references: set[str] = set()
    for reference in segment.ink_references:
        if reference.id in seen_references:
            raise ValueError(f"duplicate ink reference: {reference.id}")
        seen_references.add(reference.id)
        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", reference.id):
            raise ValueError("ink reference ids must be safe 1-64 character names")
        if (not isinstance(reference.label, str) or not reference.label
                or len(reference.label) > 256):
            raise ValueError("ink reference labels must be 1-256 character strings")
        if segment.display(reference.display) is None:
            raise ValueError(
                f"ink reference {reference.id!r} names a missing display"
            )
        if not reference.pixels:
            raise ValueError(f"ink reference {reference.id!r} has no samples")
        coords: set[tuple[int, int]] = set()
        for x, y, color in reference.pixels:
            if (isinstance(x, bool) or not isinstance(x, int)
                    or isinstance(y, bool) or not isinstance(y, int)):
                raise ValueError("ink reference coordinates must be integers")
            if (x, y) in coords:
                raise ValueError(
                    f"ink reference {reference.id!r} repeats a coordinate"
                )
            coords.add((x, y))
            if (not isinstance(color, tuple) or len(color) != 3 or any(
                isinstance(channel, bool) or not isinstance(channel, int)
                or not 0 <= channel <= 255 for channel in color
            )):
                raise ValueError("ink reference colors must be RGB byte triples")
        if (any(isinstance(index, bool) or not isinstance(index, int)
                for index in reference.frame_indices)
                or len(reference.frame_indices) != len(set(reference.frame_indices))):
            raise ValueError(
                "ink reference frame indices must be unique integers"
            )
    validate_signals(segment.signals, duration_us=segment.duration_us)


@dataclass(frozen=True, slots=True)
class PublishedArtifact:
    artifact_id: str
    path: Path
    passed: bool
    manifest: dict[str, Any]


@dataclass(frozen=True, slots=True)
class VerifiedArtifact:
    """One manifest whose claimed content identity has been authenticated."""

    artifact_id: str
    path: Path
    passed: bool
    manifest: dict[str, Any]


def _artifact_id_for_payload(payload: dict[str, Any]) -> str:
    return sha256_bytes(canonical_json(payload))


def _safe_inventory_path(logical: object) -> str:
    if not isinstance(logical, str):
        raise ValueError("artifact inventory paths must be strings")
    path = Path(logical)
    if (
        not logical
        or path.is_absolute()
        or "\\" in logical
        or "\0" in logical
        or any(part in {"", ".", ".."} for part in path.parts)
        or path.as_posix() != logical
    ):
        raise ValueError(f"unsafe artifact inventory path: {logical!r}")
    return logical


def _reject_symlinked_path(path: Path) -> None:
    """Reject symlinks in a supplied artifact path without resolving them."""

    absolute = path.absolute()
    for component in (absolute, *absolute.parents):
        if component.is_symlink():
            raise ValueError("artifact paths may not traverse symlinks")


def _validate_manifest_semantics(manifest: dict[str, Any]) -> None:
    passed = manifest.get("passed")
    if not isinstance(passed, bool):
        raise ValueError("artifact manifest has an invalid pass result")

    checks = manifest.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("artifact manifest has no automated checks")
    seen: set[str] = set()
    required_statuses: list[str] = []
    for check in checks:
        if not isinstance(check, dict):
            raise ValueError("artifact manifest has invalid check metadata")
        check_id = check.get("id")
        if not isinstance(check_id, str) or not _SAFE_CHECK_ID_RE.fullmatch(check_id):
            raise ValueError("artifact manifest has an unsafe check id")
        if check_id in seen:
            raise ValueError(f"artifact manifest repeats check {check_id!r}")
        seen.add(check_id)
        severity = check.get("severity")
        if severity not in _CHECK_SEVERITIES:
            raise ValueError("artifact manifest has an invalid check severity")
        status = check.get("status")
        if status not in {"pass", "fail", "unknown", "skipped"}:
            raise ValueError("artifact manifest has an invalid check status")
        if severity == "error":
            required_statuses.append(status)
    if not required_statuses:
        raise ValueError("artifact manifest has no error-severity check")
    if passed != all(status == "pass" for status in required_statuses):
        raise ValueError("artifact pass result disagrees with its required checks")

    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict):
        raise ValueError("artifact manifest has invalid evidence metadata")
    automatic = evidence.get("automatic_level")
    if automatic not in {
        None,
        EvidenceLevel.RENDERER_VERIFIED.value,
        EvidenceLevel.FRAMEBUFFER_CAPTURED.value,
    }:
        raise ValueError("artifact has an invalid automatic evidence level")
    if evidence.get("reviewed_level") is not None:
        raise ValueError("immutable artifacts may not contain reviewed evidence")
    if automatic is not None and not passed:
        raise ValueError("failed artifacts may not claim automatic evidence")


def verify_artifact(
    path: Path,
    *,
    full: bool = True,
    expected_artifact_id: str | None = None,
) -> VerifiedArtifact:
    """Authenticate a busybar-viz artifact and optionally every bundled file.

    Manifest identity is always checked.  ``full=True`` additionally rejects
    missing, unlisted, non-regular, symlinked, oversized, or hash-mismatched
    files and cross-checks authoritative RGB frame metadata.
    """

    candidate = path.expanduser()
    _reject_symlinked_path(candidate)
    manifest_path = candidate / "manifest.json" if candidate.is_dir() else candidate
    _reject_symlinked_path(manifest_path)
    if not manifest_path.is_file():
        raise ValueError("artifact manifest does not exist")
    if manifest_path.stat().st_size > MAX_ARTIFACT_BYTES:
        raise LimitError("artifact manifest exceeds the publication budget")
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("artifact manifest is not readable JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("schema") != EVIDENCE_SCHEMA:
        raise ValueError("artifact is not busybar-viz evidence")
    artifact_id = manifest.get("artifact_id")
    if not isinstance(artifact_id, str) or not _ARTIFACT_ID_RE.fullmatch(artifact_id):
        raise ValueError("artifact manifest has an invalid identity")
    if expected_artifact_id is not None and artifact_id != expected_artifact_id:
        raise ValueError("artifact identity does not match the expected digest")
    payload = dict(manifest)
    payload.pop("artifact_id")
    if _artifact_id_for_payload(payload) != artifact_id:
        raise ValueError("artifact manifest payload does not match its identity")
    _validate_manifest_semantics(manifest)

    directory = manifest_path.parent
    if full:
        inventory = manifest.get("files")
        if not isinstance(inventory, dict):
            raise ValueError("artifact manifest has an invalid file inventory")
        inventory_paths: set[str] = set()
        for raw_logical, metadata in inventory.items():
            logical = _safe_inventory_path(raw_logical)
            if logical == "manifest.json":
                raise ValueError("manifest.json may not inventory itself")
            if logical in inventory_paths:
                raise ValueError(f"artifact inventory repeats {logical!r}")
            inventory_paths.add(logical)
            if not isinstance(metadata, dict) or set(metadata) != {"sha256", "role"}:
                raise ValueError("artifact file has invalid inventory metadata")
            digest = metadata.get("sha256")
            role = metadata.get("role")
            if not isinstance(digest, str) or not _ARTIFACT_ID_RE.fullmatch(digest):
                raise ValueError("artifact inventory has an invalid file digest")
            if not isinstance(role, str) or not role:
                raise ValueError("artifact inventory has an invalid file role")

        actual_paths: set[str] = set()
        actual_files: dict[str, Path] = {}
        total_size = manifest_path.stat().st_size
        for entry in directory.rglob("*"):
            if entry.is_symlink():
                raise ValueError("artifact bundles may not contain symlinks")
            if entry.is_dir():
                continue
            if not entry.is_file():
                raise ValueError("artifact bundles may contain only regular files")
            logical = entry.relative_to(directory).as_posix()
            if logical == "manifest.json":
                continue
            actual_paths.add(logical)
            actual_files[logical] = entry
            total_size += entry.stat().st_size
        if actual_paths != inventory_paths:
            missing = sorted(inventory_paths - actual_paths)
            unlisted = sorted(actual_paths - inventory_paths)
            raise ValueError(
                "artifact inventory does not match its files"
                f" (missing={missing}, unlisted={unlisted})"
            )
        if total_size > MAX_ARTIFACT_BYTES:
            raise LimitError("artifact exceeds the 64 MiB publication budget")

        file_hashes: dict[str, str] = {}
        for logical, source in actual_files.items():
            digest = hashlib.sha256()
            with source.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    digest.update(chunk)
            actual = digest.hexdigest()
            expected = inventory[logical]["sha256"]
            if actual != expected:
                raise ValueError(f"artifact file hash mismatch: {logical}")
            file_hashes[logical] = actual

        displays = manifest.get("displays")
        if not isinstance(displays, dict) or not 1 <= len(displays) <= 2:
            raise ValueError("artifact manifest has invalid display metadata")
        expected_authoritative: set[str] = set()
        for display_id, metadata in displays.items():
            if not isinstance(metadata, dict):
                raise ValueError("artifact display metadata must be an object")
            profile = profile_for(display_id)
            if (metadata.get("width"), metadata.get("height")) != profile.size:
                raise ValueError("artifact display dimensions do not match its profile")
            if metadata.get("pixel_format") != profile.pixel_format:
                raise ValueError(
                    "artifact display pixel format does not match its profile"
                )
            frame_count = metadata.get("frame_count")
            fps = metadata.get("fps")
            if (
                isinstance(frame_count, bool)
                or not isinstance(frame_count, int)
                or isinstance(fps, bool)
                or not isinstance(fps, int)
            ):
                raise LimitError("frame count and fps must be integers")
            validate_timing(frame_count=frame_count, fps=fps)
            if metadata.get("duration_us") != round(frame_count * 1_000_000 / fps):
                raise ValueError("artifact display duration does not match its clock")
            confidence_value = metadata.get("confidence")
            if not isinstance(confidence_value, str):
                raise ValueError("artifact display confidence is invalid")
            try:
                confidence = Confidence(confidence_value)
            except (TypeError, ValueError) as exc:
                raise ValueError("artifact display confidence is invalid") from exc
            if (
                manifest["evidence"].get("automatic_level")
                == EvidenceLevel.RENDERER_VERIFIED.value
                and confidence is not Confidence.SOURCE_EXACT
            ):
                raise ValueError(
                    "renderer-verified evidence requires source-exact tracks"
                )
            if (
                manifest["evidence"].get("automatic_level")
                == EvidenceLevel.FRAMEBUFFER_CAPTURED.value
                and confidence is not Confidence.FRAMEBUFFER_OBSERVED
            ):
                raise ValueError(
                    "framebuffer-captured evidence requires "
                    "framebuffer-observed tracks"
                )
            frame_hashes = metadata.get("frame_hashes")
            baseline_hashes = metadata.get("baseline_hashes")
            if (
                not isinstance(frame_hashes, list)
                or len(frame_hashes) != frame_count
                or not isinstance(baseline_hashes, list)
                or len(baseline_hashes) not in {0, 1, frame_count}
            ):
                raise ValueError("artifact display hashes are inconsistent")
            byte_count = profile.width * profile.height * 3
            for prefix, hashes, role in (
                ("frames", frame_hashes, "authoritative-rgb"),
                ("baselines", baseline_hashes, "authoritative-audit-baseline"),
            ):
                stem = "frame" if prefix == "frames" else "baseline"
                for index, digest in enumerate(hashes):
                    if (
                        not isinstance(digest, str)
                        or not _ARTIFACT_ID_RE.fullmatch(digest)
                    ):
                        raise ValueError("artifact display has an invalid RGB digest")
                    logical = f"{prefix}/{display_id}/{stem}-{index:03d}.rgb"
                    expected_authoritative.add(logical)
                    authoritative_file = actual_files.get(logical)
                    if (
                        authoritative_file is None
                        or authoritative_file.stat().st_size != byte_count
                    ):
                        raise ValueError(
                            f"authoritative RGB frame has the wrong length: {logical}"
                        )
                    if file_hashes[logical] != digest:
                        raise ValueError(
                            "authoritative RGB hash disagrees with display metadata: "
                            f"{logical}"
                        )
                    if inventory[logical]["role"] != role:
                        raise ValueError(
                            f"authoritative RGB frame has the wrong role: {logical}"
                        )
        actual_authoritative = {
            logical for logical, metadata in inventory.items()
            if metadata["role"] in {
                "authoritative-rgb", "authoritative-audit-baseline",
            }
        }
        if actual_authoritative != expected_authoritative:
            raise ValueError("artifact has unexpected authoritative RGB files")

    return VerifiedArtifact(
        artifact_id,
        directory.resolve(),
        bool(manifest["passed"]),
        manifest,
    )


class ArtifactStore:
    def __init__(self, root: Path, repo_root: Path) -> None:
        self.root = root.resolve()
        self.repo_root = repo_root.resolve()
        self.artifacts_dir = self.root / "artifacts"
        self.work_dir = self.root / "work"

    def publish(
        self,
        request: RenderRequest,
        segment: RenderedSegment,
    ) -> PublishedArtifact:
        validate_parameters(dict(request.parameters))
        validate_inputs(request.inputs)
        _validate_segment(segment)
        tracks = tuple(sorted(segment.displays, key=lambda track: track.id))
        results = analyze(segment)
        passed = required_checks_pass(results)
        automatic_evidence_level = (
            segment.evidence_level
            if passed and segment.evidence_level in (
                EvidenceLevel.RENDERER_VERIFIED,
                EvidenceLevel.FRAMEBUFFER_CAPTURED,
            )
            else None
        )
        raw_by_display: dict[str, list[bytes]] = {
            track.id: [frame.tobytes() for frame in track.frames]
            for track in tracks
        }
        hashes_by_display = {
            display_id: [sha256_bytes(value) for value in values]
            for display_id, values in raw_by_display.items()
        }
        baseline_raw_by_display = {
            track.id: [frame.tobytes() for frame in track.baselines]
            for track in tracks
        }
        baseline_hashes_by_display = {
            display_id: [sha256_bytes(value) for value in values]
            for display_id, values in baseline_raw_by_display.items()
        }
        source = _source_fingerprint(self.repo_root, segment.source_paths)
        normalized_request = request.as_dict()
        signals = [{
            "t_us": signal.t_us,
            "kind": signal.kind,
            "value": signal.value,
            "confidence": signal.confidence.value,
        } for signal in segment.signals]
        audit = [result.as_dict() for result in results]
        references = [{
            "id": reference.id,
            "label": reference.label,
            "display": reference.display,
            "frame_indices": list(reference.frame_indices),
            "pixels": [
                [x, y, list(color)] for x, y, color in reference.pixels
            ],
        } for reference in sorted(
            segment.ink_references, key=lambda item: item.id,
        )]
        self.work_dir.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="publish-", dir=self.work_dir))
        try:
            inventory: dict[str, dict[str, Any]] = {}
            display_manifest: dict[str, Any] = {}
            for track in tracks:
                frame_dir = temporary / "frames" / track.id
                frame_dir.mkdir(parents=True)
                raw_values = raw_by_display[track.id]
                hashes = hashes_by_display[track.id]
                for index, (frame, raw, digest) in enumerate(
                    zip(track.frames, raw_values, hashes),
                ):
                    rgb_name = f"frames/{track.id}/frame-{index:03d}.rgb"
                    png_name = f"frames/{track.id}/frame-{index:03d}.png"
                    (temporary / rgb_name).write_bytes(raw)
                    png = _png_bytes(frame)
                    (temporary / png_name).write_bytes(png)
                    inventory[rgb_name] = {
                        "sha256": digest,
                        "role": "authoritative-rgb",
                    }
                    inventory[png_name] = {
                        "sha256": sha256_bytes(png),
                        "role": "frame-preview",
                    }

                baseline_dir = temporary / "baselines" / track.id
                if track.baselines:
                    baseline_dir.mkdir(parents=True)
                for index, (baseline, raw, digest) in enumerate(zip(
                    track.baselines,
                    baseline_raw_by_display[track.id],
                    baseline_hashes_by_display[track.id],
                )):
                    rgb_name = f"baselines/{track.id}/baseline-{index:03d}.rgb"
                    png_name = f"baselines/{track.id}/baseline-{index:03d}.png"
                    (temporary / rgb_name).write_bytes(raw)
                    png = _png_bytes(baseline)
                    (temporary / png_name).write_bytes(png)
                    inventory[rgb_name] = {
                        "sha256": digest,
                        "role": "authoritative-audit-baseline",
                    }
                    inventory[png_name] = {
                        "sha256": sha256_bytes(png),
                        "role": "audit-baseline-preview",
                    }

                contact_frames = _sample_frames(
                    track.frames, 12 if track.id == "front" else 6,
                )
                contact_indices = _sample_indices(
                    len(track.frames), 12 if track.id == "front" else 6,
                )
                gap_frames = _sample_frames(
                    track.frames, 60 if track.id == "front" else 30,
                )
                gap_indices = _sample_indices(
                    len(track.frames), 60 if track.id == "front" else 30,
                )
                gap_kwargs = {} if track.id == "front" else {
                    "led_size": 5, "gap_size": 4,
                }
                derived = {
                    f"{track.id}.gif": _gif_bytes(
                        track.frames, track.fps, gaps=False,
                        display_id=track.id,
                    ),
                    f"{track.id}-gap.gif": _gif_bytes(
                        gap_frames, track.fps, gaps=True,
                        display_id=track.id,
                        source_indices=gap_indices,
                        source_frame_count=len(track.frames),
                    ),
                    f"{track.id}-contact-sheet.png": _png_bytes(contact_sheet(
                        contact_frames,
                        fps=track.fps,
                        columns=4 if track.id == "front" else 2,
                        frame_indices=contact_indices,
                    )),
                    f"{track.id}-gap-contact-sheet.png": _png_bytes(contact_sheet(
                        contact_frames,
                        fps=track.fps,
                        gap_view=True,
                        columns=4 if track.id == "front" else 2,
                        frame_indices=contact_indices,
                        **gap_kwargs,
                    )),
                    f"{track.id}-change-heatmap.png": _png_bytes(upscale(
                        change_heatmap(track.frames),
                        scale=8 if track.id == "front" else 4,
                    )),
                }
                for logical, value in derived.items():
                    (temporary / logical).write_bytes(value)
                    inventory[logical] = {
                        "sha256": sha256_bytes(value),
                        "role": "derived-preview",
                    }
                profile = profile_for(track.id)
                display_manifest[track.id] = {
                    "width": profile.width,
                    "height": profile.height,
                    "pixel_format": profile.pixel_format,
                    "frame_count": len(track.frames),
                    "fps": track.fps,
                    "duration_us": track.duration_us,
                    "confidence": track.confidence.value,
                    "frame_hashes": hashes,
                    "baseline_hashes": baseline_hashes_by_display[track.id],
                    "gap_preview_sampled": len(gap_frames) != len(track.frames),
                    "gap_preview_indices": list(gap_indices),
                    "contact_sheet_indices": list(contact_indices),
                }

            if references:
                (temporary / "references").mkdir()
            for reference, serialized in zip(
                sorted(segment.ink_references, key=lambda item: item.id),
                references,
            ):
                logical_json = f"references/{reference.id}.json"
                logical_png = f"references/{reference.id}.png"
                _write_json(temporary / logical_json, serialized)
                reference_track = segment.display(reference.display)
                assert reference_track is not None
                preview = Image.new("RGB", reference_track.size, (0, 0, 0))
                for x, y, color in reference.pixels:
                    if 0 <= x < preview.width and 0 <= y < preview.height:
                        preview.putpixel((x, y), color)
                png = _png_bytes(upscale(
                    preview, scale=8 if reference.display == "front" else 4,
                ))
                (temporary / logical_png).write_bytes(png)
                inventory[logical_json] = {
                    "sha256": sha256_bytes((temporary / logical_json).read_bytes()),
                    "role": "authoritative-full-ink-reference",
                }
                inventory[logical_png] = {
                    "sha256": sha256_bytes(png),
                    "role": "full-ink-reference-preview",
                }

            trace = _trace(segment, results, hashes_by_display)
            trace_bytes = b"".join(canonical_json(event) + b"\n" for event in trace)
            (temporary / "trace.jsonl").write_bytes(trace_bytes)
            scenario_bytes = canonical_json(normalized_request) + b"\n"
            audit_bytes = canonical_json({
                "schema": EVIDENCE_SCHEMA, "passed": passed, "checks": audit,
            }) + b"\n"
            signals_bytes = canonical_json(signals) + b"\n"
            (temporary / "scenario.normalized.json").write_bytes(scenario_bytes)
            (temporary / "audit.json").write_bytes(audit_bytes)
            (temporary / "signals.json").write_bytes(signals_bytes)
            inventory.update({
                "trace.jsonl": {
                    "sha256": sha256_bytes(trace_bytes),
                    "role": "machine-readable-event-trace",
                },
                "scenario.normalized.json": {
                    "sha256": sha256_bytes(scenario_bytes),
                    "role": "normalized-render-request",
                },
                "audit.json": {
                    "sha256": sha256_bytes(audit_bytes),
                    "role": "machine-readable-audit",
                },
                "signals.json": {
                    "sha256": sha256_bytes(signals_bytes),
                    "role": "logical-signal-trace",
                },
            })

            summary_lines = [
                f"# busybar-viz evidence: {request.scenario_id}",
                "",
                f"Result: **{'PASS' if passed else 'FAIL'}**",
                "Automatic evidence level: "
                + (f"`{automatic_evidence_level.value}`"
                   if automatic_evidence_level else "none"),
                "Gap preview generated: yes; inspected: not recorded in this artifact.",
                "",
                "## Displays",
                "",
            ]
            summary_lines.extend(
                f"- `{track.id}`: {len(track.frames)} frames at {track.fps} FPS, "
                f"{track.size[0]}x{track.size[1]} RGB "
                f"(`{track.confidence.value}`)"
                for track in tracks
            )
            summary_lines.extend(("", "## Checks", ""))
            summary_lines.extend(
                f"- `{result.status.value}` `{result.id}` — {result.message}"
                for result in results
            )
            if segment.notes:
                summary_lines.extend(("", "## Limits", ""))
                summary_lines.extend(f"- {note}" for note in segment.notes)
            summary_bytes = ("\n".join(summary_lines) + "\n").encode("utf-8")
            (temporary / "summary.md").write_bytes(summary_bytes)
            inventory["summary.md"] = {
                "sha256": sha256_bytes(summary_bytes),
                "role": "human-readable-evidence-summary",
            }

            manifest_payload: dict[str, Any] = {
                "schema": EVIDENCE_SCHEMA,
                "scenario": normalized_request,
                "source": source,
                "passed": passed,
                "evidence": {
                    "automatic_level": (
                        automatic_evidence_level.value
                        if automatic_evidence_level else None
                    ),
                    "reviewed_level": None,
                    "available_previews": [
                        "native-raster", "led-gap-simulation",
                    ],
                    "notice": (
                        "Generating a gap preview does not mean it was inspected. "
                        "Review evidence belongs in the session journal."
                    ),
                },
                "displays": display_manifest,
                "signals": signals,
                "ink_references": references,
                "checks": audit,
                "notes": list(segment.notes),
                "files": inventory,
            }
            artifact_id = _artifact_id_for_payload(manifest_payload)
            manifest: dict[str, Any] = {
                **manifest_payload,
                "artifact_id": artifact_id,
            }
            _write_json(temporary / "manifest.json", manifest)

            total = sum(path.stat().st_size for path in temporary.rglob("*")
                        if path.is_file())
            if total > MAX_ARTIFACT_BYTES:
                raise LimitError("artifact exceeds the 64 MiB publication budget")
            verify_artifact(
                temporary,
                full=True,
                expected_artifact_id=artifact_id,
            )
            destination = self.artifacts_dir / artifact_id[:2] / artifact_id
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists() or destination.is_symlink():
                verified = verify_artifact(
                    destination,
                    full=True,
                    expected_artifact_id=artifact_id,
                )
                shutil.rmtree(temporary)
                return PublishedArtifact(
                    verified.artifact_id,
                    verified.path,
                    verified.passed,
                    verified.manifest,
                )
            try:
                os.replace(temporary, destination)
            except OSError as exc:
                if (destination.exists() or destination.is_symlink()) and exc.errno in {
                    errno.EEXIST, errno.ENOTEMPTY,
                }:
                    verified = verify_artifact(
                        destination,
                        full=True,
                        expected_artifact_id=artifact_id,
                    )
                    shutil.rmtree(temporary)
                    return PublishedArtifact(
                        verified.artifact_id,
                        verified.path,
                        verified.passed,
                        verified.manifest,
                    )
                raise
            return PublishedArtifact(artifact_id, destination, passed, manifest)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

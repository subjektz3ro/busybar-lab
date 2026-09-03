"""Deterministic pixel comparisons between immutable evidence artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from PIL import Image

from .artifacts import canonical_json, sha256_bytes, verify_artifact
from .panel import contact_sheet

COMPARISON_SCHEMA = "busybar.comparison/v1"


class _RGBPixelAccess(Protocol):
    def __getitem__(self, point: tuple[int, int], /) -> tuple[int, int, int]: ...

    def __setitem__(
        self,
        point: tuple[int, int],
        value: tuple[int, int, int],
        /,
    ) -> None: ...


def _read_manifest(path: Path) -> tuple[Path, dict[str, Any]]:
    verified = verify_artifact(path, full=True)
    return verified.path, verified.manifest


def _verify_cached_comparison(
    destination: Path,
    candidate: Path,
) -> None:
    """Accept a concurrent/cache hit only when every derived byte matches."""

    if destination.is_symlink() or not destination.is_dir():
        raise ValueError("comparison cache path is not a regular directory")

    def files(root: Path) -> dict[str, Path]:
        result: dict[str, Path] = {}
        for entry in root.rglob("*"):
            if entry.is_symlink():
                raise ValueError("comparison bundles may not contain symlinks")
            if entry.is_dir():
                continue
            if not entry.is_file():
                raise ValueError("comparison bundles may contain only regular files")
            result[entry.relative_to(root).as_posix()] = entry
        return result

    existing = files(destination)
    generated = files(candidate)
    if set(existing) != set(generated):
        raise ValueError("cached comparison inventory does not match its identity")
    for logical, generated_path in generated.items():
        existing_path = existing[logical]
        if (
            existing_path.stat().st_size != generated_path.stat().st_size
            or sha256_bytes(existing_path.read_bytes())
            != sha256_bytes(generated_path.read_bytes())
        ):
            raise ValueError(
                f"cached comparison content does not match its identity: {logical}"
            )


def _frame(
    directory: Path,
    manifest: dict[str, Any],
    display_id: str,
    index: int,
) -> Image.Image:
    display = manifest["displays"][display_id]
    width, height = int(display["width"]), int(display["height"])
    candidate = directory / f"frames/{display_id}/frame-{index:03d}.rgb"
    if candidate.is_symlink():
        raise ValueError("comparison artifacts may not contain symlinks")
    resolved = candidate.resolve()
    try:
        resolved.relative_to(directory)
    except ValueError as exc:
        raise ValueError("comparison frame escapes its artifact") from exc
    raw = resolved.read_bytes()
    expected_size = width * height * 3
    if len(raw) != expected_size:
        raise ValueError("authoritative RGB frame has the wrong byte length")
    expected_hash = display["frame_hashes"][index]
    if sha256_bytes(raw) != expected_hash:
        raise ValueError("authoritative RGB frame does not match its manifest")
    return Image.frombytes("RGB", (width, height), raw)


def _diff(left: Image.Image, right: Image.Image) -> tuple[Image.Image, dict[str, Any]]:
    if left.size != right.size:
        raise ValueError("cannot compare display frames with different dimensions")
    out = Image.new("RGB", left.size)
    changed = 0
    total_delta = 0
    maximum = 0
    xs: list[int] = []
    ys: list[int] = []
    left_pixels = cast(_RGBPixelAccess, left.load())
    right_pixels = cast(_RGBPixelAccess, right.load())
    out_pixels = cast(_RGBPixelAccess, out.load())
    for y in range(left.height):
        for x in range(left.width):
            delta = tuple(abs(a - b) for a, b in zip(
                left_pixels[x, y], right_pixels[x, y],
            ))
            pixel_max = max(delta)
            if pixel_max:
                strength = min(255, 32 + pixel_max * 7)
                out_pixels[x, y] = (strength, strength // 3, 0)
            else:
                out_pixels[x, y] = (0, 0, 0)
            total_delta += sum(delta)
            maximum = max(maximum, pixel_max)
            if pixel_max:
                changed += 1
                xs.append(x)
                ys.append(y)
    count = left.width * left.height
    return out, {
        "changed_pixels": changed,
        "changed_fraction": changed / count,
        "mean_channel_delta": total_delta / (count * 3),
        "max_channel_delta": maximum,
        "changed_bbox": None if not xs else [min(xs), min(ys), max(xs) + 1, max(ys) + 1],
    }


def _sample_indices(count: int, maximum: int = 12) -> tuple[int, ...]:
    if count <= maximum:
        return tuple(range(count))
    return tuple(sorted({round(index * (count - 1) / (maximum - 1))
                         for index in range(maximum)}))


@dataclass(frozen=True, slots=True)
class PublishedComparison:
    comparison_id: str
    path: Path
    summary: dict[str, Any]


class ComparisonStore:
    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.comparisons_dir = self.root / "comparisons"
        self.work_dir = self.root / "work"

    def publish(self, before: Path, after: Path) -> PublishedComparison:
        before_dir, before_manifest = _read_manifest(before)
        after_dir, after_manifest = _read_manifest(after)
        identity = hashlib.sha256(canonical_json({
            "schema": COMPARISON_SCHEMA,
            "algorithm": 2,
            "before": before_manifest["artifact_id"],
            "after": after_manifest["artifact_id"],
        })).hexdigest()
        destination = self.comparisons_dir / identity[:2] / identity

        self.work_dir.mkdir(parents=True, exist_ok=True)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix="compare-", dir=self.work_dir))
        try:
            displays: dict[str, Any] = {}
            changed = False
            display_ids = sorted(
                set(before_manifest["displays"]) | set(after_manifest["displays"])
            )
            for display_id in display_ids:
                left_meta = before_manifest["displays"].get(display_id)
                right_meta = after_manifest["displays"].get(display_id)
                if left_meta is None or right_meta is None:
                    state = "added" if left_meta is None else "removed"
                    displays[display_id] = {"state": state}
                    changed = True
                    continue
                if (left_meta["width"], left_meta["height"]) != (
                    right_meta["width"], right_meta["height"],
                ):
                    displays[display_id] = {
                        "state": "dimensions_changed",
                        "before_size": [left_meta["width"], left_meta["height"]],
                        "after_size": [right_meta["width"], right_meta["height"]],
                    }
                    changed = True
                    continue
                common = min(left_meta["frame_count"], right_meta["frame_count"])
                after_fps = int(right_meta["fps"])
                frame_metrics: list[dict[str, Any]] = []
                diff_frames: list[Image.Image] = []
                for index in range(common):
                    image, metrics = _diff(
                        _frame(before_dir, before_manifest, display_id, index),
                        _frame(after_dir, after_manifest, display_id, index),
                    )
                    metrics["frame_index"] = index
                    frame_metrics.append(metrics)
                    diff_frames.append(image)
                indices = _sample_indices(common)
                if indices:
                    sheet = contact_sheet(
                        tuple(diff_frames[index] for index in indices),
                        fps=after_fps,
                        columns=4 if display_id == "front" else 2,
                        frame_indices=indices,
                    )
                    sheet.save(temporary / f"{display_id}-diff-contact-sheet.png")
                track_changed = (
                    any(item["changed_pixels"] for item in frame_metrics)
                    or left_meta["frame_count"] != right_meta["frame_count"]
                    or left_meta["fps"] != right_meta["fps"]
                )
                changed = changed or track_changed
                displays[display_id] = {
                    "state": "changed" if track_changed else "identical",
                    "before_frame_count": left_meta["frame_count"],
                    "after_frame_count": right_meta["frame_count"],
                    "before_fps": left_meta["fps"],
                    "after_fps": after_fps,
                    "compared_frame_count": common,
                    "diff_contact_sheet_indices": list(indices),
                    "frames": frame_metrics,
                }
            summary = {
                "schema": COMPARISON_SCHEMA,
                "comparison_id": identity,
                "before": before_manifest["artifact_id"],
                "after": after_manifest["artifact_id"],
                "changed": changed,
                "displays": displays,
            }
            (temporary / "comparison.json").write_bytes(canonical_json(summary) + b"\n")
            try:
                os.replace(temporary, destination)
            except OSError:
                if destination.exists() or destination.is_symlink():
                    _verify_cached_comparison(destination, temporary)
                    shutil.rmtree(temporary)
                    return PublishedComparison(identity, destination, summary)
                raise
            return PublishedComparison(identity, destination, summary)
        except BaseException:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

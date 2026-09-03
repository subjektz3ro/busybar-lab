"""Load repository-native raster assets without pretending to emulate firmware."""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

from PIL import Image, UnidentifiedImageError

from busybar_dev.anim import (
    DEFAULT_DECODE_LIMITS,
    decode_anim,
)

from .limits import MAX_FRAMES, LimitError, validate_timing
from .models import CheckSpec, Confidence, DisplayTrack, RenderedSegment, RenderRequest
from .profiles import profile_for


def repository_file(path: Path, repo_root: Path) -> tuple[Path, str]:
    resolved = path.expanduser().resolve()
    try:
        logical = resolved.relative_to(repo_root.resolve()).as_posix()
    except ValueError as exc:
        raise ValueError("asset paths must stay inside the repository") from exc
    if not resolved.is_file():
        raise ValueError(f"asset does not exist: {logical}")
    return resolved, logical


def read_source_bounded(path: Path) -> bytes:
    """Snapshot no more than the decoder's declared source-file budget.

    Both PIL and ``decode_anim`` ultimately consume bytes. A plain path-based
    open can accept a tiny valid header followed by a multi-gigabyte sparse
    file, and hashing the path later can describe different bytes if the file
    changes between decode and publication. The stat is a fast rejection; the
    capped read closes the resize race and becomes the authoritative snapshot.
    """

    limit = DEFAULT_DECODE_LIMITS.max_source_bytes
    if path.stat().st_size > limit:
        raise LimitError(
            f"asset source exceeds the {limit}-byte decode budget"
        )
    with path.open("rb") as handle:
        source = handle.read(limit + 1)
    if len(source) > limit:
        raise LimitError(
            f"asset source exceeds the {limit}-byte decode budget"
        )
    return source


def load_asset_segment(
    path: Path,
    *,
    repo_root: Path,
    display_id: str,
    section: str = "default",
) -> tuple[RenderRequest, RenderedSegment]:
    """Decode a PNG or native ``.anim`` into ordinary evidence tracks."""

    resolved, logical = repository_file(path, repo_root)
    profile = profile_for(display_id)
    suffix = resolved.suffix.lower()
    if suffix not in {".anim", ".png"}:
        raise ValueError("busybar-viz asset accepts only .png or .anim files")
    source_bytes = read_source_bounded(resolved)
    source_sha256 = hashlib.sha256(source_bytes).hexdigest()
    if suffix == ".anim":
        decoded = decode_anim(source_bytes)
        if decoded.size != profile.size:
            raise ValueError(
                f"{logical} is {decoded.width}x{decoded.height}, not the "
                f"{display_id} display's {profile.width}x{profile.height}"
            )
        selected = decoded.section(section)
        if selected.display_frame_count > MAX_FRAMES:
            raise ValueError(
                f"section {section!r} expands to {selected.display_frame_count} "
                f"frames; busybar-viz limit is {MAX_FRAMES}"
            )
        validate_timing(
            frame_count=selected.display_frame_count,
            fps=decoded.fps,
        )
        frames = tuple(decoded.iter_display_frames(section))
        fps = decoded.fps
        notes = (
            "Pixels and timing were decoded exactly from the hashed native .anim snapshot.",
            "Firmware element composition, native text, and panel optics were not emulated.",
        )
        parameters: dict[str, object] = {
            "path": logical,
            "display": display_id,
            "section": section,
            "source_sha256": source_sha256,
        }
    elif suffix == ".png":
        try:
            with Image.open(io.BytesIO(source_bytes)) as source:
                if source.format != "PNG":
                    raise ValueError(f"{logical} is not a valid PNG file")
                if source.size != profile.size:
                    raise ValueError(
                        f"{logical} is {source.width}x{source.height}, not the "
                        f"{display_id} display's {profile.width}x{profile.height}"
                    )
                frame = source.convert("RGB")
        except UnidentifiedImageError as exc:
            raise ValueError(f"{logical} is not a valid PNG file") from exc
        frames, fps = (frame,), 1
        notes = (
            "Pixels were decoded exactly from the hashed repository PNG snapshot.",
            "This still does not emulate firmware composition or panel optics.",
        )
        parameters = {
            "path": logical,
            "display": display_id,
            "source_sha256": source_sha256,
        }
    request = RenderRequest.from_values(f"asset/{logical}", parameters)
    checks = (
        CheckSpec.create(
            f"{display_id}-dimensions",
            "frame.dimensions",
            display=display_id,
            size=profile.size,
        ),
        CheckSpec.create(
            f"{display_id}-metrics",
            "frame.summary_metrics",
            severity="info",
            display=display_id,
        ),
        CheckSpec.create(
            f"{display_id}-loop-seam",
            "animation.loop_seam",
            severity="info",
            display=display_id,
        ),
    )
    segment = RenderedSegment(
        displays=(DisplayTrack(
            display_id,
            frames,
            fps,
            Confidence.SOURCE_EXACT,
        ),),
        checks=checks,
        notes=notes,
        # The loaded asset's hash is frozen in the normalized request above.
        # Do not fingerprint its mutable path again after decoding.
        source_paths=("busybar_dev/anim.py", "busybar_viz/assets.py"),
    )
    return request, segment

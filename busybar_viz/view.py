"""Ad-hoc in-development frames enter the same evidence pipeline.

The registered-scenario path (`run`) proves provenance: a production-owned
renderer produced the pixels. This path provides sight: an agent that just
wrote frames to disk gets the identical audit engine, gap simulation,
contact sheets, immutable artifact, and `compare`-able SHA — without first
building an adapter. Nothing here verifies that the frames came from
production code, and the recorded notes and track confidence say so.

The device-law checks are the point. A picture alone is something an agent
could cobble together itself; `region.contrast_floor` and
`region.min_feature_size` against `busybar_viz/device_laws.py` are what its
own ad-hoc script would not enforce. Declare regions (and their ink colours)
on the command line and the same checks that guard registered scenarios run
against these frames.
"""

from __future__ import annotations

import hashlib
import io
import re
from pathlib import Path
from typing import Mapping, Sequence

from PIL import Image, UnidentifiedImageError

from .assets import read_source_bounded, repository_file
from .limits import MAX_FRAMES
from .models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    RegionSpec,
    RenderedSegment,
    RenderRequest,
)
from .profiles import profile_for

_REGION_NAME = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_INK_COLOR = re.compile(r"#?[0-9A-Fa-f]{6}\Z")
_APP_MODULE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")


def parse_region(raw: str) -> tuple[str, tuple[int, int, int, int]]:
    """Parse one ``NAME=X0,Y0,X1,Y1`` half-open rectangle declaration."""

    name, separator, rect_text = raw.partition("=")
    if not separator or not _REGION_NAME.fullmatch(name):
        raise ValueError(
            f"--region needs NAME=X0,Y0,X1,Y1 with a 1-32 char name: {raw!r}"
        )
    parts = rect_text.split(",")
    if len(parts) != 4:
        raise ValueError(f"--region {name} needs exactly four integers")
    try:
        x0, y0, x1, y1 = (int(part) for part in parts)
    except ValueError as exc:
        raise ValueError(f"--region {name} needs exactly four integers") from exc
    if not (x0 < x1 and y0 < y1):
        raise ValueError(
            f"--region {name} must satisfy X0<X1 and Y0<Y1 (half-open rect)"
        )
    return name, (x0, y0, x1, y1)


def parse_ink(raw: str) -> tuple[str, tuple[str, ...]]:
    """Parse one ``NAME=#RRGGBB[,#RRGGBB...]`` declared-ink assignment."""

    name, separator, colors_text = raw.partition("=")
    if not separator or not _REGION_NAME.fullmatch(name):
        raise ValueError(
            f"--ink needs NAME=#RRGGBB[,#RRGGBB...] with a region name: {raw!r}"
        )
    colors: list[str] = []
    for part in colors_text.split(","):
        text = part.strip()
        if not _INK_COLOR.fullmatch(text):
            raise ValueError(f"--ink {name} colours must be 6-digit hex: {part!r}")
        colors.append("#" + text.lstrip("#").upper())
    if not colors:
        raise ValueError(f"--ink {name} declares no colours")
    return name, tuple(colors)


def emit_declaration(
    app: str,
    *,
    display_id: str,
    regions: Mapping[str, tuple[int, int, int, int]],
    inks: Mapping[str, Sequence[str]],
    max_isolated: int,
) -> str:
    """A paste-ready `[<app>.viz]` block matching this view invocation.

    Promotion from iterating with `view` to registration in apps.toml should
    be a copy, not a retype. The renderer line is the convention the template
    documents; the seam itself still has to exist.
    """

    if not _APP_MODULE.fullmatch(app):
        raise ValueError(
            "--emit-declaration app names must start with a lowercase letter "
            "and contain only lowercase letters, digits, or underscores"
        )
    lines = [
        f"[{app}.viz]",
        f'renderer = "apps.{app}:render_visual"',
        f'displays = ["{display_id}"]',
    ]
    for name in sorted(regions):
        lines.append("")
        lines.append(f"[{app}.viz.regions.{name}]")
        lines.append(f"rect = [{', '.join(str(v) for v in regions[name])}]")
        if name in inks:
            colors = ", ".join(f'"{color}"' for color in inks[name])
            lines.append(f"ink = [{colors}]")
        if max_isolated:
            lines.append(f"max_isolated = {max_isolated}")
    return "\n".join(lines) + "\n"


def _frame_files(paths: Sequence[Path], repo_root: Path) -> tuple[tuple[Path, str], ...]:
    """Expand file and directory arguments into an ordered PNG frame list."""

    ordered: list[tuple[Path, str]] = []
    for path in paths:
        expanded = path.expanduser()
        if expanded.is_dir():
            resolved_dir = expanded.resolve()
            entries = sorted(
                entry for entry in resolved_dir.iterdir()
                if entry.is_file() and entry.suffix.lower() == ".png"
            )
            if not entries:
                raise ValueError(f"{path} contains no .png frames")
            ordered.extend(repository_file(entry, repo_root) for entry in entries)
            continue
        if expanded.suffix.lower() != ".png":
            raise ValueError(
                "busybar-viz view accepts .png frames or a directory of them; "
                "use `asset` for native .anim files"
            )
        ordered.append(repository_file(expanded, repo_root))
    if not ordered:
        raise ValueError("no input frames were given")
    if len(ordered) > MAX_FRAMES:
        raise ValueError(
            f"{len(ordered)} frames exceed the {MAX_FRAMES}-frame budget"
        )
    return tuple(ordered)


def _decode_frame(source_bytes: bytes, logical: str) -> Image.Image:
    try:
        with Image.open(io.BytesIO(source_bytes)) as source:
            if source.format != "PNG":
                raise ValueError(f"{logical} is not a valid PNG file")
            return source.convert("RGB")
    except UnidentifiedImageError as exc:
        raise ValueError(f"{logical} is not a valid PNG file") from exc


def _detect_scale(
    size: tuple[int, int],
    native: tuple[int, int],
    requested: int | None,
    logical: str,
) -> int:
    width, height = size
    native_width, native_height = native
    if requested is not None:
        if size != (native_width * requested, native_height * requested):
            raise ValueError(
                f"{logical} is {width}x{height}, not the requested scale "
                f"{requested} of the native {native_width}x{native_height}"
            )
        return requested
    if size == native:
        return 1
    factor, remainder = divmod(width, native_width)
    if (remainder == 0 and factor >= 2
            and height == native_height * factor):
        return factor
    raise ValueError(
        f"{logical} is {width}x{height}; view accepts the native "
        f"{native_width}x{native_height} or an exact integer enlargement of it"
    )


def load_view_segment(
    paths: Sequence[Path],
    *,
    repo_root: Path,
    display_id: str,
    fps: int | None = None,
    scale: int | None = None,
    regions: Mapping[str, tuple[int, int, int, int]] | None = None,
    inks: Mapping[str, Sequence[str]] | None = None,
    max_isolated: int = 0,
) -> tuple[RenderRequest, RenderedSegment]:
    """Decode ordered ad-hoc PNG frames into ordinary evidence tracks."""

    regions = dict(regions or {})
    inks = {name: tuple(colors) for name, colors in (inks or {}).items()}
    unknown_inks = sorted(set(inks) - set(regions))
    if unknown_inks:
        raise ValueError(
            f"--ink names a region that was not declared: {', '.join(unknown_inks)}"
        )

    profile = profile_for(display_id)
    for name, (x0, y0, x1, y1) in regions.items():
        if not (0 <= x0 and 0 <= y0 and x1 <= profile.width and y1 <= profile.height):
            raise ValueError(
                f"region {name} exceeds the {display_id} display's "
                f"{profile.width}x{profile.height} bounds"
            )

    files = _frame_files(paths, repo_root)
    frames: list[Image.Image] = []
    frame_hashes: list[str] = []
    detected_scale: int | None = None
    for resolved, logical in files:
        source_bytes = read_source_bounded(resolved)
        frame_hashes.append(hashlib.sha256(source_bytes).hexdigest())
        frame = _decode_frame(source_bytes, logical)
        frame_scale = _detect_scale(frame.size, profile.size, scale, logical)
        if detected_scale is None:
            detected_scale = frame_scale
        elif frame_scale != detected_scale:
            raise ValueError(
                f"{logical} is scale {frame_scale}, but earlier frames are "
                f"scale {detected_scale}; view frames must share one size"
            )
        if frame_scale != 1:
            frame = frame.resize(profile.size, Image.Resampling.NEAREST)
        frames.append(frame)
    assert detected_scale is not None

    if fps is None:
        fps = 1 if len(frames) == 1 else 5

    exact = detected_scale == 1
    confidence = Confidence.SOURCE_EXACT if exact else Confidence.APPROXIMATE
    notes = [
        "Frames were decoded from hashed snapshots of ad-hoc in-development "
        "files; nothing verifies they came from production rendering code.",
    ]
    if exact:
        notes.append("Pixels are byte-exact copies of the snapshotted frames.")
    else:
        notes.append(
            f"Input was an exact {detected_scale}x enlargement and was "
            "nearest-neighbour downsampled; pixels are approximate, not "
            "byte-exact renderer output."
        )

    logical_paths = [logical for _resolved, logical in files]
    parameters: dict[str, object] = {
        "paths": logical_paths,
        "display": display_id,
        "fps": fps,
        "scale": detected_scale,
        "frame_sha256": frame_hashes,
        "max_isolated": max_isolated,
        "regions": {name: list(rect) for name, rect in sorted(regions.items())},
        "ink": {name: list(colors) for name, colors in sorted(inks.items())},
    }
    request = RenderRequest.from_values(f"view/{logical_paths[0]}", parameters)

    checks: list[CheckSpec] = [
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
    ]
    region_specs: list[RegionSpec] = []
    for name in sorted(regions):
        region_specs.append(RegionSpec(name, display=display_id, rect=regions[name]))
        checks.append(CheckSpec.create(
            f"{name}-body",
            "region.min_feature_size",
            display=display_id,
            region=name,
            max_isolated=max_isolated,
        ))
        if name in inks:
            checks.append(CheckSpec.create(
                f"{name}-contrast",
                "region.contrast_floor",
                display=display_id,
                region=name,
                ink=list(inks[name]),
            ))

    segment = RenderedSegment(
        displays=(DisplayTrack(display_id, tuple(frames), fps, confidence),),
        regions=tuple(region_specs),
        checks=tuple(checks),
        notes=tuple(notes),
        # Frame hashes are frozen in the normalized request above; the loader
        # itself is the only source file whose behaviour shapes the evidence.
        source_paths=("busybar_viz/view.py",),
    )
    return request, segment

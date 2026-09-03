"""Publish the device's current framebuffer as ordinary, honest evidence.

This is the visualizer's one device-network-client path. Every render surface
blocks sockets; `capture` instead exists to *close the evidence ladder*: after
renderer-verified pixels and a gap preview, the remaining offline question is
what the device actually composited, and only its readback API can answer
that. The capture is read-only — it draws nothing, clears nothing, and leaves
the panel exactly as found.

What a capture proves is deliberately narrow, and the recorded notes say so:
a framebuffer still is a composited moment. It does not identify the frame
currently playing inside a native `.anim`, and it is not a physical-panel
observation — contrast, bloom, and apparent pixel size still need eyes.
"""

from __future__ import annotations

from .models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    EvidenceLevel,
    RenderedSegment,
    RenderRequest,
)
from .profiles import profile_for


def load_capture_segment(
    display_ids: tuple[str, ...] = ("front", "back"),
) -> tuple[RenderRequest, RenderedSegment]:
    """Read each requested framebuffer once and wrap it in evidence types.

    Deliberately excludes the device host, token, or any machine identity
    from the recorded request: artifacts are shareable and content-addressed,
    and two captures of identical pixels should be the same artifact.
    """

    # Device access stays out of module import so registry discovery and
    # offline renders never touch it.
    from busybar_dev import connect
    from busybar_dev.screen import back_image, front_image

    if not display_ids or len(display_ids) != len(set(display_ids)):
        raise ValueError("capture displays must be a non-empty unique list")

    readers = {"front": front_image, "back": back_image}
    tracks: list[DisplayTrack] = []
    checks: list[CheckSpec] = []
    with connect() as bb:
        for display_id in display_ids:
            frame = readers[display_id](bb).convert("RGB")
            profile = profile_for(display_id)
            if frame.size != profile.size:
                raise ValueError(
                    f"device returned a {frame.width}x{frame.height} {display_id} "
                    f"framebuffer, not {profile.width}x{profile.height}"
                )
            tracks.append(DisplayTrack(
                display_id, (frame,), 1, Confidence.FRAMEBUFFER_OBSERVED,
            ))
            checks.append(CheckSpec.create(
                f"{display_id}-dimensions",
                "frame.dimensions",
                display=display_id,
                size=profile.size,
            ))
            checks.append(CheckSpec.create(
                f"{display_id}-metrics",
                "frame.summary_metrics",
                severity="info",
                display=display_id,
            ))

    request = RenderRequest.from_values(
        "capture/framebuffer", {"displays": list(display_ids)},
    )
    segment = RenderedSegment(
        displays=tuple(tracks),
        evidence_level=EvidenceLevel.FRAMEBUFFER_CAPTURED,
        checks=tuple(checks),
        notes=(
            "Captured once from the device framebuffer readback API; the "
            "capture drew nothing and left the panel as found.",
            "A framebuffer still is a composited moment: it does not "
            "identify the frame currently playing inside a native .anim, "
            "and it is not a physical-panel observation.",
        ),
        source_paths=("busybar_dev/screen.py", "busybar_viz/capture.py"),
    )
    return request, segment


__all__ = ["load_capture_segment"]

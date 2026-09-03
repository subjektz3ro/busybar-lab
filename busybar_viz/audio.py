"""Inspect a `.snd` asset: is this PCM actually what the app meant to say?

The device format is headerless raw PCM, signed 16-bit little-endian, mono,
44100 Hz (`AGENTS.md`). Headerless means structural mistakes are easy to miss:
an odd byte length is a truncated write, a peak near zero is an inaudible
asset, and hard clipping turns speech into buzz. This inspector reports the
measurable properties as numbers and can draw a min/max envelope PNG that a
person or vision model can read at a glance. Raw samples carry no channel
layout metadata, so this tool cannot prove that the producer exported mono
rather than interleaved stereo.

This is an inspection aid, not an evidence bundle: audio has no display
track, no panel physics, and no gap simulation, so it does not enter the
content-addressed artifact store. The JSON carries its own sha256 instead.
"""

from __future__ import annotations

import hashlib
import sys
from array import array
from pathlib import Path

from PIL import Image

from .assets import read_source_bounded, repository_file

SAMPLE_RATE = 44100
_FULL_SCALE = 32768.0
_CLIP_VALUES = frozenset({-32768, -32767, 32767})


def load_samples(path: Path, *, repo_root: Path) -> tuple[str, bytes, array]:
    """Snapshot and decode one `.snd`, rejecting anything structurally wrong."""

    resolved, logical = repository_file(path, repo_root)
    if resolved.suffix.lower() != ".snd":
        raise ValueError("busybar-viz audio accepts only .snd files")
    source = read_source_bounded(resolved)
    if not source:
        raise ValueError(f"{logical} is empty")
    if len(source) % 2:
        raise ValueError(
            f"{logical} has an odd byte length; s16le samples are two bytes, "
            "so this file was truncated or is not raw PCM"
        )
    samples = array("h")
    samples.frombytes(source)
    if sys.byteorder == "big":
        samples.byteswap()
    return logical, source, samples


def measure(samples: array) -> dict[str, object]:
    """Deterministic scalar properties of one mono s16le sample track."""

    peak = max(abs(value) for value in samples)
    total = 0.0
    mean = 0.0
    for value in samples:
        total += float(value) * value
        mean += value
    rms = (total / len(samples)) ** 0.5
    clipped = sum(value in _CLIP_VALUES for value in samples)
    return {
        "sample_rate": SAMPLE_RATE,
        "samples": len(samples),
        "duration_s": round(len(samples) / SAMPLE_RATE, 4),
        "peak_fraction": round(peak / _FULL_SCALE, 4),
        "rms_fraction": round(rms / _FULL_SCALE, 4),
        "dc_offset_fraction": round(mean / len(samples) / _FULL_SCALE, 4),
        "clipped_samples": clipped,
        "silent": peak == 0,
    }


def waveform_png(
    samples: array,
    out_path: Path,
    *,
    width: int = 720,
    height: int = 160,
) -> Path:
    """A min/max envelope per pixel column — the shape of the sound."""

    if not 16 <= width <= 4096 or not 16 <= height <= 1024:
        raise ValueError("waveform dimensions must be sane pixel sizes")
    image = Image.new("RGB", (width, height), (12, 12, 16))
    pixels = image.load()
    if pixels is None:  # pragma: no cover - Pillow returns None only on error
        raise RuntimeError("Pillow could not expose waveform pixel access")
    midline = height // 2
    for x in range(width):
        start = x * len(samples) // width
        stop = max(start + 1, (x + 1) * len(samples) // width)
        window = samples[start:stop]
        low = min(window) / _FULL_SCALE
        high = max(window) / _FULL_SCALE
        y0 = int(midline - high * (midline - 1))
        y1 = int(midline - low * (midline - 1))
        for y in range(min(y0, y1), max(y0, y1) + 1):
            pixels[x, y] = (120, 190, 255)
        pixels[x, midline] = (60, 70, 90) if pixels[x, midline] == (12, 12, 16) \
            else pixels[x, midline]
    image.save(out_path)
    return out_path


def inspect_snd(
    path: Path,
    *,
    repo_root: Path,
    waveform: Path | None = None,
) -> dict[str, object]:
    logical, source, samples = load_samples(path, repo_root=repo_root)
    result: dict[str, object] = {
        "path": logical,
        "bytes": len(source),
        "source_sha256": hashlib.sha256(source).hexdigest(),
        **measure(samples),
    }
    if waveform is not None:
        result["waveform_path"] = str(waveform_png(samples, waveform))
    return result

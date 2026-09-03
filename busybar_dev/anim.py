"""Encoder and decoder for the BUSY Bar `.anim` format ("bicycle0").

Ported from the official firmware tooling (busy-app/busybar-firmware,
``scripts/seq2anim.py``, ``scripts/flipper/rle.py``, and
``lib/anim_file/anim_file_format.h`` at upstream revision
2cfd1f3ad94071056f3f96784183bab62dea423e).

Those portions are Copyright 2024-2026 Flipper FZCO and licensed
GPL-2.0-or-later. This repository distributes its combined adaptation under the
same GPL-2.0-or-later terms; see LICENSE and NOTICE.md. Produces BGR888 animations the device
plays natively via an ``animation`` display element.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass, fields
from typing import Literal

from PIL import Image

_SIGNATURE = b"bicycle0"
_HEADER_FMT = "<8s BBBB BHB II III"
_SECTION_FMT = "<IIIB"
_FRAME_FMT = "<BBH"
_COLOR_BGR888 = 0
_MAX_BLOCKS = 127
_RLE_THRESHOLD = 3


class AnimDecodeError(ValueError):
    """The input is not a supported, internally consistent ``.anim`` file."""


class AnimDecodeBudgetError(AnimDecodeError):
    """The input exceeds a caller-controlled decoding work budget."""


@dataclass(frozen=True, slots=True)
class AnimDecodeLimits:
    """Hard bounds for decoding untrusted animation files.

    ``max_expanded_rgb_bytes`` covers the conceptual playback timeline, not
    merely the packed file frames.  A tiny file can otherwise claim enormous
    frame durations and make a later visualizer allocate gigabytes while
    expanding it.
    """

    max_source_bytes: int = 32 * 1024 * 1024
    max_sections: int = 1024
    max_section_name_bytes: int = 255
    max_file_frames: int = 10_000
    max_display_frames: int = 100_000
    max_expanded_rgb_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) <= 0:
                raise ValueError(f"{field.name} must be positive")


@dataclass(frozen=True, slots=True)
class AnimFileFrame:
    """One packed file frame and its decoded RGB888 pixels.

    ``duration`` is measured in display ticks at the parent animation's FPS.
    ``rgb`` is canonical row-major RGB888; the BGR wire order is deliberately
    removed at this boundary so callers cannot accidentally preview red as
    blue.
    """

    rgb: bytes
    duration: int
    encoding: Literal["raw", "rle"]
    encoded_size: int
    file_offset: int

    def image(self, size: tuple[int, int]) -> Image.Image:
        """Return an independent PIL image for this packed frame."""
        return Image.frombytes("RGB", size, self.rgb)


@dataclass(frozen=True, slots=True)
class AnimSection:
    """An inclusive range in the expanded display-frame timeline."""

    name: str
    first_display_frame: int
    last_display_frame: int
    frame_offset: int
    duration_override: int

    @property
    def display_frame_count(self) -> int:
        return self.last_display_frame - self.first_display_frame + 1


@dataclass(frozen=True, slots=True)
class DecodedAnim:
    """Validated native animation data suitable for deterministic playback."""

    width: int
    height: int
    fps: int
    frames: tuple[AnimFileFrame, ...]
    sections: tuple[AnimSection, ...]
    display_frame_count: int
    flags: int
    color_format: int
    max_encoded_frame_size: int
    source_size: int

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def duration_seconds(self) -> float:
        return self.display_frame_count / self.fps

    def section(self, name: str = "default") -> AnimSection:
        """Return a named section, raising ``KeyError`` when it is absent."""
        for section in self.sections:
            if section.name == name:
                return section
        raise KeyError(name)

    def iter_display_rgb(self, section: str = "default") -> Iterator[bytes]:
        """Yield expanded RGB frames for ``section`` without copying pixels."""
        selected = self.section(section)
        cursor = 0
        for frame in self.frames:
            frame_end = cursor + frame.duration - 1
            first = max(cursor, selected.first_display_frame)
            last = min(frame_end, selected.last_display_frame)
            for _ in range(max(0, last - first + 1)):
                yield frame.rgb
            cursor = frame_end + 1
            if cursor > selected.last_display_frame:
                break

    def iter_display_frames(
        self, section: str = "default",
    ) -> Iterator[Image.Image]:
        """Yield independent PIL images for every display tick in a section."""
        for rgb in self.iter_display_rgb(section):
            yield Image.frombytes("RGB", self.size, rgb)

    def section_duration_seconds(self, name: str = "default") -> float:
        return self.section(name).display_frame_count / self.fps


DEFAULT_DECODE_LIMITS = AnimDecodeLimits()


def _rle_compress(source: bytes, blk_size: int) -> bytes:
    src_i = 0
    src_len = len(source)
    dest = bytearray()
    while src_i < src_len:
        repeat_count = 0
        for i in range(src_i, src_len, blk_size):
            if source[i:i + blk_size] == source[src_i:src_i + blk_size]:
                repeat_count += 1
            else:
                break
        repeat_count = min(repeat_count, _MAX_BLOCKS)
        if repeat_count == 0:
            break
        if repeat_count < _RLE_THRESHOLD:
            repeat_count = 0
            verbatim_count = 0
            for i in range(src_i, src_len, blk_size):
                if source[i:i + blk_size] == source[i + blk_size:i + blk_size * 2]:
                    repeat_count += 1
                    if repeat_count > _RLE_THRESHOLD:
                        break
                else:
                    verbatim_count += 1 + repeat_count
                    repeat_count = 0
            verbatim_count += repeat_count
            verbatim_count = min(verbatim_count, _MAX_BLOCKS)
            dest.append(0x80 | verbatim_count)
            dest.extend(source[src_i:src_i + verbatim_count * blk_size])
            src_i += verbatim_count * blk_size
        else:
            dest.append(repeat_count)
            dest.extend(source[src_i:src_i + blk_size])
            src_i += repeat_count * blk_size
    return bytes(dest)


def _decode_rle_bgr(source: bytes, expected_size: int) -> bytes:
    """Decode firmware RLE while refusing malformed or oversized output."""
    out = bytearray()
    cursor = 0
    while cursor < len(source):
        control = source[cursor]
        cursor += 1
        count = control & 0x7F if control & 0x80 else control
        if count == 0:
            raise AnimDecodeError("RLE block has a zero repeat count")
        if control & 0x80:
            byte_count = count * 3
            end = cursor + byte_count
            if end > len(source):
                raise AnimDecodeError("truncated RLE literal block")
            if len(out) + byte_count > expected_size:
                raise AnimDecodeError("RLE frame expands beyond its dimensions")
            out.extend(source[cursor:end])
            cursor = end
        else:
            end = cursor + 3
            if end > len(source):
                raise AnimDecodeError("truncated RLE repeat block")
            byte_count = count * 3
            if len(out) + byte_count > expected_size:
                raise AnimDecodeError("RLE frame expands beyond its dimensions")
            out.extend(source[cursor:end] * count)
            cursor = end
    if len(out) != expected_size:
        raise AnimDecodeError(
            f"RLE frame expands to {len(out)} bytes, expected {expected_size}"
        )
    return bytes(out)


def _bgr_to_rgb(source: bytes) -> bytes:
    out = bytearray(len(source))
    out[0::3] = source[2::3]
    out[1::3] = source[1::3]
    out[2::3] = source[0::3]
    return bytes(out)


def _unpack_from(fmt: str, source: bytes, offset: int, what: str) -> tuple:
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(source):
        raise AnimDecodeError(f"truncated {what}")
    return struct.unpack_from(fmt, source, offset)


def decode_anim(
    source: bytes | bytearray | memoryview,
    *,
    limits: AnimDecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodedAnim:
    """Decode and validate a native BUSY Bar ``.anim`` blob.

    The returned packed frames retain native durations while exposing pixels
    in ordinary RGB order.  ``iter_display_rgb`` and ``iter_display_frames``
    expand those durations lazily, including sections that begin part-way
    through a packed frame.

    Files are treated as untrusted input: chunk lengths, frame counts, RLE
    output, section offsets, and conceptual expanded playback are all checked
    against both internal metadata and ``limits`` before returning.
    """
    if not isinstance(source, (bytes, bytearray, memoryview)):
        raise TypeError("source must be bytes-like")
    data = bytes(source)
    if len(data) > limits.max_source_bytes:
        raise AnimDecodeBudgetError(
            f"animation is {len(data)} bytes; budget is "
            f"{limits.max_source_bytes}"
        )

    header_size = struct.calcsize(_HEADER_FMT)
    (
        signature,
        flags,
        width,
        height,
        color_format,
        fps,
        max_encoded_size,
        _unused,
        sections_chunk_size,
        frames_chunk_size,
        section_count,
        file_frame_count,
        display_frame_count,
    ) = _unpack_from(_HEADER_FMT, data, 0, "animation header")

    if signature != _SIGNATURE:
        raise AnimDecodeError("invalid animation signature")
    if flags != 0:
        raise AnimDecodeError(f"unsupported animation flags: {flags}")
    if color_format != _COLOR_BGR888:
        raise AnimDecodeError(f"unsupported animation color format: {color_format}")
    if width == 0 or height == 0:
        raise AnimDecodeError("animation dimensions must be non-zero")
    if fps == 0:
        raise AnimDecodeError("animation FPS must be non-zero")
    if section_count == 0:
        raise AnimDecodeError("animation must contain a default section")
    if file_frame_count == 0 or display_frame_count == 0:
        raise AnimDecodeError("animation must contain at least one frame")
    if section_count > limits.max_sections:
        raise AnimDecodeBudgetError(
            f"animation has {section_count} sections; budget is "
            f"{limits.max_sections}"
        )
    if file_frame_count > limits.max_file_frames:
        raise AnimDecodeBudgetError(
            f"animation has {file_frame_count} file frames; budget is "
            f"{limits.max_file_frames}"
        )
    if display_frame_count > limits.max_display_frames:
        raise AnimDecodeBudgetError(
            f"animation has {display_frame_count} display frames; budget is "
            f"{limits.max_display_frames}"
        )

    rgb_size = width * height * 3
    expanded_size = rgb_size * display_frame_count
    if expanded_size > limits.max_expanded_rgb_bytes:
        raise AnimDecodeBudgetError(
            f"expanded animation is {expanded_size} RGB bytes; budget is "
            f"{limits.max_expanded_rgb_bytes}"
        )

    sections_start = header_size
    sections_end = sections_start + sections_chunk_size
    frames_end = sections_end + frames_chunk_size
    if sections_end > len(data) or frames_end > len(data):
        raise AnimDecodeError("animation chunks extend beyond the input")
    if frames_end != len(data):
        raise AnimDecodeError("animation has trailing bytes outside its chunks")

    section_rows: list[tuple[str, int, int, int, int]] = []
    cursor = sections_start
    section_header_size = struct.calcsize(_SECTION_FMT)
    names: set[str] = set()
    for index in range(section_count):
        if cursor + section_header_size > sections_end:
            raise AnimDecodeError(f"truncated section header {index}")
        first, last, frame_offset, duration_override = _unpack_from(
            _SECTION_FMT, data, cursor, f"section header {index}"
        )
        cursor += section_header_size
        nul = data.find(b"\x00", cursor, sections_end)
        if nul < 0:
            raise AnimDecodeError(f"section {index} has no terminating NUL")
        name_bytes = data[cursor:nul]
        if not name_bytes:
            raise AnimDecodeError(f"section {index} has an empty name")
        if len(name_bytes) > limits.max_section_name_bytes:
            raise AnimDecodeBudgetError(
                f"section {index} name is {len(name_bytes)} bytes; budget is "
                f"{limits.max_section_name_bytes}"
            )
        try:
            name = name_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise AnimDecodeError(
                f"section {index} name is not valid UTF-8"
            ) from exc
        if name in names:
            raise AnimDecodeError(f"duplicate animation section: {name}")
        names.add(name)
        if first > last or last >= display_frame_count:
            raise AnimDecodeError(
                f"section {name!r} range {first}..{last} is outside "
                f"0..{display_frame_count - 1}"
            )
        section_rows.append(
            (name, first, last, frame_offset, duration_override)
        )
        cursor = nul + 1
    if cursor != sections_end:
        raise AnimDecodeError("section chunk length/count mismatch")

    frames: list[AnimFileFrame] = []
    cursor = sections_end
    frame_header_size = struct.calcsize(_FRAME_FMT)
    for index in range(file_frame_count):
        file_offset = cursor
        if cursor + frame_header_size > frames_end:
            raise AnimDecodeError(f"truncated frame header {index}")
        encoding, duration, encoded_size = _unpack_from(
            _FRAME_FMT, data, cursor, f"frame header {index}"
        )
        cursor += frame_header_size
        if duration == 0:
            raise AnimDecodeError(f"frame {index} has zero duration")
        if encoded_size == 0:
            raise AnimDecodeError(f"frame {index} has no pixel data")
        if encoded_size > max_encoded_size:
            raise AnimDecodeError(
                f"frame {index} is larger than header max_encoded_frame_size"
            )
        end = cursor + encoded_size
        if end > frames_end:
            raise AnimDecodeError(f"truncated frame data {index}")
        encoded = data[cursor:end]
        cursor = end
        if encoding == 0:
            if encoded_size != rgb_size:
                raise AnimDecodeError(
                    f"raw frame {index} has {encoded_size} bytes, "
                    f"expected {rgb_size}"
                )
            bgr = encoded
            encoding_name: Literal["raw", "rle"] = "raw"
        elif encoding == 1:
            bgr = _decode_rle_bgr(encoded, rgb_size)
            encoding_name = "rle"
        else:
            raise AnimDecodeError(
                f"frame {index} uses unsupported encoding {encoding}"
            )
        frames.append(AnimFileFrame(
            rgb=_bgr_to_rgb(bgr),
            duration=duration,
            encoding=encoding_name,
            encoded_size=encoded_size,
            file_offset=file_offset,
        ))
    if cursor != frames_end:
        raise AnimDecodeError("frame chunk length/count mismatch")

    actual_display_frames = sum(frame.duration for frame in frames)
    if actual_display_frames != display_frame_count:
        raise AnimDecodeError(
            "display frame count does not match packed frame durations"
        )
    actual_max_encoded = max(frame.encoded_size for frame in frames)
    if actual_max_encoded != max_encoded_size:
        raise AnimDecodeError(
            "max encoded frame size does not match frame data"
        )

    sections: list[AnimSection] = []
    for name, first, last, frame_offset, duration_override in section_rows:
        display_cursor = 0
        expected_frame: AnimFileFrame | None = None
        expected_override = 0
        for frame in frames:
            if display_cursor <= first < display_cursor + frame.duration:
                expected_frame = frame
                expected_override = frame.duration - (first - display_cursor)
                break
            display_cursor += frame.duration
        if expected_frame is None:  # guarded by the range check above
            raise AnimDecodeError(f"section {name!r} has no starting frame")
        if frame_offset != expected_frame.file_offset:
            raise AnimDecodeError(
                f"section {name!r} points at the wrong file frame"
            )
        if duration_override != expected_override:
            raise AnimDecodeError(
                f"section {name!r} has an invalid duration override"
            )
        sections.append(AnimSection(
            name=name,
            first_display_frame=first,
            last_display_frame=last,
            frame_offset=frame_offset,
            duration_override=duration_override,
        ))

    try:
        default = next(section for section in sections if section.name == "default")
    except StopIteration as exc:
        raise AnimDecodeError("animation has no default section") from exc
    if (
        default.first_display_frame != 0
        or default.last_display_frame != display_frame_count - 1
    ):
        raise AnimDecodeError("default section does not cover the full animation")

    return DecodedAnim(
        width=width,
        height=height,
        fps=fps,
        frames=tuple(frames),
        sections=tuple(sections),
        display_frame_count=display_frame_count,
        flags=flags,
        color_format=color_format,
        max_encoded_frame_size=max_encoded_size,
        source_size=len(data),
    )


def encode_anim(frames: list[Image.Image], fps: int,
                sections: list[tuple[str, int, int]] | None = None,
                durations: list[int] | None = None) -> bytes:
    """Encode PIL frames into a looping BGR888 .anim blob.

    `sections` are (name, first_display_frame, last_display_frame) ranges
    the device can play individually; "default" (everything) is always
    included first.
    """
    if not frames:
        raise ValueError("need at least one frame")
    width, height = frames[0].size

    # Pack frames, folding consecutive identical ones into a longer duration
    packed_frames: list[list] = []  # [encoding, duration, encoded_bytes]
    last_rgb: bytes | None = None
    for fi, img in enumerate(frames):
        if img.size != (width, height):
            raise ValueError("all frames must be the same size")
        rgb = img.convert("RGB").tobytes()
        if durations is None and rgb == last_rgb:
            packed_frames[-1][1] += 1
            continue
        last_rgb = rgb
        buf = bytearray()
        for i in range(0, len(rgb), 3):
            buf.extend((rgb[i + 2], rgb[i + 1], rgb[i]))
        bgr = bytes(buf)
        dur = min(255, durations[fi]) if durations else 1
        rle = _rle_compress(bgr, 3)
        if len(rle) < len(bgr):
            packed_frames.append([1, dur, rle])
        else:
            packed_frames.append([0, dur, bgr])

    frame_headers_len = struct.calcsize(_FRAME_FMT)
    frames_chunk_len = sum(frame_headers_len + len(e) for _, _, e in packed_frames)
    max_encoded_len = max(len(e) for _, _, e in packed_frames)
    display_frame_count = sum(d for _, d, _ in packed_frames)

    all_sections = [("default", 0, display_frame_count - 1)]
    all_sections += list(sections or [])
    sections_chunk_len = sum(
        struct.calcsize(_SECTION_FMT) + len(name.encode()) + 1
        for name, _s, _e in all_sections)
    header_len = struct.calcsize(_HEADER_FMT)

    # Map each display frame to (file offset of its file frame, remaining
    # duration at that point) — mirrors the firmware's seq2anim
    frames_base = header_len + sections_chunk_len
    display_start = []
    offs = frames_base
    for _encoding, duration, encoded in packed_frames:
        for disp_off in range(duration, 0, -1):
            display_start.append((offs, disp_off))
        offs += struct.calcsize(_FRAME_FMT) + len(encoded)

    out = bytearray()
    out += struct.pack(
        _HEADER_FMT,
        _SIGNATURE,
        0,  # flags
        width,
        height,
        _COLOR_BGR888,
        fps,
        max_encoded_len,
        0,  # unused
        sections_chunk_len,
        frames_chunk_len,
        len(all_sections),
        len(packed_frames),
        display_frame_count,
    )
    for name, s, e in all_sections:
        frame_offs, dur_override = display_start[s]
        out += struct.pack(_SECTION_FMT, s, e, frame_offs, dur_override)
        out += name.encode() + b"\x00"
    for encoding, duration, encoded in packed_frames:
        out += struct.pack(_FRAME_FMT, encoding, duration, len(encoded)) + encoded
    return bytes(out)

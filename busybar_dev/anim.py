"""Encoder and decoder for the BUSY Bar ``.anim`` container ("bicycle1").

Ported from the official firmware tooling (busy-app/busybar-firmware,
``scripts/seq2anim.py``, ``scripts/flipper/rle.py``, and
``lib/anim_file/anim_file_format.h`` at upstream revision
8edd2da42ed2facbab73796c456746e7dcbed2e7, the 1.3.1 release).

Those portions are Copyright 2024-2026 Flipper FZCO and licensed
GPL-2.0-or-later. This repository distributes its combined adaptation under the
same GPL-2.0-or-later terms; see LICENSE and NOTICE.md. Produces BGR888 animations the device
plays natively via an ``animation`` display element.

Firmware 1.3.1 replaced the ``bicycle0`` container with ``bicycle1``. Every
frame is now one display tick. Each frame carries a change mask against the
previous frame plus only the pixels under that mask, encoded raw, with the
firmware's RLE, or with a header-less QOI variant. A frame identical to its
predecessor costs five bytes. The first frame of every section is a keyframe
(full mask), so the device can start playback there. The 1.3.1 loader
rejects ``bicycle0`` outright: the draw request still returns 200, but the
panel shows the firmware's broken-asset placeholder instead of the scene.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, fields
from typing import Literal

from PIL import Image

_SIGNATURE = b"bicycle1"
_LEGACY_SIGNATURE = b"bicycle0"
_HEADER_FMT = "<8s BBBB HHBB II II"
_SECTION_FMT = "<III"
_FRAME_FMT = "<BHH"
_COLOR_BGR888 = 0
_MAX_BLOCKS = 127
_RLE_THRESHOLD = 3

# AnimFileMaskEncoding / AnimFilePixelEncoding, lib/anim_file/anim_file_format.h
_MASK_NONE, _MASK_FULL, _MASK_RLE_FIRST_BLACK, _MASK_RLE_FIRST_WHITE, _MASK_BITMAP = range(5)
_PIXELS_RAW, _PIXELS_RLE, _PIXELS_QOI = range(3)
_MASK_NAMES: dict[int, MaskEncoding] = {
    _MASK_NONE: "none", _MASK_FULL: "full", _MASK_RLE_FIRST_BLACK: "rle",
    _MASK_RLE_FIRST_WHITE: "rle", _MASK_BITMAP: "bitmap",
}
_PIXEL_NAMES: dict[int, PixelEncoding] = {
    _PIXELS_RAW: "raw", _PIXELS_RLE: "rle", _PIXELS_QOI: "qoi",
}

# Binary run-length mask: 3-bit runs, a 7 escapes to an 8-bit run.
_SHORT_RUN_BITS = 3
_LONG_RUN_BITS = 8
_LONG_RUN_MARKER = (1 << _SHORT_RUN_BITS) - 1
_LONG_RUN_MAX = (1 << _LONG_RUN_BITS) - 1
# The vendor only keeps the run-length mask when it beats a bitmap by a
# quarter; otherwise the bitmap decodes faster for the same size.
_MASK_RLE_WORTH_IT = 0.75

_QOI_OP_RGB = 0xFE
_QOI_OP_RGBA = 0xFF
_QOI_TAG_MASK = 0xC0
_QOI_OP_INDEX = 0x00
_QOI_OP_DIFF = 0x40
_QOI_OP_LUMA = 0x80
_QOI_OP_RUN = 0xC0
_QOI_RUN_BYTE_LIMIT = 0b11_111110  # 0xFE/0xFF are the RGB/RGBA opcodes
_QOI_FIRST_PIXEL = b"\x00\x00\x00\xff"

MaskEncoding = Literal["none", "full", "rle", "bitmap"]
PixelEncoding = Literal["raw", "rle", "qoi"]


class AnimDecodeError(ValueError):
    """The input is not a supported, internally consistent ``.anim`` file."""


class AnimDecodeBudgetError(AnimDecodeError):
    """The input exceeds a caller-controlled decoding work budget."""


@dataclass(frozen=True, slots=True)
class AnimDecodeLimits:
    """Hard bounds for decoding untrusted animation files.

    ``max_expanded_rgb_bytes`` covers the composed playback timeline, not
    merely the packed file bytes.  A tiny file can otherwise claim thousands
    of unchanged frames and make a later visualizer allocate gigabytes while
    expanding it.
    """

    max_source_bytes: int = 32 * 1024 * 1024
    max_sections: int = 1024
    max_section_name_bytes: int = 255
    max_frames: int = 100_000
    max_expanded_rgb_bytes: int = 256 * 1024 * 1024

    def __post_init__(self) -> None:
        for field in fields(self):
            if getattr(self, field.name) <= 0:
                raise ValueError(f"{field.name} must be positive")


@dataclass(frozen=True, slots=True)
class AnimFileFrame:
    """One file frame, composed into the full RGB888 picture it displays.

    ``rgb`` is canonical row-major RGB888 of the whole frame after applying
    this frame's change mask on top of its predecessor; the BGR wire order is
    deliberately removed at this boundary so callers cannot accidentally
    preview red as blue.  ``changed_pixels`` and ``encoded_size`` describe
    what the file actually carried for the frame.
    """

    rgb: bytes
    mask_encoding: MaskEncoding
    pixel_encoding: PixelEncoding
    changed_pixels: int
    mask_bits: int
    encoded_size: int
    file_offset: int

    def image(self, size: tuple[int, int]) -> Image.Image:
        """Return an independent PIL image for this frame."""
        return Image.frombytes("RGB", size, self.rgb)


@dataclass(frozen=True, slots=True)
class AnimSection:
    """An inclusive range of frames the device can play on its own."""

    name: str
    first_display_frame: int
    last_display_frame: int
    frame_offset: int

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
    flags: int
    color_format: int
    max_mask_bits: int
    max_pixel_bytes: int
    source_size: int

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    @property
    def display_frame_count(self) -> int:
        return len(self.frames)

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
        """Yield composed RGB frames for ``section`` without copying pixels."""
        selected = self.section(section)
        for frame in self.frames[
            selected.first_display_frame:selected.last_display_frame + 1
        ]:
            yield frame.rgb

    def iter_display_frames(
        self, section: str = "default",
    ) -> Iterator[Image.Image]:
        """Yield independent PIL images for every display tick in a section."""
        for rgb in self.iter_display_rgb(section):
            yield Image.frombytes("RGB", self.size, rgb)

    def section_duration_seconds(self, name: str = "default") -> float:
        return self.section(name).display_frame_count / self.fps


DEFAULT_DECODE_LIMITS = AnimDecodeLimits()


# --- firmware RLE (toolbox/rle_encode) -------------------------------------

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


def _rle_decompress(source: bytes, expected_size: int) -> bytes:
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
                raise AnimDecodeError("RLE frame expands beyond its masked pixels")
            out.extend(source[cursor:end])
            cursor = end
        else:
            end = cursor + 3
            if end > len(source):
                raise AnimDecodeError("truncated RLE repeat block")
            byte_count = count * 3
            if len(out) + byte_count > expected_size:
                raise AnimDecodeError("RLE frame expands beyond its masked pixels")
            out.extend(source[cursor:end] * count)
            cursor = end
    if len(out) != expected_size:
        raise AnimDecodeError(
            f"RLE frame expands to {len(out)} bytes, expected {expected_size}"
        )
    return bytes(out)


# --- header-less QOI (lib/anim_file/components/anim_file_img.c) -------------

def _qoi_hash(pixel: bytes) -> int:
    r, g, b, a = pixel
    return (r * 3 + g * 5 + b * 7 + a * 11) % 64


def _qoi_compress(rgba: bytes) -> bytes:
    """Mirror ``qoi_headerless_compress`` from the vendor's seq2anim.py.

    The firmware seeds its decoder with an opaque black "previous pixel" and
    an all-zero hash table, so a frame is never self-describing; both sides
    must agree on those seeds, which is why this is a port and not a QOI
    library call.
    """
    lut = [b"\x00\x00\x00\x00"] * 64
    out = bytearray()
    last = _QOI_FIRST_PIXEL
    last_opcode_is_run = False
    for i in range(0, len(rgba), 4):
        px = rgba[i:i + 4]
        if px == last:
            extended = False
            if last_opcode_is_run:
                new_run = out[-1] + 1
                if new_run < _QOI_RUN_BYTE_LIMIT:
                    out[-1] = new_run
                    extended = True
            if not extended:
                out.append(_QOI_OP_RUN)
            lut[_qoi_hash(px)] = px
            last_opcode_is_run = True
            last = px
            continue
        last_opcode_is_run = False
        slot = _qoi_hash(px)
        if lut[slot] == px:
            out.append(_QOI_OP_INDEX | slot)
            last = px
            continue
        dr = px[0] - last[0]
        dg = px[1] - last[1]
        db = px[2] - last[2]
        same_alpha = px[3] == last[3]
        if same_alpha and -2 <= dr < 2 and -2 <= dg < 2 and -2 <= db < 2:
            out.append(_QOI_OP_DIFF | ((dr + 2) << 4) | ((dg + 2) << 2) | (db + 2))
        elif same_alpha and -32 <= dg < 32 and -8 <= dr - dg < 8 and -8 <= db - dg < 8:
            out.append(_QOI_OP_LUMA | (dg + 32))
            out.append(((dr - dg + 8) << 4) | (db - dg + 8))
        elif same_alpha:
            out.append(_QOI_OP_RGB)
            out.extend(px[:3])
        else:
            out.append(_QOI_OP_RGBA)
            out.extend(px)
        lut[slot] = px
        last = px
    return bytes(out)


def _qoi_decompress(source: bytes, expected_pixels: int, index: int) -> bytes:
    """Decode the masked pixels of frame ``index`` to RGB888."""
    lut: list[tuple[int, int, int, int]] = [(0, 0, 0, 0)] * 64
    last = (0, 0, 0, 255)
    out = bytearray()
    count = 0

    def emit(px: tuple[int, int, int, int]) -> None:
        nonlocal last, count
        lut[(px[0] * 3 + px[1] * 5 + px[2] * 7 + px[3] * 11) % 64] = px
        last = px
        count += 1
        if count > expected_pixels:
            raise AnimDecodeError(
                f"QOI frame {index} decodes more than its {expected_pixels} "
                "masked pixels"
            )
        out.extend(px[:3])

    i = 0
    size = len(source)
    while i < size:
        tag = source[i]
        if tag == _QOI_OP_RGB:
            if size - i < 4:
                raise AnimDecodeError(f"truncated QOI data in frame {index}")
            emit((source[i + 1], source[i + 2], source[i + 3], last[3]))
            i += 4
        elif tag == _QOI_OP_RGBA:
            if size - i < 5:
                raise AnimDecodeError(f"truncated QOI data in frame {index}")
            emit((source[i + 1], source[i + 2], source[i + 3], source[i + 4]))
            i += 5
        else:
            short = tag & _QOI_TAG_MASK
            payload = tag & ~_QOI_TAG_MASK & 0xFF
            if short == _QOI_OP_INDEX:
                emit(lut[payload])
                i += 1
            elif short == _QOI_OP_DIFF:
                dr = ((payload >> 4) & 0x3) - 2
                dg = ((payload >> 2) & 0x3) - 2
                db = (payload & 0x3) - 2
                emit((
                    (last[0] + dr) & 0xFF, (last[1] + dg) & 0xFF,
                    (last[2] + db) & 0xFF, last[3],
                ))
                i += 1
            elif short == _QOI_OP_LUMA:
                if size - i < 2:
                    raise AnimDecodeError(f"truncated QOI data in frame {index}")
                dg = (payload & 0x3F) - 32
                dr = ((source[i + 1] >> 4) & 0xF) - 8 + dg
                db = (source[i + 1] & 0xF) - 8 + dg
                emit((
                    (last[0] + dr) & 0xFF, (last[1] + dg) & 0xFF,
                    (last[2] + db) & 0xFF, last[3],
                ))
                i += 2
            else:  # _QOI_OP_RUN
                for _ in range(payload + 1):
                    emit(last)
                i += 1
    if count != expected_pixels:
        raise AnimDecodeError(
            f"QOI frame {index} decodes {count} pixels for {expected_pixels} "
            "masked pixels"
        )
    return bytes(out)


# --- change masks (lib/anim_file/components/anim_file_mask.c) ---------------

class _BitWriter:
    """MSB-first bit packer matching the vendor's ``BitQueue``."""

    def __init__(self) -> None:
        self.value = 0
        self.bits = 0

    def push(self, value: int, count: int) -> None:
        self.value = (self.value << count) | (value & ((1 << count) - 1))
        self.bits += count

    def finish(self) -> tuple[bytes, int]:
        padded = self.value << (-self.bits % 8)
        return padded.to_bytes((self.bits + 7) // 8, "big"), self.bits


def _encode_mask(mask: Sequence[bool]) -> tuple[int, bytes, int]:
    """Return ``(encoding, data, bit_count)`` for a per-pixel change mask."""
    first = mask[0]
    runs: list[int] = []
    run_value = first
    run_length = 0
    for px in mask:
        if px == run_value:
            run_length += 1
        else:
            runs.append(run_length)
            run_value = px
            run_length = 1
    runs.append(run_length)
    if len(runs) == 1:
        return (_MASK_FULL if first else _MASK_NONE), b"", 0

    writer = _BitWriter()
    for length in runs:
        if length >= _LONG_RUN_MARKER:
            while length >= _LONG_RUN_MAX:
                writer.push(_LONG_RUN_MARKER, _SHORT_RUN_BITS)
                writer.push(_LONG_RUN_MAX, _LONG_RUN_BITS)
                length -= _LONG_RUN_MAX
                writer.push(0, _SHORT_RUN_BITS)
            writer.push(_LONG_RUN_MARKER, _SHORT_RUN_BITS)
            writer.push(length, _LONG_RUN_BITS)
        else:
            writer.push(length, _SHORT_RUN_BITS)
    data, bits = writer.finish()
    if bits <= len(mask) * _MASK_RLE_WORTH_IT:
        return (_MASK_RLE_FIRST_WHITE if first else _MASK_RLE_FIRST_BLACK), data, bits

    bitmap = bytearray()
    for i in range(0, len(mask), 8):
        byte = 0
        for j in range(min(8, len(mask) - i)):
            if mask[i + j]:
                byte |= 128 >> j
        bitmap.append(byte)
    return _MASK_BITMAP, bytes(bitmap), len(mask)


def _read_bits(data: bytes, position: int, count: int) -> int:
    value = 0
    for k in range(position, position + count):
        value = (value << 1) | ((data[k >> 3] >> (7 - (k & 7))) & 1)
    return value


def _decode_mask(
    encoding: int, data: bytes, bits: int, pixel_count: int, index: int,
) -> list[int]:
    """Return the indices of the pixels frame ``index`` replaces."""
    if encoding == _MASK_NONE:
        return []
    if encoding == _MASK_FULL:
        return list(range(pixel_count))
    if encoding == _MASK_BITMAP:
        if bits != pixel_count:
            raise AnimDecodeError(
                f"frame {index} bitmap mask must cover every pixel"
            )
        return [k for k in range(bits) if data[k >> 3] & (128 >> (k & 7))]

    changed: list[int] = []
    is_white = encoding == _MASK_RLE_FIRST_WHITE
    cursor = 0
    position = 0
    while position < bits:
        if position + _SHORT_RUN_BITS > bits:
            raise AnimDecodeError(f"frame {index} run-length mask is truncated")
        run = _read_bits(data, position, _SHORT_RUN_BITS)
        position += _SHORT_RUN_BITS
        if run == _LONG_RUN_MARKER:
            if position + _LONG_RUN_BITS > bits:
                raise AnimDecodeError(f"frame {index} run-length mask is truncated")
            run = _read_bits(data, position, _LONG_RUN_BITS)
            position += _LONG_RUN_BITS
        if is_white:
            changed.extend(range(cursor, cursor + run))
        cursor += run
        is_white = not is_white
    if cursor != pixel_count:
        raise AnimDecodeError(
            f"frame {index} run-length mask does not cover the frame exactly"
        )
    return changed


# --- pixel packing ----------------------------------------------------------

def _opaque_rgba(image: Image.Image) -> bytes:
    """RGBA bytes with alpha forced opaque, as the vendor's rgb888 mode does."""
    rgb = image.convert("RGB").tobytes()
    out = bytearray(len(rgb) // 3 * 4)
    out[0::4] = rgb[0::3]
    out[1::4] = rgb[1::3]
    out[2::4] = rgb[2::3]
    out[3::4] = b"\xff" * (len(rgb) // 3)
    return bytes(out)


def _pack_bgr888(rgba: bytes) -> bytes:
    out = bytearray(len(rgba) // 4 * 3)
    out[0::3] = rgba[2::4]
    out[1::3] = rgba[1::4]
    out[2::3] = rgba[0::4]
    return bytes(out)


def _bgr_to_rgb(source: bytes) -> bytes:
    out = bytearray(len(source))
    out[0::3] = source[2::3]
    out[1::3] = source[1::3]
    out[2::3] = source[0::3]
    return bytes(out)


def _subtract(rgba: bytes, previous: bytes | None) -> tuple[bytes, list[bool]]:
    """Return the pixels that differ from ``previous`` and their mask."""
    pixel_count = len(rgba) // 4
    if previous is None:
        return rgba, [True] * pixel_count
    if rgba == previous:
        return b"", [False] * pixel_count
    new = memoryview(rgba).cast("I")
    old = memoryview(previous).cast("I")
    mask = [a != b for a, b in zip(new, old)]
    changed = b"".join(rgba[i * 4:i * 4 + 4] for i, hit in enumerate(mask) if hit)
    return changed, mask


def _choose_pixel_encoding(
    packed: bytes, unpacked: bytes, delta: bool,
) -> tuple[int, bytes]:
    # Vendor order: raw, RLE, then QOI for delta frames only. ``min`` keeps
    # the first candidate on a tie, which is what makes the output byte-exact.
    candidates = [(_PIXELS_RAW, packed), (_PIXELS_RLE, _rle_compress(packed, 3))]
    if delta:
        candidates.append((_PIXELS_QOI, _qoi_compress(unpacked)))
    return min(candidates, key=lambda candidate: len(candidate[1]))


def _unpack_from(fmt: str, source: bytes, offset: int, what: str) -> tuple:
    size = struct.calcsize(fmt)
    if offset < 0 or offset + size > len(source):
        raise AnimDecodeError(f"truncated {what}")
    return struct.unpack_from(fmt, source, offset)


# --- decode -----------------------------------------------------------------

def decode_anim(
    source: bytes | bytearray | memoryview,
    *,
    limits: AnimDecodeLimits = DEFAULT_DECODE_LIMITS,
) -> DecodedAnim:
    """Decode and validate a native BUSY Bar ``.anim`` blob.

    Every frame comes back composed: its change mask is applied on top of
    the previous frame, so ``iter_display_rgb`` yields what the panel shows
    for each tick.  Sections are exposed as inclusive frame ranges.

    Files are treated as untrusted input: chunk lengths, frame counts, mask
    coverage, RLE and QOI output, section offsets, and the composed playback
    size are all checked against both internal metadata and ``limits``
    before returning.
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
        max_mask_bits,
        max_pixel_bytes,
        fps,
        _unused,
        sections_chunk_size,
        frames_chunk_size,
        section_count,
        frame_count,
    ) = _unpack_from(_HEADER_FMT, data, 0, "animation header")

    if signature == _LEGACY_SIGNATURE:
        raise AnimDecodeError(
            "legacy bicycle0 animation signature; firmware 1.3.1 and later "
            "play only bicycle1"
        )
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
    if frame_count == 0:
        raise AnimDecodeError("animation must contain at least one frame")
    if section_count > limits.max_sections:
        raise AnimDecodeBudgetError(
            f"animation has {section_count} sections; budget is "
            f"{limits.max_sections}"
        )
    if frame_count > limits.max_frames:
        raise AnimDecodeBudgetError(
            f"animation has {frame_count} frames; budget is {limits.max_frames}"
        )

    pixel_count = width * height
    rgb_size = pixel_count * 3
    expanded_size = rgb_size * frame_count
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

    section_rows: list[tuple[str, int, int, int]] = []
    cursor = sections_start
    section_header_size = struct.calcsize(_SECTION_FMT)
    names: set[str] = set()
    for index in range(section_count):
        if cursor + section_header_size > sections_end:
            raise AnimDecodeError(f"truncated section header {index}")
        first, last, frame_offset = _unpack_from(
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
        if index == 0 and name != "default":
            raise AnimDecodeError("first animation section must be 'default'")
        if name in names:
            raise AnimDecodeError(f"duplicate animation section: {name}")
        names.add(name)
        if first > last or last >= frame_count:
            raise AnimDecodeError(
                f"section {name!r} range {first}..{last} is outside "
                f"0..{frame_count - 1}"
            )
        section_rows.append((name, first, last, frame_offset))
        cursor = nul + 1
    if cursor != sections_end:
        raise AnimDecodeError("section chunk length/count mismatch")

    frames: list[AnimFileFrame] = []
    cursor = sections_end
    frame_header_size = struct.calcsize(_FRAME_FMT)
    previous_rgb: bytes | None = None
    for index in range(frame_count):
        file_offset = cursor
        if cursor + frame_header_size > frames_end:
            raise AnimDecodeError(f"truncated frame header {index}")
        joint, mask_bits, pixel_size = _unpack_from(
            _FRAME_FMT, data, cursor, f"frame header {index}"
        )
        cursor += frame_header_size
        mask_code = joint >> 4
        pixel_code = joint & 0xF
        if mask_code > _MASK_BITMAP:
            raise AnimDecodeError(
                f"frame {index} uses unsupported mask encoding {mask_code}"
            )
        if pixel_code > _PIXELS_QOI:
            raise AnimDecodeError(
                f"frame {index} uses unsupported pixel encoding {pixel_code}"
            )
        if mask_code in (_MASK_NONE, _MASK_FULL):
            if mask_bits:
                raise AnimDecodeError(
                    f"frame {index} declares mask bits for a full or empty mask"
                )
        else:
            if mask_bits == 0:
                raise AnimDecodeError(f"frame {index} has an empty change mask")
            if mask_bits > max_mask_bits:
                raise AnimDecodeError(
                    f"frame {index} mask is larger than header max_mask_bits"
                )
        if pixel_size > max_pixel_bytes:
            raise AnimDecodeError(
                f"frame {index} is larger than header max_pixel_bytes"
            )
        mask_end = cursor + (mask_bits + 7) // 8
        pixel_end = mask_end + pixel_size
        if pixel_end > frames_end:
            raise AnimDecodeError(f"truncated frame data {index}")
        changed = _decode_mask(
            mask_code, data[cursor:mask_end], mask_bits, pixel_count, index,
        )
        encoded = data[mask_end:pixel_end]
        cursor = pixel_end

        changed_size = len(changed) * 3
        if pixel_code == _PIXELS_RAW:
            if pixel_size != changed_size:
                raise AnimDecodeError(
                    f"raw frame {index} has {pixel_size} bytes, "
                    f"expected {changed_size}"
                )
            rgb_changed = _bgr_to_rgb(encoded)
        elif pixel_code == _PIXELS_RLE:
            rgb_changed = _bgr_to_rgb(_rle_decompress(encoded, changed_size))
        else:
            rgb_changed = _qoi_decompress(encoded, len(changed), index)

        if mask_code == _MASK_FULL:
            rgb = rgb_changed
        else:
            composed = bytearray(previous_rgb) if previous_rgb else bytearray(rgb_size)
            for k, pixel in enumerate(changed):
                composed[pixel * 3:pixel * 3 + 3] = rgb_changed[k * 3:k * 3 + 3]
            rgb = bytes(composed)
        frames.append(AnimFileFrame(
            rgb=rgb,
            mask_encoding=_MASK_NAMES[mask_code],
            pixel_encoding=_PIXEL_NAMES[pixel_code],
            changed_pixels=len(changed),
            mask_bits=mask_bits,
            encoded_size=pixel_size,
            file_offset=file_offset,
        ))
        previous_rgb = rgb
    if cursor != frames_end:
        raise AnimDecodeError("frame chunk length/count mismatch")

    sections: list[AnimSection] = []
    for name, first, last, frame_offset in section_rows:
        if frame_offset != frames[first].file_offset:
            raise AnimDecodeError(
                f"section {name!r} points at the wrong file frame"
            )
        if frames[first].mask_encoding != "full":
            raise AnimDecodeError(f"section {name!r} must start on a keyframe")
        sections.append(AnimSection(
            name=name,
            first_display_frame=first,
            last_display_frame=last,
            frame_offset=frame_offset,
        ))

    default = sections[0]
    if default.first_display_frame != 0 or default.last_display_frame != frame_count - 1:
        raise AnimDecodeError("default section does not cover the full animation")

    return DecodedAnim(
        width=width,
        height=height,
        fps=fps,
        frames=tuple(frames),
        sections=tuple(sections),
        flags=flags,
        color_format=color_format,
        max_mask_bits=max_mask_bits,
        max_pixel_bytes=max_pixel_bytes,
        source_size=len(data),
    )


# --- encode -----------------------------------------------------------------

def encode_anim(frames: list[Image.Image], fps: int,
                sections: list[tuple[str, int, int]] | None = None,
                durations: list[int] | None = None) -> bytes:
    """Encode PIL frames into a looping BGR888 .anim blob.

    Every frame is one display tick at ``fps``.  ``durations`` repeats a
    frame for that many ticks; the repeats cost five bytes each on the wire.
    ``sections`` are (name, first_frame, last_frame) ranges the device can
    play individually; "default" (everything) is always included first, and
    the first frame of every section is written as a keyframe.
    """
    if not frames:
        raise ValueError("need at least one frame")
    if not 1 <= fps <= 255:
        raise ValueError("fps must be 1..255, one byte in the container")
    width, height = frames[0].size
    if not (1 <= width <= 255 and 1 <= height <= 255):
        raise ValueError("frame dimensions must be 1..255 pixels")
    if durations is not None:
        if len(durations) != len(frames):
            raise ValueError("durations must have one entry per frame")
        if any(duration <= 0 for duration in durations):
            raise ValueError("every duration must be at least one tick")

    timeline: list[bytes] = []
    for image, duration in zip(frames, durations or [1] * len(frames)):
        if image.size != (width, height):
            raise ValueError("all frames must be the same size")
        timeline.extend([_opaque_rgba(image)] * duration)
    frame_count = len(timeline)

    all_sections = [("default", 0, frame_count - 1)]
    names = {"default"}
    for name, first, last in sections or []:
        if name == "default":
            raise ValueError('section name "default" is reserved')
        if not name:
            raise ValueError("section names must be non-empty")
        if name in names:
            raise ValueError(f"duplicate section name {name!r}")
        if first < 0 or last >= frame_count:
            raise ValueError(
                f"section {name!r} range {first}..{last} is outside "
                f"0..{frame_count - 1}"
            )
        if first > last:
            raise ValueError(f"section {name!r} start {first} is after its end {last}")
        names.add(name)
        all_sections.append((name, first, last))
    keyframes = {first for _name, first, _last in all_sections}

    encoded_frames: list[bytes] = []
    max_mask_bits = 0
    max_pixel_bytes = 0
    previous: bytes | None = None
    for index, rgba in enumerate(timeline):
        reference = None if index in keyframes else previous
        changed_rgba, mask = _subtract(rgba, reference)
        pixel_code, pixel_bytes = _choose_pixel_encoding(
            _pack_bgr888(changed_rgba), changed_rgba, delta=reference is not None,
        )
        mask_code, mask_bytes, mask_bits = _encode_mask(mask)
        if len(pixel_bytes) > 0xFFFF or mask_bits > 0xFFFF:
            raise ValueError("frame data exceeds the container's 16-bit length fields")
        encoded_frames.append(
            struct.pack(_FRAME_FMT, pixel_code | (mask_code << 4), mask_bits, len(pixel_bytes))
            + mask_bytes + pixel_bytes
        )
        max_mask_bits = max(max_mask_bits, mask_bits)
        max_pixel_bytes = max(max_pixel_bytes, len(pixel_bytes))
        previous = rgba

    section_names = [name.encode("utf-8") + b"\x00" for name, _f, _l in all_sections]
    sections_chunk_len = sum(
        struct.calcsize(_SECTION_FMT) + len(encoded) for encoded in section_names
    )
    frame_offsets: list[int] = []
    offset = struct.calcsize(_HEADER_FMT) + sections_chunk_len
    for encoded in encoded_frames:
        frame_offsets.append(offset)
        offset += len(encoded)

    out = bytearray(struct.pack(
        _HEADER_FMT,
        _SIGNATURE,
        0,  # flags
        width,
        height,
        _COLOR_BGR888,
        max_mask_bits,
        max_pixel_bytes,
        fps,
        0,  # unused
        sections_chunk_len,
        sum(len(encoded) for encoded in encoded_frames),
        len(all_sections),
        frame_count,
    ))
    for (_name, first, last), encoded_name in zip(all_sections, section_names):
        out += struct.pack(_SECTION_FMT, first, last, frame_offsets[first])
        out += encoded_name
    for encoded in encoded_frames:
        out += encoded
    return bytes(out)

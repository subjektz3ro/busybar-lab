from __future__ import annotations

import struct
from dataclasses import replace

import pytest
from PIL import Image

from busybar_dev import anim


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (4, 2)) -> Image.Image:
    return Image.new("RGB", size, color)


def _colors(decoded: anim.DecodedAnim, section: str = "default") -> list[tuple[int, int, int]]:
    return [frame.getpixel((0, 0)) for frame in decoded.iter_display_frames(section)]


def _frame_start(blob: bytes) -> int:
    header = struct.unpack_from(anim._HEADER_FMT, blob, 0)
    return struct.calcsize(anim._HEADER_FMT) + header[8]


def _mutate_header(blob: bytes, index: int, value: int | bytes) -> bytes:
    fields = list(struct.unpack_from(anim._HEADER_FMT, blob, 0))
    fields[index] = value
    changed = bytearray(blob)
    struct.pack_into(anim._HEADER_FMT, changed, 0, *fields)
    return bytes(changed)


def test_raw_frame_decodes_bgr_wire_bytes_back_to_rgb():
    source = Image.new("RGB", (1, 1), (0x12, 0x34, 0x56))
    blob = anim.encode_anim([source], fps=7)

    # A one-pixel frame is smaller raw than RLE and exposes the native order.
    assert blob[-3:] == bytes((0x56, 0x34, 0x12))
    decoded = anim.decode_anim(blob)

    assert decoded.size == (1, 1)
    assert decoded.fps == 7
    assert decoded.frames[0].encoding == "raw"
    assert decoded.frames[0].rgb == bytes((0x12, 0x34, 0x56))
    assert list(decoded.iter_display_frames())[0].getpixel((0, 0)) == (
        0x12, 0x34, 0x56,
    )


def test_rle_frame_decodes_repeated_bgr_pixels_to_rgb():
    source = _solid((201, 37, 9))
    decoded = anim.decode_anim(anim.encode_anim([source], fps=5))

    assert decoded.frames[0].encoding == "rle"
    assert decoded.frames[0].encoded_size < len(decoded.frames[0].rgb)
    assert decoded.frames[0].image(decoded.size).tobytes() == source.tobytes()


def test_implicit_duplicate_frames_are_folded_then_expanded_lazily():
    red = _solid((255, 0, 0))
    blue = _solid((0, 0, 255))
    decoded = anim.decode_anim(anim.encode_anim([red, red.copy(), blue], fps=6))

    assert [frame.duration for frame in decoded.frames] == [2, 1]
    assert decoded.display_frame_count == 3
    assert decoded.duration_seconds == pytest.approx(0.5)
    assert _colors(decoded) == [(255, 0, 0), (255, 0, 0), (0, 0, 255)]


def test_explicit_durations_preserve_file_frames_even_when_pixels_match():
    amber = _solid((240, 130, 20))
    decoded = anim.decode_anim(
        anim.encode_anim([amber, amber.copy()], fps=10, durations=[2, 3])
    )

    assert len(decoded.frames) == 2
    assert [frame.duration for frame in decoded.frames] == [2, 3]
    assert decoded.display_frame_count == 5
    assert _colors(decoded) == [(240, 130, 20)] * 5


def test_named_section_can_begin_part_way_through_a_folded_frame():
    red = _solid((180, 0, 0))
    green = _solid((0, 180, 0))
    blob = anim.encode_anim(
        [red, red.copy(), green],
        fps=4,
        sections=[("transition", 1, 2), ("last", 2, 2)],
    )
    decoded = anim.decode_anim(blob)

    transition = decoded.section("transition")
    assert transition.display_frame_count == 2
    assert transition.frame_offset == decoded.frames[0].file_offset
    assert transition.duration_override == 1
    assert decoded.section_duration_seconds("transition") == pytest.approx(0.5)
    assert _colors(decoded, "transition") == [(180, 0, 0), (0, 180, 0)]
    assert _colors(decoded, "last") == [(0, 180, 0)]
    with pytest.raises(KeyError):
        decoded.section("missing")


@pytest.mark.parametrize("size", [(1, 1), (7, 3), (72, 16)])
def test_encode_decode_round_trip_preserves_every_rgb_pixel(size):
    width, height = size
    frames = []
    for phase in range(3):
        image = Image.new("RGBA", size)
        image.putdata([
            (
                (x * 37 + phase * 19) % 256,
                (y * 61 + phase * 23) % 256,
                (x * 11 + y * 17 + phase * 71) % 256,
                255,
            )
            for y in range(height)
            for x in range(width)
        ])
        frames.append(image)

    decoded = anim.decode_anim(
        anim.encode_anim(frames, fps=12, sections=[("middle", 1, 1)])
    )

    assert [frame.tobytes() for frame in decoded.iter_display_frames()] == [
        frame.convert("RGB").tobytes() for frame in frames
    ]
    assert next(decoded.iter_display_frames("middle")).tobytes() == (
        frames[1].convert("RGB").tobytes()
    )


@pytest.mark.parametrize("cut", [0, 1, struct.calcsize(anim._HEADER_FMT) - 1])
def test_truncated_header_is_rejected(cut):
    blob = anim.encode_anim([_solid((1, 2, 3))], fps=5)
    with pytest.raises(anim.AnimDecodeError, match="truncated animation header"):
        anim.decode_anim(blob[:cut])


def test_truncated_chunks_and_trailing_bytes_are_rejected():
    blob = anim.encode_anim([_solid((1, 2, 3))], fps=5)

    with pytest.raises(anim.AnimDecodeError, match="chunks extend"):
        anim.decode_anim(blob[:-1])
    with pytest.raises(anim.AnimDecodeError, match="trailing bytes"):
        anim.decode_anim(blob + b"\x00")


def test_bad_signature_and_unsupported_header_values_are_rejected():
    blob = anim.encode_anim([_solid((1, 2, 3))], fps=5)

    with pytest.raises(anim.AnimDecodeError, match="signature"):
        anim.decode_anim(_mutate_header(blob, 0, b"notanim!"))
    with pytest.raises(anim.AnimDecodeError, match="flags"):
        anim.decode_anim(_mutate_header(blob, 1, 1))
    with pytest.raises(anim.AnimDecodeError, match="color format"):
        anim.decode_anim(_mutate_header(blob, 4, 1))


def test_malformed_raw_and_rle_frames_are_rejected():
    raw_blob = bytearray(anim.encode_anim([Image.new("RGB", (1, 1), (1, 2, 3))], 5))
    raw_blob[_frame_start(raw_blob)] = 2
    with pytest.raises(anim.AnimDecodeError, match="unsupported encoding"):
        anim.decode_anim(raw_blob)

    rle_blob = bytearray(anim.encode_anim([_solid((1, 2, 3), (4, 1))], 5))
    frame_data = _frame_start(rle_blob) + struct.calcsize(anim._FRAME_FMT)
    assert rle_blob[_frame_start(rle_blob)] == 1
    rle_blob[frame_data] = 0x84  # literal four pixels, but only one follows
    with pytest.raises(anim.AnimDecodeError, match="truncated RLE literal"):
        anim.decode_anim(rle_blob)


def test_section_offset_and_display_count_metadata_are_validated():
    blob = anim.encode_anim(
        [_solid((1, 2, 3)), _solid((4, 5, 6))],
        fps=5,
        sections=[("second", 1, 1)],
    )
    wrong_offset = bytearray(blob)
    # The default section header begins immediately after the file header.
    struct.pack_into("<I", wrong_offset, struct.calcsize(anim._HEADER_FMT) + 8, 0)
    with pytest.raises(anim.AnimDecodeError, match="wrong file frame"):
        anim.decode_anim(wrong_offset)

    with pytest.raises(anim.AnimDecodeError, match="display frame count"):
        anim.decode_anim(_mutate_header(blob, 12, 3))


def test_every_decode_budget_is_enforced_before_expansion():
    blob = anim.encode_anim(
        [_solid((1, 2, 3)), _solid((4, 5, 6))],
        fps=5,
        sections=[("second", 1, 1)],
        durations=[2, 3],
    )

    constrained = [
        replace(anim.DEFAULT_DECODE_LIMITS, max_source_bytes=len(blob) - 1),
        replace(anim.DEFAULT_DECODE_LIMITS, max_sections=1),
        replace(anim.DEFAULT_DECODE_LIMITS, max_file_frames=1),
        replace(anim.DEFAULT_DECODE_LIMITS, max_display_frames=4),
        replace(anim.DEFAULT_DECODE_LIMITS, max_expanded_rgb_bytes=4 * 2 * 3 * 5 - 1),
    ]
    for limits in constrained:
        with pytest.raises(anim.AnimDecodeBudgetError):
            anim.decode_anim(blob, limits=limits)


def test_rle_output_larger_than_frame_dimensions_is_rejected():
    blob = bytearray(anim.encode_anim([_solid((1, 2, 3), (4, 1))], fps=5))
    frame_data = _frame_start(blob) + struct.calcsize(anim._FRAME_FMT)
    blob[frame_data] = 5  # repeat five pixels into a four-pixel frame

    with pytest.raises(anim.AnimDecodeError, match="beyond its dimensions"):
        anim.decode_anim(blob)


def test_decode_limits_reject_nonpositive_configuration():
    with pytest.raises(ValueError, match="max_display_frames must be positive"):
        anim.AnimDecodeLimits(max_display_frames=0)

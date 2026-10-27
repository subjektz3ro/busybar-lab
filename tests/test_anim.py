"""Behavioural claims about ``busybar_dev.anim``, the ``bicycle1`` codec.

The golden byte strings below are the output of the firmware's own
``scripts/seq2anim.py`` at the 1.3.1 release (busy-app/busybar-firmware
8edd2da4), run on identical frames with ``color_mode = "rgb888"``.  The
device is the only other decoder of this container, so matching the vendor
encoder bit for bit is the strongest claim the suite can make without a
panel in the loop.
"""
from __future__ import annotations

import hashlib
import struct
from dataclasses import replace

import pytest
from PIL import Image

from busybar_dev import anim


def _solid(color: tuple[int, int, int], size: tuple[int, int] = (4, 2)) -> Image.Image:
    return Image.new("RGB", size, color)


def _gradient(size: tuple[int, int], phase: int) -> Image.Image:
    width, height = size
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
    return image


def _partial_change() -> list[Image.Image]:
    base = bytes((10, 20, 30))
    changed = base * 8 + bytes((200, 20, 30)) * 2 + base * 6
    return [_solid((10, 20, 30), (8, 2)), Image.frombytes("RGB", (8, 2), changed)]


def _sparse_change() -> list[Image.Image]:
    base = Image.new("RGB", (72, 16), (5, 6, 7))
    changed = base.copy()
    changed.putpixel((68, 6), (200, 100, 50))
    return [base, changed]


def _colors(decoded: anim.DecodedAnim, section: str = "default") -> list[tuple[int, int, int]]:
    return [frame.getpixel((0, 0)) for frame in decoded.iter_display_frames(section)]


def _frame_start(blob: bytes) -> int:
    header = struct.unpack_from(anim._HEADER_FMT, blob, 0)
    return struct.calcsize(anim._HEADER_FMT) + header[9]


def _mutate_header(blob: bytes, index: int, value: int | bytes) -> bytes:
    fields = list(struct.unpack_from(anim._HEADER_FMT, blob, 0))
    fields[index] = value
    changed = bytearray(blob)
    struct.pack_into(anim._HEADER_FMT, changed, 0, *fields)
    return bytes(changed)


REFERENCE = {
    "one_pixel": (
        lambda: [Image.new("RGB", (1, 1), (0x12, 0x34, 0x56))], {"fps": 7},
        "62696379636c6531000101000000030007001400000008000000010000000100000000"
        "000000000000003600000064656661756c74001000000300563412",
    ),
    "solid_4x2": (
        lambda: [_solid((201, 37, 9))], {"fps": 5},
        "62696379636c6531000402000000040005001400000009000000010000000100000000"
        "000000000000003600000064656661756c74001100000400080925c9",
    ),
    "red_red_blue": (
        lambda: [_solid((255, 0, 0)), _solid((255, 0, 0)), _solid((0, 0, 255))], {"fps": 6},
        "62696379636c6531000402000000040006001400000017000000010000000300000000"
        "000000020000003600000064656661756c74001100000400080000ff00000000001100"
        "00040008ff0000",
    ),
    "partial_change": (
        _partial_change, {"fps": 4},
        "62696379636c6531000802001000050004001400000015000000010000000200000000"
        "000000010000003600000064656661756c74001100000400101e140a421000050000c0"
        "fec8141ec0",
    ),
    "section_keyframe": (
        lambda: [_solid((180, 0, 0)), _solid((0, 180, 0)), _solid((0, 180, 0))],
        {"fps": 4, "sections": [("tail", 2, 2)]},
        "62696379636c653100040200000004000400250000001b000000020000000300000000"
        "000000020000004700000064656661756c7400020000000200000059000000746169"
        "6c001100000400080000b411000004000800b40011000004000800b400",
    ),
    "gradient_7x3": (
        lambda: [_gradient((7, 3), phase) for phase in range(3)],
        {"fps": 12, "sections": [("middle", 1, 1)]},
        "62696379636c65310007030000003f000c0027000000cc000000020000000300000000"
        "000000020000004900000064656661756c740001000000010000008d0000006d696464"
        "6c65001000003f000000000b002516004a21006f2c00943700b94200de113d001c3d25"
        "273d4a323d6f3d3d94483db9533dde227a002d7a25387a4a437a6f4e7a94597ab9647a"
        "de1000003f004717135217385d175d6817827317a77e17cc8917f15854136354386e54"
        "5d7954828454a78f54cc9a54f16991137491387f915d8a91829591a7a091ccab91f110"
        "00003f008e2e26992e4ba42e70af2e95ba2ebac52edfd02e049f6b26aa6b4bb56b70c0"
        "6b95cb6bbad66bdfe16b04b0a826bba84bc6a870d1a895dca8bae7a8dff2a804",
    ),
    "sparse_72x16": (
        _sparse_change, {"fps": 3},
        "62696379636c653100481000430028000300140000003e000000010000000200000000"
        "000000010000003600000064656661756c740011000028007f0706057f0706057f0706"
        "057f0706057f0706057f0706057f0706057f0706057f070605090706052043000300ff"
        "e3fa9ffe3ff8f1a03264c8",
    ),
}


def _reference(name: str) -> tuple[list[Image.Image], dict, bytes]:
    frames, kwargs, hex_blob = REFERENCE[name]
    return frames(), kwargs, bytes.fromhex(hex_blob)


@pytest.mark.parametrize("name", sorted(REFERENCE))
def test_encoder_output_is_byte_identical_to_the_firmware_reference_encoder(name):
    frames, kwargs, expected = _reference(name)
    assert anim.encode_anim(frames, **kwargs) == expected


def test_a_panel_sized_sequence_matches_the_reference_encoder_digest():
    blob = anim.encode_anim([_gradient((72, 16), phase) for phase in range(3)], fps=12)

    assert len(blob) == 10437
    assert hashlib.sha256(blob).hexdigest() == (
        "77f15fca736b8cdf80c2db65eb6c90efd3e642925be0acf394a8032db0fabdc4"
    )


def test_header_declares_bicycle1_dimensions_fps_and_chunk_sizes():
    frames, kwargs, _ = _reference("red_red_blue")
    blob = anim.encode_anim(frames, **kwargs)
    (
        signature, flags, width, height, color_format, max_mask_bits,
        max_pixel_bytes, fps, _unused, sections_len, frames_len,
        section_count, frame_count,
    ) = struct.unpack_from(anim._HEADER_FMT, blob, 0)

    assert signature == b"bicycle1"
    assert (flags, width, height, color_format, fps) == (0, 4, 2, 0, 6)
    # Fully masked frames carry no mask bits; eight red pixels RLE to 4 bytes.
    assert (max_mask_bits, max_pixel_bytes) == (0, 4)
    assert (section_count, frame_count) == (1, 3)
    assert struct.calcsize(anim._HEADER_FMT) + sections_len + frames_len == len(blob)


def test_an_unchanged_frame_is_a_five_byte_marker_with_no_pixels():
    frames, kwargs, _ = _reference("red_red_blue")
    decoded = anim.decode_anim(anim.encode_anim(frames, **kwargs))
    repeat = decoded.frames[1]

    assert (repeat.mask_encoding, repeat.pixel_encoding) == ("none", "raw")
    assert (repeat.changed_pixels, repeat.pixel_bytes) == (0, 0)
    assert decoded.frames[2].file_offset - repeat.file_offset == 5
    assert repeat.rgb is decoded.frames[0].rgb  # shares, never copies, the held picture
    assert decoded.display_frame_count == 3
    assert decoded.duration_seconds == pytest.approx(0.5)
    assert _colors(decoded) == [(255, 0, 0), (255, 0, 0), (0, 0, 255)]


def test_explicit_durations_repeat_frames_as_unchanged_markers():
    amber = _solid((240, 130, 20))
    decoded = anim.decode_anim(
        anim.encode_anim([amber, _solid((0, 0, 0))], fps=10, durations=[2, 3])
    )

    assert decoded.display_frame_count == 5
    assert [frame.mask_encoding for frame in decoded.frames] == [
        "full", "none", "full", "none", "none",
    ]
    assert _colors(decoded) == [(240, 130, 20)] * 2 + [(0, 0, 0)] * 3


def test_a_section_start_is_a_keyframe_even_when_pixels_match():
    frames, kwargs, _ = _reference("section_keyframe")
    decoded = anim.decode_anim(anim.encode_anim(frames, **kwargs))

    assert [frame.mask_encoding for frame in decoded.frames] == ["full", "full", "full"]
    tail = decoded.section("tail")
    assert (tail.first_display_frame, tail.last_display_frame) == (2, 2)
    assert tail.frame_offset == decoded.frames[2].file_offset
    assert decoded.section_duration_seconds("tail") == pytest.approx(0.25)
    assert _colors(decoded, "tail") == [(0, 180, 0)]
    with pytest.raises(KeyError):
        decoded.section("missing")


def test_a_small_change_ships_only_the_changed_pixels():
    frames, kwargs, _ = _reference("partial_change")
    decoded = anim.decode_anim(anim.encode_anim(frames, **kwargs))
    delta = decoded.frames[1]

    assert (delta.mask_encoding, delta.pixel_encoding, delta.changed_pixels) == (
        "bitmap", "qoi", 2,
    )
    assert delta.pixel_bytes == 5
    assert delta.image(decoded.size).tobytes() == frames[1].tobytes()


def test_a_sparse_change_in_a_panel_sized_frame_uses_a_run_length_mask():
    frames, kwargs, _ = _reference("sparse_72x16")
    decoded = anim.decode_anim(anim.encode_anim(frames, **kwargs))
    delta = decoded.frames[1]

    assert (delta.mask_encoding, delta.changed_pixels, delta.mask_bits) == ("rle", 1, 67)
    image = delta.image(decoded.size)
    assert image.getpixel((68, 6)) == (200, 100, 50)
    assert image.tobytes() == frames[1].tobytes()


@pytest.mark.parametrize("size", [(1, 1), (7, 3), (72, 16)])
def test_encode_decode_round_trip_preserves_every_rgb_pixel(size):
    frames = [_gradient(size, phase) for phase in range(3)]
    decoded = anim.decode_anim(
        anim.encode_anim(frames, fps=12, sections=[("middle", 1, 1)])
    )

    assert [frame.tobytes() for frame in decoded.iter_display_frames()] == [
        frame.convert("RGB").tobytes() for frame in frames
    ]
    assert next(decoded.iter_display_frames("middle")).tobytes() == (
        frames[1].convert("RGB").tobytes()
    )


def test_legacy_bicycle0_files_are_rejected_with_a_pointer_to_the_firmware_change():
    frames, kwargs, _ = _reference("one_pixel")
    blob = _mutate_header(anim.encode_anim(frames, **kwargs), 0, b"bicycle0")

    with pytest.raises(anim.AnimDecodeError, match="bicycle0"):
        anim.decode_anim(blob)


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


def test_malformed_frame_headers_are_rejected():
    frames, kwargs, _ = _reference("solid_4x2")
    blob = anim.encode_anim(frames, **kwargs)
    start = _frame_start(blob)

    bad_mask = bytearray(blob)
    bad_mask[start] = 0x50  # mask encoding 5 does not exist
    with pytest.raises(anim.AnimDecodeError, match="mask encoding"):
        anim.decode_anim(bad_mask)

    bad_pixels = bytearray(blob)
    bad_pixels[start] = 0x13  # pixel encoding 3 does not exist
    with pytest.raises(anim.AnimDecodeError, match="pixel encoding"):
        anim.decode_anim(bad_pixels)

    bits_on_full = bytearray(blob)
    struct.pack_into("<H", bits_on_full, start + 1, 8)
    with pytest.raises(anim.AnimDecodeError, match="mask bits"):
        anim.decode_anim(bits_on_full)

    with pytest.raises(anim.AnimDecodeError, match="max_pixel"):
        anim.decode_anim(_mutate_header(blob, 6, 3))  # frame carries 4 bytes


def test_rle_pixels_that_expand_beyond_the_masked_pixels_are_rejected():
    frames, kwargs, _ = _reference("solid_4x2")
    blob = bytearray(anim.encode_anim(frames, **kwargs))
    blob[_frame_start(blob) + 5] = 9  # repeat nine pixels into an eight-pixel frame

    with pytest.raises(anim.AnimDecodeError, match="beyond its masked pixels"):
        anim.decode_anim(blob)


def test_qoi_pixels_must_cover_exactly_the_masked_pixels():
    frames, kwargs, _ = _reference("partial_change")
    blob = anim.encode_anim(frames, **kwargs)
    delta = _frame_start(blob) + 9  # frame 0 is a 5-byte header plus 4 RLE bytes
    run_op = delta + 5 + 2 + 4  # header, two mask bytes, one QOI_OP_RGB
    assert blob[run_op] == 0xC0

    overrun = bytearray(blob)
    overrun[run_op] = 0xC1  # a run of two: three pixels for a two-pixel mask
    with pytest.raises(anim.AnimDecodeError, match="masked pixels"):
        anim.decode_anim(overrun)

    short = bytearray(blob[:run_op] + blob[run_op + 1:])  # drop the run entirely
    struct.pack_into("<H", short, delta + 3, 4)
    header = list(struct.unpack_from(anim._HEADER_FMT, short, 0))
    header[10] -= 1
    struct.pack_into(anim._HEADER_FMT, short, 0, *header)
    with pytest.raises(anim.AnimDecodeError, match="masked pixels"):
        anim.decode_anim(short)


def test_section_offset_and_range_metadata_are_validated():
    frames, kwargs, _ = _reference("section_keyframe")
    blob = anim.encode_anim(frames, **kwargs)
    header_size = struct.calcsize(anim._HEADER_FMT)

    wrong_offset = bytearray(blob)
    struct.pack_into("<I", wrong_offset, header_size + 8, 0)
    with pytest.raises(anim.AnimDecodeError, match="wrong file frame"):
        anim.decode_anim(wrong_offset)

    out_of_range = bytearray(blob)
    struct.pack_into("<I", out_of_range, header_size + 20 + 4, 7)  # "tail" ends past the end
    with pytest.raises(anim.AnimDecodeError, match="outside"):
        anim.decode_anim(out_of_range)


def test_a_section_may_not_start_on_a_delta_frame():
    red = _solid((200, 0, 0))
    blob = anim.encode_anim([red, red.copy(), red.copy()], fps=5, sections=[("mid", 1, 2)])
    honest = anim.decode_anim(blob)
    assert honest.frames[2].mask_encoding == "none"
    header_size = struct.calcsize(anim._HEADER_FMT)

    moved = bytearray(blob)  # point "mid" at frame 2, which only says "unchanged"
    struct.pack_into("<I", moved, header_size + 20, 2)
    struct.pack_into("<I", moved, header_size + 28, honest.frames[2].file_offset)
    with pytest.raises(anim.AnimDecodeError, match="keyframe"):
        anim.decode_anim(moved)


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
        replace(anim.DEFAULT_DECODE_LIMITS, max_frames=4),
        replace(anim.DEFAULT_DECODE_LIMITS, max_expanded_rgb_bytes=4 * 2 * 3 * 5 - 1),
    ]
    for limits in constrained:
        with pytest.raises(anim.AnimDecodeBudgetError):
            anim.decode_anim(blob, limits=limits)


def test_decode_limits_reject_nonpositive_configuration():
    with pytest.raises(ValueError, match="max_frames must be positive"):
        anim.AnimDecodeLimits(max_frames=0)


def test_encoder_rejects_sections_the_firmware_would_refuse():
    frames = [_solid((1, 2, 3)), _solid((4, 5, 6))]

    with pytest.raises(ValueError, match="reserved"):
        anim.encode_anim(frames, fps=5, sections=[("default", 0, 1)])
    with pytest.raises(ValueError, match="duplicate"):
        anim.encode_anim(frames, fps=5, sections=[("a", 0, 0), ("a", 1, 1)])
    with pytest.raises(ValueError, match="outside"):
        anim.encode_anim(frames, fps=5, sections=[("late", 1, 2)])
    with pytest.raises(ValueError, match="start"):
        anim.encode_anim(frames, fps=5, sections=[("backwards", 1, 0)])


def test_encoder_rejects_frames_the_container_cannot_describe():
    with pytest.raises(ValueError, match="at least one frame"):
        anim.encode_anim([], fps=5)
    with pytest.raises(ValueError, match="same size"):
        anim.encode_anim([_solid((1, 2, 3)), _solid((1, 2, 3), (5, 2))], fps=5)
    with pytest.raises(ValueError, match="fps"):
        anim.encode_anim([_solid((1, 2, 3))], fps=0)
    with pytest.raises(ValueError, match="fps"):
        anim.encode_anim([_solid((1, 2, 3))], fps=256)
    with pytest.raises(ValueError, match="durations"):
        anim.encode_anim([_solid((1, 2, 3))], fps=5, durations=[1, 1])
    with pytest.raises(ValueError, match="duration"):
        anim.encode_anim([_solid((1, 2, 3))], fps=5, durations=[0])


def test_encoder_rejects_section_names_the_container_cannot_terminate():
    frames = [_solid((1, 2, 3)), _solid((4, 5, 6))]
    with pytest.raises(ValueError, match="NUL"):
        anim.encode_anim(frames, fps=5, sections=[("a\x00b", 1, 1)])


def test_encoder_rejects_non_integer_fps_and_durations():
    frame = _solid((1, 2, 3))
    with pytest.raises(ValueError, match="fps"):
        anim.encode_anim([frame], fps=5.0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="fps"):
        anim.encode_anim([frame], fps=True)
    with pytest.raises(ValueError, match="duration"):
        anim.encode_anim([frame], fps=5, durations=[2.0])  # type: ignore[list-item]


def test_header_maxima_must_match_the_frames_they_describe():
    frames, kwargs, _ = _reference("partial_change")
    blob = anim.encode_anim(frames, **kwargs)

    with pytest.raises(anim.AnimDecodeError, match="max_mask_bits"):
        anim.decode_anim(_mutate_header(blob, 5, 999))
    with pytest.raises(anim.AnimDecodeError, match="max_pixel_bytes"):
        anim.decode_anim(_mutate_header(blob, 6, 999))


def test_truncated_pixel_data_is_rejected():
    frames, kwargs, _ = _reference("solid_4x2")
    blob = bytearray(anim.encode_anim(frames, **kwargs))
    blob[_frame_start(blob) + 5] = 0x84  # literal four pixels, but three bytes follow
    with pytest.raises(anim.AnimDecodeError, match="truncated RLE literal"):
        anim.decode_anim(blob)

    frames, kwargs, _ = _reference("partial_change")
    blob = anim.encode_anim(frames, **kwargs)
    delta = _frame_start(blob) + 9
    pixels = delta + 5 + 2
    assert blob[pixels] == 0xFE
    short = bytearray(blob[:pixels + 1] + blob[pixels + 5:])  # QOI_OP_RGB, no payload
    struct.pack_into("<H", short, delta + 3, 1)
    header = list(struct.unpack_from(anim._HEADER_FMT, short, 0))
    header[10] -= 4
    struct.pack_into(anim._HEADER_FMT, short, 0, *header)
    with pytest.raises(anim.AnimDecodeError, match="truncated QOI"):
        anim.decode_anim(short)


def test_masks_that_do_not_cover_the_frame_are_rejected():
    frames, kwargs, _ = _reference("sparse_72x16")
    blob = anim.encode_anim(frames, **kwargs)
    delta = _frame_start(blob) + 5 + 40  # frame 0 is a header plus 40 RLE bytes
    assert blob[delta] == 0x20

    truncated = bytearray(blob)
    struct.pack_into("<H", truncated, delta + 1, 66)  # one bit short of the last run
    with pytest.raises(anim.AnimDecodeError, match="run-length mask is truncated"):
        anim.decode_anim(truncated)

    short_run = bytearray(blob)
    short_run[delta + 5 + 1] = 0xC3  # first run 255 -> 254 leaves a pixel uncovered
    with pytest.raises(anim.AnimDecodeError, match="does not cover the frame"):
        anim.decode_anim(short_run)

    frames, kwargs, _ = _reference("partial_change")
    blob = anim.encode_anim(frames, **kwargs)
    delta = _frame_start(blob) + 9
    bitmap = bytearray(blob)
    struct.pack_into("<H", bitmap, delta + 1, 15)  # 16-pixel frame, 15-bit bitmap
    with pytest.raises(anim.AnimDecodeError, match="bitmap mask must cover"):
        anim.decode_anim(bitmap)

    frames, kwargs, _ = _reference("solid_4x2")
    blob = bytearray(anim.encode_anim(frames, **kwargs))
    blob[_frame_start(blob)] = 0x20  # run-length mask with zero bits
    with pytest.raises(anim.AnimDecodeError, match="empty change mask"):
        anim.decode_anim(blob)


def test_first_section_must_be_default():
    frames, kwargs, _ = _reference("one_pixel")
    blob = bytearray(anim.encode_anim(frames, **kwargs))
    name = blob.index(b"default\x00")
    blob[name:name + 7] = b"defaulx"
    with pytest.raises(anim.AnimDecodeError, match="must be 'default'"):
        anim.decode_anim(blob)

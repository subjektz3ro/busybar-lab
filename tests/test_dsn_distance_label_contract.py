"""Final-pixel contracts for the Distance view's bottom semantic row.

These tests deliberately rasterise the finite labels without calling dsn._text.
Containment alone approved both the clipped ``NO LINK`` and ``UPLI`` bugs; the
contract here is the final composed 72x16 pixels across every native frame.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


NOW = datetime(2026, 8, 8, tzinfo=timezone.utc)
BOTTOM_Y = dsn.H - 5
DIVIDER = (40, 55, 75)


def link_at(light_s: float | None = 1 / 3, *, bps: float | None = 0.0,
            receiving: bool = False, up: bool = True) -> dsn.Link:
    streams = ((dsn.DownStream("X", bps, -140.0),) if receiving else ())
    return dsn.Link(
        complex_name="Canberra", dish="DSS43", craft="MMS2",
        elevation=21.0, band="X", down_bps=bps, up_active=up,
        range_km=(dsn.C_KM_S * light_s if light_s is not None else None),
        down_streams=streams,
    )


def ink_width(text: str) -> int:
    """Independent width calculation from the declared glyphs."""
    if not text:
        return 0
    return sum(len(dsn.FONT[ch][0]) + dsn.GLYPH_GAP
               for ch in text.upper()) - dsn.GLYPH_GAP


def source_ink(text: str) -> set[tuple[int, int]]:
    """The complete unclipped source ink, independent of dsn._text."""
    result: set[tuple[int, int]] = set()
    x = 0
    for ch in text.upper():
        glyph = dsn.FONT[ch]
        for y, row in enumerate(glyph):
            for dx, bit in enumerate(row):
                if bit == "1":
                    result.add((x + dx, y))
        x += len(glyph[0]) + dsn.GLYPH_GAP
    return result


def paint_text(image: Image.Image, text: str, x: int,
               colour: tuple[int, int, int],
               clip: tuple[int, int] | None = None) -> None:
    """Minimal independent compositor; clip endpoints are inclusive."""
    px = image.load()
    for source_x, source_y in source_ink(text):
        target_x = x + source_x
        if not (0 <= target_x < dsn.W):
            continue
        if clip is not None and not (clip[0] <= target_x <= clip[1]):
            continue
        px[target_x, BOTTOM_Y + source_y] = colour


def inclusive_crop(box: tuple[int, int], y0: int,
                   y1: int) -> tuple[int, int, int, int]:
    """Convert this renderer's inclusive x box to PIL's exclusive crop."""
    return box[0], y0, box[1] + 1, y1


def expected_bottom(far: str, fast: str, frame_index: int,
                    frame_count: int, *, live: bool,
                    ) -> tuple[Image.Image, tuple[int, int], int]:
    """Compose the full bottom row from semantic layout rules, not dsn._text."""
    expected = Image.new("RGB", (dsn.W, dsn.H), dsn.OFF)
    tx = dsn.GLOBE_CX + dsn.GLOBE_R + 3
    far_width = ink_width(far)
    fast_width = ink_width(fast)
    room = dsn.W - 1 - tx

    assert far_width <= room
    if not live:
        paint_text(expected, far, tx, dsn.DIST)

    if far_width + 3 + fast_width <= room:
        fast_x = dsn.W - 1 - fast_width
        fast_box = (fast_x, dsn.W - 1)
        paint_text(expected, fast, fast_x, dsn.RATE)
    else:
        fast_x = tx + far_width + 3
        fast_box = (fast_x, dsn.W - 1)
        box_width = fast_box[1] - fast_box[0] + 1
        cycle = fast_width + dsn.SCROLL_GAP_PX
        offset = dsn.independent_scroll_offset(
            fast, box_width, frame_index, frame_count)
        paint_text(expected, fast, fast_box[0] - offset, dsn.RATE, fast_box)
        paint_text(expected, fast, fast_box[0] - offset + cycle,
                   dsn.RATE, fast_box)

    gap0, gap1 = tx + far_width, fast_x
    divider_x = -1
    if gap1 - gap0 >= 3:
        divider_x = (gap0 + gap1) // 2
        for y in range(BOTTOM_Y, dsn.H):
            expected.putpixel((divider_x, y), DIVIDER)
    return expected, fast_box, divider_x


CASES = [
    pytest.param(link_at(None, up=False), {}, "?", "QUIET", id="unknown-distance"),
    pytest.param(link_at(up=False), {}, "SUBSEC", "QUIET", id="quiet"),
    pytest.param(link_at(), {}, "SUBSEC", "UPLINK", id="uplink-only"),
    pytest.param(link_at(5.0, bps=160.0, receiving=True), {},
                 "5SEC", "160BPS", id="seconds"),
    pytest.param(link_at(3_120.0, bps=160.0, receiving=True), {},
                 "52M", "160BPS", id="minutes"),
    pytest.param(link_at(), {"freshness": "delayed"},
                 "SUBSEC", "DELAY", id="delayed"),
    pytest.param(link_at(), {"freshness": "stale"},
                 "SUBSEC", "STALE", id="stale"),
    pytest.param(link_at(), {"freshness": "offline"},
                 "SUBSEC", "STALE", id="offline"),
    pytest.param(link_at(bps=None, receiving=True), {},
                 "SUBSEC", "RATE?", id="unknown-rate"),
    pytest.param(link_at(bps=0.0, receiving=True), {},
                 "SUBSEC", "0BPS", id="zero-rate"),
    pytest.param(link_at(bps=160.0, receiving=True), {},
                 "SUBSEC", "160BPS", id="bps"),
    pytest.param(link_at(bps=999_999.0, receiving=True), {},
                 "SUBSEC", "1000KBPS", id="rounded-kbps"),
    pytest.param(link_at(bps=999_999_999.0, receiving=True), {},
                 "SUBSEC", "1000MBPS", id="rounded-mbps"),
    pytest.param(link_at(bps=999_999_999_999.0, receiving=True), {},
                 "SUBSEC", ">999GBPS", id="overflow-gbps"),
    pytest.param(link_at(71_382.0, bps=160.0, receiving=True), {},
                 "19.8H", "160BPS", id="voyager-static"),
    pytest.param(link_at(71_382.0, bps=999_999_999.0, receiving=True), {},
                 "19.8H", "1000MBPS", id="voyager-rate-marquee"),
    pytest.param(
        link_at(120.0),
        {"realtime_since": NOW.timestamp() - 1, "on_air": False},
        "1:59", "OFF AIR", id="watch-off-air"),
    pytest.param(
        link_at(120.0),
        {"realtime_since": NOW.timestamp() - 1,
         "on_air": True, "handoff": True},
        "1:59", "HANDOFF", id="watch-handoff"),
    pytest.param(
        link_at(120.0),
        {"realtime_since": NOW.timestamp() - 1, "freshness": "delayed"},
        "1:59", "DELAY", id="watch-delayed"),
    pytest.param(
        link_at(71_382.0),
        {"realtime_since": NOW.timestamp() - 1,
         "on_air": True, "handoff": True},
        "19:49", "HANDOFF", id="watch-voyager-countdown"),
]


@pytest.mark.parametrize(("link", "render_args", "far", "fast"), CASES)
def test_every_distance_status_and_rate_preserves_the_complete_bottom_row(
        link: dsn.Link, render_args: dict, far: str, fast: str) -> None:
    frames, _, _ = dsn.render_frames(link, NOW, **render_args)
    live = render_args.get("realtime_since") is not None
    row_box = (dsn.GLOBE_CX + dsn.GLOBE_R + 3, dsn.W - 1)
    row_crop = inclusive_crop(row_box, BOTTOM_Y, dsn.H)

    far_crops: set[bytes] = set()
    divider_crops: set[bytes] = set()
    visible_source_columns: set[int] = set()
    offsets: list[int] = []

    for index, frame in enumerate(frames):
        expected, fast_box, divider_x = expected_bottom(
            far, fast, index, len(frames), live=live)
        assert frame.crop(row_crop).tobytes() == expected.crop(row_crop).tobytes()

        far_box = (row_box[0], row_box[0] + ink_width(far) - 1)
        far_crops.add(frame.crop(inclusive_crop(far_box, BOTTOM_Y, dsn.H)).tobytes())
        if divider_x >= 0:
            divider_crops.add(frame.crop(
                inclusive_crop((divider_x, divider_x), BOTTOM_Y, dsn.H)
            ).tobytes())

        fast_width = ink_width(fast)
        box_width = fast_box[1] - fast_box[0] + 1
        if fast_width > box_width:
            cycle = fast_width + dsn.SCROLL_GAP_PX
            offset = dsn.independent_scroll_offset(
                fast, box_width, index, len(frames))
            offsets.append(offset)
            for source_x, _ in source_ink(fast):
                if any(0 <= source_x - offset + shift < box_width
                       for shift in (0, cycle)):
                    visible_source_columns.add(source_x)

    # The distance/countdown reservation and divider are semantic neighbours,
    # never part of the marquee's motion.
    assert len(far_crops) == 1
    assert len(divider_crops) <= 1

    if offsets:
        assert visible_source_columns == {x for x, _ in source_ink(fast)}
        cycle = ink_width(fast) + dsn.SCROLL_GAP_PX
        steps = [(later - earlier) % cycle
                 for earlier, later in zip(offsets, offsets[1:])]
        seam_step = (-offsets[-1]) % cycle
        assert max([*steps, seam_step]) - min([*steps, seam_step]) <= 1

        # At phase 1 the repeat occupies exactly the phase-0 position. Compare
        # final pixels, not merely the arithmetic offset.
        first, first_box, _ = expected_bottom(
            far, fast, 0, len(frames), live=live)
        looped, looped_box, _ = expected_bottom(
            far, fast, len(frames), len(frames), live=live)
        assert first_box == looped_box
        crop = inclusive_crop(first_box, BOTTOM_Y, dsn.H)
        assert first.crop(crop).tobytes() == looped.crop(crop).tobytes()

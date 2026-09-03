"""Generic visualizer presentation helpers, independent of any app."""

from __future__ import annotations

import pytest
from PIL import Image

from busybar_viz.panel import (
    GAP_SIZE,
    LED_SIZE,
    PITCH,
    change_heatmap,
    contact_sheet,
    panelise,
    upscale,
)


def test_gap_preview_maps_one_source_pixel_to_one_separated_led_package():
    source = Image.new("RGB", (2, 1), (0, 0, 0))
    source.putpixel((1, 0), (12, 34, 56))

    preview = panelise(source)

    assert preview.size == (2 * PITCH, PITCH)
    assert preview.crop((0, 0, PITCH, PITCH)).getbbox() is None
    pad = GAP_SIZE // 2
    package = preview.crop((PITCH + pad, pad,
                            PITCH + pad + LED_SIZE, pad + LED_SIZE))
    assert set(package.get_flattened_data()) == {(12, 34, 56)}
    assert sum(
        pixel != (0, 0, 0) for pixel in preview.get_flattened_data()
    ) == LED_SIZE**2


@pytest.mark.parametrize(
    ("led_size", "gap_size"),
    ((0, 8), (-1, 8), (10, -1)),
)
def test_gap_preview_rejects_impossible_geometry(led_size, gap_size):
    with pytest.raises(ValueError):
        panelise(Image.new("RGB", (1, 1)), led_size=led_size, gap_size=gap_size)


def test_package_offset_can_preserve_an_existing_uncentred_presentation():
    source = Image.new("RGB", (1, 1), (9, 8, 7))

    centred = panelise(source, led_size=2, gap_size=2)
    legacy = panelise(source, led_size=2, gap_size=2, package_offset=0)

    assert centred.getpixel((0, 0)) == (0, 0, 0)
    assert centred.getpixel((1, 1)) == (9, 8, 7)
    assert legacy.getpixel((0, 0)) == (9, 8, 7)
    with pytest.raises(ValueError, match="package_offset"):
        panelise(source, led_size=2, gap_size=2, package_offset=3)


def test_native_upscale_is_nearest_neighbour_and_does_not_blend_pixels():
    source = Image.new("RGB", (2, 1))
    source.putdata([(255, 0, 0), (0, 0, 255)])

    enlarged = upscale(source, scale=3)

    assert enlarged.size == (6, 3)
    assert set(enlarged.crop((0, 0, 3, 3)).get_flattened_data()) == {
        (255, 0, 0),
    }
    assert set(enlarged.crop((3, 0, 6, 3)).get_flattened_data()) == {
        (0, 0, 255),
    }
    with pytest.raises(ValueError, match="scale"):
        upscale(source, scale=0)


def test_change_heatmap_marks_only_pixels_that_change_between_frames():
    before = Image.new("RGB", (3, 1), (0, 0, 0))
    middle = before.copy()
    middle.putpixel((1, 0), (10, 20, 30))
    after = middle.copy()

    heatmap = change_heatmap((before, middle, after))

    assert heatmap.getpixel((0, 0)) == (0, 0, 0)
    assert heatmap.getpixel((1, 0)) == (128, 42, 0)
    assert heatmap.getpixel((2, 0)) == (0, 0, 0)


def test_change_heatmap_rejects_empty_or_mixed_size_input():
    with pytest.raises(ValueError, match="at least one"):
        change_heatmap(())
    with pytest.raises(ValueError, match="same size"):
        change_heatmap((Image.new("RGB", (1, 1)), Image.new("RGB", (2, 1))))


def test_contact_sheet_dimensions_are_stable_for_front_and_back_frames():
    front = Image.new("RGB", (72, 16), (10, 20, 30))
    back = Image.new("RGB", (160, 80), (10, 20, 30))

    front_sheet = contact_sheet((front,) * 5, fps=5, columns=4)
    back_gap_sheet = contact_sheet((back,), fps=10, gap_view=True, columns=1)

    # Native sheets use the fixed 8x inspection zoom plus a 20px label row.
    assert front_sheet.size == (72 * 8 * 4, (16 * 8 + 20) * 2)
    assert back_gap_sheet.size == (160 * PITCH, 80 * PITCH + 20)


@pytest.mark.parametrize(("fps", "columns"), ((0, 1), (1, 0)))
def test_contact_sheet_rejects_nonpositive_timing_or_layout(fps, columns):
    with pytest.raises(ValueError):
        contact_sheet((Image.new("RGB", (1, 1)),), fps=fps, columns=columns)

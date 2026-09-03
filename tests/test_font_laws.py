"""The pixel-font laws, enforced over every font in the repository.

There are two 5-high proportional alphabets — apps/dsn.py's and
busybar_dev/pixel_text.py's. The laws were asserted against dsn's only, while
pixel_text's is the one behind the severe-weather alert card, which is the
life-safety text in this repo.

Both pass today. This is a lock, not a fix: the laws are structural failures
learned on hardware ('0' byte-identical to 'O', M and W rendering as filled
rectangles, ACE reading as 55) and a single glyph edit can reintroduce any of
them invisibly.
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

import pytest
from PIL import Image

import busybar_dev.pixel_text as pixel_text

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import dsn  # noqa: E402

FONTS = [
    pytest.param(pixel_text.FONT, pixel_text.glyph_width, id="pixel_text"),
    pytest.param(dsn.FONT, dsn.glyph_width, id="dsn"),
]


@pytest.mark.parametrize("font,width_of", FONTS)
def test_no_glyph_has_ragged_rows(font, width_of):
    for ch, glyph in font.items():
        assert len({len(row) for row in glyph}) == 1, f"{ch!r} is ragged"


@pytest.mark.parametrize("font,width_of", FONTS)
def test_no_glyph_is_a_solid_block(font, width_of):
    """A single filled row is a legitimate crossbar — E, T and Z all have one.
    Two touching ones are a rectangle, and on a panel this sparse the densest
    glyph is exactly the one that loses its shape."""
    for ch, glyph in font.items():
        width = len(glyph[0])
        full = [i for i, row in enumerate(glyph) if row == "1" * width]
        adjacent = [i for i in full if i + 1 in full]
        assert not adjacent, f"{ch!r} has adjacent filled rows {adjacent}"
        ink = sum(row.count("1") for row in glyph)
        assert ink / (width * len(glyph)) < 0.70, f"{ch!r} is too dense"


@pytest.mark.parametrize("font,width_of", FONTS)
def test_no_two_glyphs_of_equal_width_are_confusable(font, width_of):
    """Measured at 3 columns: '0' and 'O' came out byte-identical, '5' and 'S'
    byte-identical, and 26 more pairs differed by exactly one pixel — which is
    invisible once the LED gaps are in play."""
    items = [(c, g) for c, g in font.items() if c.strip()]
    for (a, ga), (b, gb) in itertools.combinations(items, 2):
        if len(ga[0]) != len(gb[0]) or len(ga) != len(gb):
            continue                     # different widths cannot be confused
        diff = sum(1 for ra, rb in zip(ga, gb)
                   for ca, cb in zip(ra, rb) if ca != cb)
        assert diff > 1, f"{a!r} and {b!r} differ by only {diff} pixel(s)"


@pytest.mark.parametrize("font,width_of", FONTS)
def test_m_and_w_get_five_columns_and_i_and_one_give_them_back(font, width_of):
    """Four columns holds everything except M and W, which need two outer
    strokes AND a centre one. I and 1 only need three, which buys the space."""
    assert width_of("M") == 5 and width_of("W") == 5
    assert width_of("I") == 3 and width_of("1") == 3


@pytest.mark.parametrize("font,width_of", FONTS)
def test_the_zero_is_slashed_so_it_can_never_be_an_o(font, width_of):
    assert font["0"] != font["O"]
    diff = sum(1 for ra, rb in zip(font["0"], font["O"])
               for ca, cb in zip(ra, rb) if ca != cb)
    assert diff > 1, "'0' and 'O' differ by one pixel"


@pytest.mark.parametrize("font,width_of", FONTS)
def test_every_letter_and_digit_is_drawable(font, width_of):
    """A missing glyph is silently skipped, leaving a gap in the word."""
    for ch in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 ":
        assert ch in font, f"{ch!r} is not drawable"


def test_pixel_text_measurement_matches_what_it_renders():
    """text_width() drives every layout decision — the scroll box, the row
    fit — so if it disagrees with the renderer, labels overlap or scroll when
    they would have fit. Render to a buffer and compare the rightmost lit
    column against the measurement."""
    for text in ("M", "W", "I", "1", "MW", "TORNADO WARNING", "0O5S", "8:45"):
        measured = pixel_text.text_width(text)
        image = Image.new("RGB", (measured + 40, 8), (0, 0, 0))
        pixel_text.draw_text(image, 0, 0, text, (255, 255, 255))
        lit = [x for x in range(image.width) for y in range(image.height)
               if image.getpixel((x, y)) != (0, 0, 0)]
        assert lit, f"{text!r} drew nothing"
        # The measurement excludes the trailing inter-glyph gap, so the
        # rightmost lit column is the last ink column of the last glyph.
        assert max(lit) < measured, (
            f"{text!r} renders past its measured width "
            f"({max(lit)} >= {measured})")
        assert max(lit) >= measured - pixel_text.GLYPH_GAP - 1, (
            f"{text!r} measures {measured} but only reaches {max(lit)}")


def test_an_unknown_character_measures_what_it_draws():
    """draw_text substitutes '?' for anything missing; glyph_width must agree
    or the substitution shifts everything after it."""
    assert pixel_text.glyph_width("\x00") == pixel_text.DEFAULT_GLYPH_W
    assert len(pixel_text.FONT["?"][0]) == pixel_text.DEFAULT_GLYPH_W

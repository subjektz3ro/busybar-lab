"""DSN render / text."""

from __future__ import annotations

import math
import unicodedata
from typing import Protocol, cast

from PIL import Image

from apps.dsn_app import limits as _limits


class PixelBuffer(Protocol):
    """The RGB pixel-access operations used by the pure renderers."""

    def __getitem__(self, xy: tuple[int, int]) -> float | tuple[int, ...]: ...

    def __setitem__(
        self,
        xy: tuple[int, int],
        colour: float | tuple[int, ...],
    ) -> None: ...


def image_pixels(image: Image.Image) -> PixelBuffer:
    """Return writable pixels for an in-memory image created by this module."""
    pixels = image.load()
    if pixels is None:
        raise RuntimeError("Pillow did not expose pixels for an in-memory image")
    return cast(PixelBuffer, pixels)


# A PROPORTIONAL 5-tall font: most glyphs are 4 wide, M and W are 5, I and 1
# are 3. Three sizes, each forced by the panel rather than chosen.
#
# 3 columns was too cramped for the alphabet at all: it rendered '0'
# identical to 'O' and '5' identical to 'S', plus 26 pairs a single pixel
# apart, and 'ACE' read as '55'. The zero is still slashed so it can never
# be an O.
#
# 4 columns then held everything except M and W, which need two outer
# strokes AND a centre one. Cramming them in left each 14 cells of 20 with
# two ADJACENT fully-filled rows — a filled rectangle, not a letter — and
# every lighter 4-wide attempt landed a single pixel away from N. They get
# a fifth column; I and 1 give back a third. Tests enforce all of it.
FONT = {
    "A": ("0110", "1001", "1111", "1001", "1001"),
    "B": ("1110", "1001", "1110", "1001", "1110"),
    "C": ("0111", "1000", "1000", "1000", "0111"),
    "D": ("1110", "1001", "1001", "1001", "1110"),
    "E": ("1111", "1000", "1110", "1000", "1111"),
    "F": ("1111", "1000", "1110", "1000", "1000"),
    "G": ("0111", "1000", "1011", "1001", "0111"),
    "H": ("1001", "1001", "1111", "1001", "1001"),
    "I": ("111", "010", "010", "010", "111"),
    "J": ("0011", "0001", "0001", "1001", "0110"),
    "K": ("1001", "1010", "1100", "1010", "1001"),
    "L": ("1000", "1000", "1000", "1000", "1111"),
    "M": ("10001", "11011", "10101", "10001", "10001"),
    "N": ("1001", "1101", "1011", "1001", "1001"),
    "O": ("0110", "1001", "1001", "1001", "0110"),
    "P": ("1110", "1001", "1110", "1000", "1000"),
    "Q": ("0110", "1001", "1001", "1011", "0111"),
    "R": ("1110", "1001", "1110", "1010", "1001"),
    "S": ("0110", "1000", "0110", "0001", "0110"),
    "T": ("1111", "0100", "0100", "0100", "0100"),
    "U": ("1001", "1001", "1001", "1001", "0110"),
    "V": ("1001", "1001", "1001", "1010", "0100"),
    "W": ("10001", "10001", "10101", "11011", "10001"),
    "X": ("1001", "1001", "0110", "1001", "1001"),
    "Y": ("1001", "1001", "0110", "0100", "0100"),
    "Z": ("1111", "0001", "0110", "1000", "1111"),
    "0": ("0110", "1011", "1101", "1001", "0110"),
    "1": ("010", "110", "010", "010", "111"),
    "2": ("0110", "1001", "0010", "0100", "1111"),
    "3": ("1110", "0001", "0110", "0001", "1110"),
    "4": ("0010", "0110", "1010", "1111", "0010"),
    "5": ("1111", "1000", "1110", "0001", "1110"),
    "6": ("0110", "1000", "1110", "1001", "0110"),
    "7": ("1111", "0001", "0010", "0100", "0100"),
    "8": ("0110", "1001", "0110", "1001", "0110"),
    "9": ("0110", "1001", "0111", "0001", "0110"),
    " ": ("0000", "0000", "0000", "0000", "0000"),
    ".": ("0000", "0000", "0000", "0000", "0100"),
    "-": ("0000", "0000", "1111", "0000", "0000"),
    "?": ("0110", "1001", "0010", "0000", "0100"),
    "+": ("0000", "0100", "1110", "0100", "0000"),
    "/": ("0001", "0010", "0100", "1000", "0000"),
    ":": ("0000", "0100", "0000", "0100", "0000"),
    "(": ("001", "010", "100", "010", "001"),
    ")": ("100", "010", "001", "010", "100"),
    ">": ("1000", "0100", "0010", "0100", "1000"),
}

GLYPH_GAP = 1  # blank columns between glyphs

DEFAULT_GLYPH_W = 4  # most glyphs; M and W are 5, I and 1 are 3


def glyph_width(ch: str) -> int:
    """This font is PROPORTIONAL. Four columns cannot hold an M or a W: they
    need two outer strokes and a centre one, and every 4-wide attempt either
    kept a solid row (which reads as a filled block on a spaced panel) or
    landed a single pixel from N. They get five. I and 1 get three, which
    buys back the space."""
    glyph = FONT.get(ch.upper())
    return len(glyph[0]) if glyph else DEFAULT_GLYPH_W


def _f(value: str | None, default: float = 0.0) -> float:
    """The feed uses '', '-1' and 'NaN' interchangeably for 'no data'."""
    if value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out < 0 or not math.isfinite(out) else out


def device_text(value: object, fallback: str = "?") -> str:
    """Printable ASCII for the firmware's native bitmap TextElement.

    NFKD preserves the readable base letters of Latin names (Zuerich's
    umlaut becomes U) while the final filter is the API contract itself.
    """
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii")
    text = "".join(ch for ch in text if 0x20 <= ord(ch) <= 0x7E).strip()
    return text or fallback


# --- rendering -------------------------------------------------------------
def _text(
    px,
    x: int,
    y: int,
    s: str,
    color: tuple[int, int, int],
    clip: tuple[int, int] | None = None,
) -> None:
    """Draw text; `clip` is an (x0, x1) window outside which nothing lands —
    which is what makes a scrolling label stay inside its box."""
    for ch in s.upper():
        glyph = FONT.get(ch)
        if glyph:
            for dy, row in enumerate(glyph):
                for dx, bit in enumerate(row):
                    gx = x + dx
                    if bit != "1" or not (
                        0 <= gx < _limits.W and 0 <= y + dy < _limits.H
                    ):
                        continue
                    if clip and not (clip[0] <= gx <= clip[1]):
                        continue
                    px[gx, y + dy] = color
        x += glyph_width(ch) + GLYPH_GAP


NAME_CHARS = 11  # roughly, at the default glyph width

SCROLL_GAP_PX = 8  # blank run between a scrolling label and its repeat


def scroll_offset(text: str, phase: float, box_px: int) -> int:
    """Pixels to shift a too-wide label left, looping seamlessly.

    The label travels exactly one full cycle (its own width plus a gap) per
    animation loop, so the last frame hands back to the first with no jump.
    Text that fits doesn't move at all.
    """
    text_px = text_width(text)
    if text_px <= box_px:
        return 0
    cycle = text_px + SCROLL_GAP_PX
    return int(phase * cycle)


def scroll_frame_count(
    text: str, box_px: int, minimum: int = _limits.INSTRUMENT_FRAMES
) -> int:
    """A whole native-loop length that keeps marquees physically readable.

    Long labels used to cover their entire width in four seconds, jumping as
    many as eight spaced LEDs per frame. Round up to complete eight-second
    RF cycles so the name stays below a fixed apparent speed and both the RF
    motion and the final->first animation seam remain continuous.
    """
    if text_width(text) <= box_px:
        return minimum
    needed = math.ceil(
        (text_width(text) + SCROLL_GAP_PX)
        / _limits.SCROLL_SPEED_PX_S
        * _limits.INSTRUMENT_FPS
    )
    cycles = max(1, math.ceil(needed / _limits.INSTRUMENT_FRAMES))
    return min(
        _limits.MAX_ANIMATION_FRAMES, max(minimum, cycles * _limits.INSTRUMENT_FRAMES)
    )


def independent_scroll_offset(
    text: str,
    box_px: int,
    index: int,
    frame_count: int,
) -> int:
    """A seam-safe per-label clock capped at ``SCROLL_SPEED_PX_S``.

    Several labels may share one native asset without sharing one apparent
    speed.  Each completes an integer number of its own cycles over the asset,
    which makes the last-to-first step ordinary rather than a jump.  Flooring
    the cycle count keeps every label at or below the physical readability
    ceiling; a short label may move more slowly, never faster.
    """
    return independent_pixel_scroll_offset(text_width(text), box_px, index, frame_count)


def independent_pixel_scroll_offset(
    width: int,
    box_px: int,
    index: int,
    frame_count: int,
) -> int:
    """The measured-strip form of :func:`independent_scroll_offset`."""
    if width <= box_px or frame_count <= 0:
        return 0
    cycle = width + SCROLL_GAP_PX
    duration_s = frame_count / _limits.INSTRUMENT_FPS
    turns = max(1, math.floor(_limits.SCROLL_SPEED_PX_S * duration_s / cycle))
    return int(index * turns * cycle / frame_count) % cycle


def text_width(text: str) -> int:
    """Ink width. The trailing inter-glyph gap isn't ink, and counting it
    was enough to make 'VOYAGER 2' scroll in a box it fits exactly."""
    if not text:
        return 0
    return sum(glyph_width(ch) + GLYPH_GAP for ch in text.upper()) - GLYPH_GAP

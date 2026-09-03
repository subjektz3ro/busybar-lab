"""Small, complete bitmap text for BUSY Bar animation frames.

The firmware's native text marquee is useful for short picker labels, but a
life-safety message must be testable frame by frame and must not depend on a
host redraw preserving an undocumented scroll phase.  These helpers render a
proportional 5-pixel alphabet into the same ``.anim`` frames as the rest of an
application.
"""

from __future__ import annotations

import math
import unicodedata

from PIL import Image


# This is the established proportional 5-high alphabet used by the DSN app.
# M/W need five columns to remain distinct from N; I/1 give that space back.
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
    "V": ("1001", "1001", "0110", "1010", "0100"),
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
    "/": ("0001", "0010", "0100", "1000", "0000"),
    ":": ("0000", "0100", "0000", "0100", "0000"),
}
GLYPH_GAP = 1
DEFAULT_GLYPH_W = 4


def device_text(value: object, fallback: str = "?") -> str:
    """Normalize untrusted presentation text to this drawable ASCII font."""
    text = unicodedata.normalize("NFKD", str(value or ""))
    text = text.encode("ascii", "ignore").decode("ascii").upper().strip()
    normalized = "".join(ch if ch in FONT else "?" for ch in text)
    return normalized or fallback


def glyph_width(char: str) -> int:
    glyph = FONT.get(char.upper())
    return len(glyph[0]) if glyph else DEFAULT_GLYPH_W


def text_width(text: str) -> int:
    """Full ink width, excluding the trailing inter-glyph gap."""
    if not text:
        return 0
    return sum(glyph_width(ch) + GLYPH_GAP for ch in text) - GLYPH_GAP


def draw_text(
    image: Image.Image,
    x: int,
    y: int,
    text: str,
    color: tuple[int, int, int],
    *,
    clip: tuple[int, int] | None = None,
) -> None:
    """Draw complete glyphs into an inclusive horizontal clip window."""
    pixels = image.load()
    if pixels is None:  # Pillow returns None only for an image it cannot map
        raise ValueError("image has no pixel access")
    for char in text:
        glyph = FONT.get(char.upper(), FONT["?"])
        for dy, row in enumerate(glyph):
            for dx, bit in enumerate(row):
                gx, gy = x + dx, y + dy
                if bit != "1" or not (0 <= gx < image.width and 0 <= gy < image.height):
                    continue
                if clip is not None and not (clip[0] <= gx <= clip[1]):
                    continue
                pixels[gx, gy] = color
        x += glyph_width(char) + GLYPH_GAP


def marquee_frame_count(
    text: str,
    box_width: int,
    *,
    fps: int = 5,
    speed_px_s: float = 12.0,
    minimum: int = 40,
    maximum: int = 240,
    gap: int = 8,
) -> int:
    """A bounded whole traversal whose loop seam is an ordinary pixel step.

    `maximum` and `speed_px_s` are two budgets that have to compose, and they
    did not. Past the width `maximum` frames can carry at
    `speed_px_s`, draw_marquee's per-frame step silently grows: the label
    keeps completing exactly one cycle per loop, but faster than the speed
    that was asked for. On the severe-weather card — the one place in this
    repo where a marquee is life-safety text — an over-long event name would
    have scrolled past the readability ceiling without anything saying so.

    `max_text_width()` is the width at which they meet. Callers that ingest
    remote text should check their own ingestion bound against it rather than
    discovering the mismatch on the panel.
    """
    width = text_width(text)
    if width <= box_width:
        return minimum
    needed = math.ceil((width + gap) / speed_px_s * fps)
    return max(minimum, min(maximum, needed))


def max_text_width(*, fps: int = 5, speed_px_s: float = 12.0,
                   maximum: int = 240, gap: int = 8) -> int:
    """The widest text `marquee_frame_count` can carry at its declared speed.

    Beyond this the frame cap binds and the realised speed exceeds
    `speed_px_s`.
    """
    return max(0, int(maximum * speed_px_s / fps) - gap)


def marquee_speed_px_s(text: str, box_width: int, *, fps: int = 5,
                       speed_px_s: float = 12.0, gap: int = 8,
                       **kwargs) -> float:
    """The speed a marquee will actually run at, once the cap is applied."""
    width = text_width(text)
    if width <= box_width:
        return 0.0
    frames = marquee_frame_count(text, box_width, fps=fps,
                                 speed_px_s=speed_px_s, gap=gap, **kwargs)
    return (width + gap) / frames * fps


def draw_marquee(
    image: Image.Image,
    text: str,
    *,
    y: int,
    color: tuple[int, int, int],
    box: tuple[int, int],
    frame_index: int,
    frame_count: int,
    gap: int = 8,
) -> None:
    """Draw a static centered label or one seamless repeated marquee frame."""
    width = text_width(text)
    box_width = box[1] - box[0] + 1
    if width <= box_width:
        draw_text(image, box[0] + (box_width - width) // 2, y, text, color, clip=box)
        return
    cycle = width + gap
    offset = int(frame_index * cycle / frame_count) % cycle
    x = box[0] - offset
    draw_text(image, x, y, text, color, clip=box)
    draw_text(image, x + cycle, y, text, color, clip=box)


__all__ = [
    "FONT",
    "marquee_speed_px_s",
    "max_text_width",
    "device_text",
    "draw_marquee",
    "draw_text",
    "glyph_width",
    "marquee_frame_count",
    "text_width",
]

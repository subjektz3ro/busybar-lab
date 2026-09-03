"""Human previews that preserve the physical spacing of the LED packages."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Protocol, cast

from PIL import Image, ImageDraw

# Measured panel geometry: a 1.23 mm lit package on a 2.2 mm pitch.  Ten
# preview pixels of light and eight of darkness preserve that relationship.
LED_SIZE = 10
GAP_SIZE = 8
PITCH = LED_SIZE + GAP_SIZE
PAD = GAP_SIZE // 2
PREVIEW_BACKGROUND = (0, 0, 0)


class _RGBPixelReader(Protocol):
    def __getitem__(self, point: tuple[int, int], /) -> tuple[int, int, int]: ...


def panelise(
    frame: Image.Image,
    *,
    led_size: int = LED_SIZE,
    gap_size: int = GAP_SIZE,
    background: tuple[int, int, int] = PREVIEW_BACKGROUND,
    package_offset: int | None = None,
) -> Image.Image:
    """Render one source pixel as one separated physical LED package.

    This models package spacing only.  It is not a gamma or optical proof.
    """
    if led_size < 1 or gap_size < 0:
        raise ValueError("led_size must be positive and gap_size nonnegative")
    offset = gap_size // 2 if package_offset is None else package_offset
    if (
        isinstance(offset, bool)
        or not isinstance(offset, int)
        or not 0 <= offset <= gap_size
    ):
        raise ValueError("package_offset must be an integer within the gap")
    source = frame.convert("RGB")
    width, height = source.size
    pitch = led_size + gap_size
    result = Image.new("RGB", (width * pitch, height * pitch), background)
    pixels = cast(_RGBPixelReader, source.load())
    for y in range(height):
        for x in range(width):
            color = pixels[x, y]
            if color == (0, 0, 0):
                continue
            x0, y0 = x * pitch + offset, y * pitch + offset
            result.paste(color, (x0, y0, x0 + led_size, y0 + led_size))
    return result


def upscale(frame: Image.Image, *, scale: int = 8) -> Image.Image:
    if scale < 1:
        raise ValueError("scale must be positive")
    return frame.convert("RGB").resize(
        (frame.width * scale, frame.height * scale), Image.Resampling.NEAREST,
    )


def contact_sheet(
    frames: Sequence[Image.Image],
    *,
    fps: int,
    gap_view: bool = False,
    columns: int = 4,
    led_size: int = LED_SIZE,
    gap_size: int = GAP_SIZE,
    frame_indices: Sequence[int] | None = None,
) -> Image.Image:
    """Create a labelled, deterministic overview of every supplied frame."""
    if not frames:
        raise ValueError("need at least one frame")
    if fps < 1 or columns < 1:
        raise ValueError("fps and columns must be positive")
    if frame_indices is None:
        frame_indices = tuple(range(len(frames)))
    if len(frame_indices) != len(frames):
        raise ValueError("frame_indices must match the supplied frame count")
    prepared = [
        panelise(frame, led_size=led_size, gap_size=gap_size)
        if gap_view else upscale(frame)
        for frame in frames
    ]
    label_height = 20
    cell_width = max(image.width for image in prepared)
    cell_height = max(image.height for image in prepared) + label_height
    rows = math.ceil(len(prepared) / columns)
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), (8, 8, 10))
    draw = ImageDraw.Draw(sheet)
    for index, image in enumerate(prepared):
        column, row = index % columns, index // columns
        x, y = column * cell_width, row * cell_height
        sheet.paste(image, (x, y + label_height))
        source_index = frame_indices[index]
        elapsed_ms = round(source_index * 1000 / fps)
        draw.text((x + 3, y + 3), f"F{source_index:03d}  {elapsed_ms:05d}ms",
                  fill=(230, 230, 230))
    return sheet


def change_heatmap(frames: Sequence[Image.Image]) -> Image.Image:
    """Map how often each pixel changes across adjacent frames."""
    if not frames:
        raise ValueError("need at least one frame")
    rgb = [frame.convert("RGB") for frame in frames]
    size = rgb[0].size
    if any(frame.size != size for frame in rgb):
        raise ValueError("all frames must have the same size")
    width, height = size
    comparisons = max(1, len(rgb) - 1)
    out = Image.new("RGB", size, (0, 0, 0))
    for y in range(height):
        for x in range(width):
            changes = sum(
                rgb[index - 1].getpixel((x, y)) != rgb[index].getpixel((x, y))
                for index in range(1, len(rgb))
            )
            strength = round(255 * changes / comparisons)
            out.putpixel((x, y), (strength, strength // 3, 0))
    return out

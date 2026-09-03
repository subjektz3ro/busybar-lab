"""Save what the bar is currently showing as PNGs (for verifying draws)."""

from __future__ import annotations

from pathlib import Path

from busylib import BusyBar
from PIL import Image

FRONT_SIZE = (72, 16)  # RGB888
BACK_SIZE = (160, 80)  # RGB888


def front_image(bb: BusyBar) -> Image.Image:
    # Busylib 2 normalizes the device's wire format to canonical row-major
    # RGB888.  Swapping channels here would turn every red pixel blue again.
    return Image.frombytes("RGB", FRONT_SIZE, bb.screen(0))


def back_image(bb: BusyBar) -> Image.Image:
    # The same public contract applies to the back display even when older
    # firmware supplied packed greyscale on the wire: Busylib expands it to
    # RGB before returning from screen().
    return Image.frombytes("RGB", BACK_SIZE, bb.screen(1))


def save_screens(bb: BusyBar, directory: str | Path = ".", scale: int = 8) -> tuple[Path, Path]:
    """Save front.png and back.png, scaled up for visibility. Returns the paths."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = []
    for name, img in (("front", front_image(bb)), ("back", back_image(bb))):
        img = img.resize((img.width * scale, img.height * scale), Image.Resampling.NEAREST)
        path = directory / f"{name}.png"
        img.save(path)
        paths.append(path)
    return paths[0], paths[1]

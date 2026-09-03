"""Save what the bar is currently showing as PNGs (for verifying draws)."""

from __future__ import annotations

from pathlib import Path

from busylib import BusyBar
from busylib.display import unpack_l4_to_l8
from PIL import Image

FRONT_SIZE = (72, 16)  # RGB888
BACK_SIZE = (160, 80)  # L4-packed grayscale, two pixels per byte


def front_image(bb: BusyBar) -> Image.Image:
    raw = bb.screen(0)
    img = Image.frombytes("RGB", FRONT_SIZE, raw)
    # Firmware 1.1.1 streams the front framebuffer as BGR; relabel to RGB
    # (verified against a known-amber pixel that read back blue).
    return Image.merge("RGB", img.split()[::-1])


def back_image(bb: BusyBar) -> Image.Image:
    raw = bb.screen(1)
    w, h = BACK_SIZE
    if len(raw) == w * h * 3:
        # Firmware 1.1.1 streams the back framebuffer as full RGB888 (same
        # BGR byte order as the front — relabel). busylib's L4 helper
        # predates this; keep the old formats as fallbacks.
        img = Image.frombytes("RGB", BACK_SIZE, raw)
        return Image.merge("RGB", img.split()[::-1])
    if len(raw) == w * h:
        return Image.frombytes("L", BACK_SIZE, raw)
    l8 = bytes(v * 17 for v in unpack_l4_to_l8(raw))
    return Image.frombytes("L", BACK_SIZE, l8)


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

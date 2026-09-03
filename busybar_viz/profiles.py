"""Physical display profiles shared by adapters, audits, and previews."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DisplayProfile:
    id: str
    width: int
    height: int
    pixel_format: str = "RGB888"
    led_size_units: int = 10
    gap_size_units: int = 8

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height


FRONT = DisplayProfile("front", 72, 16)
BACK = DisplayProfile("back", 160, 80)
DISPLAY_PROFILES = {profile.id: profile for profile in (FRONT, BACK)}


def profile_for(display_id: str) -> DisplayProfile:
    try:
        return DISPLAY_PROFILES[display_id]
    except KeyError as exc:
        raise ValueError(f"unknown BUSY Bar display: {display_id}") from exc

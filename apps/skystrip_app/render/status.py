"""Skystrip render / status."""

from __future__ import annotations

from datetime import datetime

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.render import art as _render_art


def clock_str(now: datetime) -> str:
    local = now.astimezone(_settings.TZ)
    return f"{local.hour % 12 or 12}:{local.minute:02d}"


# Lilac: the Time Machine tell, and nothing else. It was amber for a long
# time, but amber is the orange clock's next-door neighbour — no orange can
# clear both amber and alarm red by 30%. Lilac is unclaimed by any scene,
# time-travel-flavoured, and genuinely clears every corner background —
# R >= +81 against the brightest horizon-glow blue the viz check found
# (two darker violets failed there with every channel under the floor),
# G-separation against overcast and against pink, B against the warm inks
# — which amber never managed. The tell is swept through the same
# extremes as the clock inks.
STATUS_INK_SCRUBBED = (220, 148, 255)


def _bake_status(
    px,
    now: datetime,
    wx: _weather.WeatherState,
    phase: float,
    scene: str = "house",
    scrubbed: bool = False,
) -> None:
    """Top-left status in our 3x5 digits: the time, and for the last stretch
    of each loop, the real temperature in Fahrenheit."""
    if phase >= 0.7:
        if _settings.UNITS == "c":
            text = f"{round(wx.temp_c)}°"
        else:
            text = f"{round(wx.temp_c * 9 / 5 + 32)}°"
    else:
        text = clock_str(now)
    # Ink history, condensed because each chapter was paid for: amber-by-day
    # sat 6-39 under the panel's ~76 luminance floor and shipped for months;
    # a brightness lerp fixed clear skies but died on white clouds; a black
    # halo, then a full card, then a translucent shadow each bought
    # guaranteed contrast with scene pixels until the operator ruled any
    # black around the text too expensive; a black/white flip machine then
    # carried four special cases (weather estimate, sun-in-corner, a forest
    # exception, a bough grown to serve it). Now: one saturated hue from
    # the operator-chosen closed set — see STATUS_INKS. Amber remains the
    # Time Machine tell, channel-distinguishable from every choice.
    color = STATUS_INK_SCRUBBED if scrubbed else _settings.CLOCK_INK
    cells: set[tuple[int, int]] = set()
    # Center the text in the fixed card: a short string at cx=2 left five
    # black columns on one side and two on the other, which read as
    # asymmetric padding rather than a card.
    text_w = sum(len(_render_art.DIGITS_3X5[ch][0]) + 1 for ch in text) - 1
    cx = max(1, (_limits.STATUS_CARD_W - text_w) // 2)
    for ch in text:
        glyph = _render_art.DIGITS_3X5[ch]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1" and 0 <= cx + gx < _limits.W:
                    cells.add((cx + gx, 1 + gy))
        cx += len(glyph[0]) + 1

    # No halo, no shadow: the strokes land directly on the scene. The
    # corner is a declared quiet zone (STATUS_CARD_W) so point noise the
    # same hue as the ink — a star beside a white digit — cannot weld
    # onto a letterform.
    for x, y in cells:
        px[x, y] = color

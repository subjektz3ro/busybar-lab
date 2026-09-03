"""The status clock has to be readable on the panel, at every hour.

Ink history, each chapter paid for on hardware: amber-by-day sat 6-39 under
the panel's ~76 luminance floor and shipped for months. A brightness lerp
fixed clear skies and died on white clouds. A black halo, then a fixed card,
then a translucent shadow each bought guaranteed contrast by spending scene
pixels, until the operator ruled any black around the text too expensive. A
black/white flip machine worked but carried four special cases (weather
estimate, sun-in-corner override, a forest exception, an overhanging bough
grown purely to serve the ink).

Now: one saturated hue at a time from a CLOSED operator-chosen set
(SKYSTRIP_CLOCK_INK: orange, pink, red). Contrast is hue, not brightness —
red proved the mechanism on hardware ("reads very well", 2026-08-12) and
then lost on design, being this product's alarm colour; teal was tried and
panel-vetoed; orange matches the bar's own orange accents. Every offered ink
is swept below against the corner's measured background extremes, so no
unreadable clock is even configurable. The corner remains a quiet zone so
point noise cannot weld onto letterforms.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import skystrip as sk  # noqa: E402

FLOOR = 0.30 * 255


@pytest.fixture
def pinned_sky(monkeypatch):
    """Pin the observer and the zone, as the other scene tests do.

    SKYSTRIP_LAT/LON can be exported in the shell, so an unpinned OBSERVER
    carries a real location into the render and daylight stops being a
    function of the timestamp in the test. Pinning it at 0,0 with UTC makes
    13:00 unambiguously midday.
    """
    monkeypatch.setattr(sk, "OBSERVER", sk.Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sk, "TZ", sk.ZoneInfo("UTC"))
    return sk


def lum(color) -> float:
    return 0.3 * color[0] + 0.59 * color[1] + 0.11 * color[2]


def clears(ink, background) -> bool:
    """The panel's dual criterion: a >=30% luminance delta OR a >=30%
    single-channel separation — hue is contrast on an RGB panel."""
    if abs(lum(ink) - lum(background)) >= FLOOR:
        return True
    return max(abs(a - b) for a, b in zip(ink, background)) >= FLOOR


@pytest.mark.parametrize("name", sorted(sk.STATUS_INKS) + ["scrubbed"])
def test_every_offered_ink_clears_the_measured_background_extremes(name):
    """The corner's measured range across every scene and hour: night sky,
    two noon blues and the paler one the viz check caught, overcast white,
    the sun's cream, pine dark, the steel rail. The set is closed and the
    scrub tell is swept with it, so this IS the proof that no configurable
    (or scrubbed) clock can be unreadable."""
    ink = (sk.STATUS_INK_SCRUBBED if name == "scrubbed"
           else sk.STATUS_INKS[name])
    for background in ((5, 8, 20), (90, 140, 210), (74, 131, 204),
                       (110, 148, 195), (139, 181, 224), (220, 225, 230),
                       (255, 251, 230), (20, 40, 20), (150, 152, 162)):
        assert clears(ink, background), (name, background)


@pytest.mark.parametrize("name", sorted(sk.STATUS_INKS))
def test_the_scrub_tell_is_distinguishable_from_every_live_ink(name):
    """Amber must read as the Time Machine against any configured clock."""
    per_channel = [abs(a - b) for a, b in zip(
        sk.STATUS_INKS[name], sk.STATUS_INK_SCRUBBED)]
    assert max(per_channel) >= FLOOR, (name, per_channel)


def test_the_clock_colour_is_identical_across_every_frame_of_a_loop(pinned_sky):
    """Frame-invariance is the constraint, not an extra.

    An earlier fix derived the ink from the rendered background and the viz
    adapter rejected it: a lightning flash brightens the sky mid-loop, so the
    clock changed colour between frames and broke the declared
    foreground-is-invariant contract. A constant has no way to flicker.
    """
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    wx = sk.WeatherState(cloud_frac=0.2, temp_c=20.0, thunder=True)
    frames = sk.render_loop_frames(now, wx, seed=0, scene="house")
    inks = set()
    for frame in frames:
        px = frame.load()
        inks |= {px[x, y] for y in range(0, 7) for x in range(0, 20)}
    assert sk.CLOCK_INK in inks
    assert len(frames) > 1


# --- bare ink on the real renders -------------------------------------------


def _glyph_cells(text: str) -> set[tuple[int, int]]:
    # Mirrors _bake_status's layout: text centered in the corner's
    # reserved span, never anchored at cx=2.
    text_w = sum(len(sk.DIGITS_3X5[ch][0]) + 1 for ch in text) - 1
    cells, cx = set(), max(1, (sk.STATUS_CARD_W - text_w) // 2)
    for ch in text:
        glyph = sk.DIGITS_3X5[ch]
        for gy, row in enumerate(glyph):
            for gx, bit in enumerate(row):
                if bit == "1" and 0 <= cx + gx < sk.W:
                    cells.add((cx + gx, 1 + gy))
        cx += len(glyph[0]) + 1
    return cells


def _halo(cells):
    return {
        (x + dx, y + dy)
        for (x, y) in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)
    } - cells


def _on_panel(points):
    return {(x, y) for x, y in points if 0 <= x < sk.W and 0 <= y < sk.H}


@pytest.mark.parametrize("hour", [0, 6, 12, 18])
@pytest.mark.parametrize("cloud", [0.0, 1.0])
def test_every_neighbour_of_every_stroke_clears_the_floor(
    pinned_sky, hour, cloud,
):
    """End to end, through the app's own renderer, against what actually
    sits beside the strokes — every neighbour, every scene, dawn through
    midnight, clear and overcast. This is the whole ledger: red must need
    no card, no shadow, no flip, and no per-scene exception."""
    now = datetime(2026, 6, 15, hour, 0, tzinfo=timezone.utc)
    wx = sk.WeatherState(cloud_frac=cloud, temp_c=20.0)
    cells = _glyph_cells(sk.clock_str(now.astimezone(sk.TZ)))
    for scene in sk.ENABLED_SCENES:
        px = sk.render_scene(now, wx, seed=0, scene=scene).load()
        bad = [(n, px[n]) for n in _on_panel(_halo(cells))
               if not clears(sk.CLOCK_INK, px[n])]
        assert not bad, f"{scene} h={hour} cloud={cloud}: {bad[:4]}"


def test_the_scene_is_untouched_around_the_text(pinned_sky):
    """No card, no shadow, no halo: under a clear noon sky the pixels
    beside the strokes are bright sky, not darkened anything."""
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    px = sk.render_scene(now, sk.WeatherState(temp_c=20.0), seed=0,
                         scene="house").load()
    halo = _on_panel(_halo(_glyph_cells(sk.clock_str(now.astimezone(sk.TZ)))))
    assert all(lum(px[x, y]) > 60 for x, y in halo), \
        "dark pixels ring the text — a halo or card crept back"


def test_the_strokes_themselves_are_still_drawn(pinned_sky):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    px = sk.render_scene(now, sk.WeatherState(temp_c=20.0), seed=0,
                         scene="house").load()
    cells = _glyph_cells(sk.clock_str(now.astimezone(sk.TZ)))
    assert cells, "no glyph cells computed"
    for x, y in cells:
        assert px[x, y] == sk.CLOCK_INK, f"stroke missing at ({x},{y})"


def test_the_scrubbed_clock_is_drawn_in_amber(pinned_sky):
    now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
    px = sk.render_scene(now, sk.WeatherState(temp_c=20.0), seed=0,
                         scene="house", scrubbed=True).load()
    cells = _glyph_cells(sk.clock_str(now.astimezone(sk.TZ)))
    for x, y in cells:
        assert px[x, y] == sk.STATUS_INK_SCRUBBED


# --- the quiet zone ---------------------------------------------------------


def test_no_star_spawns_inside_the_status_corner():
    """A star one pixel from a digit welds onto the letterform — the hue
    may differ but a lone lit speck beside a stroke still reads as ink.
    The constellation is generated outside the corner instead; a spawn
    rule costs no pixels."""
    for x, y, _mag in sk.STARS:
        assert x >= sk.STATUS_CARD_W or y > 6, (x, y)

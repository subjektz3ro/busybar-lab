"""The eclipse where it actually has to be right: pixels and speech.

`test_skystrip_eclipse.py` pins the astronomy against NASA's catalogue.
This file pins what Skystrip does with it — that the shadow lands on the
correct limb of the drawn disc, that the moonlight it casts falls with it,
and that all three report styles speak one set of numbers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from PIL import Image

from apps.skystrip_app import config as sky_config
from apps.skystrip_app import eclipse as sky_eclipse
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain
from apps.skystrip_app.render import art as sky_render_art
from apps.skystrip_app.render import astronomy as sky_render_astronomy
from apps.skystrip_app.render import primitives as sky_render_primitives
from apps.skystrip_app.render import scene as sky_render_scene
from apps.skystrip_app.eclipse import state_at

GREATEST = datetime(2026, 8, 28, 4, 13, tzinfo=timezone.utc)
CENTRE = (36, 8)  # middle of the panel, clear of every edge
# Public landmark fixture: Chicago O'Hare International Airport (KORD), not a
# home or operator location.
CHICAGO_OHARE_LATITUDE = 41.9742
CHICAGO_OHARE_LONGITUDE = -87.9073


@pytest.fixture(autouse=True)
def _chicago(monkeypatch):
    """Pin the location this eclipse is visible from.

    Skystrip's module defaults are the deliberate 0,0 / UTC fallback for an
    unconfigured install. Tests that assert on local contact times must not
    inherit it, or they pin the Gulf of Guinea by accident.
    """
    from astral import Observer

    monkeypatch.setattr(sky_settings, "OBSERVER",
        Observer(
            latitude=CHICAGO_OHARE_LATITUDE,
            longitude=CHICAGO_OHARE_LONGITUDE,
        ),
    )
    monkeypatch.setattr(sky_settings, "TZ", sky_config.ZoneInfo("America/Chicago"))
    monkeypatch.setattr(sky_render_astronomy, "_ECLIPSE_CACHE", None)
    yield
    sky_render_astronomy._ECLIPSE_CACHE = None


def _moon(when: datetime, *, phase_days: float = 14.8) -> dict:
    """Draw only the moon on a black field and return {(dx, dy): rgb}."""
    image = Image.new("RGB", (sky_limits.W, sky_limits.H), (0, 0, 0))
    sky_render_astronomy._draw_moon(image.load(), *CENTRE, phase_days, 0.0, state_at(when))
    px = image.load()
    return {(dx, dy): px[CENTRE[0] + dx, CENTRE[1] + dy]
            for dx in range(-3, 4) for dy in range(-3, 4)}


def _classify(rgb: tuple[int, int, int]) -> str:
    """Name a drawn pixel by hue, not by an exact palette match.

    The moon's halo is added over the disc after it is drawn, so a shadowed
    pixel reaches the frame as (151, 53, 23) rather than the (150, 52, 22)
    the palette names. An equality test silently reports "no shadow" for
    every partly-lit moment of the eclipse — which is most of it.
    """
    red, green, blue = rgb
    if sum(rgb) < 40:
        return "sky"
    if red > 2 * green and green > blue:  # copper: only the umbra is warm
        return "rim" if red > 110 else "ember"
    if blue > 80:  # cream, its maria, and the terminator are all neutral
        return "lit"
    return "other"


def _shape(when: datetime) -> str:
    """The disc as a 7x7 picture, for asserting on the whole thing at once."""
    disc = _moon(when)
    rows = []
    for dy in range(-3, 4):
        rows.append("".join(
            {"lit": "O", "rim": "r", "ember": "e", "sky": ".", "other": "?"}[
                _classify(disc[(dx, dy)])]
            for dx in range(-3, 4)))
    return "\n".join(rows)


class TestTheShadowLandsOnTheRightLimb:
    """Tonight gamma is positive and the Moon moves east, so Earth's shadow
    arrives at the lower-left, deepens upward, and leaves at the lower-right.
    A sign error anywhere between Meeus' frame and the panel mirrors this,
    and a mirrored eclipse is wrong in a way only the sky can contradict."""

    def test_no_shadow_before_first_contact(self):
        assert "r" not in _shape(GREATEST - timedelta(hours=2))
        assert "e" not in _shape(GREATEST - timedelta(hours=2))

    @staticmethod
    def _centroid(disc, *, kinds):
        picked = [xy for xy, c in disc.items() if _classify(c) in kinds]
        assert picked, f"nothing classified as {kinds}"
        return (sum(dx for dx, _ in picked) / len(picked),
                sum(dy for _, dy in picked) / len(picked))

    # The umbra is 2.7 Moon-radii across, so its edge crosses this disc as a
    # shallow arc rather than a steep one: an advancing shadow covers the
    # whole bottom of the Moon, not only its left. The direction it leans is
    # the falsifiable claim, not a hard bound on any single pixel.

    def test_the_bite_leans_lower_left_on_the_way_in(self):
        centre = self._centroid(_moon(GREATEST - timedelta(minutes=50)),
                                kinds=("rim", "ember"))
        assert centre[0] < -0.3, centre
        assert centre[1] > 0.3, centre

    def test_what_stays_lit_on_the_way_in_is_the_upper_right(self):
        centre = self._centroid(_moon(GREATEST - timedelta(minutes=50)),
                                kinds=("lit",))
        assert centre[0] > 0.3, centre
        assert centre[1] < -0.3, centre

    def test_the_bite_leans_lower_right_on_the_way_out(self):
        centre = self._centroid(_moon(GREATEST + timedelta(minutes=50)),
                                kinds=("rim", "ember"))
        assert centre[0] > 0.3, centre
        assert centre[1] > 0.3, centre

    def test_what_stays_lit_on_the_way_out_is_the_upper_left(self):
        centre = self._centroid(_moon(GREATEST + timedelta(minutes=50)),
                                kinds=("lit",))
        assert centre[0] < -0.3, centre
        assert centre[1] < -0.3, centre

    def test_the_shadow_sweeps_left_to_right(self):
        """The shadow's centre of mass must travel one way across the disc.
        A stationary bite that merely grows and shrinks would satisfy every
        'is some of it dark' check while depicting the wrong event."""
        centroids = []
        for minutes in (-70, -35, 0, 35, 70):
            disc = _moon(GREATEST + timedelta(minutes=minutes))
            dark = [dx for (dx, _), c in disc.items()
                    if _classify(c) in ("rim", "ember")]
            centroids.append(sum(dark) / len(dark))
        assert centroids == sorted(centroids), centroids

    def test_the_surviving_sliver_is_at_the_top(self):
        """At 0.93 magnitude the northern limb stays in sunlight. If the
        strip shows a fully dark disc it is claiming a totality that is not
        happening tonight."""
        disc = _moon(GREATEST)
        lit = [xy for xy, c in disc.items() if _classify(c) == "lit"]
        assert lit, "a partial eclipse must leave something lit"
        assert max(dy for _, dy in lit) < 0, "the sliver belongs up top"
        shadowed = [xy for xy, c in disc.items()
                    if _classify(c) in ("rim", "ember")]
        assert len(shadowed) > len(lit) * 3, "0.93 magnitude is nearly all of it"


class TestTheShadowIsCopperNotBlack:
    def test_the_two_shadow_levels_clear_the_panel_contrast_floor(self):
        """The device crushes brightness deltas under about 30% per channel
        (busybar-app law 5), so a shadow gradient finer than that is a
        gradient nobody sees."""
        for a, b in zip(sky_render_primitives.MOON_UMBRA_RIM, sky_render_primitives.MOON_UMBRA_EMBER):
            assert (a - b) / a >= 0.30, (sky_render_primitives.MOON_UMBRA_RIM, sky_render_primitives.MOON_UMBRA_EMBER)

    def test_the_shadow_edge_is_unmistakable_against_the_lit_moon(self):
        for lit, shadow in zip(sky_render_art.MOON_COLOR, sky_render_primitives.MOON_UMBRA_RIM):
            assert (lit - shadow) / lit >= 0.30

    def test_the_umbra_is_never_drawn_as_earthshine(self):
        """Earthshine is the blue-grey of an unlit crescent. Earth's shadow
        is refracted sunlight and reads copper; reusing the crescent colour
        would draw the wrong physical thing."""
        disc = _moon(GREATEST)
        assert sky_render_primitives.MOON_EARTHSHINE not in disc.values()

    def test_shadowed_pixels_are_warm(self):
        disc = _moon(GREATEST)
        for rgb in disc.values():
            if _classify(rgb) in ("rim", "ember"):
                assert rgb[0] > rgb[1] > rgb[2], rgb


class TestPenumbraIsNotDrawn:
    """A penumbral eclipse dims the Moon by a few percent. Drawing one would
    invent a spectacle nobody outside can confirm."""

    def test_the_penumbral_phase_of_tonight_leaves_the_disc_clean(self):
        before_partial = GREATEST - timedelta(hours=2)
        state = state_at(before_partial)
        assert state is not None and state.phase == "penumbral"
        assert set(_shape(before_partial).replace("\n", "")) <= {"O", "."}

    def test_a_wholly_penumbral_eclipse_is_never_drawn(self):
        penumbral_only = datetime(2027, 2, 20, 23, 13, tzinfo=timezone.utc)
        assert state_at(penumbral_only).in_umbra is False
        assert set(_shape(penumbral_only).replace("\n", "")) <= {"O", "."}


def test_moonlight_falls_with_the_eclipsed_disc():
    """The silver pool, the roofline rim and the tower sheen all scale with
    the lit fraction. A 96%-covered moon flooding the yard with light is the
    same lie as drawing the disc uncovered, one layer further out."""
    wx = sky_weather.WeatherState(cloud_frac=0.0, temp_c=20.0)
    clean = sky_render_scene.render_scene(GREATEST - timedelta(hours=3), wx, seed=7)
    eclipsed = sky_render_scene.render_scene(GREATEST, wx, seed=7)

    def ground_light(image):
        px = image.load()
        return sum(sum(px[x, y]) for x in range(sky_limits.W) for y in range(13, sky_limits.H))

    assert ground_light(eclipsed) < ground_light(clean) * 0.9


def test_an_eclipse_below_the_horizon_is_not_drawn(monkeypatch):
    """Half the planet cannot see any given eclipse. The gate is the moon's
    real altitude, so pointing Skystrip at Tokyo must leave the sky alone."""
    from astral import Observer
    monkeypatch.setattr(sky_settings, "OBSERVER", Observer(latitude=35.68,
                                                longitude=139.69))
    sky_render_astronomy._ECLIPSE_CACHE = None
    assert sky_render_astronomy._eclipse_now(GREATEST) is None


def test_a_broken_eclipse_calculation_loses_the_shadow_not_the_sky(monkeypatch):
    """This runs inside the frame renderer. A night with no moon at all is a
    far worse failure than a night with an unmarked eclipse."""
    def boom(*_args, **_kwargs):
        raise RuntimeError("ephemeris on fire")

    monkeypatch.setattr(sky_eclipse, "visible_state", boom)
    sky_render_astronomy._ECLIPSE_CACHE = None
    assert sky_render_astronomy._eclipse_now(GREATEST) is None
    image = sky_render_scene.render_scene(GREATEST, sky_weather.WeatherState(temp_c=20.0), seed=7)
    assert any(sum(image.load()[x, y]) > 200
               for x in range(sky_limits.W) for y in range(sky_limits.H)), "sky went dark"


class TestEveryStyleSpeaksTheSameNumbers:
    """skystrip.md's standing promise for `genz`: every number it speaks is
    identical to what `plain` would have said."""

    @staticmethod
    def _report(style: str, when: datetime) -> str:
        original = sky_settings.STYLE
        sky_settings.STYLE = style
        try:
            return sky_audio_report_plain._compose_report(
                sky_weather.WeatherState(cloud_frac=0.05, temp_c=22.0), None,
                when.astimezone(sky_settings.TZ), None)
        finally:
            sky_settings.STYLE = original

    @pytest.mark.parametrize("moment", [
        GREATEST, GREATEST - timedelta(hours=2), GREATEST - timedelta(minutes=40),
    ])
    def test_the_obscuration_and_time_agree_across_styles(self, moment):
        import re
        numbers = {style: re.findall(r"\d+(?::\d+)?",
                                     self._report(style, moment))
                   for style in ("plain", "chicago", "genz")}
        assert numbers["plain"] == numbers["chicago"] == numbers["genz"]

    def test_the_percentage_is_area_covered_not_umbral_magnitude(self):
        """0.926 magnitude covers 96% of the face. Speaking the catalogue's
        number as if it described the face is wrong by four points."""
        assert "96 percent" in self._report("plain", GREATEST)
        assert "93 percent" not in self._report("plain", GREATEST)

    def test_the_phase_line_is_replaced_not_joined(self):
        """During an eclipse the moon is always full, so "the moon is full,
        and also in Earth's shadow" makes the first clause noise."""
        spoken = self._report("plain", GREATEST)
        assert "eclipse" in spoken
        assert "The moon is full tonight" not in spoken

    def test_an_ordinary_night_still_gets_its_phase_line(self):
        ordinary = datetime(2026, 7, 29, 4, 0, tzinfo=timezone.utc)
        spoken = self._report("plain", ordinary)
        assert "eclipse" not in spoken
        assert "The moon is" in spoken

    def test_the_heads_up_names_first_contact_not_greatest(self):
        spoken = self._report("plain", GREATEST - timedelta(hours=2))
        assert "9:34" in spoken, spoken
        assert "11:13" not in spoken

    def test_no_heads_up_once_the_eclipse_has_ended(self):
        after = GREATEST + timedelta(hours=4)
        assert "eclipse" not in self._report("plain", after)

    def test_a_severe_warning_still_silences_the_genz_bit(self):
        """A Tornado Warning drops the flavour for the whole report — an
        eclipse is not a reason to reopen it."""
        original = sky_settings.STYLE
        sky_settings.STYLE = "genz"
        try:
            spoken = sky_audio_report_plain._compose_report(
                sky_weather.WeatherState(severe=True, thunder=True, temp_c=22.0,
                               severe_event="Tornado Warning"),
                None, GREATEST.astimezone(sky_settings.TZ), None)
        finally:
            sky_settings.STYLE = original
        assert "eclipse" not in spoken
        assert spoken.endswith("Stay safe out there.")

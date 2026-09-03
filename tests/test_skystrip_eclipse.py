"""Lunar eclipse math, pinned against published circumstances.

Every expectation here comes from NASA's five-millennium lunar eclipse
catalogue, not from this module's own output. A test that asserts the code
agrees with itself would pass just as happily with a sign error in gamma,
which is the one term that decides which limb the shadow bites.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

import pytest

from apps.skystrip_eclipse import (
    MOON_RADIUS,
    EclipseState,
    eclipses_near,
    state_at,
)

# Public landmark fixture: Chicago O'Hare International Airport (KORD).
# It preserves the Chicago timezone/visibility semantics without identifying a
# home or operator location.
CHICAGO_OHARE_LATITUDE = 41.9742
CHICAGO_OHARE_LONGITUDE = -87.9073


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text).replace(tzinfo=timezone.utc)


# NASA GSFC five-millennium catalogue, greatest eclipse in UTC with the
# published umbral magnitude and gamma. Penumbral events carry a negative
# umbral magnitude, which is how the catalogue reports a total miss.
CATALOGUE = [
    ("2025-03-14 06:58", "total", 1.178, +0.3475),
    ("2025-09-07 18:11", "total", 1.362, -0.2762),
    ("2026-03-03 11:33", "total", 1.151, -0.3791),
    ("2026-08-28 04:13", "partial", 0.926, +0.4995),
    ("2027-02-20 23:13", "penumbral", -0.064, -1.0530),
]


@pytest.mark.parametrize("moment,kind,magnitude,gamma", CATALOGUE)
def test_matches_the_published_catalogue(moment, kind, magnitude, gamma):
    published = _utc(moment)
    found = [e for e in eclipses_near(published)
             if abs((e.greatest - published).total_seconds()) < 3600]
    assert len(found) == 1, f"no single eclipse near {moment}"
    eclipse = found[0]

    # Meeus' series is good to well under a minute for contemporary dates;
    # two minutes leaves room for the catalogue's own rounding.
    drift = abs((eclipse.greatest - published).total_seconds())
    assert drift < 120, f"greatest eclipse off by {drift:.0f}s"
    assert eclipse.kind == kind
    assert eclipse.umbral_magnitude == pytest.approx(magnitude, abs=0.01)
    # Sign, not just size: gamma's sign is which side of the shadow axis the
    # Moon passes, and therefore which limb goes dark first.
    assert eclipse.gamma == pytest.approx(gamma, abs=0.005)


def test_finds_no_eclipse_at_an_ordinary_full_moon():
    # 2026-07-29 is a full moon far from a node. Nothing should be found
    # whose penumbral window covers it.
    assert state_at(_utc("2026-07-29 12:00")) is None


def test_penumbral_only_eclipse_is_never_drawn():
    """February 2027 is penumbral: real, and invisible to the naked eye."""
    state = state_at(_utc("2027-02-20 23:13"))
    assert state is not None
    assert state.phase == "penumbral"
    assert state.in_umbra is False
    assert state.obscuration == 0.0


class TestTonight:
    """2026-08-28, the deep partial. Contact times from the catalogue."""

    GREATEST = _utc("2026-08-28 04:13")

    def test_contact_times_match_the_catalogue(self):
        eclipse = state_at(self.GREATEST).eclipse
        partial = eclipse.contact("partial")
        penumbral = eclipse.contact("penumbral")
        assert eclipse.contact("total") is None  # it never goes total

        for got, expected in (
            (partial[0], _utc("2026-08-28 02:34")),
            (partial[1], _utc("2026-08-28 05:52")),
            (penumbral[0], _utc("2026-08-28 01:24")),
            (penumbral[1], _utc("2026-08-28 07:02")),
        ):
            assert abs((got - expected).total_seconds()) < 180

    def test_the_shadow_enters_from_the_lower_left(self):
        """The Moon moves east through the shadow, so the shadow appears to
        come at it from the east — screen-left in a northern view. Gamma is
        positive tonight, putting the bite on the southern, lower limb."""
        early = state_at(self.GREATEST - timedelta(minutes=40))
        assert early.umbra_dx < 0, "shadow should be left of centre before max"
        assert early.umbra_dy > 0, "positive gamma puts the umbra below"

    def test_the_shadow_leaves_to_the_lower_right(self):
        late = state_at(self.GREATEST + timedelta(minutes=40))
        assert late.umbra_dx > 0
        assert late.umbra_dy > 0

    def test_the_umbra_crosses_the_centre_line_exactly_at_greatest(self):
        assert state_at(self.GREATEST).umbra_dx == pytest.approx(0, abs=0.02)

    def test_a_sliver_survives_at_greatest(self):
        """A 0.926-magnitude partial leaves the northern limb in sunlight.
        If this ever reads as fully covered, the strip is drawing a total
        eclipse that is not happening."""
        state = state_at(self.GREATEST)
        assert state.phase == "partial"
        assert 0.85 < state.obscuration < 0.98

    def test_obscuration_is_area_not_diameter(self):
        """The catalogue's 0.926 is a fraction of the *diameter*. Area
        coverage is a different number and the report speaks the area one;
        conflating them is how a wrong percentage reaches the speaker."""
        state = state_at(self.GREATEST)
        assert state.eclipse.umbral_magnitude == pytest.approx(0.926, abs=0.01)
        assert abs(state.obscuration - state.eclipse.umbral_magnitude) > 0.01

    def test_obscuration_rises_then_falls(self):
        depths = [state_at(self.GREATEST + timedelta(minutes=m)).obscuration
                  for m in (-95, -60, -20, 0, 20, 60, 95)]
        assert depths == sorted(depths[:4]) + sorted(depths[4:], reverse=True)
        assert depths[0] < 0.05 and depths[-1] < 0.05

    def test_outside_the_penumbral_window_there_is_no_state(self):
        assert state_at(self.GREATEST - timedelta(hours=4)) is None
        assert state_at(self.GREATEST + timedelta(hours=4)) is None


def test_umbra_is_much_larger_than_the_moon():
    """Earth's umbra at lunar distance is roughly 2.6 Moon-diameters across.
    The renderer scales its shadow circle by this, so a units slip here
    would draw a shadow the size of the Moon itself."""
    state = state_at(TestTonight.GREATEST)
    assert 2.5 < state.umbra_r < 2.9


def test_total_eclipse_reports_full_obscuration():
    state = state_at(_utc("2026-03-03 11:33"))
    assert state.phase == "total"
    assert state.obscuration == pytest.approx(1.0)
    assert state.in_umbra is True


def test_state_requires_an_aware_datetime():
    with pytest.raises(ValueError):
        state_at(datetime(2026, 8, 28, 4, 13))


def test_visible_state_is_gated_on_the_moon_being_up():
    """Tonight's eclipse is over the Americas. Someone in Tokyo is asleep
    under a daylit sky and must not be shown Earth's shadow."""
    from astral import Observer

    from apps.skystrip_eclipse import visible_state

    chicago = Observer(latitude=CHICAGO_OHARE_LATITUDE,
                       longitude=CHICAGO_OHARE_LONGITUDE)
    tokyo = Observer(latitude=35.68, longitude=139.69)
    assert visible_state(TestTonight.GREATEST, chicago) is not None
    assert visible_state(TestTonight.GREATEST, tokyo) is None


def test_overlap_area_matches_a_numeric_integration():
    """The circular-lens formula is easy to get subtly wrong at the
    containment boundaries, so check it against brute force."""
    from apps.skystrip_eclipse import _overlap_area

    for r1, r2, d in ((1.0, 2.7, 1.8), (1.0, 2.7, 0.5), (1.0, 1.0, 1.5),
                      (0.3, 2.0, 1.71), (1.0, 2.7, 3.69)):
        step = 0.004
        hits = 0
        total = 0
        n = int(2 * r1 / step)
        for i in range(n):
            for j in range(n):
                x = -r1 + (i + 0.5) * step
                y = -r1 + (j + 0.5) * step
                if x * x + y * y > r1 * r1:
                    continue
                total += 1
                if (x - d) ** 2 + y * y <= r2 * r2:
                    hits += 1
        numeric = math.pi * r1 * r1 * hits / total
        assert _overlap_area(r1, r2, d) == pytest.approx(numeric, abs=0.02)


def test_moon_radius_is_consistent_with_the_contact_radii():
    """Meeus' contact radii are only self-consistent with MOON_RADIUS; the
    published semiduration formulae use 1.0128 and 0.4678, which are the
    umbral radius plus and minus one Moon radius."""
    eclipse = state_at(TestTonight.GREATEST).eclipse
    assert eclipse.umbral_radius + MOON_RADIUS == pytest.approx(
        1.0128 - eclipse.u, abs=1e-9)
    assert eclipse.umbral_radius - MOON_RADIUS == pytest.approx(
        0.4678 - eclipse.u, abs=1e-9)
    assert eclipse.penumbral_radius + MOON_RADIUS == pytest.approx(
        1.5573 + eclipse.u, abs=1e-9)


def test_eclipse_state_is_immutable():
    state = state_at(TestTonight.GREATEST)
    assert isinstance(state, EclipseState)
    with pytest.raises(AttributeError):
        state.obscuration = 0.5  # type: ignore[misc]

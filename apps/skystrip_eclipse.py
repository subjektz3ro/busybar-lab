"""Lunar eclipse geometry, computed locally from Meeus.

Skystrip's sun and moon are already local math rather than an API call, and
an eclipse belongs on the same footing: a network outage must not be able to
make the strip draw a clean full moon on a night the real one is copper.
There is no ephemeris file and no download — this is Meeus, *Astronomical
Algorithms* 2nd ed., chapter 54, whose periodic terms reproduce NASA's
five-millennium catalogue to well under a minute for contemporary dates.
``tests/test_skystrip_eclipse.py`` pins that agreement against published
circumstances rather than against this module's own output.

Units follow Meeus throughout: distances in the fundamental plane are in
Earth equatorial radii, where the Moon's own radius is ``MOON_RADIUS``. The
drawing layer wants moon-radii on screen axes, so :class:`EclipseState`
converts once, at the boundary, and nothing downstream re-derives it.

Deliberately **lunar only**. A solar eclipse changes the daylight gradient,
the sun icon, and the ambient wash on every scene at once, which is a
different and larger problem than putting Earth's shadow on a 7-pixel disc.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

# Meeus ch. 54. The Moon's radius in units of Earth's equatorial radius; the
# umbral and penumbral contact radii below are only self-consistent with it.
MOON_RADIUS = 0.2725

# A synodic month, for stepping the lunation index k between full moons.
SYNODIC_DAYS = 29.530588861

# Terrestrial Time minus UTC. Real value drifts with leap seconds; at this
# magnitude it moves contact times by ~1 second, which is far below anything
# a one-minute-per-frame strip can express. Not worth a leap-second table.
TT_MINUS_UTC_S = 69.2

# An eclipse is only possible when the Moon is near a node. Meeus gives this
# as the cheap rejection test before any of the periodic series are summed.
_NODE_REJECT = 0.36


def _sin(deg: float) -> float:
    return math.sin(math.radians(deg))


def _cos(deg: float) -> float:
    return math.cos(math.radians(deg))


def _jde_to_utc(jde: float) -> datetime:
    """Julian Ephemeris Day (TT) to an aware UTC datetime."""
    unix_days = jde - 2440587.5
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(days=unix_days)
            - timedelta(seconds=TT_MINUS_UTC_S))


@dataclass(frozen=True)
class LunarEclipse:
    """One eclipse's circumstances, independent of any observer.

    ``gamma`` is the least distance from the Moon's centre to the axis of
    Earth's shadow, positive when the Moon passes north of it. Its sign is
    what decides which limb the shadow bites, so it survives to the renderer
    rather than being collapsed into an absolute value here.
    """

    greatest: datetime          # UTC, instant of greatest eclipse
    gamma: float                # Earth radii, signed (+ = Moon north of axis)
    u: float                    # Meeus' shadow-radius parameter
    hourly_motion: float        # Earth radii per hour, along the shadow plane

    @property
    def umbral_radius(self) -> float:
        return 0.7403 - self.u

    @property
    def penumbral_radius(self) -> float:
        return 1.2848 + self.u

    @property
    def umbral_magnitude(self) -> float:
        """Fraction of the Moon's *diameter* inside the umbra at greatest.

        This is the number eclipse catalogues quote. It is not the fraction
        of the visible face covered — see :attr:`EclipseState.obscuration`
        for that. Speaking one while meaning the other is the easy way to
        put a wrong number in the report.
        """
        return (self.umbral_radius + MOON_RADIUS - abs(self.gamma)) / (
            2 * MOON_RADIUS)

    @property
    def penumbral_magnitude(self) -> float:
        return (self.penumbral_radius + MOON_RADIUS - abs(self.gamma)) / (
            2 * MOON_RADIUS)

    @property
    def kind(self) -> str:
        """``total``, ``partial`` or ``penumbral``."""
        if self.umbral_magnitude >= 1.0:
            return "total"
        if self.umbral_magnitude > 0.0:
            return "partial"
        return "penumbral"

    def _semiduration_h(self, radius: float) -> float:
        """Half the time the Moon spends inside a shadow of `radius`."""
        chord = radius * radius - self.gamma * self.gamma
        if chord <= 0:
            return 0.0
        return math.sqrt(chord) / self.hourly_motion

    def contact(self, which: str) -> tuple[datetime, datetime] | None:
        """UTC (begin, end) of ``penumbral``, ``partial`` or ``total``."""
        radius = {
            "penumbral": self.penumbral_radius + MOON_RADIUS,
            "partial": self.umbral_radius + MOON_RADIUS,
            "total": self.umbral_radius - MOON_RADIUS,
        }[which]
        half = self._semiduration_h(radius)
        if half <= 0:
            return None
        span = timedelta(hours=half)
        return self.greatest - span, self.greatest + span


def _eclipse_at_lunation(k: float) -> LunarEclipse | None:
    """Meeus 54: circumstances of the eclipse at full-moon index `k`, if any.

    `k` must be an integer plus 0.5 (integers are new moons, hence solar).
    Returns None when the Moon misses even the penumbra.
    """
    t = k / 1236.85
    jde = (2451550.09766 + 29.530588861 * k + 0.00015437 * t**2
           - 0.000000150 * t**3 + 0.00000000073 * t**4)
    # Eccentricity correction on terms involving the Sun's anomaly.
    e = 1 - 0.002516 * t - 0.0000074 * t**2
    # Sun's mean anomaly, Moon's mean anomaly, Moon's argument of latitude,
    # and the longitude of the ascending node.
    ms = 2.5534 + 29.10535670 * k - 0.0000014 * t**2 - 0.00000011 * t**3
    mm = (201.5643 + 385.81693528 * k + 0.0107582 * t**2
          + 0.00001238 * t**3 - 0.000000058 * t**4)
    f = (160.7108 + 390.67050284 * k - 0.0016118 * t**2
         - 0.00000227 * t**3 + 0.000000011 * t**4)
    om = 124.7746 - 1.56375588 * k + 0.0020672 * t**2 + 0.00000215 * t**3

    # Too far from a node for the Moon to touch even the penumbra.
    if abs(_sin(f)) > _NODE_REJECT:
        return None

    f1 = f - 0.02665 * _sin(om)
    a1 = 299.77 + 0.107408 * k - 0.009173 * t**2

    jde += (-0.4065 * _sin(mm)
            + 0.1727 * e * _sin(ms)
            + 0.0161 * _sin(2 * mm)
            - 0.0097 * _sin(2 * f1)
            + 0.0073 * e * _sin(mm - ms)
            - 0.0050 * e * _sin(mm + ms)
            - 0.0023 * _sin(mm - 2 * f1)
            + 0.0021 * e * _sin(2 * ms)
            + 0.0012 * _sin(mm + 2 * f1)
            + 0.0006 * e * _sin(2 * mm + ms)
            - 0.0004 * _sin(3 * mm)
            - 0.0003 * e * _sin(ms + 2 * f1)
            + 0.0003 * _sin(a1)
            - 0.0002 * e * _sin(ms - 2 * f1)
            - 0.0002 * e * _sin(2 * mm - ms)
            - 0.0002 * _sin(om))

    p = (0.2070 * e * _sin(ms) + 0.0024 * e * _sin(2 * ms) - 0.0392 * _sin(mm)
         + 0.0116 * _sin(2 * mm) - 0.0073 * e * _sin(mm + ms)
         + 0.0067 * e * _sin(mm - ms) + 0.0118 * _sin(2 * f1))
    q = (5.2207 - 0.0048 * e * _cos(ms) + 0.0020 * e * _cos(2 * ms)
         - 0.3299 * _cos(mm) - 0.0060 * e * _cos(mm + ms)
         + 0.0041 * e * _cos(mm - ms))
    w = abs(_cos(f1))
    gamma = (p * _cos(f1) + q * _sin(f1)) * (1 - 0.0048 * w)
    u = (0.0059 + 0.0046 * e * _cos(ms) - 0.0182 * _cos(mm)
         + 0.0004 * _cos(2 * mm) - 0.0005 * e * _cos(ms + mm))

    eclipse = LunarEclipse(
        greatest=_jde_to_utc(jde),
        gamma=gamma,
        u=u,
        hourly_motion=0.5458 + 0.0400 * _cos(mm),
    )
    # The node test above is a coarse filter; this is the real one.
    if eclipse.penumbral_magnitude <= 0:
        return None
    return eclipse


def _lunation_index(when: datetime) -> float:
    """Approximate full-moon index k for `when`, as an integer plus 0.5."""
    year = when.year + (when.timetuple().tm_yday - 1) / 365.25
    return round((year - 2000) * 12.3685 - 0.5) + 0.5


def eclipses_near(when: datetime, *, lunations: int = 2) -> list[LunarEclipse]:
    """Every lunar eclipse within a few lunations of `when`, in time order."""
    base = _lunation_index(when)
    found = []
    for step in range(-lunations, lunations + 1):
        eclipse = _eclipse_at_lunation(base + step)
        if eclipse is not None:
            found.append(eclipse)
    return sorted(found, key=lambda ecl: ecl.greatest)


def _overlap_area(r1: float, r2: float, d: float) -> float:
    """Area shared by two circles of radii `r1`, `r2` whose centres are `d`
    apart. Standard circular-lens formula, with both containment cases."""
    if d >= r1 + r2:
        return 0.0
    if d <= abs(r1 - r2):
        return math.pi * min(r1, r2) ** 2
    a1 = math.acos((d * d + r1 * r1 - r2 * r2) / (2 * d * r1))
    a2 = math.acos((d * d + r2 * r2 - r1 * r1) / (2 * d * r2))
    triangle = 0.5 * math.sqrt(
        max(0.0, (-d + r1 + r2) * (d + r1 - r2)
            * (d - r1 + r2) * (d + r1 + r2)))
    return r1 * r1 * a1 + r2 * r2 * a2 - triangle


@dataclass(frozen=True)
class EclipseState:
    """How an eclipse looks at one instant, in the units the renderer wants.

    The offsets are in **Moon radii on screen axes** — x rightwards, y
    downwards — for a northern-hemisphere view. Getting from Meeus' frame to
    that one is two sign flips (east is screen-left, north is screen-up) and
    they live here, once, so ``_draw_moon`` can stay geometry-free.
    """

    eclipse: LunarEclipse
    phase: str            # "penumbral" | "partial" | "total"
    umbra_dx: float       # umbra centre offset from the Moon's centre
    umbra_dy: float
    umbra_r: float
    obscuration: float    # fraction of the Moon's disc *area* in the umbra

    @property
    def in_umbra(self) -> bool:
        """Whether anything is happening the naked eye can actually see.

        A penumbral eclipse dims the Moon by a few percent and is famously
        hard to notice even when you know it is underway. Skystrip does not
        draw one: inventing a visible shadow for an invisible event would be
        the same class of lie as drawing a full moon during totality.
        """
        return self.phase in ("partial", "total")


def state_at(when: datetime, *, eclipse: LunarEclipse | None = None
             ) -> EclipseState | None:
    """Geometry of whichever eclipse is underway at `when`, or None.

    `when` must be timezone-aware. Pass `eclipse` to evaluate a specific one
    (the report uses this to describe an event that has not started yet).
    """
    if when.tzinfo is None:
        raise ValueError("state_at() needs an aware datetime")
    candidates = [eclipse] if eclipse is not None else eclipses_near(when)
    for ecl in candidates:
        window = ecl.contact("penumbral")
        if window is None or not window[0] <= when <= window[1]:
            continue
        # Along-track offset of the Moon from the shadow axis, and the
        # constant across-track offset that is gamma by definition.
        dt_h = (when - ecl.greatest).total_seconds() / 3600.0
        along = ecl.hourly_motion * dt_h
        separation = math.hypot(along, ecl.gamma)

        if separation < ecl.umbral_radius - MOON_RADIUS:
            phase = "total"
        elif separation < ecl.umbral_radius + MOON_RADIUS:
            phase = "partial"
        else:
            phase = "penumbral"

        obscured = _overlap_area(MOON_RADIUS, ecl.umbral_radius, separation)
        return EclipseState(
            eclipse=ecl,
            phase=phase,
            # Meeus' frame has the Moon at (+along east, +gamma north) of the
            # shadow, so the shadow sits at the negative of that from the
            # Moon. Screen x is west-positive and screen y is south-positive,
            # which flips both signs back again.
            umbra_dx=along / MOON_RADIUS,
            umbra_dy=ecl.gamma / MOON_RADIUS,
            umbra_r=ecl.umbral_radius / MOON_RADIUS,
            obscuration=obscured / (math.pi * MOON_RADIUS ** 2),
        )
    return None


def visible_state(when: datetime, observer) -> EclipseState | None:
    """`state_at`, but only when the Moon is actually above the horizon.

    An eclipse is a whole-Earth event and half the planet cannot see it.
    Without this gate the strip would paint Earth's shadow on a moon that is
    below the horizon — or worse, on a daylit afternoon sky.
    """
    state = state_at(when)
    if state is None:
        return None
    from astral import moon as _moon  # local: keeps import cost off the path
    if _moon.elevation(observer, when) <= 0:
        return None
    return state

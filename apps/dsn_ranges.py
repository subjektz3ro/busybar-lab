"""NASA/JPL Horizons range enrichment for the DSN app.

The module owns range cache state, validation, persistence and the optional
Horizons worker.  Runtime dependencies are explicit so importing it performs
no network, filesystem, clock or device work, and the ``dsn`` entry point can
keep its established monkeypatch-friendly facade.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import importlib
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import dsn_source as _dsn_source
elif __package__:
    _dsn_source = importlib.import_module(".dsn_source", __package__)
else:
    _dsn_source = importlib.import_module("dsn_source")

Link = _dsn_source.Link
C_KM_S = _dsn_source.C_KM_S
MAX_RANGE_KM = _dsn_source.MAX_RANGE_KM


HORIZONS = "https://ssd.jpl.nasa.gov/api/horizons.api"
AU_LIGHT_S = 499.004784  # seconds of light per astronomical unit
RANGE_NEAR_EARTH_TTL_S = 5 * 60
RANGE_INTERMEDIATE_TTL_S = 30 * 60
RANGE_TTL_S = 6 * 3600  # maximum, for genuinely deep-space targets
RANGE_RETRY_S = 60  # failure backoff is not a successful zero range
RANGE_UNAVAILABLE_RETRY_S = RANGE_TTL_S  # Horizons has no record for this id
RANGE_CACHE_VERSION = 1


class HorizonsUnavailable(ValueError):
    """The official service answered, but has no ephemeris for this target."""


def horizons_au(body: str) -> float:
    """Extract Horizons' observer range, preserving a useful source error."""
    if "$$SOE" not in body or "$$EOE" not in body:
        detail = next(
            (
                line.strip()
                for line in body.splitlines()
                if line.strip() and not line.startswith("API ")
            ),
            "no ephemeris data",
        )
        if "no such record" in body.lower():
            raise HorizonsUnavailable(detail)
        # A proxy page, truncated success response or future API-format drift
        # is not evidence that the target lacks an ephemeris. Keep it on the
        # short transient retry path instead of suppressing checks for hours.
        raise ValueError(f"missing Horizons ephemeris table: {detail}")
    row = body.split("$$SOE", 1)[1].split("$$EOE", 1)[0].strip().split()
    if len(row) < 3:
        raise ValueError("empty Horizons ephemeris row")
    au = float(row[2])
    max_au = MAX_RANGE_KM / (AU_LIGHT_S * C_KM_S)
    if not math.isfinite(au) or not 0 < au <= max_au:
        raise ValueError("invalid Horizons observer range")
    return au


def range_ttl_s(km: float) -> float:
    """Return the range-sensitive lifetime for a geocentric observation."""
    if km < 2_000_000:
        return RANGE_NEAR_EARTH_TTL_S
    if km < 50_000_000:
        return RANGE_INTERMEDIATE_TTL_S
    return RANGE_TTL_S


def range_cache_fresh(entry: tuple[float, float], now: float) -> bool:
    """Validate a cached ``(km, observed_at)`` pair without clocks or I/O."""
    try:
        km, observed_at = float(entry[0]), float(entry[1])
    except (IndexError, TypeError, ValueError, OverflowError):
        return False
    if not (
        math.isfinite(km)
        and 0 < km <= MAX_RANGE_KM
        and math.isfinite(observed_at)
    ):
        return False
    age = now - observed_at
    return 0.0 <= age < range_ttl_s(km)


@dataclass
class RangeState:
    """The single owner of successful values and retry/availability state."""

    values: dict[int, tuple[float, float]] = field(default_factory=dict)
    retry_at: dict[int, float] = field(default_factory=dict)
    unavailable: set[int] = field(default_factory=set)

    def current(self, naif: int, now: float) -> float | None:
        """Return a defensible value, evicting it exactly at its TTL."""
        entry = self.values.get(naif)
        if entry is None:
            return None
        if not range_cache_fresh(entry, now):
            self.values.pop(naif, None)
            return None
        return float(entry[0])

    def replace_loaded(self, values: dict[int, tuple[float, float]]) -> None:
        """Atomically adopt a fully validated cache candidate."""
        self.values = dict(values)

    def note_native(self, naif: int) -> None:
        """Record authoritative feed range availability without moving retry."""
        self.unavailable.discard(naif)

    def note_success(self, naif: int, km: float, observed_at: float) -> None:
        """Atomically publish a successful value and clear negative state."""
        self.values[naif] = (km, observed_at)
        self.retry_at.pop(naif, None)
        self.unavailable.discard(naif)

    def note_unavailable(self, naif: int, retry_at: float) -> None:
        """Record a valid negative answer and its longer reconsideration time."""
        self.unavailable.add(naif)
        self.retry_at[naif] = retry_at

    def note_retry(self, naif: int, retry_at: float) -> None:
        """Move the retry deadline after a transient failure."""
        self.retry_at[naif] = retry_at

    def retry_due(self, naif: int, now: float) -> bool:
        """Whether an unresolved target may be queried at ``now``."""
        return now >= self.retry_at.get(naif, 0.0)


def cached_range(state: RangeState, naif: int, now: float) -> float | None:
    """Return a current cached value through its state owner."""
    return state.current(naif, now)


def load_ranges(
    state: RangeState,
    *,
    path: Path,
    clock: Callable[[], float],
    logger: logging.Logger,
) -> None:
    """Load a versioned cache atomically; malformed JSON is a cold start."""
    try:
        raw = json.loads(path.read_text())
        if (
            not isinstance(raw, dict)
            or raw.get("version") != RANGE_CACHE_VERSION
            or not isinstance(raw.get("ranges"), dict)
        ):
            raise ValueError("unsupported range-cache schema")
        now = clock()
        loaded: dict[int, tuple[float, float]] = {}
        for raw_naif, raw_entry in raw["ranges"].items():
            if (
                not isinstance(raw_naif, str)
                or not isinstance(raw_entry, list)
                or len(raw_entry) != 2
                or any(isinstance(value, bool) for value in raw_entry)
            ):
                raise ValueError("invalid range-cache entry")
            naif = int(raw_naif)
            km, observed_at = float(raw_entry[0]), float(raw_entry[1])
            if not (
                math.isfinite(km)
                and 0 < km <= MAX_RANGE_KM
                and math.isfinite(observed_at)
            ):
                raise ValueError("invalid range-cache value")
            entry = (km, observed_at)
            if range_cache_fresh(entry, now):
                loaded[naif] = entry
    except Exception as exc:  # noqa: BLE001 - a cold/corrupt cache is normal
        logger.debug("range cache ignored: %s", exc)
        return
    state.replace_loaded(loaded)
    if state.values:
        logger.info("loaded %d cached distances", len(state.values))


def save_ranges(
    state: RangeState,
    *,
    path: Path,
    clock: Callable[[], float],
    logger: logging.Logger,
) -> None:
    """Persist only current values; cache failure never stops the app."""
    try:
        now = clock()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "version": RANGE_CACHE_VERSION,
                    "ranges": {
                        str(naif): [entry[0], entry[1]]
                        for naif, entry in state.values.items()
                        if range_cache_fresh(entry, now)
                    },
                },
                sort_keys=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - persistence is never fatal
        logger.debug("range cache not written: %s", exc)


async def poll_ranges(
    state: RangeState,
    *,
    links_getter: Callable[[], list[Link]],
    wake: Callable[[], None],
    client_factory: Callable[..., Any],
    headers: dict[str, str],
    endpoint: str,
    clock: Callable[[], float],
    sleep: Callable[[float], Awaitable[object]],
    parser: Callable[[str], float],
    persist: Callable[[], None],
    logger: logging.Logger,
) -> None:
    """Fill feed range gaps through an explicitly configured service worker."""
    async with client_factory(headers=headers, timeout=30) as client:
        while True:
            # The first pass normally precedes the feed. A short idle poll
            # keeps the initial unresolved scene from waiting a full minute.
            now = clock()
            pending_by_naif: dict[int, Link] = {}
            for link in list(links_getter()):
                if not link.naif or link.range_km:
                    continue
                cached = state.current(link.naif, now)
                if cached is not None:
                    link.range_km = cached
                    wake()
                    continue
                if state.retry_due(link.naif, now):
                    # Several aliases/dishes can share one NAIF id. One query
                    # fills all of them; multiplicity must not multiply calls.
                    pending_by_naif.setdefault(link.naif, link)
            pending = list(pending_by_naif.values())
            if not pending:
                await sleep(10)
                continue
            for link in pending:
                naif = link.naif
                if naif is None:
                    continue
                try:
                    jd = 2440587.5 + clock() / 86400.0
                    response = await client.get(
                        endpoint,
                        params={
                            "format": "text",
                            "COMMAND": f"'{naif}'",
                            "OBJ_DATA": "NO",
                            "MAKE_EPHEM": "YES",
                            "EPHEM_TYPE": "OBSERVER",
                            "CENTER": "'500@399'",
                            "QUANTITIES": "'20'",
                            "TLIST_TYPE": "JD",
                            "TLIST": f"'{jd:.5f}'",
                        },
                    )
                    response.raise_for_status()
                    au = parser(response.text)
                    km = au * AU_LIGHT_S * C_KM_S
                    observed_at = clock()
                    state.note_success(naif, km, observed_at)
                    persist()
                    for matching in links_getter():
                        if matching.naif == naif and matching.range_km is None:
                            matching.range_km = km
                    logger.info(
                        "horizons: %s at %.3f AU (%.0f min light)",
                        link.craft,
                        au,
                        au * AU_LIGHT_S / 60,
                    )
                    wake()
                except HorizonsUnavailable as exc:
                    # A valid negative answer is not a successful zero range.
                    logger.info("horizons %s unavailable: %s", link.craft, exc)
                    state.note_unavailable(
                        naif, clock() + RANGE_UNAVAILABLE_RETRY_S
                    )
                except Exception as exc:  # noqa: BLE001 - optional enrichment
                    logger.warning("horizons %s failed: %s", link.craft, exc)
                    state.note_retry(naif, clock() + RANGE_RETRY_S)
                await sleep(2)  # be a good citizen
            await sleep(10)

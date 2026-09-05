"""DSN ranges."""

from __future__ import annotations

import asyncio
import json
import math
import time

import httpx

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source


def range_ttl_s(km: float) -> float:
    """A range-sensitive TTL for geocentric observer distance.

    Earth-orbiting observatories can change range several-fold in six hours;
    truly deep-space targets move slowly enough for the former maximum TTL.
    """
    if km < 2_000_000:
        return _limits.RANGE_NEAR_EARTH_TTL_S
    if km < 50_000_000:
        return _limits.RANGE_INTERMEDIATE_TTL_S
    return _limits.RANGE_TTL_S


def range_cache_fresh(entry: tuple[float, float], now: float) -> bool:
    """Validate a cached (km, observed_at) pair without trusting JSON types."""
    try:
        km, observed_at = float(entry[0]), float(entry[1])
    except (IndexError, TypeError, ValueError, OverflowError):
        return False
    if not (
        math.isfinite(km)
        and 0 < km <= _source.MAX_RANGE_KM
        and math.isfinite(observed_at)
    ):
        return False
    age = now - observed_at
    return 0.0 <= age < range_ttl_s(km)


def cached_range(
    state: _model.State, naif: int, now: float | None = None
) -> float | None:
    """A currently defensible range, expiring invalid entries in memory."""
    entry = state.ranges.get(naif)
    if entry is None:
        return None
    current = time.time() if now is None else now
    if not range_cache_fresh(entry, current):
        state.ranges.pop(naif, None)
        return None
    return float(entry[0])


def load_ranges(state: _model.State) -> None:
    """Load a versioned range cache atomically; malformed JSON is a cold start."""
    try:
        raw = json.loads(_settings.RANGE_CACHE.read_text())
        if (
            not isinstance(raw, dict)
            or raw.get("version") != _limits.RANGE_CACHE_VERSION
            or not isinstance(raw.get("ranges"), dict)
        ):
            raise ValueError("unsupported range-cache schema")
        now = time.time()
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
                and 0 < km <= _source.MAX_RANGE_KM
                and math.isfinite(observed_at)
            ):
                raise ValueError("invalid range-cache value")
            entry = (km, observed_at)
            if range_cache_fresh(entry, now):
                loaded[naif] = entry
    except Exception as exc:  # noqa: BLE001 - a cold/corrupt cache is normal
        _limits.logger.debug("range cache ignored: %s", exc)
        return
    state.ranges = loaded
    if state.ranges:
        _limits.logger.info("loaded %d cached distances", len(state.ranges))


def save_ranges(state: _model.State) -> None:
    try:
        now = time.time()
        _settings.RANGE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _settings.RANGE_CACHE.write_text(
            json.dumps(
                {
                    "version": _limits.RANGE_CACHE_VERSION,
                    "ranges": {
                        str(k): [v[0], v[1]]
                        for k, v in state.ranges.items()
                        if range_cache_fresh(v, now)
                    },
                },
                sort_keys=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - never fatal
        _limits.logger.debug("range cache not written: %s", exc)


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
    max_au = _source.MAX_RANGE_KM / (_limits.AU_LIGHT_S * _source.C_KM_S)
    if not math.isfinite(au) or not 0 < au <= max_au:
        raise ValueError("invalid Horizons observer range")
    return au


async def poll_ranges(state: _model.State) -> None:
    """Fill in light-time for craft the feed doesn't range.

    The feed's own rtlt has been -1 since NASA degraded it, and downlegRange
    is populated for only some targets — so ask Horizons for the rest, keyed
    on the NAIF id the feed hands us. Cache lifetime follows geocentric range:
    minutes for near-Earth observatories, hours only for deep-space targets.
    """
    async with httpx.AsyncClient(headers=_limits.UA, timeout=30) as client:
        while True:
            # Short idle poll, not a long one: the first pass runs before the
            # feed has landed, so state.links is still empty. Sleeping a full
            # minute there left every unranged craft showing "?" for the first
            # minute of every run — which is most of a short run.
            now = time.time()
            pending_by_naif: dict[int, _source.Link] = {}
            for link in list(state.links):
                if not link.naif or link.range_km:
                    continue
                cached = cached_range(state, link.naif, now)
                if cached is not None:
                    link.range_km = cached
                    state.dirty.set()
                    continue
                if now >= state.range_retry_at.get(link.naif, 0.0):
                    # Several aliases/dishes can share one NAIF id. One query
                    # fills all of them; source multiplicity must not multiply
                    # requests to JPL.
                    pending_by_naif.setdefault(link.naif, link)
            pending = list(pending_by_naif.values())
            if not pending:
                await asyncio.sleep(10)
                continue
            for link in pending:
                naif = link.naif
                if naif is None:
                    continue
                try:
                    jd = 2440587.5 + time.time() / 86400.0
                    r = await client.get(
                        _limits.HORIZONS,
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
                    r.raise_for_status()
                    # " 2026-Aug-12 00:00:00.000   142.946932178947  26.198"
                    au = horizons_au(r.text)
                    km = au * _limits.AU_LIGHT_S * _source.C_KM_S
                    observed_at = time.time()
                    state.ranges[naif] = (km, observed_at)
                    state.range_retry_at.pop(naif, None)
                    state.range_unavailable.discard(naif)
                    save_ranges(state)
                    for matching in state.links:
                        if matching.naif == naif and matching.range_km is None:
                            matching.range_km = km
                    _limits.logger.info(
                        "horizons: %s at %.3f AU (%.0f min light)",
                        link.craft,
                        au,
                        au * _limits.AU_LIGHT_S / 60,
                    )
                    state.dirty.set()
                except HorizonsUnavailable as exc:
                    # A valid negative answer is not a six-hour success cache,
                    # but asking the same unsupported spacecraft every minute
                    # only hammers JPL and floods the log. Keep '?' truthful and
                    # reconsider after the ordinary range-cache horizon.
                    _limits.logger.info("horizons %s unavailable: %s", link.craft, exc)
                    state.range_unavailable.add(naif)
                    state.range_retry_at[naif] = (
                        time.time() + _limits.RANGE_UNAVAILABLE_RETRY_S
                    )
                except Exception as exc:  # noqa: BLE001 - optional enrichment
                    _limits.logger.warning("horizons %s failed: %s", link.craft, exc)
                    state.range_retry_at[naif] = time.time() + _limits.RANGE_RETRY_S
                await asyncio.sleep(2)  # be a good citizen
            await asyncio.sleep(10)

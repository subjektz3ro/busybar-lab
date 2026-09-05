"""DSN telemetry."""

from __future__ import annotations

import math
import time

from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source


def rate_bucket(bps: float | None) -> int:
    """Device-resolution occupancy buckets; rate never changes propagation speed."""
    if bps is None or not math.isfinite(bps) or bps <= 0:
        return 0
    if bps < 1_000:
        return 1
    if bps < 100_000:
        return 2
    if bps < 1_000_000:
        return 3
    if bps < 10_000_000:
        return 4
    return 5


def receive_power_bucket(dbm: float | None) -> int:
    if (
        dbm is None
        or not math.isfinite(dbm)
        or not _source.RECEIVE_POWER_MIN_DBM <= dbm < 0.0
    ):
        return 0
    if dbm < -150:
        return 1
    if dbm < -135:
        return 2
    return 3


def transmit_power_bucket(kw: float | None) -> int:
    if kw is None or not math.isfinite(kw) or kw <= 0:
        return 0
    if kw < 1:
        return 1
    if kw < 10:
        return 2
    return 3


def wind_bucket(kmh: float | None) -> int | str | None:
    """Five km/h steps are useful live context without 1 km/h asset churn."""
    if kmh is None or not math.isfinite(kmh) or kmh < 0:
        return None
    if kmh > 995:
        return "UNKNOWN"  # malformed/extreme input, never a false exact cap
    return int(round(kmh / 5.0) * 5)


def link_streams(link: _source.Link) -> tuple[_source.DownStream, ...]:
    """Keep old fixtures/caches useful while the parser now retains streams."""
    if link.down_streams:
        streams = link.down_streams
    elif (link.down_bps is not None and link.down_bps > 0) or link.streams > 0:
        streams = (_source.DownStream(link.band, link.down_bps, link.down_dbm),)
    else:
        streams = ()
    # XML ordering is not telemetry. Canonicalise by what the panel can
    # distinguish so a harmless source reorder cannot swap lanes or emit an
    # event; raw jitter inside a bucket likewise cannot reshuffle them.
    return tuple(
        sorted(
            streams,
            key=lambda stream: (
                _source.band_key(stream.band),
                rate_bucket(stream.bps),
                receive_power_bucket(stream.dbm),
            ),
        )
    )


def link_upstreams(link: _source.Link) -> tuple[_source.UpStream, ...]:
    """Canonical active up-signal records, with legacy-fixture fallback."""
    if link.up_streams:
        streams = link.up_streams
    elif link.up_active:
        streams = (_source.UpStream(link.up_band, link.up_kw, ""),)
    else:
        streams = ()
    return tuple(
        sorted(
            streams,
            key=lambda stream: (
                _source.band_key(stream.band),
                transmit_power_bucket(stream.kw or 0.0),
                stream.signal_type,
            ),
        )
    )


def feed_freshness(state: _model.State, now: float | None = None) -> str:
    """Require both source advancement and a reasonably current source epoch."""
    if state.feed_timestamp_ms is None or state.feed_advanced_at is None:
        return "offline"
    current = now if now is not None else time.time()
    timestamp = state.feed_timestamp_ms
    if (
        not isinstance(timestamp, int)
        or not _source.MIN_UNIX_TIMESTAMP_MS
        <= timestamp
        <= _source.MAX_UNIX_TIMESTAMP_MS
        or not math.isfinite(current)
        or not math.isfinite(state.feed_advanced_at)
    ):
        return "stale"
    age = current - state.feed_advanced_at
    source_age = current - timestamp / 1000.0
    if source_age < -_source.FEED_FUTURE_SKEW_S or age < -_source.FEED_FUTURE_SKEW_S:
        return "stale"
    age = max(age, source_age)
    if age <= _settings.FEED_DELAYED_S:
        return "fresh"
    if age <= _settings.FEED_STALE_S:
        return "delayed"
    return "stale"

"""Bounded, deterministic ingestion for NASA's Deep Space Network feeds.

This module is the trust boundary between remote XML and the rest of the DSN
app.  It owns the source vocabulary, semantic budgets, domain records and pure
parsers.  It deliberately performs no network, filesystem, device or rendering
work, so a rejected snapshot cannot partially mutate runtime state.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from xml.etree.ElementTree import Element

import defusedxml.ElementTree as ET

C_KM_S = 299792.458

# NASA's source clock may be slightly ahead of the host, but accepting an
# arbitrary future version poisons the high-water mark until wall time catches
# up. One minute is generous beside the feed's ordinary five-second cadence.
FEED_FUTURE_SKEW_S = 60.0
MIN_UNIX_TIMESTAMP_MS = 1_000_000_000_000  # 2001-09-09, not a fixture counter
MAX_UNIX_TIMESTAMP_MS = 253_402_300_799_999  # 9999-12-31T23:59:59.999Z

# Parse only bounded source documents and identities. These limits are far
# above the current public feeds (roughly 6/19 KiB and names under 100 chars),
# while preventing a remote label from planning millions of animation frames.
FEED_XML_MAX_BYTES = 1_000_000
CONFIG_XML_MAX_BYTES = 2_000_000
SOURCE_CODE_MAX = 32
SOURCE_DISH_CODE_MAX = 16
SOURCE_NAME_MAX = 96
SOURCE_ACTIVITY_MAX = 96
FEED_DISH_ELEMENTS_MAX = 64
FEED_DISHES_PER_SITE_MAX = 8
FEED_LINKS_PER_DISH_MAX = 8
FEED_LINKS_MAX = 128
FEED_SIGNAL_RECORDS_PER_DISH_MAX = 64
CONFIG_SPACECRAFT_MAX = 512
CONFIG_DISHES_MAX = 64
CONFIG_SITES_MAX = 16
MAX_SOURCE_NUMERIC_ID = 2_147_483_647
MAX_SOURCE_NUMBER_CHARS = 32

# Far beyond every Solar-System target, but finite enough that one hostile
# exponent cannot become a hundreds-of-digits distance sentence or `inf` light
# time. Out-of-domain range remains unknown.
MAX_RANGE_KM = 100_000_000_000_000.0

# DSN receive records describe a faint spacecraft carrier. Zero is the feed's
# missing-value sentinel; a positive value or anything below this defensive
# floor is not a plausible observation and must remain unknown.
RECEIVE_POWER_MIN_DBM = -250.0

SITE_NAMES = {
    "gdscc": "Goldstone",
    "goldstone": "Goldstone",
    "mdscc": "Madrid",
    "madrid": "Madrid",
    "cdscc": "Canberra",
    "canberra": "Canberra",
}
REQUIRED_SITE_NAMES = frozenset({"Goldstone", "Madrid", "Canberra"})

# The target list includes calibration, test and radio-astronomy identities
# with the same XML shape as spacecraft. Keep the filter beside the parser that
# applies it, rather than in narration several thousand lines downstream.
NOT_SPACECRAFT = {
    "DSN",
    "DSS",
    "TEST",
    "ATOT",
    "EGS",
    "GVRT",
    "GSSR",
    "GBRA",
    "HCRA",
    "RFC",
    "RSTS",
    "SGP",
    "VLBI",
    "CASS",
    "DOUG",
    "SHAN",
}


@dataclass
class DownStream:
    """One published receive record; it may be a link or redundant chain."""

    band: str
    bps: float | None
    dbm: float | None
    signal_type: str = ""


@dataclass
class UpStream:
    """One published active-uplink record; power is per feed record."""

    band: str
    kw: float | None
    signal_type: str = ""


@dataclass
class Link:
    """One live antenna-to-spacecraft link."""

    complex_name: str
    dish: str
    craft: str
    elevation: float
    band: str
    down_bps: float | None
    up_active: bool
    range_km: float | None
    naif: int | None = None
    down_dbm: float | None = None  # received power; deep space is ~-140
    up_kw: float | None = 0.0  # strongest published transmit power
    streams: int = 0  # active receive records, not inferred streams
    mspa: bool = False  # one dish, several spacecraft at once
    arrayed: bool = False  # several dishes combined on one signal
    ddor: bool = False  # a precision navigation fix in progress
    azimuth: float = 0.0  # clockwise from north, straight from DSN
    pointing_valid: bool = True  # false when either published angle is absent
    activity: str = ""
    down_streams: tuple[DownStream, ...] = ()
    up_band: str = ""
    up_streams: tuple[UpStream, ...] = ()
    up_range_km: float | None = None
    down_range_km: float | None = None
    wind_kmh: float | None = None

    @property
    def light_s(self) -> float | None:
        return (
            self.range_km / C_KM_S
            if (
                self.range_km is not None
                and math.isfinite(self.range_km)
                and 0 < self.range_km <= MAX_RANGE_KM
            )
            else None
        )

    @property
    def up_light_s(self) -> float | None:
        return (
            self.up_range_km / C_KM_S
            if (
                self.up_range_km is not None
                and math.isfinite(self.up_range_km)
                and 0 < self.up_range_km <= MAX_RANGE_KM
            )
            else None
        )

    @property
    def down_light_s(self) -> float | None:
        return (
            self.down_range_km / C_KM_S
            if (
                self.down_range_km is not None
                and math.isfinite(self.down_range_km)
                and 0 < self.down_range_km <= MAX_RANGE_KM
            )
            else None
        )

    @property
    def key(self) -> str:
        return f"{self.dish}/{self.craft}"

    @property
    def down_active(self) -> bool:
        """Carrier presence is independent of whether NASA published a rate."""
        return (
            bool(self.down_streams)
            or self.streams > 0
            or (self.down_bps is not None and self.down_bps > 0)
        )


class SourceValidationError(ValueError):
    """A complete remote snapshot is unsafe or semantically ambiguous."""


def band_key(band: str) -> str:
    """Normalize source band variants such as ``Ka`` and ``Ka-band``."""
    key = re.sub(r"[\s_-]+", "", band or "").upper()
    return key[:-4] if key.endswith("BAND") else key


def _rate(value: str | None, maximum: float | None = None) -> float | None:
    """A bounded nonnegative source number; zero remains an observation."""
    raw = (value or "").strip()
    if not raw or len(raw) > MAX_SOURCE_NUMBER_CHARS:
        return None
    try:
        out = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out < 0:
        return None
    return out if maximum is None or out <= maximum else None


def _uplink_power(value: str | None) -> float | None:
    """A positive published transmitter power; zero means unavailable."""
    power = _rate(value)
    return power if power is not None and power > 0 else None


def _angle(value: str | None, low: float, high: float) -> float | None:
    """A published pointing angle, or None instead of invented geometry."""
    if value is None:
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or not low <= out <= high:
        return None
    return out


def _dbm(signals: list[Element]) -> float | None:
    """Strongest usable received power across a link's records, in dBm."""
    best = None
    for signal in signals:
        raw = signal.get("power")
        if raw is None:
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or not RECEIVE_POWER_MIN_DBM <= value < 0.0:
            continue
        best = value if best is None else max(best, value)
    return best


def _signal_dbm(signal: Element) -> float | None:
    """A single downSignal's power, preserving legitimate negative dBm."""
    return _dbm([signal])


def _bounded_source_text(value: str | None, limit: int) -> str | None:
    """A stripped source token, or None when it exceeds its semantic budget.

    Truncating an identifier could merge two spacecraft or dishes. Rejecting
    the offending record is both bounded and honest; presentation helpers may
    abbreviate only after identity has been established.
    """
    if value is None:
        return None
    value = value.strip()
    return value if value and len(value) <= limit else None


def _required_source_text(value: str | None, limit: int, field: str) -> str:
    """A required identity; never silently turn a real contact into no data."""
    bounded = _bounded_source_text(value, limit)
    if bounded is None:
        reason = "missing" if not (value or "").strip() else "oversized"
        raise SourceValidationError(f"{reason} {field}")
    return bounded


def _source_numeric_id(value: str | None) -> int | None:
    """A bounded NAIF/source id, never an arbitrary-precision remote integer."""
    raw = (value or "").strip()
    if len(raw) > 11 or re.fullmatch(r"[+-]?\d+", raw) is None:
        return None
    parsed = int(raw)
    return parsed if 0 < abs(parsed) <= MAX_SOURCE_NUMERIC_ID else None


def canonical_site_name(name: str | None, friendly_name: str | None = None) -> str:
    """One stable complex name for dsn.xml and config.xml vocabularies."""
    for candidate in (name, friendly_name):
        bounded = _bounded_source_text(candidate, SOURCE_NAME_MAX)
        if bounded is None:
            continue
        key = re.sub(r"[^a-z0-9]+", "", bounded.lower())
        if key in SITE_NAMES:
            return SITE_NAMES[key]
    return (
        _bounded_source_text(friendly_name, SOURCE_NAME_MAX)
        or _bounded_source_text(name, SOURCE_CODE_MAX)
        or ""
    )


def feed_timestamp_ms(xml_bytes: bytes) -> int | None:
    """NASA's source timestamp, not the time our HTTP request completed."""
    try:
        if len(xml_bytes) > FEED_XML_MAX_BYTES:
            return None
        raw = ET.fromstring(xml_bytes).findtext("timestamp")
        if raw is None or len(raw.strip()) > 18:
            return None
        timestamp = int(raw)
        return timestamp if 0 <= timestamp <= MAX_UNIX_TIMESTAMP_MS else None
    except (ET.ParseError, TypeError, ValueError):
        return None


def source_timestamp_valid(timestamp_ms: int, received_at: float) -> bool:
    """Whether a real-world epoch may advance the source watermark."""
    if not isinstance(timestamp_ms, int):
        return False
    if not MIN_UNIX_TIMESTAMP_MS <= timestamp_ms <= MAX_UNIX_TIMESTAMP_MS:
        return False
    if not isinstance(received_at, (int, float)) or not math.isfinite(received_at):
        return False
    return timestamp_ms <= int((received_at + FEED_FUTURE_SKEW_S) * 1000)


def parse_feed(xml_bytes: bytes) -> list[Link]:
    """Parse one complete, bounded DSN Now snapshot.

    ``station`` and ``dish`` are flat siblings: a station announces a complex
    and every dish after it belongs to that complex until the next one.
    """
    if len(xml_bytes) > FEED_XML_MAX_BYTES:
        raise ValueError("DSN feed exceeds the bounded document size")
    root = ET.fromstring(xml_bytes)
    complex_name = ""
    out: list[Link] = []
    dish_elements = 0
    dishes_by_site: dict[str, set[str]] = {}
    for element in root:
        if element.tag == "station":
            complex_name = canonical_site_name(
                element.get("name"), element.get("friendlyName")
            )
            if complex_name not in REQUIRED_SITE_NAMES:
                raise SourceValidationError(
                    f"unknown DSN station identity: {complex_name or '?'}"
                )
            # The globe needs longitude from config.xml, not this station's
            # civil-clock offset, so timeZoneOffset is intentionally ignored.
        elif element.tag == "dish":
            if complex_name not in REQUIRED_SITE_NAMES:
                raise SourceValidationError("dish arrived before a known station")
            dish_elements += 1
            if dish_elements > FEED_DISH_ELEMENTS_MAX:
                raise SourceValidationError("too many dish elements in DSN feed")
            dish = _required_source_text(
                element.get("name"), SOURCE_DISH_CODE_MAX, "dish identity"
            )
            site_dishes = dishes_by_site.setdefault(complex_name, set())
            site_dishes.add(dish)
            if len(site_dishes) > FEED_DISHES_PER_SITE_MAX:
                raise SourceValidationError(f"too many dishes for {complex_name}")
            elevation = _angle(element.get("elevationAngle"), 0.0, 90.0)
            azimuth = _angle(element.get("azimuthAngle"), 0.0, 360.0)

            targets: dict[str, Element] = {}
            target_conflicts: set[str] = set()
            for target in element.findall("target"):
                craft = _required_source_text(
                    target.get("name"), SOURCE_CODE_MAX, "target identity"
                )
                if craft in target_conflicts:
                    continue
                prior = targets.get(craft)
                if prior is None:
                    targets[craft] = target
                elif prior.attrib != target.attrib:
                    targets.pop(craft, None)
                    target_conflicts.add(craft)

            # Preserve every unique signal record. The source does not say
            # whether multiple receiver records are links or redundant chains.
            down_list: dict[str, list[Element]] = {}
            down_seen: set[tuple[tuple[str, str], ...]] = set()
            down_elements = element.findall("downSignal")
            up_elements = element.findall("upSignal")
            if len(down_elements) + len(up_elements) > FEED_SIGNAL_RECORDS_PER_DISH_MAX:
                raise SourceValidationError(f"too many signal records for {dish}")
            for signal in down_elements:
                signature = tuple(sorted(signal.attrib.items()))
                down_craft = (
                    _required_source_text(
                        signal.get("spacecraft"),
                        SOURCE_CODE_MAX,
                        "active downSignal spacecraft identity",
                    )
                    if signal.get("active") == "true"
                    else None
                )
                if down_craft is not None and signature not in down_seen:
                    down_seen.add(signature)
                    down_list.setdefault(down_craft, []).append(signal)
            downs = {craft: signals[0] for craft, signals in down_list.items()}

            up_list: dict[str, list[Element]] = {}
            up_seen: set[tuple[tuple[str, str], ...]] = set()
            for signal in up_elements:
                signature = tuple(sorted(signal.attrib.items()))
                up_craft = (
                    _required_source_text(
                        signal.get("spacecraft"),
                        SOURCE_CODE_MAX,
                        "active upSignal spacecraft identity",
                    )
                    if signal.get("active") == "true"
                    else None
                )
                if up_craft is not None and signature not in up_seen:
                    up_seen.add(signature)
                    up_list.setdefault(up_craft, []).append(signal)
            up_signals = {craft: signals[0] for craft, signals in up_list.items()}
            ups = set(up_list)
            active_crafts = set(downs) | ups
            if len(active_crafts) > FEED_LINKS_PER_DISH_MAX:
                raise SourceValidationError(f"too many active links for {dish}")

            for craft in sorted(active_crafts):
                if craft.upper() in NOT_SPACECRAFT:
                    continue
                down = downs.get(craft)
                target = targets.get(craft)

                # Uplink-only records may carry the NAIF id in upSignal or
                # target rather than downSignal, so all three are candidates.
                naif = None
                for source, negate in (
                    (down, False),
                    (up_signals.get(craft), False),
                    (target, True),
                ):
                    if source is None:
                        continue
                    raw = source.get("spacecraftID") or source.get("id")
                    value = _source_numeric_id(raw)
                    if value is not None:
                        naif = -abs(value) if negate else value
                        break

                streams = down_list.get(craft, [])
                stream_data = tuple(
                    sorted(
                        (
                            DownStream(
                                band=(
                                    _bounded_source_text(
                                        signal.get("band"), SOURCE_CODE_MAX
                                    )
                                    or ""
                                ),
                                bps=_rate(signal.get("dataRate")),
                                dbm=_signal_dbm(signal),
                                signal_type=(
                                    _bounded_source_text(
                                        signal.get("signalType"), SOURCE_CODE_MAX
                                    )
                                    or ""
                                ),
                            )
                            for signal in streams
                        ),
                        key=lambda stream: (
                            not bool(band_key(stream.band)),
                            band_key(stream.band),
                            stream.bps is None,
                            stream.bps or 0.0,
                            stream.dbm is None,
                            stream.dbm or 0.0,
                            stream.signal_type,
                        ),
                    )
                )
                up_stream_data = tuple(
                    sorted(
                        (
                            UpStream(
                                band=(
                                    _bounded_source_text(
                                        signal.get("band"), SOURCE_CODE_MAX
                                    )
                                    or ""
                                ),
                                kw=_uplink_power(signal.get("power")),
                                signal_type=(
                                    _bounded_source_text(
                                        signal.get("signalType"), SOURCE_CODE_MAX
                                    )
                                    or ""
                                ),
                            )
                            for signal in up_list.get(craft, [])
                        ),
                        key=lambda stream: (
                            not bool(band_key(stream.band)),
                            band_key(stream.band),
                            stream.kw is None,
                            stream.kw or 0.0,
                            stream.signal_type,
                        ),
                    )
                )

                scalar_rate = stream_data[0].bps if len(stream_data) == 1 else None
                powers = [
                    stream.kw for stream in up_stream_data if stream.kw is not None
                ]
                up_band_values = tuple(
                    band_key(stream.band) for stream in up_stream_data
                )
                down_band_values = tuple(
                    band_key(stream.band) for stream in stream_data
                )
                up_band_keys = set(up_band_values)
                down_band_keys = set(down_band_values)
                down_range = (
                    _rate(target.get("downlegRange"), MAX_RANGE_KM)
                    if target is not None
                    else None
                )
                up_range = (
                    _rate(target.get("uplegRange"), MAX_RANGE_KM)
                    if target is not None
                    else None
                )
                represented_range = (
                    down_range
                    if stream_data
                    else up_range
                    if up_stream_data
                    else down_range or up_range
                )

                out.append(
                    Link(
                        complex_name=complex_name,
                        dish=dish,
                        craft=craft,
                        elevation=elevation if elevation is not None else 0.0,
                        band=(
                            next(iter(down_band_keys))
                            if down_band_values
                            and "" not in down_band_keys
                            and len(down_band_keys) == 1
                            else ""
                        ),
                        down_bps=scalar_rate,
                        up_active=craft in ups,
                        range_km=represented_range,
                        naif=naif,
                        down_dbm=_dbm(streams),
                        up_kw=max(powers, default=None),
                        streams=len(streams),
                        mspa=element.get("isMSPA") == "true",
                        arrayed=element.get("isArray") == "true",
                        ddor=element.get("isDDOR") == "true",
                        azimuth=(azimuth % 360.0) if azimuth is not None else 0.0,
                        pointing_valid=elevation is not None and azimuth is not None,
                        activity=(
                            _bounded_source_text(
                                element.get("activity"), SOURCE_ACTIVITY_MAX
                            )
                            or ""
                        ),
                        down_streams=stream_data,
                        up_band=(
                            next(iter(up_band_keys))
                            if up_band_values
                            and "" not in up_band_keys
                            and len(up_band_keys) == 1
                            else ""
                        ),
                        up_streams=up_stream_data,
                        up_range_km=up_range,
                        down_range_km=down_range,
                        wind_kmh=_rate(element.get("windSpeed")),
                    )
                )
                if len(out) > FEED_LINKS_MAX:
                    raise SourceValidationError("too many active links in DSN feed")

    # Exact duplicate blocks collapse. A contradictory physical dish/craft
    # quarantines the complete snapshot instead of manufacturing a link loss.
    canonical: dict[str, Link] = {}
    order: list[str] = []
    for link in out:
        prior_link = canonical.get(link.key)
        if prior_link is None:
            canonical[link.key] = link
            order.append(link.key)
        elif prior_link != link:
            raise SourceValidationError(f"contradictory duplicate DSN link {link.key}")
    return [canonical[key] for key in order]


def parse_config(
    xml_bytes: bytes,
) -> tuple[dict[str, str], dict[str, str], dict[str, float]]:
    """Validate NASA config into one complete replacement snapshot."""
    if len(xml_bytes) > CONFIG_XML_MAX_BYTES:
        raise ValueError("DSN config exceeds the bounded document size")
    root = ET.fromstring(xml_bytes)

    spacecraft_elements = list(root.iter("spacecraft"))
    if len(spacecraft_elements) > CONFIG_SPACECRAFT_MAX:
        raise SourceValidationError("too many spacecraft in DSN config")
    names: dict[str, str] = {}
    for element in spacecraft_elements:
        code = _required_source_text(
            element.get("name"), SOURCE_CODE_MAX, "config spacecraft code"
        ).lower()
        raw_friendly = (element.get("friendlyName") or "").strip()
        if not raw_friendly:
            continue
        friendly = _required_source_text(
            raw_friendly, SOURCE_NAME_MAX, "config spacecraft friendly name"
        )
        prior = names.get(code)
        if prior is not None and prior != friendly:
            raise SourceValidationError(
                f"conflicting config spacecraft identity {code}"
            )
        names[code] = friendly

    dish_elements = list(root.iter("dish"))
    if len(dish_elements) > CONFIG_DISHES_MAX:
        raise SourceValidationError("too many dishes in DSN config")
    dish_types: dict[str, str] = {}
    for element in dish_elements:
        dish = _required_source_text(
            element.get("name"), SOURCE_DISH_CODE_MAX, "config dish identity"
        )
        kind = _required_source_text(
            element.get("type"), SOURCE_CODE_MAX, "config dish type"
        )
        if re.fullmatch(r"\d{2,3}M[A-Z0-9]*", kind.upper()) is None:
            raise SourceValidationError(f"invalid dish type for {dish}")
        kind = kind.upper()
        prior = dish_types.get(dish)
        if prior is not None and prior != kind:
            raise SourceValidationError(f"conflicting config dish type for {dish}")
        dish_types[dish] = kind

    site_elements = list(root.iter("site"))
    if len(site_elements) > CONFIG_SITES_MAX:
        raise SourceValidationError("too many sites in DSN config")
    site_lons: dict[str, float] = {}
    for site in site_elements:
        canonical = canonical_site_name(site.get("name"), site.get("friendlyName"))
        if not canonical:
            continue
        try:
            longitude = float(site.get("longitude"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"invalid longitude for {canonical}") from exc
        if not math.isfinite(longitude) or not -180.0 <= longitude <= 180.0:
            raise ValueError(f"invalid longitude for {canonical}")
        prior_longitude = site_lons.get(canonical)
        if prior_longitude is not None and prior_longitude != longitude:
            raise ValueError(f"conflicting longitude for {canonical}")
        site_lons[canonical] = longitude

    missing_sites = REQUIRED_SITE_NAMES - site_lons.keys()
    if missing_sites:
        raise ValueError(
            "config missing DSN sites: " + ", ".join(sorted(missing_sites))
        )
    if not names or not dish_types:
        raise ValueError("config missing spacecraft or dish identities")
    return names, dish_types, site_lons

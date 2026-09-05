"""DSN audio / words."""

from __future__ import annotations

import math
import re

from apps.dsn_app import formatting as _formatting
from apps.dsn_app import missions as _missions
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry


def spoken_name(code: str, names: dict[str, str]) -> str:
    """NASA's friendlyName exactly as written — it is already cased for
    reading aloud ('SOHO', 'Voyager 2', 'MAVEN'), which no amount of
    title-casing survives. Parentheticals are stripped: nobody says
    'Deep Space Climate Observatory open paren DSCOVR'.
    """
    full = names.get(code.lower(), "")
    full = re.sub(r"\s*\([^)]*\)", "", full)
    full = "".join(ch for ch in full if 32 <= ord(ch) <= 126).strip()
    return full or code


def _plural(count: int, unit: str) -> str:
    return f"{count} {unit}" if count == 1 else f"{count} {unit}s"


def light_words(secs: float | None) -> str:
    """Light time, spoken. Coarse on purpose: the cache is keyed by the text,
    so a value that jitters would re-bake the line every poll."""
    if not secs:
        return ""
    if secs < 1:
        return "less than a second"  # Chandra said "0 seconds"
    if secs < 90:
        return _plural(round(secs), "second")
    mins = secs / 60
    if mins < 60:
        return _plural(round(mins), "minute")
    hours, rest = divmod(int(round(mins)), 60)
    return (
        _plural(hours, "hour")
        if rest == 0
        else f"{_plural(hours, 'hour')} and {_plural(rest, 'minute')}"
    )


AU_KM = 149_597_870.7  # Earth to Sun

LIGHT_YEAR_KM = 9.4607e12


def distance_words(range_km: float | None) -> str:
    """How far away, in units that mean something at this scale.

    NOT light-years. Everything the DSN talks to is inside the solar system,
    so light-years give you 0.0022 for Voyager 2, 0.0001 for Juno and
    0.0000000 for Chandra — every craft reads "zero point zero zero zero".
    Kilometres carry the size; the Earth-Sun distance carries the meaning.

    Returns a phrase that already contains "away", so the comparison lands
    after it rather than stranding the preposition at the end of the clause.
    """
    if (
        range_km is None
        or not math.isfinite(range_km)
        or not 0 < range_km <= _source.MAX_RANGE_KM
    ):
        return ""
    if range_km >= 1e9:
        far = f"{range_km / 1e9:.0f} billion kilometres"
    elif range_km >= 1e6:
        far = f"{range_km / 1e6:.0f} million kilometres"
    elif range_km >= 1e3:
        far = f"{range_km / 1e3:.0f} thousand kilometres"
    else:
        far = f"{range_km:.0f} kilometres"
    au = range_km / AU_KM
    if au >= 1.5:
        return f"{far} away, {au:.0f} times the Earth's distance from the Sun"
    return f"{far} away"


def lightyear_words(range_km: float | None) -> str:
    """The light-year, used as the humbling comparison it actually is.

    Kept for genuinely deep space only. Saying a light year is ten thousand
    times further than Juno is technically true and rhetorically limp; saying
    it about Voyager, the most distant thing we have ever built, is the point.
    """
    if (
        range_km is None
        or not math.isfinite(range_km)
        or not 0 < range_km <= _source.MAX_RANGE_KM
        or range_km / AU_KM < 50
    ):
        return ""
    ratio = LIGHT_YEAR_KM / range_km
    return (
        f"A single light year is {ratio:.0f} times further than that, "
        f"and the nearest star is more than four of them away."
    )


def power_words(dbm: float | None) -> str:
    """Received power, said in units a person can feel.

    This is the most astonishing number the feed carries and we were throwing
    it away. Juno arrives at -140 dBm: ten billionths of a billionth of a
    watt, which a 34-metre dish pulls a clean data stream out of.
    """
    if (
        dbm is None
        or not math.isfinite(dbm)
        or not _source.RECEIVE_POWER_MIN_DBM <= dbm < 0.0
    ):
        return ""
    watts = 10 ** ((dbm - 30) / 10.0)
    for scale, unit in (
        (1e-18, "attowatt"),
        (1e-15, "femtowatt"),
        (1e-12, "picowatt"),
        (1e-9, "nanowatt"),
    ):
        if watts < scale * 1000:
            count = watts / scale
            if count >= 1.5:
                return f"{count:.0f} {unit}s"
            return f"one {unit}" if count >= 0.8 else f"under one {unit}"
    return ""


def rate_words(bps: float | None) -> str:
    if bps is None or not math.isfinite(bps) or bps <= 0:
        return ""
    if bps > _formatting.RATE_LABEL_MAX_GBPS * 1e9:
        return f"more than {_formatting.RATE_LABEL_MAX_GBPS:.0f} gigabits per second"
    if bps >= 1e9:
        return f"about {_plural(round(bps / 1e9), 'gigabit')} per second"
    if bps >= 1e6:
        return f"about {_plural(round(bps / 1e6), 'megabit')} per second"
    if bps >= 1e3:
        return f"about {_plural(round(bps / 1e3), 'kilobit')} per second"
    return f"{_plural(round(bps), 'bit')} per second"


def transmit_power_words(kw: float | None) -> str:
    """Spoken source power with the same honest ceiling as the panel."""
    if kw is None or not math.isfinite(kw) or kw < 0.05:
        return ""
    if kw > _formatting.POWER_LABEL_MAX:
        return f"more than {_formatting.POWER_LABEL_MAX:.0f} kilowatts"
    if kw >= 1:
        return f"{kw:.0f} kilowatts"
    watts = kw * 1000
    return (
        f"more than {_formatting.POWER_LABEL_MAX:.0f} watts"
        if watts > _formatting.POWER_LABEL_MAX
        else f"{watts:.0f} watts"
    )


def receive_records_words(records: tuple[_source.DownStream, ...], count: int) -> str:
    """Describe source records without inventing a contact-wide throughput.

    DSN receiver and telemetry-processing records may represent independent
    links or redundant processing of the same link. Their published rates are
    therefore retained and spoken per record, never summed.
    """
    if count <= 1:
        return ""
    opening = f"The source publishes {count} active receive signal records"
    if len(records) != count:
        return (
            f"{opening}. Their individual rates are not all available, "
            "and the records are not added into one contact throughput "
            "because they can include receiver redundancy."
        )
    if count > _formatting.NARRATION_RECORD_DETAIL_MAX:
        return (
            f"{opening}. Their individual rates are not enumerated in "
            "speech, and the records are not added into one contact "
            "throughput because they can include receiver redundancy."
        )

    rates: list[str] = []
    for record in records:
        if record.bps is None or not math.isfinite(record.bps):
            rates.append("an unavailable rate")
        elif record.bps == 0:
            rates.append("zero bits per second")
        else:
            rates.append(rate_words(record.bps))
    joined = rates[0] if len(rates) == 1 else f"{', '.join(rates[:-1])} and {rates[-1]}"
    return (
        f"{opening}, with per-record rates of {joined}. Those records "
        "are not added into one contact throughput because they can "
        "include receiver redundancy."
    )


def band_words(band: str) -> str:
    """Name the observed band without pretending it determines live rate."""
    kind = _source.band_key(band)
    if kind == "KA":
        return (
            "This is Ka band, a high-frequency channel that can support "
            "wide bandwidth when the rest of the link allows it."
        )
    if kind == "K":
        return (
            "This is K band, the network's high-frequency near-Earth "
            "service. Deep-space high-rate service uses Ka band instead."
        )
    if kind == "S":
        return (
            "This is S band, a long-established channel valued for robust "
            "tracking, telemetry and command links."
        )
    if kind == "X":
        return "This is X band, the network's workhorse."
    return ""


def spoken(
    link: _source.Link,
    names: dict[str, str] | None = None,
    dish_types: dict[str, str] | None = None,
) -> str:
    """The narration: which antenna, what the spacecraft is, how long the
    signal takes, and how fast it is arriving."""
    where = link.complex_name or "The Deep Space Network"
    number = link.dish.replace("DSS", "").lstrip("0") or link.dish
    size = _formatting.dish_metres(link.dish, dish_types)
    craft = spoken_name(link.craft, names or {})
    listening = link.down_active
    published_downstreams = tuple(link.down_streams)
    down_record_count = max(link.streams, len(published_downstreams))
    # A contact scalar is meaningful only for one receive record. Multiple
    # records may be independent links or redundant receiver chains; neither
    # the XML nor DSN documentation makes their rates summable.
    single_down_bps = (
        published_downstreams[0].bps
        if down_record_count == 1 and published_downstreams
        else link.down_bps
        if down_record_count <= 1
        else None
    )

    if listening and link.up_active:
        action = f"receiving from and transmitting to {craft}"
    elif listening:
        action = f"receiving from {craft}"
    elif link.up_active:
        action = f"transmitting to {craft}"
    else:
        action = f"tracking {craft}"
    dish_clause = (
        f"on the {size} metre dish, number {number}"
        if size
        else f"on dish number {number}"
    )
    lines = [f"DSN Now reports {where} is {action}, {dish_clause}."]

    badge = _formatting.activity_badge(link.activity)
    if badge in {"DEMO", "UPGRADE", "ENGINEER"}:
        source_activity = " ".join(re.findall(r"[A-Za-z0-9]+", link.activity))[:48]
        if source_activity:
            lines.append(
                f"The source labels this antenna activity as {source_activity}."
            )

    blurb = _missions.mission_blurb(link.craft)
    if blurb:
        lines.append(f"{blurb}.")

    how_far = distance_words(link.range_km)  # already ends "... away"
    if how_far:
        lines.append(f"It is {how_far}.")

    light = light_words(link.light_s)
    rate = rate_words(single_down_bps)
    if light and listening:
        if rate:
            lines.append(
                f"Its signal takes {light} to reach us, and arrives at {rate}."
            )
        elif down_record_count > 1:
            lines.append(f"Its signal takes {light} to reach us.")
        elif single_down_bps is None:
            lines.append(
                f"Its signal takes {light} to reach us. The carrier "
                "is active, but this receive record has no usable "
                "data rate."
            )
        else:
            lines.append(
                f"Its signal takes {light} to reach us, with a "
                "published data rate of zero right now."
            )
    elif light:
        # The verb already said we are transmitting; don't say it twice.
        lines.append(f"It is {light} away at the speed of light.")
    elif rate:
        lines.append(f"Data is coming in at {rate}.")

    receive_records = receive_records_words(published_downstreams, down_record_count)
    if receive_records:
        lines.append(receive_records)

    # The number an operator actually lives by. This is light-time only, not
    # a promise about when a spacecraft will process or answer a command.
    if link.light_s and link.light_s > 60.0:
        # Preserve both source-published legs even when the represented link
        # is uplink-only. Falling back to the active-direction range is an
        # estimate; when both legs exist this is their actual published sum.
        return_light = link.down_light_s or link.light_s
        outbound_light = link.up_light_s or link.light_s
        lines.append(
            f"The light-time alone for an immediate round trip is "
            f"about {light_words(outbound_light + return_light)}."
        )

    downstreams = _telemetry.link_streams(link)
    down_band_values = tuple(_source.band_key(stream.band) for stream in downstreams)
    down_bands_complete = bool(down_band_values) and all(down_band_values)
    down_kinds = tuple(
        dict.fromkeys(
            kind if kind in _formatting.NAMED_RF_BANDS else "unknown"
            for kind in down_band_values
            if kind
        )
    )
    if len(down_kinds) > 1:
        joined = ", ".join(down_kinds[:-1]) + f" and {down_kinds[-1]}"
        lines.append(
            f"The source publishes active received carriers in "
            f"{joined} bands for this contact."
        )
    elif down_bands_complete:
        band = band_words(down_kinds[0] if down_kinds else link.band)
        if band:
            lines.append(band)
    upstreams = _telemetry.link_upstreams(link)
    up_band_values = tuple(_source.band_key(stream.band) for stream in upstreams)
    up_bands_complete = bool(up_band_values) and all(up_band_values)
    up_kinds = tuple(
        dict.fromkeys(
            kind if kind in _formatting.NAMED_RF_BANDS else "unknown"
            for kind in up_band_values
            if kind
        )
    )
    down_kind = (
        down_kinds[0]
        if (
            down_bands_complete
            and len(down_kinds) == 1
            and down_kinds[0] in _formatting.NAMED_RF_BANDS
        )
        else ""
    )
    if len(up_kinds) > 1:
        joined = ", ".join(up_kinds[:-1]) + f" and {up_kinds[-1]}"
        lines.append(
            f"The source publishes active uplink records in {joined} "
            "bands for this contact."
        )
    elif (
        up_bands_complete
        and len(up_kinds) == 1
        and up_kinds[0] in _formatting.NAMED_RF_BANDS
        and down_kind
        and up_kinds[0] != down_kind
    ):
        lines.append(
            f"The uplink is {up_kinds[0]} band while the received "
            f"carrier is {down_kind} band."
        )

    if len(upstreams) > 1:
        lines.append(
            f"The source publishes {len(upstreams)} active uplink "
            "signal records for this contact."
        )

    faint = power_words(link.down_dbm) if listening else ""
    shout = transmit_power_words(link.up_kw)
    receive_clause = ""
    if faint:
        # _dbm() deliberately keeps the strongest usable record. With more
        # than one carrier, say so instead of attributing that value to the
        # contact as a whole.
        receive_subject = (
            "The strongest published receive record" if len(downstreams) > 1 else "It"
        )
        receive_clause = f"{receive_subject} reaches the dish at {faint}"
    if receive_clause and shout and len(upstreams) > 1:
        lines.append(
            f"{receive_clause}; the strongest published uplink record is {shout}."
        )
    elif receive_clause and shout:
        # The contrast is the whole point: we shout tens of kilowatts and
        # what comes back is around 10^-22 of it.
        lines.append(f"{receive_clause}, while Earth transmits at {shout}.")
    elif receive_clause:
        lines.append(f"{receive_clause}.")
    elif shout and len(upstreams) > 1:
        lines.append(f"The strongest published uplink record is {shout}.")
    elif shout:
        lines.append(f"Earth is transmitting at {shout}.")

    humbling = lightyear_words(link.range_km)
    if humbling:
        lines.append(humbling)

    # Rare, and the most interesting thing on the network when they happen.
    if link.arrayed:
        lines.append(
            "More than one dish is being combined to improve the "
            "receive margin or usable data rate."
        )
    if link.mspa:
        lines.append("This antenna is holding several spacecraft in its beam at once.")
    if link.ddor:
        lines.append(
            "They are taking a precision navigation fix, "
            "using two complexes and usually a distant quasar as a "
            "reference."
        )
    return " ".join(lines)

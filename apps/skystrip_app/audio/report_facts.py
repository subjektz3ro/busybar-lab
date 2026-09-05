"""Skystrip audio / report facts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apps.skystrip_app import eclipse as _eclipse
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather


def _compass(deg) -> str:
    if deg is None:
        return ""
    names = (
        "north",
        "northeast",
        "east",
        "southeast",
        "south",
        "southwest",
        "west",
        "northwest",
    )
    return names[int((deg + 22.5) // 45) % 8]


# What the voice calls each obscuration. Plain English in every style —
# `genz` gets its own register elsewhere but not its own facts.
OBSCURATION_PHRASE = {
    "haze": "haze",
    "smoke": "smoke in the air",
    "dust": "blowing dust",
    "ash": "volcanic ash in the air",
}


def _moon_phase_name(day: float) -> str:
    for limit, name in (
        (1.8, "new"),
        (5.5, "a waxing crescent"),
        (9.2, "first quarter"),
        (12.9, "a waxing gibbous"),
        (16.6, "full"),
        (20.3, "a waning gibbous"),
        (24.1, "last quarter"),
        (27.7, "a waning crescent"),
    ):
        if day < limit:
            return name
    return "new"


def _eclipse_report_facts(now_local: datetime) -> dict | None:
    """What the report may say about Earth's shadow, or None.

    One source of facts for all three styles. `genz` promises that every
    number it speaks is identical to what `plain` would have said, and the
    only way to keep that promise structurally is for the styles to share
    the arithmetic and differ solely in wording.

    Reports the *area* of the disc covered, not the umbral magnitude the
    catalogues quote — magnitude is a fraction of the diameter, and a
    93%-magnitude eclipse hides 96% of the visible face. Speaking the
    catalogue number as if it described the face would be wrong by four
    points on exactly the night anyone is listening.
    """
    try:
        now_utc = now_local.astimezone(timezone.utc)
        live = _eclipse.visible_state(now_utc, _settings.OBSERVER)
        if live is not None and live.in_umbra:
            return {
                "phase": live.phase,
                "pct": round(live.obscuration * 100),
                "at": None,
            }
        # Nothing on the disc yet. If the umbral phase begins soon and the
        # moon will be up for it, that is worth a heads-up — an eclipse you
        # are told about after it ends is not a notice, it is a regret.
        for eclipse in _eclipse.eclipses_near(now_utc):
            window = eclipse.contact("partial")
            if window is None:
                continue  # penumbral-only: nothing anyone can see
            begin = window[0]
            if not now_utc < begin <= now_utc + timedelta(hours=ECLIPSE_HEADS_UP_H):
                continue
            if _eclipse.visible_state(begin, _settings.OBSERVER) is None:
                continue  # happening, but below this horizon
            peak = _eclipse.state_at(eclipse.greatest, eclipse=eclipse)
            if peak is None:
                # A candidate without geometry at its own greatest instant is
                # incomplete. Omit it instead of inventing an obscuration.
                continue
            local_begin = begin.astimezone(_settings.TZ)
            return {
                "phase": eclipse.kind,
                "pct": round(peak.obscuration * 100),
                "at": f"{local_begin.hour % 12 or 12}:{local_begin.minute:02d}",
            }
    except Exception:  # noqa: BLE001 - flavor, never fatal
        return None
    return None


def _forecast_temperature(period: dict) -> int | None:
    value = _weather._finite_number(period.get("temperature"), -150, 200)
    unit = str(period.get("temperatureUnit") or "").strip().upper()
    if value is None or unit not in {"F", "C"}:
        return None
    if _settings.UNITS == "c" and unit == "F":
        value = (value - 32.0) * 5.0 / 9.0
    elif _settings.UNITS != "c" and unit == "C":
        value = value * 9.0 / 5.0 + 32.0
    return round(value)


def _peak_hour_words(hour: int) -> str:
    """Name an hour the way a person says it out loud."""
    if hour == 12:
        return "noon"
    if hour == 0:
        return "midnight"
    part = (
        "in the evening"
        if hour >= 17
        else "this afternoon"
        if hour >= 12
        else "this morning"
    )
    return f"{hour % 12 or 12} {part}"


def _alert_phrase(wx: _weather.WeatherState) -> str:
    """Say which warning it is, not merely that one exists.

    "Severe weather in the area" is true of a Tornado Warning and of a Frost
    Advisory, and you cannot act on it. CAP event names ("Tornado Warning",
    "Flash Flood Warning") are already plain English, so the honest report
    speaks the name the NWS issued. An unnamed alert keeps the old wording:
    vague is bad, invented is worse.
    """
    event = (wx.severe_event or "").strip()
    if not event:
        return "severe weather in the area"
    article = "an" if event[:1].upper() in "AEIOU" else "a"
    return f"{article} {event} in effect"


# How much of a forecast period has to be left before the report will
# still talk about it as something ahead of you.
FORECAST_HANDOVER_MIN = 90


def _period_end(period: dict) -> datetime | None:
    """A period's end as a local datetime, or None if it isn't usable."""
    raw = period.get("endTime")
    if not isinstance(raw, str) or not raw.strip():
        return None
    try:
        end = datetime.fromisoformat(raw.strip())
    except ValueError:
        return None
    if end.tzinfo is None:
        return None
    return end.astimezone(_settings.TZ)


def _forecast_period(forecast: list, now_local: datetime) -> dict | None:
    """The period the report should actually be talking about.

    NWS `periods[0]` is the CURRENT period, not the next one, and every
    style phrases it as something still ahead ("Later we're going for 81
    this afternoon"). The greeting turns over to evening at five o'clock
    while the NWS afternoon runs until six, so for that hour the bar
    greeted you with one and then forecast the other — for a daytime high
    that had already happened.

    So skip a period with less than FORECAST_HANDOVER_MIN left and use the
    next one, which is already fetched. A period whose end cannot be read
    is used as-is: vague is bad, invented is worse.
    """
    if not forecast:
        return None
    for period in forecast:
        end = _period_end(period)
        if end is None:
            return period
        if now_local.tzinfo is None:
            end = end.replace(tzinfo=None)
        if end - now_local > timedelta(minutes=FORECAST_HANDOVER_MIN):
            return period
    return forecast[-1]


def _precip_kind(code: int) -> str:
    """What the hourly weather code is made of."""
    if code in (71, 73, 75, 77, 85, 86):
        return "snow"
    return "storms" if code >= 95 else "rain"


def _hourly_today(hourly: list, now_local: datetime) -> list:
    """The rows from now to the end of the local day."""
    end = now_local.replace(hour=23, minute=59)
    return [(dt, r) for dt, r in hourly if now_local <= dt <= end]


def _today_peak(ahead: list) -> tuple[int, str, str]:
    """(whole percent, spoken hour, kind) for the wettest hour left today.

    Model probabilities arrive as floats. Rounding here keeps the type
    artifact out of every style's prose rather than each one remembering.
    """
    pk, pk_dt, pk_row = max(
        ((r.get("prob") or 0, dt, r) for dt, r in ahead), key=lambda t: t[0]
    )
    return (
        round(pk),
        _peak_hour_words(pk_dt.hour),
        _precip_kind(pk_row.get("code", 0)),
    )


def _tomorrow_outlook(
    hourly: list,
    now_local: datetime,
) -> tuple[int, int, str] | None:
    """(high, whole percent, kind) for tomorrow's daylight, or None."""
    start = (now_local + timedelta(days=1)).replace(hour=7, minute=0)
    rows = [(dt, r) for dt, r in hourly if start <= dt <= start.replace(hour=22)]
    if not rows:
        return None
    hi_c = max(r.get("temp") or 0 for _, r in rows)
    hi = round(hi_c) if _settings.UNITS == "c" else round(hi_c * 9 / 5 + 32)
    pk, pk_row = max(((r.get("prob") or 0, r) for _, r in rows), key=lambda t: t[0])
    return hi, round(pk), _precip_kind(pk_row.get("code", 0))


def _wind_words(wx: _weather.WeatherState) -> tuple[float, str, str, int, int, int]:
    """Speed in the configured unit, its name, the compass phrase, thresholds."""
    if _settings.UNITS == "c":
        return wx.wind_kmh, "kilometers an hour", _compass(wx.wind_dir), 48, 29, 13
    return (wx.wind_kmh * 0.621, "miles an hour", _compass(wx.wind_dir), 30, 18, 8)


# How far ahead the report will announce an eclipse that has not started.
# Long enough that an evening report catches a late-night event, short
# enough that it stays news rather than a calendar.
ECLIPSE_HEADS_UP_H = 6

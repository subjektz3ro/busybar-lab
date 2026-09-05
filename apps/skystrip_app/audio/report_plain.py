"""Skystrip audio / report plain."""

from __future__ import annotations

from datetime import datetime, timedelta

from astral import moon

from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.audio import report_facts as _audio_report_facts
from apps.skystrip_app.audio import report_genz as _audio_report_genz


def _compose_report(
    wx: _weather.WeatherState,
    forecast: list | None,
    now_local: datetime,
    hourly: list | None = None,
) -> str:
    """A weather report with some manners: greeting, conditions with
    character, the forecast, and a little sky at the edges of the day."""
    if _settings.STYLE == "genz":
        return _audio_report_genz._compose_report_genz(wx, forecast, now_local, hourly)
    h = now_local.hour
    if 5 <= h < 12:
        greet = "Good morning"
    elif 12 <= h < 17:
        greet = "Good afternoon"
    elif 17 <= h < 22:
        greet = "Good evening"
    else:
        greet = "Still up"
    f = round(wx.temp_c) if _settings.UNITS == "c" else round(wx.temp_c * 9 / 5 + 32)
    if wx.severe:
        cond = _audio_report_facts._alert_phrase(wx)
    elif wx.thunder:
        cond = "thunderstorms"
    elif wx.snow:
        cond = "snow coming down"
    elif wx.rain:
        cond = "rain"
    elif wx.obscuration:
        # Ahead of the cloud tiers on purpose: a smoke day is usually
        # cloudless, so deferring to cloud_frac would have the voice say
        # "clear skies" while the panel is brown.
        cond = _audio_report_facts.OBSCURATION_PHRASE[wx.obscuration]
    elif wx.cloud_frac >= 0.85:
        cond = "overcast skies"
    elif wx.cloud_frac >= 0.55:
        cond = "mostly cloudy skies"
    elif wx.cloud_frac >= 0.25:
        cond = "partly cloudy skies"
    else:
        cond = "clear skies"
    ch = _settings.STYLE == "chicago"
    if not ch:
        parts = [f"{greet}. It's {f} degrees right now with {cond}."]
    elif wx.severe:
        parts = [
            f"{greet}, folks — there's {_audio_report_facts._alert_phrase(wx)}, "
            "and I need you to take it seriously."
        ]
    elif wx.thunder:
        parts = [
            f"{greet}, folks. We've got thunderstorms rolling "
            f"through — {f} degrees out there."
        ]
    elif wx.snow:
        parts = [f"{greet}, folks. Bundle up — snow coming down and {f} degrees."]
    elif wx.rain:
        parts = [f"{greet}, folks. Grab the umbrella — rain out there and {f} degrees."]
    elif cond == "clear skies" and 18 <= wx.temp_c <= 29:
        parts = [
            f"{greet}, folks. What a beauty out there — {f} degrees under clear skies."
        ]
    else:
        parts = [f"{greet}, folks. {f} degrees right now with {cond}."]

    spd, unit_word, dirname, hi, mid, lo = _audio_report_facts._wind_words(wx)
    outof = f" out of the {dirname}" if dirname else ""
    if spd >= hi:
        parts.append(
            f"And the wind — {round(spd)} {unit_word}{outof}. Hold onto your hat."
            if ch
            else f"Properly windy, {round(spd)} {unit_word}{outof}."
        )
    elif spd >= mid:
        parts.append(f"Breezy out there{outof}." if ch else f"Breezy{outof}.")
    elif spd >= lo and dirname:
        parts.append(f"A gentle breeze{outof}." if ch else f"A light breeze{outof}.")
    if ch and spd >= lo and wx.wind_dir is not None and 45 <= wx.wind_dir <= 135:
        parts.append("Cooler by the lake, as always.")
    if wx.humidity >= 75 and wx.temp_c >= 24 and not wx.rain:
        parts.append(
            "And it is muggy out there — a real steam bath." if ch else "Muggy one."
        )

    # Peek at the hourly outlook first: if it will name precip odds,
    # the NWS period line stays out of the rain business — one
    # umbrella mention per broadcast
    hourly_pk = 0
    if hourly:
        _ahead = _audio_report_facts._hourly_today(hourly, now_local)
        if len(_ahead) >= 3:
            hourly_pk = max((r.get("prob") or 0) for _, r in _ahead)

    period = _audio_report_facts._forecast_period(forecast or [], now_local)
    if period:
        p = period
        name = p.get("name", "later")
        name = name[0].lower() + name[1:] if name else "later"
        temp = _audio_report_facts._forecast_temperature(p)
        prob = round((p.get("probabilityOfPrecipitation") or {}).get("value") or 0)
        short = (p.get("shortForecast") or "").lower()
        kind = (
            "storms" if "thunder" in short else ("snow" if "snow" in short else "rain")
        )
        if temp is None:
            line = f"Looking ahead to {name}"
        elif p.get("isDaytime"):
            line = (
                f"We're heading for {temp} {name}"
                if ch
                else f"Heading for a high of {temp} {name}"
            )
        else:
            line = f"We slide to {temp} {name}" if ch else f"Down to {temp} {name}"
        if prob >= 30 and hourly_pk < 20:
            line += (
                f", with a {prob} percent chance of {kind} — keep an eye on the sky"
                if ch
                else f", with a {prob} percent chance of {kind}"
            )
        parts.append(line + ".")

    # The shape of the rest of the day, from the hourly table
    if hourly:
        ahead = _audio_report_facts._hourly_today(hourly, now_local)
        if len(ahead) >= 3:  # enough of today left to talk about
            pk, when_words, kind = _audio_report_facts._today_peak(ahead)
            if pk >= 45:
                tool = (
                    "Keep the shovel handy."
                    if kind == "snow"
                    else "Keep the umbrella handy."
                )
                parts.append(
                    f"Now here's the thing — a {pk} percent "
                    f"chance of {kind} around {when_words}. {tool}"
                    if ch
                    else f"{pk} percent chance of {kind} around {when_words}."
                )
            elif pk >= 20:
                parts.append(
                    f"Just a slight chance of {kind} later — "
                    f"{pk} percent — nothing to change your "
                    "plans over."
                    if ch
                    else f"A slight chance of {kind} later — {pk} percent at the most."
                )
            else:
                clouds = sum(r.get("cloud") or 0 for _, r in ahead) / len(ahead)
                span = "evening" if now_local.hour >= 17 else "day"
                if ch:
                    parts.append(
                        f"And the rest of the {span}? Gorgeous. "
                        "Not a cloud worth mentioning."
                        if clouds < 30
                        else f"Staying dry the rest of the {span}."
                    )
                else:
                    parts.append(
                        f"Staying clear the rest of the {span}."
                        if clouds < 30
                        else f"Dry the rest of the {span}."
                    )
        else:  # late night: talk about tomorrow instead
            outlook = _audio_report_facts._tomorrow_outlook(hourly, now_local)
            if outlook:
                hi, pk, kind_w = outlook
                if ch and pk >= 30:
                    parts.append(
                        f"And a heads up for tomorrow — "
                        f"we go for {hi} with a {pk} percent "
                        f"chance of {kind_w}."
                    )
                elif ch:
                    parts.append(
                        f"Tomorrow we go for {hi}, and it looks "
                        "dry — get out and enjoy it."
                    )
                elif pk >= 30:
                    parts.append(
                        f"Tomorrow heads for {hi}, with a {pk} "
                        f"percent chance of {kind_w}."
                    )
                else:
                    parts.append(f"Tomorrow heads for {hi} and looks dry.")

    try:
        from astral import sun as _sun

        stimes = _sun.sun(
            _settings.OBSERVER, date=now_local.date(), tzinfo=_settings.TZ
        )
        sr, ss = stimes["sunrise"], stimes["sunset"]
        if sr <= now_local < ss:
            parts.append(
                f"Sunset tonight at "
                f"{ss.hour % 12 or 12}:{ss.minute:02d} — "
                "don't miss it."
                if ch
                else f"Sun sets at {ss.hour % 12 or 12}:{ss.minute:02d}."
            )
        else:  # after sunset (or before dawn): moon now, sun next
            ecl = _audio_report_facts._eclipse_report_facts(now_local)
            if ecl is None:
                phase_name = _audio_report_facts._moon_phase_name(
                    moon.phase(now_local.date())
                )
                article = "" if phase_name.startswith("a ") else "a "
                parts.append(
                    f"And we've got {article}{phase_name} moon out "
                    "there tonight — worth a look."
                    if ch
                    else f"The moon is {phase_name} tonight."
                )
            else:
                # The eclipse replaces the phase line rather than joining it.
                # During an eclipse the phase is always full, so saying both
                # would be "the moon is full, and also in Earth's shadow" —
                # the second clause makes the first noise.
                total = ecl["phase"] == "total"
                if ecl["at"] is None and total:
                    parts.append(
                        "And go outside, folks — there's a total lunar "
                        "eclipse happening right now, the whole moon inside "
                        "Earth's shadow. You do not get many of these."
                        if ch
                        else "There is a total lunar eclipse right now — the "
                        "moon is entirely inside Earth's shadow."
                    )
                elif ecl["at"] is None:
                    parts.append(
                        f"And go outside, folks — there's a partial lunar "
                        f"eclipse happening right now, Earth's shadow across "
                        f"{ecl['pct']} percent of the moon. You do not get "
                        "many of these."
                        if ch
                        else f"There is a partial lunar eclipse right now — "
                        f"Earth's shadow covers {ecl['pct']} percent of "
                        "the moon."
                    )
                else:
                    kind = "total" if total else "partial"
                    parts.append(
                        f"And mark this one, folks — a {kind} lunar eclipse "
                        f"starts at {ecl['at']} tonight. Worth staying up "
                        "for."
                        if ch
                        else f"A {kind} lunar eclipse starts at {ecl['at']} tonight."
                    )
            if now_local >= ss:
                nxt = _sun.sun(
                    _settings.OBSERVER,
                    date=(now_local + timedelta(days=1)).date(),
                    tzinfo=_settings.TZ,
                )["sunrise"]
                when = "tomorrow"
            else:
                nxt, when = sr, "this morning"
            parts.append(f"Sunrise at {nxt.hour % 12 or 12}:{nxt.minute:02d} {when}.")
    except Exception:  # noqa: BLE001 - flavor, never fatal
        pass
    if ch:
        parts.append("And that's the picture, folks.")
    return " ".join(parts)

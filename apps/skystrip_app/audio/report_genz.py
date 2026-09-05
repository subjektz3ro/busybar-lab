"""Skystrip audio / report genz."""

from __future__ import annotations

import random
from datetime import datetime, timedelta

from astral import moon

from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.audio import report_facts as _audio_report_facts

# The genz register, as phrase pools rather than fixed lines.
#
# Variance here is DETERMINISTIC on purpose. The report's text is hashed
# into the device asset filename and the firmware caches assets by path
# forever, so phrasing that rerolled on every render would bake and upload
# a fresh .snd every minute. Seeding on the local date and hour gives a
# different-sounding report each hour while staying byte-stable within one
# — and you rarely hear two reports in the same hour anyway.
#
# Terminology is checked against current usage rather than memory, because
# this slang ages in months: "mid", "ate", "aura", "delulu", "cooked",
# "crashing out" and "standing on business" are the 2026 register, while
# the 2020 TikTok vocabulary now reads as a parody of itself.
GENZ_GREETINGS = {
    "morning": (
        "Morning, chat.",
        "Okay so, good morning.",
        "Morning, bestie.",
        "Good morning, I guess.",
    ),
    "afternoon": (
        "Afternoon, chat.",
        "Okay so, afternoon.",
        "Afternoon, bestie.",
        "Hi, it's the afternoon.",
    ),
    "evening": (
        "Evening, chat.",
        "Okay so, evening.",
        "Evening, bestie.",
        "Hello, it's evening.",
    ),
    "late": (
        "Bestie, it's so late.",
        "Chat, why are we awake.",
        "It's giving no sleep.",
        "Okay so we're just not sleeping.",
    ),
}

GENZ_CONDITIONS = {
    "thunder": (
        "It's storming and the sky is fully crashing out.",
        "Thunderstorms, which is a lot.",
        "It's storming. Very dramatic.",
    ),
    "snow": (
        "It's snowing, so that's a whole situation.",
        "Snow. We are not beating the winter allegations.",
        "It's snowing, which is genuinely so much.",
    ),
    "rain": (
        "It's raining, which is lowkey rude.",
        "It's raining. Respectfully, no.",
        "Rain again. The sky is crashing out.",
    ),
    "hot": (
        "It is so hot. We're cooked.",
        "It's hot. Genuinely cooked out here.",
        "The heat is standing on business.",
    ),
    "cold": (
        "It's freezing. Respectfully, no.",
        "It's so cold. I'm not okay.",
        "Freezing. We are not beating the winter allegations.",
    ),
    # Haze and dust keep the register. Smoke and ash do not: wildfire smoke
    # and volcanic ash are somebody's emergency somewhere upwind, and the
    # bit landing on those is the same misfire as joking under a warning.
    "obscured_haze": (
        "It's hazy out, everything looks kind of soft.",
        "Hazy. The sky is giving washed out.",
        "Haze everywhere. Distance is not a thing today.",
    ),
    "obscured_dust": (
        "There's dust blowing around out there.",
        "Blowing dust today, so keep your eyes covered.",
        "It's dusty out. The air is doing something.",
    ),
    "obscured_smoke": (
        "There's smoke in the air today. Take it easy outside.",
        "Smoke in the air. Maybe keep the windows shut.",
        "There's smoke out there, so go easy today.",
    ),
    "obscured_ash": (
        "There is volcanic ash in the air. Stay inside.",
        "Volcanic ash out there. Please stay indoors.",
        "There's ash in the air today. Stay in.",
    ),
    "overcast": (
        "It's fully grey out. Big yikes.",
        "Overcast. It's giving nothing.",
        "Grey. All grey. No notes, and not in a good way.",
    ),
    "cloudy": (
        "Pretty cloudy, kind of mid.",
        "Mostly cloudy, which is fine I guess.",
        "Cloudy. Mid, honestly.",
    ),
    "partly": (
        "Some clouds, nothing crazy.",
        "A few clouds. We move.",
        "Partly cloudy, no complaints.",
    ),
    "nice": (
        "Clear skies, and the weather ate.",
        "Clear skies. This one has aura.",
        "Not a cloud. Main character energy.",
    ),
    "clear": (
        "Clear skies, no notes.",
        "Clear out, genuinely nothing happening.",
        "Clear skies. We're so back.",
    ),
}

GENZ_WIND_BIG = (
    "The wind is doing {n} {u}{d}, which is actually insane.",
    "Wind's standing on business at {n} {u}{d}.",
    "{n} {u} of wind{d}. That's crazy.",
)

GENZ_WIND_MID = (
    "Kind of breezy{d}.",
    "Breezy{d}, not a big deal.",
    "A little windy{d}.",
)

GENZ_WIND_LOW = ("Light breeze{d}.", "Slight breeze{d}.")

GENZ_MUGGY = (
    "Also it's humid, which is deadass disrespectful.",
    "And it's humid. The air is soup.",
    "Humid too. Respectfully, gross.",
)

GENZ_UMBRELLA = (
    "Bring an umbrella, I'm being serious.",
    "Take the umbrella, I'm not being delulu about this.",
    "Umbrella. I'm standing on business about it.",
)

GENZ_SHOVEL = (
    "Keep the shovel close.",
    "Shovel weather, sorry.",
    "The shovel is going to be involved.",
)

GENZ_GORGEOUS = (
    "Rest of the {s} is genuinely gorgeous, no notes.",
    "Rest of the {s} absolutely ate.",
    "We're so back for the rest of the {s}.",
)

GENZ_DRY = (
    "Staying dry the rest of the {s}.",
    "Dry the rest of the {s}, at least.",
    "Rest of the {s} stays dry. Mid but fine.",
)

GENZ_SUNSET = (
    "Sunset's at {t}. Go touch grass.",
    "Sunset's at {t}, genuinely go outside for that one.",
    "Sunset at {t}. That one's worth it.",
)

GENZ_MOON = (
    "There's {a}{p} moon out and it's giving.",
    "{P} moon tonight. It has aura.",
    "The moon is {p} tonight, which is kind of beautiful.",
)

# The eclipse pools speak the same numbers `plain` does — that invariant is
# enforced by sharing `_eclipse_report_facts`, and pinned by a test. Whole
# words only, like every other pool here: the initialisms this register is
# known for are unpronounceable to a neural voice.
GENZ_ECLIPSE_PARTIAL = (
    "Also the moon is getting eaten by Earth's shadow right now, "
    "{n} percent of it gone. Go look.",
    "Earth's shadow is on the moon right now, {n} percent covered. "
    "That is a lunar eclipse and it is insane.",
    "There is a lunar eclipse happening. Earth's shadow has {n} percent "
    "of the moon. Put your shoes on.",
)

GENZ_ECLIPSE_TOTAL = (
    "Also the moon is fully inside Earth's shadow right now. "
    "A total lunar eclipse. Go outside, this is not a drill.",
    "Total lunar eclipse happening right now, the whole moon swallowed. "
    "Genuinely once in a while stuff.",
    "Earth's shadow has the entire moon right now. Total eclipse. "
    "Please go look at it.",
)

GENZ_ECLIPSE_SOON = (
    "Also there is a {k} lunar eclipse starting at {t} tonight. "
    "Set an alarm, seriously.",
    "Heads up, {k} lunar eclipse at {t} tonight. Earth's shadow, "
    "on the moon. Stay up for it.",
    "A {k} lunar eclipse kicks off at {t} tonight and you are going "
    "to want to see that.",
)

# When a warning is in effect the bit is off for the WHOLE report, not
# just the alert sentence. "Umbrella, I'm standing on business about it"
# under a Tornado Warning is the joke landing on the one line that has to
# be actionable, and the flavour at the end is worse — nobody should be
# told to go touch grass during a warning. Severe reports are short,
# factual and end by saying so.
GENZ_SERIOUS_TOOL = "Take that seriously and keep an eye on the sky."

GENZ_SERIOUS_SIGNOFF = "Stay safe out there."

GENZ_SIGNOFF = (
    "Anyway. That's the weather, no cap.",
    "Anyway. That's your weather.",
    "That's it. That's the forecast.",
    "Okay that's the weather. Bye.",
)

# Numbers that a teenager is constitutionally incapable of letting pass.
#
# The digits still get spoken normally — the gag is an ADDITION, never a
# replacement, because a listener came for the temperature. And "6-7" is
# written as words on purpose: `speakable` rewrites "-?\d+" into prose, so
# the digit form comes out of the speaker as "sixnegative seven".
GENZ_NICE = ("Nice.", "Heh. Nice.", "Nice. I said what I said.")

# No "Anyway" here: the sign-off pool owns that word and the two landed
# back to back.
GENZ_SIX_SEVEN = (
    "Six... seven.",
    "Six... seven. Sorry, I had to.",
    "Six... seven. Okay, moving on.",
)


def _genz_number_gag(rng: random.Random, value) -> str | None:
    """The aside a given number earns, if it earns one."""
    if not isinstance(value, int) or isinstance(value, bool):
        return None
    if value == 69:
        return rng.choice(GENZ_NICE)
    if value == 67:
        return rng.choice(GENZ_SIX_SEVEN)
    return None


def _genz_rng(now_local: datetime) -> random.Random:
    """Stable within the hour, different the next one.

    See GENZ_GREETINGS: rerolling per render would churn device assets.
    """
    return random.Random(int(now_local.strftime("%Y%m%d%H")))


def _genz_condition_key(wx: _weather.WeatherState) -> str:
    """Which pool describes the sky right now, in precedence order."""
    if wx.thunder:
        return "thunder"
    if wx.snow:
        return "snow"
    if wx.rain:
        return "rain"
    if wx.obscuration:
        # Before the temperature tiers too: on a smoke day the air is the
        # story, and "we're cooked" is a bad joke to make about a wildfire.
        return f"obscured_{wx.obscuration}"
    if wx.temp_c >= 32.0:
        return "hot"
    if wx.temp_c <= -7.0:
        return "cold"
    if wx.cloud_frac >= 0.85:
        return "overcast"
    if wx.cloud_frac >= 0.55:
        return "cloudy"
    if wx.cloud_frac >= 0.25:
        return "partly"
    return "nice" if 18 <= wx.temp_c <= 29 else "clear"


def _compose_report_genz(
    wx: _weather.WeatherState,
    forecast: list | None,
    now_local: datetime,
    hourly: list | None = None,
) -> str:
    """The same forecast, delivered like a stereotyped teenager.

    Two rules keep this honest. The numbers are never restyled — the
    temperature, the percentages and the clock times are exactly what the
    plain voice would have said, because a register changes the wording
    and not the facts. And a severe alert drops the bit entirely: a
    Tornado Warning is something a person has to act on, so it is named
    plainly and no slang goes anywhere near that sentence.

    The register is deliberately built from whole WORDS. The initialisms
    this slang is best known for are unpronounceable to a neural voice and
    `speakable` does not expand them, so "not gonna lie" earns its place
    and "ngl" would just be letter mush out of the speaker.
    """
    rng = _genz_rng(now_local)
    # One bit per broadcast, mirroring the one-umbrella-mention rule: two
    # gags in a single report is a comedy routine, not a forecast. A
    # warning spends none at all.
    spent = [bool(wx.severe)]

    def gag(value) -> str | None:
        if spent[0]:
            return None
        aside = _genz_number_gag(rng, value)
        if aside:
            spent[0] = True
        return aside

    h = now_local.hour
    if 5 <= h < 12:
        slot = "morning"
    elif 12 <= h < 17:
        slot = "afternoon"
    elif 17 <= h < 22:
        slot = "evening"
    else:
        slot = "late"
    greet = rng.choice(GENZ_GREETINGS[slot])
    f = round(wx.temp_c) if _settings.UNITS == "c" else round(wx.temp_c * 9 / 5 + 32)

    if wx.severe:
        # No bit here, and none below either — see GENZ_SERIOUS_TOOL.
        parts = [
            f"Heads up. There's {_audio_report_facts._alert_phrase(wx)}, "
            f"and you need to take that seriously. {f} degrees."
        ]
    else:
        cond = rng.choice(GENZ_CONDITIONS[_genz_condition_key(wx)])
        parts = [f"{greet} {cond} {f} degrees."]
        if aside := gag(f):
            parts.append(aside)

    spd, unit_word, dirname, hi, mid, lo = _audio_report_facts._wind_words(wx)
    outof = f" out of the {dirname}" if dirname else ""
    if spd >= hi:
        parts.append(
            f"The wind is {round(spd)} {unit_word}{outof}."
            if wx.severe
            else rng.choice(GENZ_WIND_BIG).format(n=round(spd), u=unit_word, d=outof)
        )
    elif spd >= mid and not wx.severe:
        parts.append(rng.choice(GENZ_WIND_MID).format(d=outof))
    elif spd >= lo and dirname and not wx.severe:
        parts.append(rng.choice(GENZ_WIND_LOW).format(d=outof))
    if wx.humidity >= 75 and wx.temp_c >= 24 and not wx.rain and not wx.severe:
        parts.append(rng.choice(GENZ_MUGGY))

    # One precip mention per broadcast: if the hourly table is going to
    # name odds, the forecast period keeps quiet about them.
    hourly_pk = 0
    if hourly:
        ahead = _audio_report_facts._hourly_today(hourly, now_local)
        if len(ahead) >= 3:
            hourly_pk = max((r.get("prob") or 0) for _, r in ahead)

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
            line = f"Later we're going for {temp} {name}"
        else:
            line = f"We're dropping to {temp} {name}"
        if prob >= 30 and hourly_pk < 20:
            line += f", with a {prob} percent chance of {kind}, so plan around that"
        parts.append(line + ".")
        if aside := gag(temp) or gag(prob):
            parts.append(aside)

    if hourly:
        ahead = _audio_report_facts._hourly_today(hourly, now_local)
        if len(ahead) >= 3:
            pk, when_words, kind = _audio_report_facts._today_peak(ahead)
            if pk >= 45:
                tool = (
                    GENZ_SERIOUS_TOOL
                    if wx.severe
                    else rng.choice(GENZ_SHOVEL if kind == "snow" else GENZ_UMBRELLA)
                )
                parts.append(
                    f"There's a {pk} percent chance of {kind} "
                    f"around {when_words}. {tool}"
                )
                if aside := gag(pk):
                    parts.append(aside)
            elif pk >= 20:
                parts.append(
                    f"Slight chance of {kind} later, like {pk} percent. Not a big deal."
                )
            else:
                clouds = sum(r.get("cloud") or 0 for _, r in ahead) / len(ahead)
                span = "evening" if now_local.hour >= 17 else "day"
                pool = GENZ_GORGEOUS if clouds < 30 else GENZ_DRY
                parts.append(rng.choice(pool).format(s=span))
        else:  # late night: tomorrow instead
            outlook = _audio_report_facts._tomorrow_outlook(hourly, now_local)
            if outlook:
                hi_t, pk, kind_w = outlook
                if pk >= 30:
                    parts.append(
                        f"Tomorrow we're going for {hi_t}, with a "
                        f"{pk} percent chance of {kind_w}."
                    )
                else:
                    parts.append(f"Tomorrow we're going for {hi_t} and it looks dry.")
                if aside := gag(hi_t):
                    parts.append(aside)

    # No sky flavour under a warning: nobody should be told to go
    # outside and touch grass while one is in effect.
    if not wx.severe:
        try:
            from astral import sun as _sun

            stimes = _sun.sun(
                _settings.OBSERVER, date=now_local.date(), tzinfo=_settings.TZ
            )
            sr, ss = stimes["sunrise"], stimes["sunset"]
            if sr <= now_local < ss:
                parts.append(
                    rng.choice(GENZ_SUNSET).format(
                        t=f"{ss.hour % 12 or 12}:{ss.minute:02d}"
                    )
                )
            else:
                ecl = _audio_report_facts._eclipse_report_facts(now_local)
                if ecl is None:
                    phase_name = _audio_report_facts._moon_phase_name(
                        moon.phase(now_local.date())
                    )
                    article = "" if phase_name.startswith("a ") else "a "
                    parts.append(
                        rng.choice(GENZ_MOON).format(
                            a=article,
                            p=phase_name,
                            P=phase_name[0].upper() + phase_name[1:],
                        )
                    )
                elif ecl["at"] is not None:
                    parts.append(
                        rng.choice(GENZ_ECLIPSE_SOON).format(
                            t=ecl["at"], k=ecl["phase"]
                        )
                    )
                elif ecl["phase"] == "total":
                    parts.append(rng.choice(GENZ_ECLIPSE_TOTAL))
                else:
                    parts.append(rng.choice(GENZ_ECLIPSE_PARTIAL).format(n=ecl["pct"]))
                if now_local >= ss:
                    nxt = _sun.sun(
                        _settings.OBSERVER,
                        date=(now_local + timedelta(days=1)).date(),
                        tzinfo=_settings.TZ,
                    )["sunrise"]
                    when = "tomorrow"
                else:
                    nxt, when = sr, "this morning"
                parts.append(
                    f"Sunrise at {nxt.hour % 12 or 12}:{nxt.minute:02d} {when}."
                )
        except Exception:  # noqa: BLE001 - flavor, never fatal
            pass
    parts.append(GENZ_SERIOUS_SIGNOFF if wx.severe else rng.choice(GENZ_SIGNOFF))
    return " ".join(parts)

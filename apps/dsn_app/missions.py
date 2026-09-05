"""Curated DSN narration and its source/review policy.

Mission text, aliases, provenance and expiry belong together. This module has
no device, network, configuration or renderer dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone

# Why each spacecraft exists or what it accomplished. NASA's feed gives us the
# full name live (see fetch_names) but never supplies mission phase or purpose.
# The review lease below prevents these curated facts from posing as permanent
# live status. Unknown or expired codes simply narrate without a blurb.
MISSIONS = {
    "vgr1": "Launched in 1977, it is the most distant object humans have ever "
    "made, and it is now flying through interstellar space",
    "vgr2": "Launched in 1977, it is the only spacecraft ever to visit Uranus "
    "and Neptune, and it is now in interstellar space",
    "jno": "It has been orbiting Jupiter since 2016, looking beneath the "
    "clouds to work out what the planet is made of",
    "mro": "It has been photographing Mars from orbit since 2006, and it "
    "relays data home for the rovers on the surface",
    "m01o": "In orbit since 2001, it is the longest-serving spacecraft at Mars",
    "mvn": "A Mars orbiter launched to trace how the planet's atmosphere "
    "escaped to space. NASA ended the mission in June 2026",
    "lro": "It has been mapping the Moon in fine detail since 2009, including "
    "the sites where people will land next",
    "soho": "It watches the Sun without pause from a point about a million "
    "miles sunward of Earth",
    "ace": "It samples the solar wind upstream of Earth, which buys us about "
    "an hour of warning before a solar storm arrives",
    "psyc": "It is on its way to a metal-rich asteroid, to see what may be the "
    "exposed core of an early planet",
    "nhpc": "It flew past Pluto in 2015 and is now out in the Kuiper Belt",
    "lucy": "It is on a twelve-year tour of the Trojan asteroids that share "
    "Jupiter's orbit",
    "eurc": "It is on its way to Europa, to survey the ocean beneath the ice "
    "of Jupiter's moon",
    "spp": "It flies closer to the Sun than anything ever built, through the "
    "outer atmosphere itself",
    "jwst": "It observes the early universe in infrared from a point beyond "
    "the Moon, about a million miles from Earth",
    "chdr": "It is an X-ray telescope, watching black holes and the remains "
    "of exploded stars",
    "dsco": "It watches the whole sunlit face of the Earth, and the solar "
    "wind arriving upstream of us",
    "tess": "It hunts for planets around other stars by watching for the dip "
    "as one crosses its star",
    "m20": "Perseverance landed in Jezero Crater in 2021 to explore ancient "
    "habitable environments and collect sealed rock cores for possible "
    "return to Earth",
    "msl": "Curiosity landed in Gale Crater in 2012 to read Mars's "
    "environmental history from layers of rock",
    "wind": "Launched in 1994 to measure the solar wind before it reaches Earth",
    "sta": "One of a pair sent to view the Sun from the side, so we could see "
    "storms coming rather than only head on",
    "orx": "It flew to an asteroid, took a sample and dropped it back to "
    "Earth by parachute, and is now on its way to another one",
    "tgo": "A European orbiter sniffing the Martian atmosphere for rare "
    "gases, and relaying data home for the rovers on the surface",
    "mex": "Europe's first Mars mission, in orbit since 2003 and still "
    "mapping the surface and sounding for water ice",
    "emm": "The United Arab Emirates' first mission to Mars, watching the "
    "whole planet's weather from a high orbit",
    "bepi": "A European and Japanese pair on their way to Mercury, braking "
    "against the Sun's gravity with flyby after flyby",
    "juice": "It is on its way to Jupiter to study three icy moons that may "
    "each hold an ocean under the shell",
    "gaia": "It charted the positions and motions of more than a billion "
    "stars, turning the Milky Way into a three-dimensional map",
    "imap": "It maps the boundary where the solar wind meets interstellar "
    "space, the bubble the Voyagers flew out of",
    "caps": "A microwave-oven-sized craft testing the odd looping orbit that "
    "the lunar Gateway station will use",
    "ltb": "A small lunar orbiter built to map water ice. NASA ended the "
    "mission in July 2025 after losing contact before science "
    "operations",
    # --- currently flying -------------------------------------------------
    "swfo": "It watches the solar wind from a million miles sunward, to give "
    "warning of a geomagnetic storm before it reaches us",
    "cgo": "It photographs the vast cloud of hydrogen that surrounds the "
    "Earth, far beyond the visible atmosphere",
    "escb": "One of a pair of small orbiters designed to measure how the "
    "solar wind strips material from Mars",
    "escg": "The other of the two ESCAPADE orbiters, designed to measure the "
    "same Martian space-weather event from a second location",
    "dart": "It was deliberately crashed into a small asteroid in 2022 to "
    "see whether the impact could shift its orbit. It could",
    "terr": "An Earth-observing satellite launched in 1999 to study the "
    "planet's land, atmosphere and oceans with five instruments",
    "hst": "The Hubble Space Telescope, in orbit since 1990 and still one of "
    "the sharpest eyes we have",
    "xmm": "A European X-ray observatory, watching the hottest and most "
    "violent objects in the sky",
    "intg": "A European observatory for gamma rays, the most energetic light there is",
    "hyb2": "It collected a sample from the asteroid Ryugu and dropped it "
    "into the Australian desert in 2020",
    "ch2o": "India's second lunar mission, still orbiting and mapping the "
    "Moon after its lander was lost",
    "ch2": "India's second lunar mission, mapping the Moon from orbit",
    "ch3": "India's third lunar mission, which landed near the Moon's south "
    "pole in 2023",
    "plc": "A Japanese orbiter that studied Venusian weather. JAXA ended "
    "operations in September 2025 after contact had been lost",
    "slim": "A Japanese lander that touched down within a hundred metres of "
    "its target in 2024, and came to rest upside down",
    "mom": "India's first mission to Mars, which reached orbit at the first attempt",
    "hera": "A European mission on its way to survey the asteroid that DART "
    "hit, to measure exactly what the impact did",
    # --- past, and mostly ended -------------------------------------------
    "cas": "It orbited Saturn for thirteen years, dropped a probe onto Titan, "
    "and was steered into the planet in 2017 so it could never "
    "contaminate a moon",
    "rose": "It escorted a comet around the Sun for two years and put a "
    "lander on its surface",
    "msgr": "The first spacecraft to orbit Mercury, which it mapped for four "
    "years before running out of fuel",
    "dawn": "The only spacecraft to orbit two different worlds: the asteroid "
    "Vesta, then the dwarf planet Ceres",
    "sdu": "It flew through a comet's tail, caught grains of it in aerogel, "
    "and parachuted them back to Earth",
    "dif": "It fired a copper slug into a comet to see what the inside of one "
    "is made of",
    "kepl": "It stared at one patch of sky for years and found thousands of "
    "planets around other stars",
    "stf": "An infrared telescope that spent sixteen years seeing the cold "
    "and the dust-hidden things visible light cannot reach",
    "ulys": "It used a Jupiter gravity assist to survey the Sun's polar "
    "regions from a steeply inclined orbit",
    "map": "It mapped the faint afterglow of the Big Bang and pinned down the "
    "age of the universe",
    "mer1": "Opportunity, a rover built for ninety days on Mars that kept "
    "going for fifteen years",
    "mer2": "Spirit, Opportunity's twin, which drove Mars for six years "
    "before its wheels bogged down for good",
    "phx": "A lander that dug into the northern plains of Mars and found "
    "water ice a few centimetres down",
    "nsyt": "It listened for marsquakes with a seismometer set directly on "
    "the ground, until dust covered its solar panels",
    "mgs": "It mapped Mars for nine years and found the gullies that argued "
    "for water having run there",
    "vex": "A European orbiter that studied the runaway greenhouse "
    "atmosphere of Venus for eight years",
    "grla": "One of a pair that flew in close formation around the Moon, "
    "measuring its gravity by the millimetre",
    "grlb": "The other of the pair, whose exact distance from its twin is "
    "what revealed the Moon's interior",
    "lade": "It measured the Moon's impossibly thin atmosphere and the dust "
    "that floats above the surface",
    "lcro": "It followed a spent rocket stage into a shadowed lunar crater "
    "and flew through the plume to look for water",
    "imag": "It made the first pictures of the invisible plasma trapped in "
    "Earth's magnetic field",
    "polr": "It watched the aurora from above, over the pole",
    "ice": "A 1978 spacecraft that visited a comet, was abandoned, and was "
    "briefly woken again by volunteers in 2014",
    "sele": "A Japanese lunar orbiter that mapped the Moon in high definition "
    "and filmed an Earthrise",
    "ch1": "India's first lunar mission, which found water bound into the "
    "soil of the Moon",
    "musc": "The first spacecraft to bring back a sample of an asteroid, "
    "limping home after nearly every system had failed",
    "spil": "A privately built Israeli lunar lander that reached the Moon in "
    "2019 and crashed on touchdown",
    "mcoa": "One of two briefcase-sized craft that flew to Mars alongside "
    "InSight and relayed its landing live",
    "mcob": "The other briefcase-sized relay, which sent home a parting "
    "photograph of Mars as it flew past",
    "lici": "An Italian cubesat that trailed DART and photographed the plume "
    "thrown off by the impact",
    # --- the cubesats that rode Artemis I ---------------------------------
    "argo": "A shoebox-sized Italian cubesat that photographed the rocket "
    "stage that carried it, as a test of autonomous imaging",
    "bios": "It carried yeast into deep space to measure what the radiation "
    "out there does to living cells",
    "equl": "A Japanese cubesat that steered itself to the far side of the "
    "Moon using water as propellant",
    "omot": "A Japanese attempt at the smallest ever lunar lander, which was "
    "lost before it could try",
    "hmap": "A cubesat built to map hydrogen, and so buried ice, at the "
    "Moon's south pole",
    "mlic": "A cubesat sent to look for water ice from lunar orbit",
    "neas": "It was to unfurl a solar sail and cruise to a near-Earth "
    "asteroid on sunlight alone",
    "cusp": "An Artemis I cubesat designed to measure solar particles and "
    "magnetic fields in deep space",
    "lfl": "It was to shine lasers into craters that never see sunlight, "
    "looking for ice in the dark",
    # --- fleets, and the flights ahead ------------------------------------
    "em1": "Artemis I: the uncrewed first flight of the Space Launch System "
    "and Orion, completed in 2022",
    "em2": "Artemis II: the first crewed flight of the Space Launch System "
    "and Orion, completed in April 2026",
    "em3": "Artemis III: the crewed low-Earth-orbit test flight in NASA's "
    "updated lunar architecture",
    "kplo": "South Korea's first mission beyond Earth orbit, photographing "
    "the Moon and scouting landing sites",
    "stab": "The second of the pair sent to view the Sun from the side. "
    "Contact was lost in 2014",
    "stb": "The second of the pair sent to view the Sun from the side. "
    "Contact was lost in 2014",
    "apm1": "A commercial lunar lander that reached space in 2024 but lost "
    "its propellant to a valve failure and never got there",
    "agm1": "A commercial lander built to carry cargo to the Moon's south pole",
    "ch2l": "Vikram, the lander India's second lunar mission carried. It was "
    "lost in the final minutes of its descent",
    "rsp": "A European rover built to drill two metres into Mars, deeper "
    "than anything has, looking for life that may be sheltered there",
    "icps": "Not a spacecraft: the upper stage that pushes an Artemis "
    "capsule out of Earth orbit and toward the Moon",
    "ltst": "Not a spacecraft: the upper stage that pushes an Artemis "
    "capsule out of Earth orbit and toward the Moon",
    "eus": "Not a spacecraft: an Exploration Upper Stage development effort "
    "that NASA terminated under its revised Artemis architecture",
    "jnsa": "One of a pair of small craft designed to fly past binary "
    "asteroids. The mission was shelved before launch",
    "jnsb": "The other of the pair built to study binary asteroids, shelved "
    "before it flew",
    "tm": "A cubesat deployed from Artemis I to test a plasma thruster. NASA "
    "detected brief downlink signals after deployment",
    "tmm": "A cubesat deployed from Artemis I to test a plasma thruster. NASA "
    "detected brief downlink signals after deployment",
    "cue3": "A student-built Cube Quest entrant designed to test deep-space "
    "radio navigation; it did not fly on Artemis I",
    "lunah-map": "A cubesat built to map hydrogen, and so buried ice, at the "
    "Moon's south pole",
    "rd1": "A proposal to land a commercial capsule on Mars. It was never flown",
    "rp": "A rover designed to prospect for ice at the lunar poles. It was "
    "cancelled before it was built",
    "olin": "One of three small satellites designed to fly in formation and "
    "study structure in the upper atmosphere",
    "lnd1": "A small radio beacon intended for the lunar surface, to help "
    "later missions navigate",
    "m01s": "In orbit since 2001, it is the longest-serving spacecraft at Mars",
    "mros": "It has been photographing Mars from orbit since 2006, and it "
    "relays data home for the rovers on the surface",
    "gtl": "A long-running Japanese and American mission through the tail of "
    "Earth's magnetic field, streaming away from the Sun",
}

# Fleets. Near-identical craft share a purpose-level description rather than
# repeating mutable operational status a dozen times by hand.
for _n in (1, 3, 4, 5, 6, 7, 8, 9):
    MISSIONS[f"tdr{_n}"] = (
        "A Tracking and Data Relay Satellite built to "
        "carry traffic between the ground and spacecraft "
        "in Earth orbit"
    )
MISSIONS["tdr1"] = (
    "The first Tracking and Data Relay Satellite, which "
    "carried traffic for spacecraft in Earth orbit before "
    "retirement"
)
MISSIONS["tdr4"] = (
    "A first-generation Tracking and Data Relay Satellite "
    "that carried traffic for spacecraft in Earth orbit "
    "before retirement"
)
for _n in (10, 11, 12, 13):
    MISSIONS[f"td{_n}"] = (
        "A Tracking and Data Relay Satellite built to "
        "carry traffic between the ground and spacecraft "
        "in Earth orbit"
    )
for _n in range(10, 18):
    MISSIONS[f"go{_n}"] = (
        "A weather satellite built for geostationary "
        "observation of the same face of Earth"
    )
for _n in range(15, 19):
    MISSIONS[f"no{_n}"] = (
        "One of NOAA's polar-orbiting weather satellites, "
        "which scanned Earth strip by strip before the "
        "POES constellation was retired in August 2025"
    )
for _n in range(1, 5):
    MISSIONS[f"mms{_n}"] = (
        "One of four flying in a pyramid, close enough "
        "to catch the moment Earth's magnetic field "
        "snaps and reconnects"
    )
    MISSIONS[f"clu{_n}"] = (
        "One of a European quartet that flew in formation "
        "through Earth's magnetic field. ESA ended the "
        "Cluster mission in September 2024"
    )
for _c in ("thb", "thc"):
    MISSIONS[_c] = (
        "One of two THEMIS probes moved into lunar orbit and "
        "renamed ARTEMIS to study the Moon's space environment"
    )

# Hyphenated aliases occur in historical/config records as well as future
# reservations, so a blanket "planned before it flies" is not truthful.
MISSIONS["em-1"] = MISSIONS["em1"]
MISSIONS["em-2"] = MISSIONS["em2"]
MISSIONS["em-3"] = MISSIONS["em3"]
for _n in range(4, 11):
    MISSIONS[f"em-{_n}"] = (
        "An Artemis flight designation in NASA's campaign to return people to the Moon"
    )


@dataclass(frozen=True)
class MissionReview:
    """Provenance policy for one spoken blurb.

    Stable history/purpose copy is reviewed once and does not pretend to be a
    live status.  Status-sensitive copy is publishable only while both its
    primary source and dated review lease are present and current.
    """

    source_url: str = ""
    reviewed_on: date | None = None
    review_by: date | None = None
    expired_fallback: str = ""
    stable: bool = False


MISSION_REVIEWED_ON = date(2026, 8, 8)
MISSION_REVIEW_BY = date(2026, 11, 8)

# Explicit is important here: a newly added blurb starts unverified and cannot
# be narrated merely because somebody forgot to classify it. These entries are
# historical facts or purpose-level descriptions whose truth does not depend on
# a craft still operating, flying a particular phase, or retaining a schedule.
STABLE_MISSION_BLURBS = frozenset(
    {
        "mvn",
        "m20",
        "msl",
        "wind",
        "sta",
        "ltb",
        "escb",
        "escg",
        "dart",
        "terr",
        "gaia",
        "plc",
        "hyb2",
        "ch3",
        "slim",
        "mom",
        "cas",
        "rose",
        "msgr",
        "dawn",
        "sdu",
        "dif",
        "kepl",
        "stf",
        "ulys",
        "map",
        "mer1",
        "mer2",
        "phx",
        "nsyt",
        "mgs",
        "vex",
        "grla",
        "grlb",
        "lade",
        "lcro",
        "imag",
        "polr",
        "ice",
        "sele",
        "ch1",
        "musc",
        "spil",
        "mcoa",
        "mcob",
        "lici",
        "argo",
        "bios",
        "equl",
        "omot",
        "hmap",
        "mlic",
        "neas",
        "cusp",
        "lfl",
        "em1",
        "em2",
        "stab",
        "stb",
        "apm1",
        "agm1",
        "ch2l",
        "rsp",
        "icps",
        "ltst",
        "eus",
        "jnsa",
        "jnsb",
        "tm",
        "tmm",
        "cue3",
        "lunah-map",
        "rd1",
        "rp",
        "olin",
        "lnd1",
        *(f"tdr{n}" for n in (1, 3, 4, 5, 6, 7, 8, 9)),
        *(f"td{n}" for n in (10, 11, 12, 13)),
        *(f"go{n}" for n in range(10, 18)),
        *(f"no{n}" for n in range(15, 19)),
        *(f"clu{n}" for n in range(1, 5)),
        "thb",
        "thc",
        "em-1",
        "em-2",
        *(f"em-{n}" for n in range(4, 11)),
    }
)

# Unclassified/status-sensitive copy fails closed. Stable entries remain
# available without a rolling deadline; source-backed status entries below get
# a finite lease. A blank URL is therefore never permission to voice mutable
# status.
MISSION_REVIEWS = {
    code: (
        MissionReview(reviewed_on=MISSION_REVIEWED_ON, stable=True)
        if code in STABLE_MISSION_BLURBS
        else MissionReview()
    )
    for code in MISSIONS
}


def _source(codes: tuple[str, ...], url: str, *, fallback: str = "") -> None:
    for code in codes:
        stable = code in STABLE_MISSION_BLURBS
        MISSION_REVIEWS[code] = MissionReview(
            source_url=url,
            reviewed_on=MISSION_REVIEWED_ON,
            review_by=None if stable else MISSION_REVIEW_BY,
            expired_fallback=fallback,
            stable=stable,
        )


_source(("vgr1", "vgr2"), "https://science.nasa.gov/mission/voyager/")
_source(("mro",), "https://science.nasa.gov/mars/mars-relay-network/")
_source(
    ("mvn",),
    "https://www.nasa.gov/news-release/"
    "nasa-says-farewell-to-maven-mars-mission-hosts-media-call-today/",
)
_source(("m20",), "https://science.nasa.gov/mission/mars-2020-perseverance/")
_source(("msl",), "https://science.nasa.gov/mission/msl-curiosity/")
_source(("wind",), "https://science.nasa.gov/mission/wind/")
_source(("ltb",), "https://science.nasa.gov/mission/lunar-trailblazer/")
_source(("escb", "escg"), "https://science.nasa.gov/mission/escapade/")
_source(("terr",), "https://terra.nasa.gov/about/terra-instrument-payload")
_source(("plc",), "https://global.jaxa.jp/press/2025/09/20250918-2_e.html")
_source(
    ("ulys",), "https://www.esa.int/Science_Exploration/Space_Science/Ulysses_overview"
)
_source(
    ("cusp", "tm", "tmm", "cue3"),
    "https://www.nasa.gov/directorates/stmd/"
    "prizes-challenges-crowdsourcing-program/centennial-challenges/"
    "cube-quest-concludes-wins-lessons-learned-from-centennial-challenge",
)
_source(("olin",), "https://www.colorado.edu/aerospace/research/cu-boulder-cubesats")
_source(
    tuple(f"tdr{n}" for n in (1, 3, 4, 5, 6, 7, 8, 9))
    + tuple(f"td{n}" for n in (10, 11, 12, 13)),
    "https://nssdc.gsfc.nasa.gov/nmc/spacecraft/display.action?id=1983-026B",
)
_source(
    tuple(f"go{n}" for n in range(10, 18)),
    "https://goes-r.noaa.gov/mission/history.html",
)
_source(
    tuple(f"no{n}" for n in range(15, 19)),
    "https://www.nesdis.noaa.gov/news/"
    "legacy-orbit-noaa-decommissions-the-poes-satellite-constellation",
)
_source(
    tuple(f"clu{n}" for n in range(1, 5)),
    "https://www.esa.int/Science_Exploration/Space_Science/Cluster",
)
_source(("thb", "thc"), "https://science.nasa.gov/mission/themis-artemis/")
_source(
    ("em1", "em2", "em3") + tuple(f"em-{n}" for n in range(1, 11)),
    "https://www.nasa.gov/wp-content/uploads/2026/03/going-back-to-the-moon.pdf",
    fallback="An Artemis flight designation in NASA's lunar campaign",
)
_source(
    ("eus",),
    "https://oig.nasa.gov/audits/"
    "nasas-management-of-programs-and-projects-after-mission-termination-"
    "canceled-or-repurposed-artemis-campaign-systems/",
)


def mission_blurb(code: str, on: date | None = None) -> str:
    """Return only stable or currently sourced-and-reviewed narration."""
    key = code.lower()
    text = MISSIONS.get(key, "")
    review = MISSION_REVIEWS.get(key)
    if not text or review is None:
        return ""
    if review.stable:
        return text
    if not review.source_url or review.reviewed_on is None or review.review_by is None:
        return review.expired_fallback
    if (on or datetime.now(timezone.utc).date()) > review.review_by:
        return review.expired_fallback
    return text

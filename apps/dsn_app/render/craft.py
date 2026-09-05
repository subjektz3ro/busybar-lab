"""DSN render / craft."""

from __future__ import annotations

from apps.dsn_app import limits as _limits

# Spacecraft silhouettes, 11x6, drawn at x61..71 / y5..10 — the whole space
# between the end of the tether and the right edge, with the label rows above
# and below. These are PORTRAITS, not archetypes: each one is built around the
# single feature that makes the real machine recognisable, because at this
# size one true feature reads and a faithful outline does not.
#
# What survives on this panel is proportion and count. Juno's three enormous
# blades read; the shape of its bus does not. Lucy's arrays are circular and
# nothing else in the fleet has that. Parker leads with a shield. A rover has
# wheels on the ground and a mast, and looks nothing like anything in orbit.
#
# Craft that genuinely look alike share a sprite on purpose — Voyager 1 and 2
# ARE identical, as are STEREO A and B, GRAIL A and B, and the MarCO pair.
# Sharing where the truth is shared is accuracy; inventing differences that
# nobody can see on the panel is not.
#
# P = solar panel   B = bus/body   D = high-gain dish   S = shield/sunshade
# lowercase = the same ink at 45%, for booms and structure that should recede
SPRITES = {
    # --- the great dish-dominant probes -----------------------------------
    # Voyager: the 3.7 m dish IS the spacecraft, with the magnetometer boom
    # trailing 13 m behind it and an RTG slung underneath.
    "voyager": (
        ".DDD.......",
        "D...D......",
        "D.#.DBBbbbb",
        "D...D.P....",
        ".DDD.......",
        "...........",
    ),
    # New Horizons: the same big dish, but on a squat triangular bus with the
    # RTG cylinder sticking straight out one side.
    "newhorizons": (
        ".DDD.......",
        "D...D.BB...",
        "D.#.DBBBB..",
        "D...D.BB.PP",
        ".DDD.......",
        "...........",
    ),
    # Cassini: the dish sat on TOP of a tall stacked bus, not out front.
    "cassini": (
        ".DDDDD.....",
        "D.....D....",
        "..BBB......",
        "..BBB..bbbb",
        "..BBB......",
        "...........",
    ),
    # --- the array giants --------------------------------------------------
    # Juno: three nine-metre blades at 120 degrees. The most recognisable
    # outline in the fleet.
    "juno": (
        "PP.........",
        "..PP.......",
        "DBBBPPPPP..",
        "..PP.......",
        "PP.........",
        "...........",
    ),
    # Lucy: two circular arrays, seven metres across. Nothing else is round.
    "lucy": (
        ".PP.....PP.",
        "P..P...P..P",
        "P..PBBBP..P",
        "P..P...P..P",
        ".PP.....PP.",
        "...........",
    ),
    # Psyche: cross-shaped arrays, an X on each side of the bus.
    "psyche": (
        "P.P.....P.P",
        ".P.......P.",
        "..PBBBBBP..",
        ".P.......P.",
        "P.P.....P.P",
        "...........",
    ),
    # Europa Clipper: a small bus between the largest arrays NASA has flown
    # into deep space — a 30 m span.
    "clipper": (
        "PPPP...PPPP",
        "...........",
        ".DDBBBBB...",
        "...........",
        "PPPP...PPPP",
        "...........",
    ),
    # Rosetta, Dawn: two enormous straight wings, the whole craft a crossbar.
    "wings": (
        "...........",
        "PPPP...PPPP",
        "...BBB.....",
        "PPPP...PPPP",
        "...........",
        "...........",
    ),
    # --- shielded ----------------------------------------------------------
    # Parker Solar Probe: the shield leads and everything hides behind it.
    "parker": (
        "SS.........",
        "SS.P.......",
        "SSBBBB.....",
        "SS.P.......",
        "SS.........",
        "...........",
    ),
    # Solar Orbiter, MESSENGER, BepiColombo: a shield out front, arrays behind
    # it rather than tucked away.
    "sunshade": (
        "S..........",
        "S..P...PPP.",
        "SSBBBB.....",
        "S..P...PPP.",
        "S..........",
        "...........",
    ),
    # --- observatories -----------------------------------------------------
    # JWST: the segmented mirror standing over a stepped sunshield.
    "jwst": (
        ".BBB.......",
        "B...B......",
        ".BBB.......",
        "SSSSSSS....",
        ".SSSSS.....",
        "...........",
    ),
    # Hubble, Chandra, XMM: a tube with the aperture open at one end.
    "telescope": (
        ".BBBBBB....",
        "PB....B....",
        "PB....B....",
        "PB....B....",
        ".BBBBBB....",
        "...........",
    ),
    # Gaia: a wide conical sunshade skirt with the instrument above it.
    "skirt": (
        "...........",
        "...BBB.....",
        "..BBBBB....",
        "SSSSSSSSS..",
        "...........",
        "...........",
    ),
    # --- orbiters at other worlds -----------------------------------------
    # MRO, Odyssey, TGO, Mars Express: relay dish on one end, two wings.
    "marsorbiter": (
        ".DDD....PP.",
        "D...D..P...",
        "D.#.DBB....",
        "D...D..P...",
        ".DDD....PP.",
        "...........",
    ),
    # LRO, Chandrayaan, Danuri: the dish hangs off one boom and the single
    # array off the other, with the bus slung between them.
    "lunarorbiter": (
        "...........",
        ".DD...BBB..",
        "D..DddBBBPP",
        ".DD...BBB..",
        "...........",
        "...........",
    ),
    # --- on a surface ------------------------------------------------------
    # Perseverance, Curiosity: a nuclear rover. Six wheels ON THE GROUND and
    # a mast with a camera head. Nothing in orbit looks remotely like this.
    "rover": (
        "...........",
        "...B.......",
        "..BBB......",
        "...B.......",
        "BBBBBBB....",
        "b.b.b.b....",
    ),
    # Spirit and Opportunity: smaller, and solar rather than nuclear — the
    # deck is one big panel.
    "solarrover": (
        "...........",
        "...B.......",
        "..BBB......",
        "PPPPPPP....",
        "..BBB......",
        ".b.b.b.....",
    ),
    # InSight, Phoenix, the lunar landers: a squat body on legs with two
    # circular arrays either side.
    "lander": (
        "...........",
        ".PP.....PP.",
        "P..P.B.P..P",
        ".PP.BBB.PP.",
        "....BBB....",
        "...b...b...",
    ),
    # --- spinners and boxes ------------------------------------------------
    # ACE, Wind, IMAP, Ulysses: spin-stabilised drums, panels wrapped round
    # the outside rather than on wings.
    "spinner": (
        "..BBBBB....",
        ".B.....B...",
        "bB.....Bb..",
        ".B.....B...",
        "..BBBBB....",
        "...........",
    ),
    # SOHO, DSCOVR, TESS and the general case: a box bus with two wings. This
    # is the fallback, and it is what most of the fleet honestly looks like.
    "boxwing": (
        "...........",
        "PP..BBB..PP",
        "PP..B.B..PP",
        "PP..BBB..PP",
        "...........",
        "...........",
    ),
    # STEREO: a compact box with one wing and a boom, flying in pairs.
    "stereo": (
        "...........",
        "...BBB..PP.",
        "bbBBBBBP...",
        "...BBB..PP.",
        "...........",
        "...........",
    ),
    # --- small craft -------------------------------------------------------
    # MarCO, LICIACube and the rest of the cubesats. The point is SIZE: this
    # one reads as tiny beside everything else, which is the truth.
    "cubesat": (
        "...........",
        "...........",
        "PP.BB.PP...",
        "...BB......",
        "...........",
        "...........",
    ),
    # OSIRIS-REx, Hayabusa2, Hera: a box with wings and a sampling arm
    # reaching down off the front.
    "sampler": (
        "...........",
        "PPP.BBB.PPP",
        "...BBBBB...",
        "...B.......",
        "..BB.......",
        "...........",
    ),
    # Orion: the capsule cone with the service module's four arrays behind.
    "orion": (
        "......P..P.",
        ".BB....PP..",
        "BBBBBBB....",
        ".BB....PP..",
        "......P..P.",
        "...........",
    ),
    # ICPS, EUS: not spacecraft at all, but the network tracks them. A rocket
    # stage is a plain cylinder with a nozzle, and should not pretend
    # otherwise.
    "stage": (
        "...........",
        "..bBBBBBB..",
        ".bBBBBBBBb.",
        "..bBBBBBB..",
        "...........",
        "...........",
    ),
}

# Which craft is which. Codes are the DSN feed's own, lowercased.
CRAFT_SHAPES = {
    # identical twins share, because they ARE identical
    "vgr1": "voyager",
    "vgr2": "voyager",
    "nhpc": "newhorizons",
    "cas": "cassini",
    "jno": "juno",
    "lucy": "lucy",
    "psyc": "psyche",
    "eurc": "clipper",
    "rose": "wings",
    "dawn": "wings",
    "juice": "wings",
    "spp": "parker",
    "bepi": "sunshade",
    "msgr": "sunshade",
    "jwst": "jwst",
    "hst": "telescope",
    "chdr": "telescope",
    "xmm": "telescope",
    "intg": "telescope",
    "stf": "telescope",
    "kepl": "telescope",
    "gaia": "skirt",
    "mro": "marsorbiter",
    "mros": "marsorbiter",
    "m01o": "marsorbiter",
    "m01s": "marsorbiter",
    "mvn": "marsorbiter",
    "tgo": "marsorbiter",
    "mex": "marsorbiter",
    "emm": "marsorbiter",
    "mom": "marsorbiter",
    "mgs": "marsorbiter",
    "vex": "marsorbiter",
    "plc": "marsorbiter",
    "escb": "marsorbiter",
    "escg": "marsorbiter",
    "lro": "lunarorbiter",
    "ch1": "lunarorbiter",
    "ch2": "lunarorbiter",
    "ch2o": "lunarorbiter",
    "kplo": "lunarorbiter",
    "sele": "lunarorbiter",
    "lade": "lunarorbiter",
    "grla": "lunarorbiter",
    "grlb": "lunarorbiter",
    "lcro": "lunarorbiter",
    "ltb": "lunarorbiter",
    # Rosalind Franklin is a ROVER. It was drawn as a Mars orbiter,
    # which is the exact error this whole set exists to correct.
    "m20": "rover",
    "msl": "rover",
    "rsp": "rover",
    # VIPER and the MERs are solar, not nuclear
    "rp": "solarrover",
    "mer1": "solarrover",
    "mer2": "solarrover",
    "nsyt": "lander",
    "phx": "lander",
    "ch3": "lander",
    "ch2l": "lander",
    "slim": "lander",
    "spil": "lander",
    "apm1": "lander",
    "agm1": "lander",
    "omot": "lander",
    "lnd1": "lander",
    "ace": "spinner",
    "wind": "spinner",
    "imap": "spinner",
    "ulys": "spinner",
    "gtl": "spinner",
    "polr": "spinner",
    "imag": "spinner",
    "ice": "spinner",
    "sta": "stereo",
    "stab": "stereo",
    "stb": "stereo",
    "mcoa": "cubesat",
    "mcob": "cubesat",
    "lici": "cubesat",
    "argo": "cubesat",
    "bios": "cubesat",
    "equl": "cubesat",
    "hmap": "cubesat",
    "cusp": "cubesat",
    "tm": "cubesat",
    "tmm": "cubesat",
    "cue3": "cubesat",
    "caps": "cubesat",
    "neas": "cubesat",
    "mlic": "cubesat",  # Lunar IceCube is a 6U cubesat
    "lfl": "cubesat",
    "olin": "cubesat",
    "jnsa": "cubesat",
    "jnsb": "cubesat",
    "orx": "sampler",
    "hyb2": "sampler",
    "musc": "sampler",
    "hera": "sampler",
    "dart": "sampler",
    "dif": "sampler",
    "sdu": "sampler",
    "em1": "orion",
    "em2": "orion",
    "em3": "orion",
    "icps": "stage",
    "ltst": "stage",
    "eus": "stage",
}

DEFAULT_SHAPE = "boxwing"

CRAFT_SPRITE = SPRITES[DEFAULT_SHAPE]  # kept: some tests and tools import it

CRAFT_X, CRAFT_Y = 61, 5  # the box, hard against the right edge

CRAFT_W, CRAFT_H = 11, 6

PANEL = (90, 150, 255)

BUS = (225, 228, 238)

DISH = (255, 236, 190)

SHIELD = (255, 190, 120)

HOT = (255, 255, 255)


def _dim(c):
    return tuple(int(v * 0.45) for v in c)


INK = {
    "P": PANEL,
    "B": BUS,
    "D": DISH,
    "S": SHIELD,
    "#": HOT,
    "p": _dim(PANEL),
    "b": _dim(BUS),
    "d": _dim(DISH),
    "s": _dim(SHIELD),
}

# Some of these craft genuinely move, and eleven pixels can show exactly one
# kind of motion: a specular highlight walking a path. That is enough, because
# for the craft listed here the highlight IS the rotation rather than a
# decoration on top of it.
#
# Juno is spin-stabilised at 2 rpm and ACE, Wind, IMAP and Ulysses are drums
# that spin for the same reason, so sunlight really does sweep their arrays.
# A spent upper stage tumbles, end over end, which is why it gets one too.
# Craft that hold a controlled attitude - JWST, Parker, Lucy, Europa Clipper,
# the rovers - get no glint, because inventing motion they do not have is the
# same error as inventing a silhouette they do not have.
CRAFT_GLINT = {
    # Juno turns at 2 rpm - one revolution every 30 s - so across an
    # 8 s loop the glint should advance about a quarter turn, not three
    # whole blades. Repeating each tip makes the sweep read at the
    # right rate without a longer loop.
    "juno": ((2, 8), (2, 8), (2, 8), (0, 0), (0, 0), (0, 0), (4, 0), (4, 0), (4, 0)),
    "spinner": ((0, 3), (2, 8), (4, 3), (2, 1)),  # round the drum
    "stage": ((1, 3), (3, 8)),  # tumbling
}


def craft_shape(code: str) -> str:
    return CRAFT_SHAPES.get(code.lower(), DEFAULT_SHAPE)


def craft_sprite(code: str) -> tuple[str, ...]:
    """The portrait for a craft, falling back to a plain box-and-wings sat."""
    return SPRITES[craft_shape(code)]


def _craft(px, x0: int, y0: int, code: str = "", phase: float = 0.0) -> None:
    """The spacecraft, drawn as itself.

    Placed by its top-left corner rather than its centre: the box is fixed
    against the right edge of the panel and the sprites are not all the same
    width, so centring them would make the fleet jitter as the scene changed.
    """
    for row, line in enumerate(craft_sprite(code)):
        for col, ch in enumerate(line):
            if ch == ".":
                continue
            x, y = x0 + col, y0 + row
            if 0 <= x < _limits.W and 0 <= y < _limits.H:
                px[x, y] = INK[ch]
    path = CRAFT_GLINT.get(craft_shape(code))
    if path:
        row, col = path[int(phase * len(path)) % len(path)]
        x, y = x0 + col, y0 + row
        if 0 <= x < _limits.W and 0 <= y < _limits.H:
            px[x, y] = HOT

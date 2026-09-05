"""DSN render / palette."""

from __future__ import annotations

# The physical panel spaces its LEDs about a pixel apart, so a filled
# background does not read as a surface — it reads as a haze of separated
# dots and it drowns everything drawn on top. Black is genuinely OFF, and
# the gaps merge with it, so bright sparse shapes on black are what read.
# Every colour here is high-contrast against black; nothing is a gradient.
OFF = (0, 0, 0)

OCEAN = (24, 74, 190)  # open water

SHELF = (48, 126, 235)  # nearer a coast: shallower, lighter

LAND = (56, 165, 82)  # vegetated

DESERT = (198, 156, 74)  # Sahara, Arabia, the Australian interior

ICE = (222, 234, 246)  # Antarctica, Greenland, the Arctic

DAYSIDE = (120, 170, 255)

CRAFT = (235, 238, 245)

PULSE = (255, 160, 60)  # historic/default X pulse used by distance art

UNKNOWN_PULSE = (100, 40, 255)  # honest unknown: distinct violet, never fake X

# S, X, K, Ka. Not decoration: hue reports only the source-published band,
# never a ranking or cause of live throughput. Distance, spacecraft power,
# antenna, ground aperture, coding and atmosphere all matter too. The palette
# follows centre frequency only — S lowest and reddest, Ka highest and nearly
# white — and remains warm so none can be mistaken for the cold blue uplink.
# Each pair differs by at least 77/255 in one channel: the physical panel's
# measured 30% visibility floor. Warm S/X remain radio-like; K and Ka step
# through mint and white, while unknown is the violet fallback above.
BAND_PULSE = {
    "S": (255, 40, 10),
    "X": PULSE,
    "K": (180, 255, 80),
    "KA": (255, 255, 170),
}

NAME = (215, 225, 240)

DIST = (224, 160, 70)

DISH_NO = (110, 145, 190)

RATE = (150, 190, 120)

GLOBE_CX, GLOBE_CY, GLOBE_R = 7, 8, 6

TRACK_Y = 8

# Meaning-bearing lines must clear the measured 30% physical-panel step from
# OFF.  The old (58, 40, 14) line vanished through the LED gaps even though it
# looked present in a solid-pixel preview.
TETHER = (78, 35, 10)

UPLINK = (120, 190, 255)  # Earth talking. Cold and bright against the amber.

UP_TETHER = (26, 48, 78)

UP_Y = TRACK_Y - 2  # Earth's half of the conversation, above

# --- live instrument -------------------------------------------------------
SCOPE_CX, SCOPE_CY, SCOPE_R = 7, 7, 6

INSTRUMENT_X0, INSTRUMENT_X1 = 16, 60

INSTRUMENT_CONTENT_X1 = 69

FRESH_GUTTER_X = 70

FRESH_X = 71

INSTRUMENT_METRIC_MIN_FRAMES = 8  # 1.6s/page at 5fps; round by RF loop

SCOPE_RING = (34, 66, 82)

SCOPE_TRAIL = (105, 170, 190)

SCOPE_HEAD = (255, 232, 150)

INSTRUMENT_TETHER = (46, 74, 96)

FRESH = (70, 220, 235)

DELAYED = (255, 174, 50)

STALE = (245, 65, 55)

# --- Three Skies network --------------------------------------------------
# Each complex owns a literal local alt-az sky.  These are three independent
# coordinate frames, not three pieces of one inertial spacecraft map.
THREE_SKIES_SCOPE_CENTERS = ((15, 7), (39, 7), (63, 7))

THREE_SKIES_SCOPE_R = 6

THREE_SKIES_NORTH = (145, 145, 145)

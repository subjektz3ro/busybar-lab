"""The panel's physical limits, in one place, as numbers a check can use.

These are measurements of the BUSY Bar's hardware, not preferences. They were
learned by shipping something that looked right in a PNG and was unreadable on
the panel, and until now they lived only as prose — stated in `AGENTS.md`,
twice in the `busybar-app` skill, and copied into whatever test happened to
need them. Prose does not fail a build. A status clock sat below the contrast
floor for months and was found by a person looking at the bar, not by the tool
built to audit exactly that.

So: the number lives here, the analyzer enforces it, and the skill references
it rather than restating it. Anything that restates it can drift, and the
skill's own line-count and siren timings had already drifted by the time
somebody checked.

Sources are cited per constant. Change one only with new hardware evidence,
and say what the evidence was.
"""

from __future__ import annotations

# --- contrast ---------------------------------------------------------------
#
# "Brightness deltas under ~30% are invisible. Verified on hardware. Two
# colors that differ by 0x10 in one channel (6%) look identical, no matter how
# distinct the PNG looks."  — busybar-app skill, law 5
#
# Expressed against the 0-255 luminance range the analyzer computes, so a
# check can compare directly without rescaling.
MIN_CONTRAST_FRACTION = 0.30
MIN_CONTRAST_DELTA = MIN_CONTRAST_FRACTION * 255  # 76.5

# The same 30% applies per channel when two colours encode different meanings
# and hue is doing the work rather than brightness.
MIN_CHANNEL_SEPARATION_FRACTION = 0.30


# --- geometry ---------------------------------------------------------------
#
# Vendor dimensions: 1.23 x 1.2 mm LEDs on a 2.2 mm pitch.
# https://docs.busy.app/bar/tech-specs
# so the dark gap is 0.97 mm, 79% as wide as the lit part.
LED_SIZE_MM = 1.23
LED_PITCH_MM = 2.2
LED_GAP_MM = LED_PITCH_MM - LED_SIZE_MM

# "Single-pixel details vanish. A one-pixel-wide feature is an isolated dot,
# not a line. Shapes need 2-3px of body to read as shapes."  — law 5
MIN_FEATURE_PIXELS = 2


# --- luminance --------------------------------------------------------------
#
# Rec. 601 weights. Defined here so every check, every test and every claim
# derives its numbers the same way; four hand-written copies of this during one
# review produced one wrong answer, because a colour-keyed variant swallowed a
# scene pixel that happened to match the ink exactly.
LUMINANCE_WEIGHTS = (0.30, 0.59, 0.11)


def luminance(pixel: tuple[int, int, int]) -> float:
    """Perceived brightness of one RGB pixel on the 0-255 scale."""
    red, green, blue = pixel
    weight_r, weight_g, weight_b = LUMINANCE_WEIGHTS
    return weight_r * red + weight_g * green + weight_b * blue


def contrast_delta(ink: tuple[int, int, int],
                   background: tuple[int, int, int]) -> float:
    """Luminance distance between two colours, as the panel resolves it."""
    return abs(luminance(ink) - luminance(background))


def reads_against(ink: tuple[int, int, int],
                  background: tuple[int, int, int],
                  *, minimum: float = MIN_CONTRAST_DELTA) -> bool:
    """Whether `ink` is distinguishable from `background` on this panel."""
    return contrast_delta(ink, background) >= minimum


def channel_separation(first: tuple[int, int, int],
                       second: tuple[int, int, int]) -> float:
    """The largest per-channel difference, as a fraction of full scale.

    The skill's escape hatch from the brightness floor: two colours may encode
    different meanings either by a >=30% luminance delta OR by changing hue.
    This measures the second.
    """
    return max(abs(a - b) for a, b in zip(first, second)) / 255.0

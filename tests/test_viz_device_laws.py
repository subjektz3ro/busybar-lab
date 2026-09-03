"""The panel's physics are executable checks rather than prose alone.

Thirteen check kinds existed and every one verified structure or time —
dimensions, duration, loop seams, region stability. None verified whether a
person could read the result. The 30% contrast floor was stated in AGENTS.md,
twice in the busybar-app skill, and copied into whatever test needed it, and
enforced nowhere. A status clock sat at a delta of 9 against a bright sky for
months and was found by somebody looking at the bar.
"""

from __future__ import annotations

import pytest
from PIL import Image

from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.device_laws import (
    MIN_CONTRAST_DELTA,
    channel_separation,
    contrast_delta,
    luminance,
    reads_against,
)
from busybar_viz.models import (
    CheckSpec,
    CheckStatus,
    Confidence,
    DisplayTrack,
    EvidenceLevel,
    RegionSpec,
    RenderedSegment,
)

WHITE = (255, 255, 255)
AMBER = (224, 160, 70)


# --- the laws themselves ----------------------------------------------------


def test_the_floor_is_thirty_percent_of_full_scale():
    """Stated on hardware: deltas under ~30% are invisible however distinct
    the PNG looks."""
    assert MIN_CONTRAST_DELTA == pytest.approx(0.30 * 255)


def test_the_measured_failure_this_came_from():
    """The amber clock against a bright day sky. 9, against a floor of 76."""
    bright_sky = (120, 170, 220)
    assert contrast_delta(AMBER, bright_sky) < 15
    assert not reads_against(AMBER, bright_sky)


def test_white_cleared_the_floor_on_average_and_still_did_not_read():
    """Why the fix ended up being an unlit outline rather than brighter ink.

    Against the MEAN daytime sky white is comfortably above the floor, which
    is what the first fix measured and why it looked sufficient. The pixels
    that decide legibility are the brightest ones actually touching a stroke —
    measured at 189-194 across the scenes — and there white falls under. A
    person looking at the panel reached the same verdict.
    """
    mean_day_sky = (120, 170, 220)
    assert reads_against(WHITE, mean_day_sky), "the mean looked fine"

    brightest_neighbour_measured = (185, 195, 200)
    assert not reads_against(WHITE, brightest_neighbour_measured)
    assert contrast_delta(WHITE, brightest_neighbour_measured) < 70


def test_an_unlit_neighbour_is_the_most_the_panel_can_do():
    assert contrast_delta(WHITE, (0, 0, 0)) == pytest.approx(255.0)
    assert reads_against(WHITE, (0, 0, 0))


def test_hue_is_the_documented_escape_from_the_brightness_floor():
    """The skill allows >=30% per channel OR a hue change."""
    assert channel_separation(WHITE, AMBER) >= 0.30


def test_luminance_has_exactly_one_definition():
    """Four hand-written copies during one review produced one wrong answer."""
    assert luminance((255, 255, 255)) == pytest.approx(255.0)
    assert luminance((0, 0, 0)) == 0.0


# --- region.contrast_floor --------------------------------------------------


def _segment(
    ink,
    background,
    *,
    size=(24, 8),
    outline=False,
    contrast_mode="luminance",
):
    frame = Image.new("RGB", size, background)
    px = frame.load()
    cells = {(4, 3), (5, 3), (6, 3), (5, 4), (5, 5)}
    if outline:
        for x, y in {(x + dx, y + dy) for (x, y) in cells
                     for dx in (-1, 0, 1) for dy in (-1, 0, 1)} - cells:
            if 0 <= x < size[0] and 0 <= y < size[1]:
                px[x, y] = (0, 0, 0)
    for x, y in cells:
        px[x, y] = ink
    return RenderedSegment(
        displays=(DisplayTrack("front", (frame,), 5, Confidence.SOURCE_EXACT),),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=(RegionSpec("label", rect=(0, 0, size[0] - 1, size[1] - 1)),),
        checks=(CheckSpec.create("contrast", "region.contrast_floor",
                                 region="label", ink=[list(ink)],
                                 contrast_mode=contrast_mode),),
    )


def _status(segment):
    return {r.id: r for r in analyze(segment)}["contrast"]


def test_illegible_ink_fails():
    result = _status(_segment(AMBER, (120, 170, 220)))
    assert result.status is CheckStatus.FAIL
    assert result.observed["worst_delta"] < MIN_CONTRAST_DELTA


def test_channel_separation_is_an_explicit_opt_in():
    background = (120, 170, 220)
    result = _status(_segment(
        AMBER,
        background,
        contrast_mode="luminance_or_channel",
    ))
    assert result.status is CheckStatus.PASS
    assert result.observed["worst_luminance_delta"] < MIN_CONTRAST_DELTA
    assert result.observed["boundaries_passing_by_channel"] > 0
    assert result.expected["contrast_mode"] == "luminance_or_channel"


def test_multiple_inks_compare_each_actual_stroke_to_its_own_neighbour():
    frame = Image.new("RGB", (24, 8), (0, 0, 0))
    px = frame.load()
    for x in range(12, 24):
        for y in range(8):
            px[x, y] = WHITE
    for point in ((3, 3), (4, 3), (5, 3)):
        px[point] = WHITE
    dark_ink = (10, 10, 10)
    for point in ((16, 3), (17, 3), (18, 3)):
        px[point] = dark_ink
    segment = RenderedSegment(
        displays=(DisplayTrack(
            "front", (frame,), 5, Confidence.SOURCE_EXACT,
        ),),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=(RegionSpec("label", rect=(0, 0, 24, 8)),),
        checks=(CheckSpec.create(
            "contrast",
            "region.contrast_floor",
            region="label",
            ink=[list(WHITE), list(dark_ink)],
        ),),
    )
    result = _status(segment)
    assert result.status is CheckStatus.PASS
    assert result.observed["worst_luminance_delta"] == pytest.approx(245.0)


def test_channel_mode_worst_metrics_describe_one_actual_boundary():
    frame = Image.new("RGB", (30, 10), (0, 0, 0))
    px = frame.load()
    sky = (120, 170, 220)
    gray = (175, 175, 175)
    for x in range(1, 12):
        for y in range(1, 9):
            px[x, y] = sky
    for x in range(16, 30):
        for y in range(1, 9):
            px[x, y] = gray
    for point in ((4, 4), (5, 4), (6, 4)):
        px[point] = AMBER
    for point in ((20, 4), (21, 4), (22, 4)):
        px[point] = WHITE
    segment = RenderedSegment(
        displays=(DisplayTrack(
            "front", (frame,), 5, Confidence.SOURCE_EXACT,
        ),),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=(RegionSpec("label", rect=(0, 0, 30, 10)),),
        checks=(CheckSpec.create(
            "contrast",
            "region.contrast_floor",
            region="label",
            ink=[list(AMBER), list(WHITE)],
            contrast_mode="luminance_or_channel",
        ),),
    )
    result = _status(segment)
    assert result.status is CheckStatus.PASS
    assert result.observed["minimum_luminance_delta"] < 15
    assert result.observed["worst_luminance_delta"] == pytest.approx(80.0)
    assert result.observed["worst_channel_separation"] == pytest.approx(
        80 / 255, abs=0.0001,
    )
    assert result.observed["worst_ink"] == [255, 255, 255]
    assert result.observed["worst_background"] == [175, 175, 175]


def test_invalid_contrast_mode_fails_closed():
    segment = _segment(WHITE, (0, 0, 0))
    check = CheckSpec.create(
        "contrast",
        "region.contrast_floor",
        region="label",
        ink=[list(WHITE)],
        contrast_mode="anything_goes",
    )
    segment = RenderedSegment(
        displays=segment.displays,
        evidence_level=segment.evidence_level,
        regions=segment.regions,
        checks=(check,),
    )
    assert _status(segment).status is CheckStatus.UNKNOWN


@pytest.mark.parametrize("minimum", [0, -1, float("inf"), float("nan"), "bad"])
def test_invalid_luminance_floor_fails_closed(minimum):
    segment = _segment(WHITE, (0, 0, 0))
    check = CheckSpec.create(
        "contrast",
        "region.contrast_floor",
        region="label",
        ink=[list(WHITE)],
        min_delta=minimum,
    )
    segment = RenderedSegment(
        displays=segment.displays,
        evidence_level=segment.evidence_level,
        regions=segment.regions,
        checks=(check,),
    )
    assert _status(segment).status is CheckStatus.UNKNOWN


def test_an_unlit_outline_passes():
    result = _status(_segment(AMBER, (120, 170, 220), outline=True))
    assert result.status is CheckStatus.PASS
    assert result.observed["worst_delta"] == pytest.approx(169.3, abs=0.5)


def test_the_worst_neighbour_decides_not_the_mean():
    """A region mean is dominated by pixels the ink never touches. It reported
    a comfortable 115 for a clock whose worst neighbour was 9."""
    frame = Image.new("RGB", (24, 8), (0, 0, 0))
    px = frame.load()
    cells = {(4, 3), (5, 3), (6, 3)}
    for x, y in cells:
        px[x, y] = WHITE
    px[4, 2] = (250, 250, 250)          # one near-white neighbour
    segment = RenderedSegment(
        displays=(DisplayTrack("front", (frame,), 5, Confidence.SOURCE_EXACT),),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=(RegionSpec("label", rect=(0, 0, 23, 7)),),
        checks=(CheckSpec.create("contrast", "region.contrast_floor",
                                 region="label", ink=[list(WHITE)]),),
    )
    result = _status(segment)
    assert result.status is CheckStatus.FAIL, "one bad neighbour must fail it"
    assert result.observed["mean_delta"] > 200, "the mean looked fine"


def test_hex_and_rgb_ink_are_both_accepted():
    for ink_param in ([[255, 255, 255]], ["#FFFFFF"], ["FFFFFF"]):
        frame = Image.new("RGB", (8, 8), (0, 0, 0))
        frame.load()[4, 4] = WHITE
        segment = RenderedSegment(
            displays=(DisplayTrack("front", (frame,), 5, Confidence.SOURCE_EXACT),),
            evidence_level=EvidenceLevel.RENDERER_VERIFIED,
            regions=(RegionSpec("label", rect=(0, 0, 7, 7)),),
            checks=(CheckSpec.create("contrast", "region.contrast_floor",
                                     region="label", ink=ink_param),),
        )
        assert _status(segment).status is CheckStatus.PASS, ink_param


def test_absent_ink_is_unknown_not_a_silent_pass():
    """A check that passes because it found nothing to measure is worse than
    no check."""
    frame = Image.new("RGB", (8, 8), (10, 10, 10))
    segment = RenderedSegment(
        displays=(DisplayTrack("front", (frame,), 5, Confidence.SOURCE_EXACT),),
        evidence_level=EvidenceLevel.RENDERER_VERIFIED,
        regions=(RegionSpec("label", rect=(0, 0, 7, 7)),),
        checks=(CheckSpec.create("contrast", "region.contrast_floor",
                                 region="label", ink=[[255, 255, 255]]),),
    )
    assert _status(segment).status is CheckStatus.UNKNOWN


# --- the registered scenario ------------------------------------------------


def _run(**parameters):
    from busybar_viz.adapters.skystrip import SkystripAdapter
    from busybar_viz.models import RenderRequest

    segment = SkystripAdapter().render(RenderRequest(
        scenario_id="skystrip/status-clock",
        parameters=parameters, inputs=()))
    return {r.id: r for r in analyze(segment)}, segment


@pytest.mark.parametrize("hour", [0, 6, 9, 12, 15, 18, 21])
def test_the_clock_reads_at_every_hour(hour):
    results, _ = _run(hour=float(hour))
    assert results["clock-contrast"].status is CheckStatus.PASS, (
        hour, dict(results["clock-contrast"].observed))


@pytest.mark.parametrize("cloud", [0.0, 0.5, 1.0])
def test_the_clock_reads_under_any_cloud(cloud):
    results, _ = _run(cloud_frac=cloud)
    assert results["clock-contrast"].status is CheckStatus.PASS


def test_the_time_machine_ink_reads_by_hue():
    """Amber sits under the LUMINANCE floor against a bright sky, but its
    blue-channel separation from the sky clears the panel's dual criterion
    — so under `luminance_or_channel` the check is required again and
    passes on merit rather than being waived."""
    results, _ = _run(scrubbed=True)
    contrast = results["clock-contrast"]
    assert contrast.severity == "error"
    assert contrast.status is CheckStatus.PASS
    assert required_checks_pass(tuple(results.values()))


def test_fault_injection_proves_the_check_rejects_what_actually_shipped():
    """The whole point. `legacy_amber` reproduces the behaviour that ran on the
    bar for months — amber lerped in as daylight rose, no outline — and the
    audit must reject it. A check nobody has watched fail is a check nobody
    should trust."""
    results, _ = _run(fault="legacy_amber")
    contrast = results["clock-contrast"]
    assert contrast.status is CheckStatus.FAIL
    assert contrast.observed["worst_delta"] < MIN_CONTRAST_DELTA
    assert not required_checks_pass(tuple(results.values()))


def test_the_glyphs_read_as_shapes_not_specks():
    """There is no plate any more — the corner is live scene with bare ink —
    so what min_feature_size guards now is the strokes themselves: the
    near-black day ink must remain visible to the lit-structure check
    (pure (0,0,0) was not), and the glyph bodies must not fragment into
    isolated pixels against the sky."""
    results, _ = _run()
    body = results["clock-has-body"]
    assert body.status is CheckStatus.PASS
    # Two isolated pixels remain by design: the colon is punctuation, whose
    # meaning is positional, and against an unlit field a lone lit pixel is
    # still at full contrast.
    assert body.observed["worst_isolated"] <= 2

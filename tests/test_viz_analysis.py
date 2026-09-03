"""App-neutral audit rules over named display tracks and logical signals."""

from __future__ import annotations

from PIL import Image

from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.models import (
    CheckSpec,
    CheckStatus,
    Confidence,
    DisplayTrack,
    InkReference,
    RegionSpec,
    RenderedSegment,
    SignalEvent,
)


def _solid(color, size=(72, 16)) -> Image.Image:
    return Image.new("RGB", size, color)


def _track(
    display_id="front",
    frames=(),
    *,
    fps=5,
    confidence=Confidence.SOURCE_EXACT,
    baselines=(),
) -> DisplayTrack:
    return DisplayTrack(display_id, tuple(frames), fps, confidence, tuple(baselines))


def test_dimension_checks_select_named_front_and_back_tracks():
    segment = RenderedSegment(
        displays=(
            _track("front", (_solid((0, 0, 0)),)),
            _track(
                "back",
                (_solid((0, 0, 0), (160, 80)),),
                confidence=Confidence.EMULATED_CONFORMANT,
            ),
        ),
        checks=(
            CheckSpec.create("front-size", "frame.dimensions", size=(72, 16)),
            CheckSpec.create(
                "back-size", "frame.dimensions", display="back", size=(160, 80),
            ),
        ),
    )

    front, back = analyze(segment)

    assert front.status is CheckStatus.PASS
    assert front.confidence is Confidence.SOURCE_EXACT
    assert front.observed["display"] == "front"
    assert back.status is CheckStatus.PASS
    assert back.confidence is Confidence.EMULATED_CONFORMANT
    assert back.observed["display"] == "back"


def test_missing_display_is_unknown_and_blocks_a_required_audit():
    segment = RenderedSegment(
        displays=(_track(frames=(_solid((0, 0, 0)),)),),
        checks=(CheckSpec.create(
            "back-size", "frame.dimensions", display="back", size=(160, 80),
        ),),
    )

    results = analyze(segment)

    assert results[0].status is CheckStatus.UNKNOWN
    assert results[0].confidence is Confidence.UNKNOWN
    assert not required_checks_pass(results)


def test_near_white_and_global_luminance_checks_report_exact_bad_frames():
    black = _solid((0, 0, 0))
    white = _solid((255, 255, 255))
    segment = RenderedSegment(
        displays=(_track(frames=(black, white)),),
        checks=(
            CheckSpec.create(
                "white", "frame.near_white_fraction",
                channel_min=245, max_fraction=0.10,
            ),
            CheckSpec.create(
                "jump", "frame.global_luminance_jump", max_mean_jump=80,
            ),
        ),
    )

    white_result, jump_result = analyze(segment)

    assert white_result.status is CheckStatus.FAIL
    assert white_result.frame_indices == (1,)
    assert white_result.observed["peak_fraction"] == 1.0
    assert jump_result.status is CheckStatus.FAIL
    assert jump_result.frame_indices == (1,)
    assert jump_result.observed["peak_mean_jump"] == 255.0


def test_region_rules_compare_final_pixels_to_the_declared_baseline():
    baseline = _solid((0, 0, 0))
    effect = baseline.copy()
    effect.putpixel((0, 0), (200, 100, 10))
    segment = RenderedSegment(
        displays=(_track(frames=(baseline, effect), baselines=(baseline, baseline)),),
        regions=(
            RegionSpec("static", rect=(1, 0, 3, 1)),
            RegionSpec("motion", points=((0, 0),)),
        ),
        checks=(
            CheckSpec.create(
                "static-stays", "region.foreground_preserved", region="static",
            ),
            CheckSpec.create(
                "motion-happens", "region.motion_required", region="motion",
                min_changed_fraction=1.0,
            ),
        ),
    )

    preserved, motion = analyze(segment)

    assert preserved.status is CheckStatus.PASS
    assert preserved.observed["changed_samples"] == 0
    assert motion.status is CheckStatus.PASS
    assert motion.observed["peak_changed_fraction"] == 1.0


def test_region_with_the_wrong_display_or_missing_baseline_is_unknown():
    segment = RenderedSegment(
        displays=(_track(frames=(_solid((0, 0, 0)),)),),
        regions=(RegionSpec("back-only", display="back", rect=(0, 0, 1, 1)),),
        checks=(CheckSpec.create(
            "preserved", "region.foreground_preserved", region="back-only",
        ),),
    )

    result = analyze(segment)[0]

    assert result.status is CheckStatus.UNKNOWN
    assert result.confidence is Confidence.UNKNOWN


def test_unique_frame_rule_is_display_scoped():
    black = _solid((0, 0, 0))
    front = _track(frames=(black, black.copy()))
    back = _track(
        "back",
        (_solid((0, 0, 0), (160, 80)), _solid((1, 2, 3), (160, 80))),
    )
    segment = RenderedSegment(
        displays=(front, back),
        checks=(
            CheckSpec.create("front-motion", "animation.unique_frames", minimum=2),
            CheckSpec.create(
                "back-motion", "animation.unique_frames", display="back", minimum=2,
            ),
        ),
    )

    front_result, back_result = analyze(segment)

    assert front_result.status is CheckStatus.FAIL
    assert back_result.status is CheckStatus.PASS


def test_duration_and_frame_window_rules_catch_a_held_animation_tail():
    moving = (_solid((0, 0, 0)), _solid((1, 1, 1)))
    held = _solid((2, 2, 2))
    segment = RenderedSegment(
        displays=(_track(frames=(*moving, held, held.copy()), fps=2),),
        checks=(
            CheckSpec.create(
                "lease-filled", "animation.duration", duration_us=2_000_000,
            ),
            CheckSpec.create(
                "tail-moves", "animation.max_static_run",
                start_frame=2, maximum=1,
            ),
        ),
    )

    duration, tail = analyze(segment)

    assert duration.status is CheckStatus.PASS
    assert duration.observed["duration_us"] == 2_000_000
    assert tail.status is CheckStatus.FAIL
    assert tail.observed == {
        "max_static_run": 2,
        "start_frame": 2,
        "end_frame": 4,
        "window_valid": True,
    }


def test_top_led_policy_is_logical_and_rejects_repeated_pulses():
    frame = _solid((0, 0, 0))
    segment = RenderedSegment(
        displays=(_track(frames=(frame,)),),
        signals=(
            SignalEvent(0, "top_led.pulse", "#FFFFFFFF"),
            SignalEvent(100_000, "top_led.pulse", "#FFFFFFFF"),
        ),
        checks=(CheckSpec.create(
            "led", "top_led.allowed_condition", present=True,
        ),),
    )

    result = analyze(segment)[0]

    assert result.status is CheckStatus.FAIL
    assert result.confidence is Confidence.LOGICAL_ONLY
    assert result.observed["pulse_count"] == 2


def test_generic_frame_metrics_record_density_without_making_a_fidelity_claim():
    frame = _solid((0, 0, 0))
    frame.putpixel((0, 0), (100, 100, 100))
    segment = RenderedSegment(
        displays=(_track(frames=(frame,)),),
        checks=(CheckSpec.create(
            "metrics", "frame.summary_metrics", severity="info",
        ),),
    )

    result = analyze(segment)[0]

    assert result.status is CheckStatus.PASS
    assert result.confidence is Confidence.SOURCE_EXACT
    assert result.observed["frames"][0]["lit_pixels"] == 1
    assert result.observed["frames"][0]["lit_fraction"] == 1 / (72 * 16)
    assert "hardware" not in result.message.lower()


def test_loop_endpoint_metric_can_be_informational_or_budgeted():
    black = _solid((0, 0, 0))
    gray = _solid((30, 30, 30))
    segment = RenderedSegment(
        displays=(_track(frames=(black, gray)),),
        checks=(
            CheckSpec.create(
                "informational", "animation.loop_seam", severity="info",
            ),
            CheckSpec.create(
                "bounded", "animation.loop_seam", max_mean_delta=20,
            ),
        ),
    )

    informational, bounded = analyze(segment)

    assert informational.status is CheckStatus.PASS
    assert informational.observed["mean_channel_delta"] == 30.0
    assert bounded.status is CheckStatus.FAIL
    assert bounded.frame_indices == (1,)


def test_full_ink_rule_compares_independent_samples_to_every_final_frame():
    complete = _solid((0, 0, 0))
    complete.putpixel((2, 3), (240, 200, 40))
    clipped = complete.copy()
    clipped.putpixel((2, 3), (0, 0, 0))
    segment = RenderedSegment(
        displays=(_track(frames=(complete, clipped)),),
        ink_references=(InkReference(
            "status-label",
            "STATUS",
            "front",
            ((2, 3, (240, 200, 40)),),
        ),),
        checks=(CheckSpec.create(
            "status-complete", "text.full_ink_preserved",
            reference="status-label",
        ),),
    )

    result = analyze(segment)[0]

    assert result.status is CheckStatus.FAIL
    assert result.frame_indices == (1,)
    assert result.observed["missing_by_frame"] == {0: 0, 1: 1}
    assert result.observed["outside_sample_count"] == 0


def test_full_ink_rule_fails_fit_for_outside_or_invalid_frame_samples():
    frame = _solid((0, 0, 0))
    reference = InkReference(
        "label",
        "A VERY LONG LABEL",
        "front",
        ((72, 0, (255, 255, 255)),),
        frame_indices=(3,),
    )
    segment = RenderedSegment(
        displays=(_track(frames=(frame,)),),
        ink_references=(reference,),
        checks=(CheckSpec.create(
            "label-fits", "text.full_ink_preserved", reference="label",
        ),),
    )

    result = analyze(segment)[0]

    assert result.status is CheckStatus.FAIL
    assert result.observed["outside_sample_count"] == 1
    assert result.observed["invalid_frame_indices"] == [3]


def test_full_ink_rule_fails_closed_without_real_independent_samples():
    frame = _solid((0, 0, 0))
    for references in (
        (),
        (InkReference("empty", "EMPTY", "front", ()),),
    ):
        segment = RenderedSegment(
            displays=(_track(frames=(frame,)),),
            ink_references=references,
            checks=(CheckSpec.create(
                "complete", "text.full_ink_preserved",
                reference="empty" if references else "missing",
            ),),
        )

        result = analyze(segment)[0]
        assert result.status is CheckStatus.UNKNOWN
        assert result.confidence is Confidence.UNKNOWN


def test_unknown_rules_fail_closed_but_warning_failures_do_not_block():
    segment = RenderedSegment(
        displays=(_track(frames=(_solid((0, 0, 0)),)),),
        checks=(
            CheckSpec.create("unknown", "made.up.rule"),
            CheckSpec.create(
                "warning", "animation.unique_frames", severity="warning", minimum=2,
            ),
        ),
    )

    unknown, warning = analyze(segment)

    assert unknown.status is CheckStatus.UNKNOWN
    assert unknown.confidence is Confidence.UNKNOWN
    assert warning.status is CheckStatus.FAIL
    assert not required_checks_pass((unknown, warning))
    assert not required_checks_pass((warning,))

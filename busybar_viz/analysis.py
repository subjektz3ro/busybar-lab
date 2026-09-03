"""Deterministic checks over exact source frames and logical signal tracks."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
import math
from types import MappingProxyType
from typing import Any, cast

from PIL import Image

from .device_laws import (
    MIN_CHANNEL_SEPARATION_FRACTION,
    MIN_CONTRAST_DELTA,
    channel_separation,
    contrast_delta,
    luminance,
)
from .models import (
    CheckResult,
    CheckSpec,
    CheckStatus,
    Confidence,
    DisplayTrack,
    RenderedSegment,
)


class UnknownCheckError(ValueError):
    pass


RGBPixel = tuple[int, int, int]


def _rgb_pixels(frame: Image.Image) -> Sequence[RGBPixel]:
    """Narrow Pillow's mode-agnostic return after RGB track validation."""
    return cast(Sequence[RGBPixel], frame.get_flattened_data())


def _rgb_pixel(frame: Image.Image, point: tuple[int, int]) -> RGBPixel:
    """Narrow one Pillow pixel after RGB track validation."""
    return cast(RGBPixel, frame.getpixel(point))


def _luminance(pixel: RGBPixel) -> float:
    """Kept as the module-local name; the definition lives in device_laws so
    every check, test and claim derives its numbers from one place."""
    return luminance(pixel)


def _result(
    spec: CheckSpec,
    status: CheckStatus,
    message: str,
    *,
    observed: dict[str, Any] | None = None,
    expected: dict[str, Any] | None = None,
    frames: Iterable[int] = (),
    confidence: Confidence = Confidence.SOURCE_EXACT,
) -> CheckResult:
    return CheckResult(
        spec.id,
        spec.kind,
        status,
        spec.severity,
        confidence,
        message,
        MappingProxyType(observed or {}),
        MappingProxyType(expected or {}),
        tuple(frames),
    )


def _check_dimensions(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    expected = tuple(spec.parameters.get("size", (72, 16)))
    bad = [index for index, frame in enumerate(track.frames)
           if frame.mode != "RGB" or frame.size != expected]
    return _result(
        spec,
        CheckStatus.FAIL if bad else CheckStatus.PASS,
        "all frames use the expected RGB dimensions" if not bad
        else "one or more frames have the wrong mode or dimensions",
        observed={"display": track.id, "frame_count": len(track.frames),
                  "bad_frame_count": len(bad)},
        expected={"mode": "RGB", "size": list(expected)},
        frames=bad,
        confidence=track.confidence,
    )


def _check_near_white(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    threshold = int(spec.parameters.get("channel_min", 230))
    maximum = float(spec.parameters.get("max_fraction", 0.10))
    fractions: list[float] = []
    for frame in track.frames:
        pixels = _rgb_pixels(frame)
        fractions.append(sum(min(pixel) >= threshold for pixel in pixels) / len(pixels))
    peak = max(fractions, default=0.0)
    failures = tuple(index for index, value in enumerate(fractions) if value > maximum)
    return _result(
        spec,
        CheckStatus.FAIL if failures else CheckStatus.PASS,
        "near-white coverage stays within the scenario budget" if not failures
        else "near-white coverage exceeds the scenario budget",
        observed={"peak_fraction": peak, "fractions": fractions},
        expected={"max_fraction": maximum, "channel_min": threshold},
        frames=failures,
        confidence=track.confidence,
    )


def _check_luminance_jump(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    maximum = float(spec.parameters.get("max_mean_jump", 80.0))
    means = [sum(_luminance(pixel) for pixel in _rgb_pixels(frame))
             / (frame.width * frame.height) for frame in track.frames]
    jumps = [abs(right - left) for left, right in zip(means, means[1:])]
    peak = max(jumps, default=0.0)
    failures = tuple(index + 1 for index, jump in enumerate(jumps) if jump > maximum)
    return _result(
        spec,
        CheckStatus.FAIL if failures else CheckStatus.PASS,
        "global luminance changes stay bounded" if not failures
        else "a near-global luminance jump exceeds the budget",
        observed={"peak_mean_jump": peak, "frame_means": means},
        expected={"max_mean_jump": maximum},
        frames=failures,
        confidence=track.confidence,
    )


def _display_track(spec: CheckSpec, segment: RenderedSegment) -> DisplayTrack | None:
    return segment.display(str(spec.parameters.get("display", "front")))


def _missing_display(spec: CheckSpec) -> CheckResult:
    display_id = str(spec.parameters.get("display", "front"))
    return _result(
        spec,
        CheckStatus.UNKNOWN,
        f"display track {display_id!r} is unavailable",
        expected={"display": display_id},
        confidence=Confidence.UNKNOWN,
    )


def _region_coordinates(
    spec: CheckSpec,
    segment: RenderedSegment,
    track: DisplayTrack,
) -> tuple[tuple[int, int], ...]:
    region_id = str(spec.parameters.get("region"))
    region = segment.region(region_id)
    if region is None or region.display != track.id:
        return ()
    width, height = track.size
    return region.coordinates(width, height)


def _matching_baselines(track: DisplayTrack) -> tuple[Image.Image, ...] | None:
    if not track.baselines:
        return None
    if len(track.baselines) == 1:
        return track.baselines * len(track.frames)
    if len(track.baselines) != len(track.frames):
        return None
    return track.baselines


def _check_region_preserved(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    coords = _region_coordinates(spec, segment, track)
    baselines = _matching_baselines(track)
    if not coords or baselines is None:
        return _result(spec, CheckStatus.UNKNOWN,
                       "the required semantic region or baseline is unavailable",
                       confidence=Confidence.UNKNOWN)
    bad: list[int] = []
    changed = 0
    for index, (frame, baseline) in enumerate(zip(track.frames, baselines)):
        count = sum(frame.getpixel(point) != baseline.getpixel(point) for point in coords)
        changed += count
        if count:
            bad.append(index)
    return _result(
        spec,
        CheckStatus.FAIL if bad else CheckStatus.PASS,
        "the semantic region is unchanged" if not bad
        else "the semantic region changed during a backdrop-only effect",
        observed={"display": track.id, "changed_samples": changed,
                  "sample_count": len(coords) * len(track.frames)},
        expected={"changed_samples": 0, "region": spec.parameters.get("region")},
        frames=bad,
        confidence=track.confidence,
    )


def _parse_ink(value: object) -> tuple[tuple[int, int, int], ...]:
    """Accept `[[255,255,255]]` or `["#FFFFFF"]` for the colours that must read."""
    if isinstance(value, (str, bytes)):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return ()
    inks: list[tuple[int, int, int]] = []
    for entry in value:
        if isinstance(entry, str):
            text = entry.lstrip("#")
            if len(text) in (6, 8):
                try:
                    inks.append((int(text[0:2], 16), int(text[2:4], 16),
                                 int(text[4:6], 16)))
                except ValueError:
                    return ()
            else:
                return ()
        elif isinstance(entry, (list, tuple)) and len(entry) >= 3:
            try:
                red, green, blue = entry[:3]
                inks.append((int(red), int(green), int(blue)))
            except (TypeError, ValueError):
                return ()
        else:
            return ()
    return tuple(inks)


def _check_region_contrast(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    """Can a person actually read this region on the panel?

    The device's most-cited law and, until now, the only one with no check:
    brightness deltas under ~30% are invisible on the physical panel however
    distinct they look in a PNG. That gap is why a status clock sat at a delta
    of 9 against a bright sky for months and was found by somebody looking at
    the bar rather than by the tool built to audit exactly this.

    Measured against what sits IMMEDIATELY AROUND each stroke, not the mean of
    the whole region. The region mean is dominated by pixels the ink never
    touches, and it reported a comfortable 115 for a clock whose worst
    neighbour was 9. What decides legibility is the boundary.
    """
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    coords = _region_coordinates(spec, segment, track)
    inks = _parse_ink(spec.parameters.get("ink"))
    try:
        minimum = float(spec.parameters.get("min_delta", MIN_CONTRAST_DELTA))
    except (TypeError, ValueError):
        minimum = float("nan")
    if not math.isfinite(minimum) or minimum <= 0:
        return _result(
            spec,
            CheckStatus.UNKNOWN,
            "the luminance contrast floor must be finite and positive",
            expected={"min_delta": "> 0"},
            confidence=Confidence.UNKNOWN,
        )
    contrast_mode = spec.parameters.get("contrast_mode", "luminance")
    if contrast_mode not in {"luminance", "luminance_or_channel"}:
        return _result(
            spec,
            CheckStatus.UNKNOWN,
            "the contrast mode is invalid",
            expected={
                "contrast_mode": ["luminance", "luminance_or_channel"],
            },
            confidence=Confidence.UNKNOWN,
        )
    allow_channel = contrast_mode == "luminance_or_channel"
    if not coords or not inks:
        return _result(
            spec, CheckStatus.UNKNOWN,
            "the semantic region or its declared ink is unavailable",
            expected={"region": spec.parameters.get("region"),
                      "ink": spec.parameters.get("ink")},
            confidence=Confidence.UNKNOWN)

    ink_set = set(inks)
    region = set(coords)
    width, height = track.size
    minimum_delta: float | None = None
    minimum_channel: float | None = None
    worst_margin: float | None = None
    worst_pair_delta: float | None = None
    worst_pair_channel: float | None = None
    worst_frame = -1
    worst_point: tuple[int, int] | None = None
    worst_ink: RGBPixel | None = None
    worst_background: RGBPixel | None = None
    deltas: list[float] = []
    channel_separations: list[float] = []
    frames_with_ink = 0
    boundaries_passing_by_channel = 0
    bad: list[int] = []

    for index, frame in enumerate(track.frames):
        strokes: dict[tuple[int, int], RGBPixel] = {}
        for point in region:
            pixel = _rgb_pixel(frame, point)
            if pixel in ink_set:
                strokes[point] = pixel
        if not strokes:
            continue
        frames_with_ink += 1
        frame_failed = False
        for (x, y), actual_ink in strokes.items():
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    if dx == 0 and dy == 0:
                        continue
                    point = (x + dx, y + dy)
                    if not (0 <= point[0] < width and 0 <= point[1] < height):
                        continue
                    background = _rgb_pixel(frame, point)
                    # The same ink remains the same stroke even when this
                    # semantic rect cuts through it. It is not a background
                    # sample and must not manufacture a zero-delta failure.
                    if background == actual_ink:
                        continue
                    delta = contrast_delta(actual_ink, background)
                    separation = channel_separation(actual_ink, background)
                    deltas.append(delta)
                    channel_separations.append(separation)
                    if minimum_delta is None or delta < minimum_delta:
                        minimum_delta = delta
                    if minimum_channel is None or separation < minimum_channel:
                        minimum_channel = separation

                    luminance_margin = delta / minimum
                    channel_margin = (
                        separation / MIN_CHANNEL_SEPARATION_FRACTION
                        if allow_channel else 0.0
                    )
                    margin = max(luminance_margin, channel_margin)
                    if worst_margin is None or margin < worst_margin:
                        worst_margin = margin
                        worst_frame = index
                        worst_point = point
                        worst_ink = actual_ink
                        worst_background = background
                        worst_pair_delta = delta
                        worst_pair_channel = separation

                    luminance_passed = delta >= minimum
                    channel_passed = (
                        allow_channel
                        and separation >= MIN_CHANNEL_SEPARATION_FRACTION
                    )
                    if channel_passed and not luminance_passed:
                        boundaries_passing_by_channel += 1
                    if not luminance_passed and not channel_passed:
                        frame_failed = True
        if frame_failed:
            bad.append(index)

    if not frames_with_ink:
        return _result(
            spec, CheckStatus.UNKNOWN,
            "the declared ink never appears inside the region",
            expected={"region": spec.parameters.get("region"),
                      "ink": spec.parameters.get("ink")},
            confidence=Confidence.UNKNOWN)

    mean_delta = sum(deltas) / len(deltas) if deltas else 0.0
    mean_channel = (
        sum(channel_separations) / len(channel_separations)
        if channel_separations else 0.0
    )
    passed = worst_margin is not None and not bad
    return _result(
        spec,
        CheckStatus.PASS if passed else CheckStatus.FAIL,
        "every pixel bordering the ink clears the declared contrast mode"
        if passed else
        "the ink is indistinguishable from what it sits on; the panel cannot "
        "resolve this boundary under the declared contrast mode",
        observed={"display": track.id,
                  # Retain the original field for consumers while naming its
                  # exact meaning alongside the opt-in channel measurement.
                  "worst_delta": round(worst_pair_delta or 0.0, 1),
                  "worst_luminance_delta": round(worst_pair_delta or 0.0, 1),
                  "worst_channel_separation": round(
                      worst_pair_channel or 0.0, 4
                  ),
                  "worst_margin": round(worst_margin or 0.0, 4),
                  "worst_frame": worst_frame,
                  "worst_pixel": list(worst_point) if worst_point else None,
                  "worst_ink": list(worst_ink) if worst_ink else None,
                  "worst_background": (
                      list(worst_background) if worst_background else None
                  ),
                  "mean_delta": round(mean_delta, 1),
                  "mean_channel_separation": round(mean_channel, 4),
                  "minimum_luminance_delta": round(minimum_delta or 0.0, 1),
                  "minimum_channel_separation": round(
                      minimum_channel or 0.0, 4
                  ),
                  "boundaries_passing_by_channel": boundaries_passing_by_channel,
                  "frames_with_ink": frames_with_ink},
        expected={"region": spec.parameters.get("region"),
                  "contrast_mode": contrast_mode,
                  "min_delta": round(minimum, 1),
                  "min_channel_separation": (
                      MIN_CHANNEL_SEPARATION_FRACTION if allow_channel else None
                  )},
        frames=bad,
        confidence=track.confidence,
    )


def _check_min_feature_size(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    """A one-pixel-wide feature is an isolated dot, not a line.

    The LEDs sit 1.23 mm lit on a 2.2 mm pitch, so a lone lit pixel has no
    neighbour to form a stroke with and reads as a speck. Shapes need 2-3px of
    body. This flags lit pixels in the region with no lit orthogonal
    neighbour — deliberately orthogonal, because a diagonal pair reads as two
    specks rather than one line at this spacing.
    """
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    coords = _region_coordinates(spec, segment, track)
    if not coords:
        return _result(spec, CheckStatus.UNKNOWN,
                       "the semantic region is unavailable",
                       confidence=Confidence.UNKNOWN)
    allowed = int(spec.parameters.get("max_isolated", 0))
    region = set(coords)
    worst = 0
    bad: list[int] = []
    for index, frame in enumerate(track.frames):
        lit = {p for p in region if _luminance(_rgb_pixel(frame, p)) > 0.0}
        isolated = sum(
            1 for (x, y) in lit
            if not any((x + dx, y + dy) in lit
                       for dx, dy in ((-1, 0), (1, 0), (0, -1), (0, 1)))
        )
        worst = max(worst, isolated)
        if isolated > allowed:
            bad.append(index)
    return _result(
        spec,
        CheckStatus.FAIL if bad else CheckStatus.PASS,
        "every lit feature has body" if not bad
        else "isolated single pixels will read as specks, not shapes",
        observed={"display": track.id, "worst_isolated": worst},
        expected={"region": spec.parameters.get("region"),
                  "max_isolated": allowed},
        frames=bad,
        confidence=track.confidence,
    )


def _check_region_motion(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    coords = _region_coordinates(spec, segment, track)
    baselines = _matching_baselines(track)
    minimum = float(spec.parameters.get("min_changed_fraction", 0.5))
    if not coords or baselines is None:
        return _result(spec, CheckStatus.UNKNOWN,
                       "the required semantic region or baseline is unavailable",
                       confidence=Confidence.UNKNOWN)
    fractions = [sum(frame.getpixel(point) != baseline.getpixel(point)
                     for point in coords) / len(coords)
                 for frame, baseline in zip(track.frames, baselines)]
    peak = max(fractions, default=0.0)
    status = CheckStatus.PASS if peak >= minimum else CheckStatus.FAIL
    return _result(
        spec,
        status,
        "the nominated region contains meaningful motion" if status is CheckStatus.PASS
        else "the nominated region does not change enough to be visible",
        observed={"peak_changed_fraction": peak, "fractions": fractions},
        expected={"min_changed_fraction": minimum, "region": spec.parameters.get("region")},
        confidence=track.confidence,
    )


def _check_unique_frames(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    minimum = int(spec.parameters.get("minimum", 2))
    start = int(spec.parameters.get("start_frame", 0))
    end = int(spec.parameters.get("end_frame", len(track.frames)))
    valid_window = 0 <= start < end <= len(track.frames)
    frames = track.frames[start:end] if valid_window else ()
    hashes = {frame.tobytes() for frame in frames}
    status = (
        CheckStatus.PASS
        if valid_window and len(hashes) >= minimum
        else CheckStatus.FAIL
    )
    return _result(
        spec,
        status,
        "the segment contains the required visual states" if status is CheckStatus.PASS
        else "the segment is unexpectedly static",
        observed={
            "unique_frames": len(hashes),
            "start_frame": start,
            "end_frame": end,
            "window_valid": valid_window,
        },
        expected={"minimum": minimum, "valid_window": True},
        confidence=track.confidence,
    )


def _check_duration(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    expected_value = spec.parameters.get("duration_us")
    if isinstance(expected_value, bool) or not isinstance(expected_value, int):
        return _result(
            spec,
            CheckStatus.UNKNOWN,
            "animation duration requires an integer duration_us contract",
            confidence=Confidence.UNKNOWN,
        )
    actual = track.duration_us
    status = CheckStatus.PASS if actual == expected_value else CheckStatus.FAIL
    return _result(
        spec,
        status,
        "animation duration matches the declared lease" if status is CheckStatus.PASS
        else "animation duration does not fill the declared lease",
        observed={"duration_us": actual},
        expected={"duration_us": expected_value},
        confidence=track.confidence,
    )


def _check_max_static_run(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    start = int(spec.parameters.get("start_frame", 0))
    end = int(spec.parameters.get("end_frame", len(track.frames)))
    maximum = int(spec.parameters.get("maximum", 1))
    valid_window = 0 <= start < end <= len(track.frames) and maximum >= 1
    frames = track.frames[start:end] if valid_window else ()
    longest = 0
    current = 0
    previous: bytes | None = None
    for frame in frames:
        digest_input = frame.tobytes()
        current = current + 1 if digest_input == previous else 1
        longest = max(longest, current)
        previous = digest_input
    status = (
        CheckStatus.PASS
        if valid_window and longest <= maximum
        else CheckStatus.FAIL
    )
    return _result(
        spec,
        status,
        "animation does not hold a static frame too long"
        if status is CheckStatus.PASS
        else "animation contains an excessive held-frame run",
        observed={
            "max_static_run": longest,
            "start_frame": start,
            "end_frame": end,
            "window_valid": valid_window,
        },
        expected={"maximum": maximum, "valid_window": True},
        confidence=track.confidence,
    )


def _check_top_led(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    expected_present = bool(spec.parameters.get("present"))
    actual = [signal for signal in segment.signals if signal.kind == "top_led.pulse"]
    ok = bool(actual) is expected_present and len(actual) <= 1
    return _result(
        spec,
        CheckStatus.PASS if ok else CheckStatus.FAIL,
        "top-LED intent matches the scenario policy" if ok
        else "top-LED intent violates the scenario policy",
        observed={"pulse_count": len(actual),
                  "values": [signal.value for signal in actual]},
        expected={"present": expected_present, "max_count": 1},
        confidence=Confidence.LOGICAL_ONLY,
    )


def _check_frame_metrics(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    rows: list[dict[str, float | int]] = []
    for frame in track.frames:
        pixels = _rgb_pixels(frame)
        lit = sum(pixel != (0, 0, 0) for pixel in pixels)
        luminances = [_luminance(pixel) for pixel in pixels]
        rows.append({
            "lit_pixels": lit,
            "lit_fraction": lit / len(pixels),
            "mean_luminance": sum(luminances) / len(luminances),
            "peak_luminance": max(luminances, default=0.0),
        })
    return _result(
        spec,
        CheckStatus.PASS,
        "frame density and luminance metrics were recorded",
        observed={"display": track.id, "frames": rows},
        confidence=track.confidence,
    )


def _check_loop_seam(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    track = _display_track(spec, segment)
    if track is None:
        return _missing_display(spec)
    first, last = track.frames[0], track.frames[-1]
    deltas = [
        sum(abs(left[channel] - right[channel]) for channel in range(3)) / 3
        for left, right in zip(
            _rgb_pixels(first), _rgb_pixels(last),
        )
    ]
    mean_delta = sum(deltas) / len(deltas)
    changed_fraction = sum(delta > 0 for delta in deltas) / len(deltas)
    maximum = spec.parameters.get("max_mean_delta")
    passed = maximum is None or mean_delta <= float(maximum)
    return _result(
        spec,
        CheckStatus.PASS if passed else CheckStatus.FAIL,
        "loop endpoint delta was recorded" if passed
        else "loop endpoint delta exceeds the scenario budget",
        observed={
            "display": track.id,
            "mean_channel_delta": mean_delta,
            "changed_fraction": changed_fraction,
        },
        expected={} if maximum is None else {"max_mean_delta": float(maximum)},
        frames=() if passed else (len(track.frames) - 1,),
        confidence=track.confidence,
    )


def _check_full_ink(spec: CheckSpec, segment: RenderedSegment) -> CheckResult:
    reference_id = str(spec.parameters.get("reference"))
    reference = segment.ink_reference(reference_id)
    if reference is None:
        return _result(
            spec,
            CheckStatus.UNKNOWN,
            "the independent full-ink reference is unavailable",
            expected={"reference": reference_id},
            confidence=Confidence.UNKNOWN,
        )
    if not reference.pixels:
        return _result(
            spec,
            CheckStatus.UNKNOWN,
            "the independent reference contains no ink samples",
            expected={"reference": reference_id, "minimum_sample_count": 1},
            confidence=Confidence.UNKNOWN,
        )
    track = segment.display(reference.display)
    if track is None:
        return _missing_display(CheckSpec.create(
            spec.id,
            spec.kind,
            severity=spec.severity,
            display=reference.display,
        ))
    width, height = track.size
    outside = [
        (x, y) for x, y, _color in reference.pixels
        if not (0 <= x < width and 0 <= y < height)
    ]
    indices = reference.frame_indices or tuple(range(len(track.frames)))
    valid_indices = tuple(
        index for index in indices
        if isinstance(index, int) and not isinstance(index, bool)
        and 0 <= index < len(track.frames)
    )
    missing_by_frame: dict[int, int] = {}
    bad_frames: list[int] = []
    for index in valid_indices:
        frame = track.frames[index]
        missing = sum(
            _rgb_pixel(frame, (x, y)) != color
            for x, y, color in reference.pixels
            if 0 <= x < width and 0 <= y < height
        )
        missing_by_frame[index] = missing
        if missing:
            bad_frames.append(index)
    invalid_indices = [
        index for index in indices
        if isinstance(index, bool) or not isinstance(index, int)
        or not 0 <= index < len(track.frames)
    ]
    failed = bool(outside or bad_frames or invalid_indices)
    return _result(
        spec,
        CheckStatus.FAIL if failed else CheckStatus.PASS,
        "the final composition preserves every independently rendered ink sample"
        if not failed else
        "the label does not fit completely in the final composition",
        observed={
            "reference": reference.id,
            "label": reference.label,
            "sample_count": len(reference.pixels),
            "outside_sample_count": len(outside),
            "missing_by_frame": missing_by_frame,
            "invalid_frame_indices": invalid_indices,
        },
        expected={
            "outside_sample_count": 0,
            "missing_samples_per_frame": 0,
        },
        frames=bad_frames,
        confidence=track.confidence,
    )


_CHECKS = {
    "frame.dimensions": _check_dimensions,
    "frame.near_white_fraction": _check_near_white,
    "frame.global_luminance_jump": _check_luminance_jump,
    "region.foreground_preserved": _check_region_preserved,
    "region.motion_required": _check_region_motion,
    "animation.unique_frames": _check_unique_frames,
    "animation.duration": _check_duration,
    "animation.max_static_run": _check_max_static_run,
    "top_led.allowed_condition": _check_top_led,
    "frame.summary_metrics": _check_frame_metrics,
    "animation.loop_seam": _check_loop_seam,
    "text.full_ink_preserved": _check_full_ink,
    "region.contrast_floor": _check_region_contrast,
    "region.min_feature_size": _check_min_feature_size,
}


def analyze(segment: RenderedSegment) -> tuple[CheckResult, ...]:
    results: list[CheckResult] = []
    for spec in segment.checks:
        check = _CHECKS.get(spec.kind)
        if check is None:
            results.append(_result(
                spec,
                CheckStatus.UNKNOWN,
                f"unsupported audit rule: {spec.kind}",
                confidence=Confidence.UNKNOWN,
            ))
            continue
        results.append(check(spec, segment))
    return tuple(results)


def required_checks_pass(results: Sequence[CheckResult]) -> bool:
    required = tuple(result for result in results if result.severity == "error")
    return bool(required) and all(
        result.status is CheckStatus.PASS for result in required
    )

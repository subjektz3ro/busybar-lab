"""Ad-hoc frames get the same pipeline, checks, and honest provenance."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

import pytest
from PIL import Image

from busybar_viz import cli
from busybar_viz.analysis import analyze, required_checks_pass
from busybar_viz.models import Confidence
from busybar_viz.view import load_view_segment, parse_ink, parse_region

REPO = Path(__file__).resolve().parents[1]


def _frame(color=(10, 20, 30), size=(72, 16)) -> Image.Image:
    return Image.new("RGB", size, color)


def test_native_png_frames_load_as_one_exact_track(tmp_path):
    first = tmp_path / "frame-000.png"
    second = tmp_path / "frame-001.png"
    _frame((10, 20, 30)).save(first)
    _frame((40, 50, 60)).save(second)

    request, segment = load_view_segment(
        [first, second], repo_root=tmp_path, display_id="front",
    )

    track = segment.displays[0]
    assert (track.id, track.size, len(track.frames), track.fps) == (
        "front", (72, 16), 2, 5,
    )
    assert track.confidence is Confidence.SOURCE_EXACT
    assert request.scenario_id == "view/frame-000.png"
    assert request.parameters["paths"] == ["frame-000.png", "frame-001.png"]
    assert request.parameters["scale"] == 1
    assert request.parameters["frame_sha256"] == [
        hashlib.sha256(first.read_bytes()).hexdigest(),
        hashlib.sha256(second.read_bytes()).hexdigest(),
    ]
    assert required_checks_pass(analyze(segment))


def test_single_frame_defaults_to_fps_one(tmp_path):
    frame = tmp_path / "still.png"
    _frame().save(frame)

    _request, segment = load_view_segment(
        [frame], repo_root=tmp_path, display_id="front",
    )

    assert segment.displays[0].fps == 1


def test_directory_input_orders_frames_by_name(tmp_path):
    frames = tmp_path / "frames"
    frames.mkdir()
    _frame((2, 0, 0)).save(frames / "b.png")
    _frame((1, 0, 0)).save(frames / "a.png")

    _request, segment = load_view_segment(
        [frames], repo_root=tmp_path, display_id="front",
    )

    assert [frame.getpixel((0, 0)) for frame in segment.displays[0].frames] == [
        (1, 0, 0), (2, 0, 0),
    ]


def test_integer_enlarged_preview_is_detected_and_downsampled(tmp_path):
    native = _frame((10, 20, 30))
    native.putpixel((3, 2), (200, 100, 0))
    preview = tmp_path / "preview.png"
    native.resize((576, 128), Image.NEAREST).save(preview)

    request, segment = load_view_segment(
        [preview], repo_root=tmp_path, display_id="front",
    )

    track = segment.displays[0]
    assert track.size == (72, 16)
    assert track.frames[0].getpixel((3, 2)) == (200, 100, 0)
    assert track.confidence is Confidence.APPROXIMATE
    assert request.parameters["scale"] == 8
    assert any("downsampled" in note for note in segment.notes)


def test_wrong_sizes_and_scale_mismatches_are_rejected(tmp_path):
    odd = tmp_path / "odd.png"
    _frame(size=(70, 20)).save(odd)
    with pytest.raises(ValueError, match="integer enlargement"):
        load_view_segment([odd], repo_root=tmp_path, display_id="front")

    doubled = tmp_path / "doubled.png"
    _frame(size=(144, 32)).save(doubled)
    with pytest.raises(ValueError, match="requested scale 4"):
        load_view_segment(
            [doubled], repo_root=tmp_path, display_id="front", scale=4,
        )

    native = tmp_path / "native.png"
    _frame().save(native)
    with pytest.raises(ValueError, match="share one size"):
        load_view_segment(
            [native, doubled], repo_root=tmp_path, display_id="front",
        )


def test_region_checks_enforce_device_laws(tmp_path):
    muddy = _frame((100, 100, 100))
    for x in range(4, 10):
        muddy.putpixel((x, 5), (120, 120, 120))
    frame = tmp_path / "muddy.png"
    muddy.save(frame)

    _request, segment = load_view_segment(
        [frame],
        repo_root=tmp_path,
        display_id="front",
        regions={"label": (0, 0, 20, 10)},
        inks={"label": ("#787878",)},
    )
    results = {result.id: result for result in analyze(segment)}
    contrast = results["label-contrast"]
    assert contrast.status.value == "fail"
    assert contrast.observed["worst_delta"] < 76.5
    assert not required_checks_pass(tuple(results.values()))

    speck = _frame((0, 0, 0))
    speck.putpixel((5, 5), (200, 0, 0))
    speck_frame = tmp_path / "speck.png"
    speck.save(speck_frame)

    _request, segment = load_view_segment(
        [speck_frame],
        repo_root=tmp_path,
        display_id="front",
        regions={"label": (0, 0, 20, 10)},
    )
    results = {result.id: result for result in analyze(segment)}
    assert results["label-body"].status.value == "fail"


def test_undeclared_ink_regions_and_bad_rects_are_rejected(tmp_path):
    frame = tmp_path / "frame.png"
    _frame().save(frame)

    with pytest.raises(ValueError, match="not declared: label"):
        load_view_segment(
            [frame], repo_root=tmp_path, display_id="front",
            inks={"label": ("#FFFFFF",)},
        )
    with pytest.raises(ValueError, match="exceeds the front display"):
        load_view_segment(
            [frame], repo_root=tmp_path, display_id="front",
            regions={"label": (0, 0, 80, 10)},
        )


def test_a_directory_of_many_frames_survives_parameter_validation(tmp_path):
    """MAX_PARAMETER_LENGTH is a per-value identity budget. Applied to the
    str() of the whole frame_sha256 list it capped `view` at three frames
    while MAX_FRAMES promises 240 — found live on 2026-08-11 when a 12-frame
    directory of train composites was refused."""
    from busybar_viz.limits import validate_parameters

    paths = []
    for i in range(24):
        p = tmp_path / f"frame-{i:03d}.png"
        _frame((i, i, i)).save(p)
        paths.append(p)

    request, _segment = load_view_segment(
        paths, repo_root=tmp_path, display_id="front",
    )

    validate_parameters(dict(request.parameters))   # must not raise


def test_the_parameter_budget_still_binds_every_leaf_value():
    """Restructuring the check must not open a smuggling hole: a single
    oversized string is refused at the top level, inside a list, and
    inside a nested mapping alike."""
    from busybar_viz.limits import LimitError, validate_parameters

    for payload in ("x" * 300, ["x" * 300], {"k": "x" * 300}):
        with pytest.raises(LimitError):
            validate_parameters({"p": payload})


def test_emit_declaration_is_paste_ready():
    from busybar_viz.view import emit_declaration

    block = emit_declaration(
        "pomodoro",
        display_id="front",
        regions={"timer": (1, 0, 26, 8)},
        inks={"timer": ("#FFFFFF",)},
        max_isolated=2,
    )
    assert block == (
        "[pomodoro.viz]\n"
        'renderer = "apps.pomodoro:render_visual"\n'
        'displays = ["front"]\n'
        "\n"
        "[pomodoro.viz.regions.timer]\n"
        "rect = [1, 0, 26, 8]\n"
        'ink = ["#FFFFFF"]\n'
        "max_isolated = 2\n"
    )
    import tomllib
    parsed = tomllib.loads(block)
    assert parsed["pomodoro"]["viz"]["regions"]["timer"]["rect"] == [1, 0, 26, 8]

    for invalid in ("Bad App", "my-app", "1app"):
        with pytest.raises(ValueError, match="lowercase letter"):
            emit_declaration(
                invalid, display_id="front", regions={}, inks={}, max_isolated=0,
            )


def test_view_each_publishes_one_artifact_per_input(
    tmp_path, capsys, repo_scratch,
):
    """Iterating a design means auditing several candidates; without --each
    that was one process, one JSON parse, and one shell-loop iteration per
    candidate (run four separate times on 2026-08-11 before this existed)."""
    a = repo_scratch / "cand-a.png"
    b = repo_scratch / "cand-b.png"
    _frame((60, 10, 10)).save(a)
    _frame((10, 60, 10)).save(b)

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "view", str(a), str(b), "--each", "--json",
    ))

    results = json.loads(capsys.readouterr().out)
    assert code == 0
    assert isinstance(results, list) and len(results) == 2
    assert [r["input"] for r in results] == [str(a), str(b)]
    assert results[0]["artifact_id"] != results[1]["artifact_id"]
    assert all(r["passed"] for r in results)


def test_view_cli_emits_declaration(tmp_path, capsys, repo_scratch):
    frame = repo_scratch / "frame.png"
    _frame().save(frame)

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "view", str(frame),
        "--region", "label=0,0,20,10",
        "--ink", "label=#FFFFFF",
        "--emit-declaration", "myidea",
        "--json",
    ))
    result = json.loads(capsys.readouterr().out)
    assert code in (0, 1)
    assert "[myidea.viz]" in result["declaration"]
    assert "[myidea.viz.regions.label]" in result["declaration"]


def test_region_and_ink_argument_parsing():
    assert parse_region("clock=1,0,20,7") == ("clock", (1, 0, 20, 7))
    assert parse_ink("clock=#ffffff,C8C8C8") == (
        "clock", ("#FFFFFF", "#C8C8C8"),
    )
    for raw in ("clock", "clock=1,2,3", "clock=7,0,1,7", "bad name=0,0,1,1"):
        with pytest.raises(ValueError):
            parse_region(raw)
    for raw in ("clock", "clock=#FFF", "clock=notacolour"):
        with pytest.raises(ValueError):
            parse_ink(raw)


@pytest.fixture
def repo_scratch(tmp_path, monkeypatch):
    """A unique frame directory inside the real checkout.

    Publication fingerprints the analyzer/loader sources against the repo
    root, so the end-to-end CLI tests must run against the actual checkout;
    `scratch/` is its gitignored working area.
    """
    monkeypatch.chdir(REPO)
    scratch = REPO / "scratch" / f"pytest-view-{tmp_path.name}"
    scratch.mkdir(parents=True)
    try:
        yield scratch
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def test_view_cli_publishes_failures_with_stable_identity(
    tmp_path, capsys, repo_scratch,
):
    muddy = _frame((100, 100, 100))
    for x in range(4, 10):
        muddy.putpixel((x, 5), (120, 120, 120))
    frame = repo_scratch / "muddy.png"
    muddy.save(frame)

    argv = (
        "--data-dir", str(tmp_path / "viz"),
        "view", str(frame),
        "--region", "label=0,0,20,10",
        "--ink", "label=#787878",
        "--json",
    )
    code = cli.main(argv)
    result = json.loads(capsys.readouterr().out)

    assert code == 1
    assert result["passed"] is False
    assert result["confidence"] == "source_exact"
    assert [failure["id"] for failure in result["failures"]] == ["label-contrast"]
    assert Path(result["previews"]["front"]["gap_contact_sheet"]).is_file()
    audit = json.loads(Path(result["audit_path"]).read_text(encoding="utf-8"))
    assert {check["id"] for check in audit["checks"]} >= {
        "front-dimensions", "label-body", "label-contrast",
    }

    rerun = cli.main(argv)
    assert rerun == 1
    assert json.loads(capsys.readouterr().out)["artifact_id"] == result["artifact_id"]


def test_view_cli_rejects_invalid_requests_with_exit_two(
    tmp_path, capsys, repo_scratch,
):
    frame = repo_scratch / "frame.png"
    _frame().save(frame)

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "view", str(frame),
        "--region", "label=0,0,20,10",
        "--region", "label=0,0,10,10",
        "--json",
    ))
    error = json.loads(capsys.readouterr().out)

    assert code == 2
    assert error["kind"] == "invalid_request"
    assert "duplicate region" in error["error"]

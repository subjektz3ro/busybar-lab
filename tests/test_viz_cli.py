"""The CLI is a thin, stable JSON boundary over the generic viz library."""

from __future__ import annotations

import json
from importlib.metadata import version
from pathlib import Path
import shutil

import pytest
from PIL import Image

from busybar_viz import cli
from busybar_viz import __version__
from busybar_viz.artifacts import ArtifactStore
from busybar_viz.journal import SessionJournal
from busybar_viz.limits import LimitError
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    RenderedSegment,
    RenderRequest,
    ScenarioSpec,
)


REPO = Path(__file__).resolve().parents[1]


def test_visualizer_version_is_the_installed_project_version():
    assert __version__ == version("busybar-lab")


def _segment(*, passing=True, include_back=False) -> RenderedSegment:
    front = DisplayTrack(
        "front",
        (Image.new("RGB", (72, 16), (1, 2, 3)),),
        5,
        Confidence.SOURCE_EXACT,
    )
    tracks = [front]
    if include_back:
        tracks.append(DisplayTrack(
            "back",
            (Image.new("RGB", (160, 80), (4, 5, 6)),),
            2,
            Confidence.EMULATED_CONFORMANT,
        ))
    return RenderedSegment(
        displays=tuple(tracks),
        checks=(CheckSpec.create(
            "states",
            "animation.unique_frames",
            minimum=1 if passing else 2,
        ),),
    )


class _Adapter:
    def __init__(self, segment):
        self.segment = segment
        self.requests = []

    def render(self, request):
        self.requests.append(request)
        return self.segment


def test_find_repo_root_works_from_nested_app_paths():
    assert cli.find_repo_root(REPO / "apps") == REPO


def test_serve_forwards_each_explicit_remote_host(tmp_path, monkeypatch):
    from busybar_viz import server

    forwarded = []

    def fake_server_main(argv):
        forwarded.extend(argv)
        return 0

    monkeypatch.chdir(REPO)
    monkeypatch.setattr(server, "main", fake_server_main)

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "serve",
        "--bind", "0.0.0.0",
        "--allow-remote",
        "--allowed-host", "reviews.example",
        "--allowed-host", "review-box.local",
    ))

    assert code == 0
    assert "--allow-remote" in forwarded
    assert [
        forwarded[index + 1]
        for index, value in enumerate(forwarded)
        if value == "--allowed-host"
    ] == ["reviews.example", "review-box.local"]


def test_parameter_parser_preserves_json_types_and_rejects_ambiguous_input():
    assert cli._parameters((
        "count=3", "enabled=true", "label=rain", 'items=[1,2]',
    )) == {
        "count": 3,
        "enabled": True,
        "label": "rain",
        "items": [1, 2],
    }
    with pytest.raises(ValueError, match="duplicate"):
        cli._parameters(("count=1", "count=2"))
    with pytest.raises(ValueError, match="KEY=VALUE"):
        cli._parameters(("count",))
    with pytest.raises(LimitError, match="finite JSON"):
        cli._parameters(("count=NaN",))


def test_input_parser_requires_exact_types_and_timestamp_order():
    values = (
        '{"t_us":0,"kind":"button.press","control":"ok"}',
        '{"t_us":100,"kind":"wheel.turn","control":"encoder","value":-1}',
    )
    events = cli._inputs(values)

    assert [event.as_dict() for event in events] == [
        {"t_us": 0, "kind": "button.press", "control": "ok", "value": True},
        {
            "t_us": 100,
            "kind": "wheel.turn",
            "control": "encoder",
            "value": -1,
        },
    ]

    for raw in (
        '{"t_us":1.5,"kind":"button.press","control":"ok"}',
        '{"t_us":true,"kind":"button.press","control":"ok"}',
        '{"t_us":1,"kind":2,"control":"ok"}',
        '{"t_us":1,"kind":"button.press","control":2}',
    ):
        with pytest.raises(ValueError):
            cli._inputs((raw,))
    with pytest.raises(LimitError, match="ordered"):
        cli._inputs((values[1], values[0]))


def test_doctor_and_scenario_discovery_are_machine_readable(capsys, monkeypatch):
    monkeypatch.chdir(REPO)

    assert cli.main(("doctor", "--json")) == 0
    doctor = json.loads(capsys.readouterr().out)
    assert doctor["ok"] is True
    assert doctor["offline"] is True
    assert doctor["scenario_count"] >= 3
    assert len(doctor["scenarios"]) == doctor["scenario_count"]
    assert all(item["ok"] for item in doctor["scenarios"])

    assert cli.main(("scenarios", "--json")) == 0
    discovered = json.loads(capsys.readouterr().out)["scenarios"]
    distant = next(
        item for item in discovered if item["id"] == "skystrip/lightning-distant"
    )
    assert distant["expected_displays"] == ["front"]
    assert distant["controls"][0]["default"] == 40.0
    assert "inputs" in distant


def test_doctor_reports_an_import_broken_adapter_without_publishing(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    spec = ScenarioSpec(
        "broken/default", "Broken fixture", "Cannot import.", "broken",
    )

    class BrokenAdapter:
        def render(self, _request):
            raise ModuleNotFoundError("missing production renderer")

    monkeypatch.setattr(cli, "scenarios", lambda: (spec,))
    monkeypatch.setattr(
        cli, "render_registered", lambda request: BrokenAdapter().render(request),
    )
    data_dir = tmp_path / "viz"

    assert cli.main(("--data-dir", str(data_dir), "doctor", "--json")) == 1
    result = json.loads(capsys.readouterr().out)

    assert result["ok"] is False
    assert result["scenarios"] == [{
        "id": "broken/default",
        "ok": False,
        "error": "missing production renderer",
        "error_type": "ModuleNotFoundError",
    }]
    assert not data_dir.exists()


def test_schema_command_exposes_the_versioned_machine_contract(capsys, monkeypatch):
    monkeypatch.chdir(REPO)

    assert cli.main(("schema", "render-request", "--json")) == 0
    schema = json.loads(capsys.readouterr().out)

    assert schema["$id"] == "busybar.render-request/v1"
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {
        "schema", "scenario_id", "parameters", "inputs",
    }


def test_run_serializes_inputs_and_returns_preview_paths_for_every_display(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    adapter = _Adapter(_segment(include_back=True))
    monkeypatch.setattr(cli, "render_registered", adapter.render)
    data_dir = tmp_path / "viz"

    code = cli.main((
        "--data-dir", str(data_dir),
        "run", "fixture/two-displays",
        "--set", "variant=storm",
        "--input",
        '{"t_us":0,"kind":"button.press","control":"ok"}',
        "--json",
    ))
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["passed"] is True
    assert set(result["previews"]) == {"front", "back"}
    assert Path(result["summary_path"]).is_file()
    assert Path(result["audit_path"]).is_file()
    assert adapter.requests[0].parameters == {"variant": "storm"}
    assert adapter.requests[0].inputs[0].kind == "button.press"


def test_failed_audit_has_exit_one_but_still_returns_an_artifact(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    monkeypatch.setattr(
        cli, "render_registered", _Adapter(_segment(passing=False)).render,
    )

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "run", "fixture/failure", "--json",
    ))
    result = json.loads(capsys.readouterr().out)

    assert code == 1
    assert result["passed"] is False
    assert Path(result["artifact_path"], "manifest.json").is_file()


def test_asset_command_uses_the_shared_publication_pipeline(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    calls = []

    def load(path, *, repo_root, display_id, section):
        calls.append((path, repo_root, display_id, section))
        return (
            RenderRequest.from_values("asset/fixture.anim"),
            RenderedSegment(displays=(DisplayTrack(
                "back",
                (Image.new("RGB", (160, 80), (1, 2, 3)),),
                5,
                Confidence.SOURCE_EXACT,
            ),), checks=(CheckSpec.create(
                "back-dimensions",
                "frame.dimensions",
                display="back",
                size=(160, 80),
            ),)),
        )

    monkeypatch.setattr(cli, "load_asset_segment", load)
    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "asset", "fixtures/example.anim",
        "--display", "back", "--section", "detail", "--json",
    ))
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["passed"] is True
    assert result["animation_path"].endswith("/back.gif")
    assert calls == [(
        Path("fixtures/example.anim"), REPO, "back", "detail",
    )]


def test_invalid_run_input_returns_structured_exit_two_without_rendering(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    adapter = _Adapter(_segment())
    monkeypatch.setattr(cli, "render_registered", adapter.render)

    code = cli.main((
        "--data-dir", str(tmp_path / "viz"),
        "run", "fixture/input",
        "--input", '{"t_us":1.5,"kind":"button.press","control":"ok"}',
        "--json",
    ))
    error = json.loads(capsys.readouterr().out)

    assert code == 2
    assert error["kind"] == "invalid_request"
    assert adapter.requests == []


def test_inspect_and_compare_accept_published_artifact_ids(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    data_dir = tmp_path / "viz"
    adapter = _Adapter(_segment())
    monkeypatch.setattr(cli, "render_registered", adapter.render)
    assert cli.main((
        "--data-dir", str(data_dir), "run", "fixture", "--json",
    )) == 0
    artifact_id = json.loads(capsys.readouterr().out)["artifact_id"]

    assert cli.main((
        "--data-dir", str(data_dir), "inspect", artifact_id, "--json",
    )) == 0
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["artifact_id"] == artifact_id

    assert cli.main((
        "--data-dir", str(data_dir),
        "compare", artifact_id, artifact_id, "--json",
    )) == 0
    compared = json.loads(capsys.readouterr().out)
    assert compared["changed"] is False
    assert compared["before"] == artifact_id
    assert compared["after"] == artifact_id
    assert compared["displays"]["front"]["state"] == "identical"
    assert Path(compared["comparison_path"], "comparison.json").is_file()
    assert Path(compared["diff_contact_sheets"]["front"]).is_file()

    assert cli.main((
        "--data-dir", str(data_dir), "schema", "comparison", "--json",
    )) == 0
    schema = json.loads(capsys.readouterr().out)
    stored = json.loads(
        Path(compared["comparison_path"], "comparison.json").read_text()
    )
    allowed = set(schema["properties"])
    required = set(schema["required"])
    for payload in (stored, compared):
        assert required <= set(payload)
        assert set(payload) <= allowed
    assert {
        "same_artifact", "same_frames", "comparison_path", "diff_contact_sheets",
    } <= allowed - required


def test_agent_can_present_a_local_artifact_to_the_shared_session(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    data_dir = tmp_path / "viz"
    artifact = ArtifactStore(data_dir, REPO).publish(
        RenderRequest.from_values("fixture/presented"),
        _segment(),
    )
    journal = SessionJournal(data_dir / "sessions.sqlite3")
    session, _created = journal.create_session("Agent-driven draft")

    code = cli.main((
        "--data-dir", str(data_dir),
        "session", "present", session.id, artifact.artifact_id,
        "--revision", str(session.revision),
        "--message", "Candidate rendered from the edited checkout",
        "--event-id", "evt_0000000000000000000000000000000a",
        "--json",
    ))
    result = json.loads(capsys.readouterr().out)

    assert code == 0
    assert result["session"]["current_artifact_id"] == artifact.artifact_id
    assert result["event"]["kind"] == "artifact.presented"
    assert result["event"]["artifact_id"] == artifact.artifact_id
    assert result["event"]["body"]["message"].startswith("Candidate rendered")


def test_session_present_rejects_a_manifest_outside_the_configured_store(
    tmp_path,
):
    store = ArtifactStore(tmp_path / "viz", REPO)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"artifact_id": "a" * 64}))

    with pytest.raises(ValueError, match="configured data store"):
        cli._stored_artifact_id(str(outside), store)


def test_inspect_and_session_present_reject_tampered_artifacts(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    data_dir = tmp_path / "viz"
    artifact = ArtifactStore(data_dir, REPO).publish(
        RenderRequest.from_values("fixture/tampered"),
        _segment(),
    )
    (artifact.path / "summary.md").write_text("tampered\n")

    assert cli.main((
        "--data-dir", str(data_dir),
        "inspect", artifact.artifact_id, "--json",
    )) == 2
    error = json.loads(capsys.readouterr().out)
    assert error["kind"] == "invalid_request"
    assert "hash mismatch" in error["error"]

    with pytest.raises(ValueError, match="hash mismatch"):
        cli._stored_artifact_id(
            artifact.artifact_id,
            ArtifactStore(data_dir, REPO),
        )


def test_digest_lookup_rejects_a_complete_artifact_under_the_wrong_identity(
    tmp_path, capsys, monkeypatch,
):
    monkeypatch.chdir(REPO)
    data_dir = tmp_path / "viz"
    store = ArtifactStore(data_dir, REPO)
    expected = store.publish(
        RenderRequest.from_values("fixture/expected"),
        _segment(),
    )
    replacement = store.publish(
        RenderRequest.from_values("fixture/replacement"),
        _segment(),
    )
    shutil.rmtree(expected.path)
    shutil.copytree(replacement.path, expected.path)

    assert cli.main((
        "--data-dir", str(data_dir),
        "inspect", expected.artifact_id, "--json",
    )) == 2
    inspected = json.loads(capsys.readouterr().out)
    assert "expected digest" in inspected["error"]

    assert cli.main((
        "--data-dir", str(data_dir),
        "compare", expected.artifact_id, replacement.artifact_id, "--json",
    )) == 2
    compared = json.loads(capsys.readouterr().out)
    assert "expected digest" in compared["error"]


# --- sweep (the exploration on-ramp) ---------------------------------------
#
# The gap this closes: the audit workflow starts at a registered scenario with
# one set of controls, so answering "does this read at every hour" meant
# writing a render script — and a script produces no artifact, no SHA and no
# journal entry, which is exactly how a visual claim ends up resting on a
# scratch PNG. Provenance should be a byproduct of the fast path, not its price.


def _sweep(tmp_path, capsys, monkeypatch, *args):
    monkeypatch.chdir(REPO)
    code = cli.main(("--data-dir", str(tmp_path / "viz"), "sweep", *args, "--json"))
    return code, json.loads(capsys.readouterr().out)


def test_sweep_renders_a_cell_per_value_and_stamps_each(
    tmp_path, capsys, monkeypatch,
):
    code, payload = _sweep(tmp_path, capsys, monkeypatch,
                           "skystrip/status-clock", "--over", "hour=0,12")
    assert code == 0, payload
    assert payload["cell_count"] == 2
    assert payload["passed"] is True
    ids = {cell["artifact_id"] for cell in payload["cells"]}
    assert len(ids) == 2, "each cell must be its own immutable artifact"
    assert all(cell["failures"] == [] for cell in payload["cells"])


def test_sweep_builds_a_matrix_from_repeated_axes(
    tmp_path, capsys, monkeypatch,
):
    code, payload = _sweep(tmp_path, capsys, monkeypatch,
                           "skystrip/status-clock",
                           "--over", "hour=6,18",
                           "--over", "cloud_frac=0.0,1.0")
    assert code == 0
    assert payload["cell_count"] == 4
    combos = {(c["parameters"]["hour"], c["parameters"]["cloud_frac"])
              for c in payload["cells"]}
    assert combos == {(6.0, 0.0), (6.0, 1.0), (18.0, 0.0), (18.0, 1.0)}


def test_sweep_fails_and_names_the_cell(tmp_path, capsys, monkeypatch):
    """A sweep reporting only an aggregate would be useless — the point is
    knowing WHICH combination broke."""
    code, payload = _sweep(tmp_path, capsys, monkeypatch,
                           "skystrip/status-clock", "--over", "hour=6,12",
                           "--set", "fault=legacy_amber")
    assert code == 1, "a failing sweep must exit non-zero"
    assert payload["passed"] is False
    assert all(not cell["passed"] for cell in payload["cells"])
    for cell in payload["cells"]:
        failure = next(f for f in cell["failures"]
                       if f["kind"] == "region.contrast_floor")
        assert failure["observed"]["worst_delta"] < 76.5


def test_sweep_rejects_a_malformed_axis(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(REPO)
    with pytest.raises(SystemExit):
        cli.main(("--data-dir", str(tmp_path / "viz"), "sweep",
                  "skystrip/status-clock", "--over", "hour", "--json"))

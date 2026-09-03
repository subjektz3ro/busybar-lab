"""Stable command-line surface for humans and model harnesses."""

from __future__ import annotations

import argparse
import importlib.resources
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Sequence

from .analysis import analyze, required_checks_pass
from .artifacts import ArtifactStore, _validate_segment, verify_artifact
from .assets import load_asset_segment
from .comparison import ComparisonStore
from .limits import LimitError, validate_inputs, validate_parameters
from .models import InputEvent, RenderRequest, ScenarioSpec
from .offline import offline_render
from .registry import render_registered, scenarios
from .scaffold import (
    AdapterScaffoldSpec,
    ScaffoldCollisionError,
    create_adapter_scaffold,
    plan_adapter_scaffold,
)
from .view import emit_declaration, load_view_segment, parse_ink, parse_region
from . import __version__

_SCHEMAS = {
    "scenario": "scenario-v1.json",
    "render-request": "render-request-v1.json",
    "trace": "trace-v1.json",
    "evidence": "evidence-v1.json",
    "session-event": "session-event-v1.json",
    "session": "session-v1.json",
    "comparison": "comparison-v1.json",
}
_ARTIFACT_ID_RE = re.compile(r"[0-9a-f]{64}\Z")


def find_repo_root(start: Path | None = None) -> Path:
    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / "AGENTS.md").is_file() and (directory / "apps").is_dir():
            return directory
    raise ValueError("busybar-viz must run inside a BUSY Bar Lab checkout")


def _parse_value(raw: str) -> object:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _parameters(values: Sequence[str]) -> dict[str, object]:
    parsed: dict[str, object] = {}
    for item in values:
        key, separator, value = item.partition("=")
        if not separator or not key:
            raise ValueError(f"expected KEY=VALUE, got {item!r}")
        if key in parsed:
            raise ValueError(f"duplicate parameter: {key}")
        parsed[key] = _parse_value(value)
    validate_parameters(parsed)
    return parsed


def _inputs(values: Sequence[str]) -> tuple[InputEvent, ...]:
    parsed: list[InputEvent] = []
    for raw in values:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("--input must be a JSON object") from exc
        if not isinstance(value, dict):
            raise ValueError("--input must be a JSON object")
        unknown = set(value) - {"t_us", "kind", "control", "value"}
        if unknown:
            raise ValueError(f"unknown input fields: {', '.join(sorted(unknown))}")
        parsed.append(InputEvent.from_dict(value))
    validate_inputs(parsed)
    return tuple(parsed)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="busybar-viz",
        description="Create deterministic visual evidence for BUSY Bar apps.",
    )
    parser.add_argument("--data-dir", type=Path,
                        help="artifact/session root (default: scratch/busybar-viz)")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    doctor = sub.add_parser(
        "doctor", help="render and validate every registered default scenario",
    )
    doctor.add_argument("--json", action="store_true")

    listed = sub.add_parser("scenarios", help="list registered scenarios")
    listed.add_argument("--json", action="store_true")

    schema = sub.add_parser("schema", help="print a machine-readable JSON schema")
    schema.add_argument("name", choices=tuple(_SCHEMAS))
    schema.add_argument("--json", action="store_true")

    run = sub.add_parser("run", help="render and audit a scenario")
    run.add_argument("scenario")
    run.add_argument("--set", dest="parameters", action="append", default=[],
                     metavar="KEY=VALUE")
    run.add_argument(
        "--input", dest="inputs", action="append", default=[], metavar="JSON",
        help="timed semantic input object; repeat in timestamp order",
    )
    run.add_argument("--json", action="store_true")

    sweep = sub.add_parser(
        "sweep",
        help="render one scenario across a control's values and audit each",
    )
    sweep.add_argument("scenario")
    sweep.add_argument("--set", dest="parameters", action="append", default=[],
                       metavar="KEY=VALUE", help="fixed for every cell")
    sweep.add_argument(
        "--over", dest="sweeps", action="append", default=[], required=True,
        metavar="KEY=V1,V2,...",
        help="the control to vary; repeat for a matrix",
    )
    sweep.add_argument("--json", action="store_true")

    view = sub.add_parser(
        "view",
        help="audit ad-hoc in-development PNG frames without a registered scenario",
    )
    view.add_argument(
        "paths", nargs="+", type=Path,
        metavar="PATH",
        help="PNG frame files in order, or a directory of them (sorted by name)",
    )
    view.add_argument("--display", choices=("front", "back"), default="front")
    view.add_argument(
        "--fps", type=int,
        help="playback rate (default: 5 for animations, 1 for a single frame)",
    )
    view.add_argument(
        "--scale", type=int,
        help="declare the input's integer enlargement factor "
             "(default: native size or an exact enlargement is auto-detected)",
    )
    view.add_argument(
        "--region", dest="regions", action="append", default=[],
        metavar="NAME=X0,Y0,X1,Y1",
        help="declare a half-open semantic rect; runs the device-law "
             "feature-size check on it (repeatable)",
    )
    view.add_argument(
        "--ink", dest="inks", action="append", default=[],
        metavar="NAME=#RRGGBB[,...]",
        help="declared ink colours for a --region; enables the device-law "
             "contrast-floor check (repeatable)",
    )
    view.add_argument(
        "--max-isolated", type=int, default=0,
        help="isolated lit pixels tolerated per declared region (default 0)",
    )
    view.add_argument(
        "--each", action="store_true",
        help="publish each PATH (file or directory) as its own artifact "
             "and emit a JSON array; exit 0 only when every one passed",
    )
    view.add_argument(
        "--emit-declaration", nargs="?", const="myapp", metavar="APP",
        help="include a paste-ready [APP.viz] apps.toml block for this "
             "invocation's regions and inks",
    )
    view.add_argument("--json", action="store_true")

    asset = sub.add_parser(
        "asset", help="decode and audit a repository PNG or native .anim file",
    )
    asset.add_argument("path", type=Path)
    asset.add_argument("--display", choices=("front", "back"), default="front")
    asset.add_argument("--section", default="default")
    asset.add_argument("--json", action="store_true")

    audio = sub.add_parser(
        "audio",
        help="inspect a raw .snd asset (s16le mono 44100) and optionally "
             "draw its waveform",
    )
    audio.add_argument("path", type=Path)
    audio.add_argument(
        "--waveform", type=Path, metavar="OUT_PNG",
        help="also write a min/max envelope PNG here",
    )
    audio.add_argument("--json", action="store_true")

    capture = sub.add_parser(
        "capture",
        help="publish the device's current framebuffer as evidence "
             "(read-only; the only device-network-client subcommand)",
    )
    capture.add_argument(
        "--display", action="append", choices=("front", "back"),
        dest="displays",
        help="capture only this display (repeatable; default: both)",
    )
    capture.add_argument("--json", action="store_true")

    scaffold = sub.add_parser(
        "scaffold", help="plan or create an explicit app visualizer adapter",
    )
    scaffold.add_argument("adapter_id")
    scaffold.add_argument(
        "--renderer", required=True, metavar="MODULE:FUNCTION",
        help="static pure renderer returning {display: (PIL frames, fps)}",
    )
    scaffold.add_argument("--scenario", default="default")
    scaffold.add_argument("--title", default="Deterministic app preview")
    scaffold.add_argument(
        "--description", default="Production-rendered visual scenario.",
    )
    scaffold.add_argument(
        "--display", action="append", choices=("front", "back"),
        dest="displays",
    )
    scaffold.add_argument(
        "--write", action="store_true",
        help="create the adapter/test pair; existing paths are never overwritten",
    )
    scaffold.add_argument("--json", action="store_true")

    serve = sub.add_parser("serve", help="run the standalone localhost review UI")
    serve.add_argument("--bind", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--allow-remote", action="store_true",
        help="allow an unauthenticated non-loopback bind with Host validation",
    )
    serve.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        dest="allowed_hosts",
        metavar="HOST",
        help=(
            "allow this exact DNS Host in remote mode (repeatable; direct IP "
            "literals need no entry)"
        ),
    )

    session = sub.add_parser(
        "session", help="create, inspect, or watch a durable review session",
    )
    session_sub = session.add_subparsers(dest="session_command", required=True)
    session_create = session_sub.add_parser("create")
    session_create.add_argument("title")
    session_create.add_argument("--event-id")
    session_create.add_argument("--json", action="store_true")
    session_list = session_sub.add_parser("list")
    session_list.add_argument("--limit", type=int, default=100)
    session_list.add_argument("--json", action="store_true")
    session_show = session_sub.add_parser("show")
    session_show.add_argument("session_id")
    session_show.add_argument("--json", action="store_true")
    session_events = session_sub.add_parser(
        "events", help="read or long-poll sequenced UI/model events",
    )
    session_events.add_argument("session_id")
    session_events.add_argument("--after", type=int, default=0)
    session_events.add_argument("--limit", type=int, default=500)
    session_events.add_argument("--wait", type=float, default=0.0)
    session_events.add_argument("--json", action="store_true")
    session_events.add_argument("--jsonl", action="store_true")
    session_note = session_sub.add_parser(
        "note", help="append an agent note using optimistic concurrency",
    )
    session_note.add_argument("session_id")
    session_note.add_argument("--revision", type=int, required=True)
    session_note.add_argument("--message", required=True)
    session_note.add_argument("--artifact")
    session_note.add_argument("--current-artifact", action="store_true")
    session_note.add_argument("--json", action="store_true")
    session_present = session_sub.add_parser(
        "present",
        help="make one locally rendered artifact current in the shared session",
    )
    session_present.add_argument("session_id")
    session_present.add_argument("artifact")
    session_present.add_argument("--revision", type=int, required=True)
    session_present.add_argument("--message")
    session_present.add_argument("--event-id")
    session_present.add_argument("--json", action="store_true")
    session_export = session_sub.add_parser("export")
    session_export.add_argument("session_id")

    gc = sub.add_parser(
        "gc",
        help="reclaim unreferenced artifacts/comparisons (dry run by default)",
    )
    gc.add_argument(
        "--delete", action="store_true",
        help="apply the plan; without this only the plan is printed",
    )
    gc.add_argument(
        "--keep-recent-hours", type=float, default=24.0,
        help="never touch entries newer than this (default 24)",
    )
    gc.add_argument("--json", action="store_true")

    baseline = sub.add_parser(
        "baseline",
        help="check or accept the committed pixel baselines for registered scenarios",
    )
    baseline_sub = baseline.add_subparsers(dest="baseline_command", required=True)
    baseline_check = baseline_sub.add_parser(
        "check",
        help="re-render every registered scenario and fail on pixel drift",
    )
    baseline_check.add_argument("--json", action="store_true")
    baseline_update = baseline_sub.add_parser(
        "update",
        help="accept current pixels; rewrites viz-baselines.toml",
    )
    baseline_update.add_argument(
        "scenarios", nargs="*", metavar="SCENARIO_ID",
        help="only update these scenarios (default: all registered)",
    )
    baseline_update.add_argument("--json", action="store_true")

    inspect = sub.add_parser("inspect", help="read an evidence manifest")
    inspect.add_argument("artifact")
    inspect.add_argument("--json", action="store_true")

    compare = sub.add_parser("compare", help="compare two evidence manifests")
    compare.add_argument("before")
    compare.add_argument("after")
    compare.add_argument("--json", action="store_true")
    return parser


def _emit(value: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(value, sort_keys=True, separators=(",", ":")))
        return
    if isinstance(value, str):
        print(value)
        return
    print(json.dumps(value, indent=2, sort_keys=True))


def _manifest_reference(
    value: str,
    store: ArtifactStore,
) -> tuple[Path, str | None]:
    """Resolve a manifest and retain the identity promised by a bare digest."""

    if _ARTIFACT_ID_RE.fullmatch(value):
        manifest = (
            store.artifacts_dir / value[:2] / value / "manifest.json"
        )
        if manifest.is_file():
            return manifest.absolute(), value
        raise ValueError(f"artifact not found: {value}")
    direct = Path(value).expanduser()
    if direct.is_dir():
        direct = direct / "manifest.json"
    if direct.is_file():
        return direct.absolute(), None
    raise ValueError(f"artifact not found: {value}")


def _manifest_path(value: str, store: ArtifactStore) -> Path:
    """Compatibility resolver for callers that need only the manifest path."""

    return _manifest_reference(value, store)[0]


def _verified_artifact(value: str, store: ArtifactStore):
    manifest_path, expected_artifact_id = _manifest_reference(value, store)
    return verify_artifact(
        manifest_path,
        full=True,
        expected_artifact_id=expected_artifact_id,
    )


def _stored_artifact_id(value: str, store: ArtifactStore) -> str:
    """Resolve only a complete artifact in this command's configured store."""

    manifest_path = _manifest_path(value, store).absolute()
    artifact_root = store.artifacts_dir.resolve()
    try:
        manifest_path.relative_to(artifact_root)
    except ValueError as exc:
        raise ValueError("session artifacts must come from the configured data store") from exc
    verified = verify_artifact(manifest_path, full=True)
    artifact_id = verified.artifact_id
    expected = (
        artifact_root / artifact_id[:2] / artifact_id / "manifest.json"
    )
    if manifest_path != expected or verified.path != expected.parent:
        raise ValueError("artifact manifest path does not match its identity")
    return artifact_id


def _doctor_scenario(spec: ScenarioSpec) -> dict[str, object]:
    parameters = {control.id: control.default for control in spec.controls}
    request = RenderRequest.from_values(spec.id, parameters)
    try:
        validate_parameters(dict(request.parameters))
        validate_inputs(request.inputs)
        with offline_render(find_repo_root()):
            segment = render_registered(request)
        _validate_segment(segment)
        actual_displays = tuple(track.id for track in segment.displays)
        audit = analyze(segment)
        failures = [
            result.id for result in audit
            if result.severity == "error" and result.status.value != "pass"
        ]
        return {
            "id": spec.id,
            "ok": required_checks_pass(audit),
            "displays": list(actual_displays),
            "required_check_failures": failures,
        }
    except Exception as exc:  # noqa: BLE001 - doctor reports each broken adapter
        return {
            "id": spec.id,
            "ok": False,
            "error": str(exc),
            "error_type": type(exc).__name__,
        }


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    json_output = bool(getattr(args, "json", False))
    try:
        repo_root = find_repo_root()
        data_root = (args.data_dir or repo_root / "scratch" / "busybar-viz").resolve()
        store = ArtifactStore(data_root, repo_root)

        if args.command == "doctor":
            specs = scenarios()
            scenario_results = [_doctor_scenario(spec) for spec in specs]
            ok = all(item["ok"] for item in scenario_results)
            result = {
                "ok": ok,
                "repo_root": str(repo_root),
                "data_root": str(data_root),
                "scenario_count": len(specs),
                "scenarios": scenario_results,
                "offline": True,
            }
            _emit(result, json_output=json_output)
            return 0 if ok else 1

        if args.command == "scenarios":
            rows = [spec.as_dict() for spec in scenarios()]
            _emit({"scenarios": rows}, json_output=json_output)
            return 0

        if args.command == "schema":
            resource = importlib.resources.files("busybar_viz").joinpath(
                "schemas", _SCHEMAS[args.name],
            )
            value = json.loads(resource.read_text(encoding="utf-8"))
            _emit(value, json_output=json_output)
            return 0

        if args.command == "sweep":
            fixed = _parameters(args.parameters)
            axes: list[tuple[str, list[object]]] = []
            for raw in args.sweeps:
                if "=" not in raw:
                    raise SystemExit(f"--over needs KEY=V1,V2,...: {raw!r}")
                key, _, values = raw.partition("=")
                axes.append((key.strip(),
                             [_parse_value(v.strip()) for v in values.split(",") if v.strip()]))
            combos: list[dict[str, object]] = [{}]
            for key, values in axes:
                combos = [{**combo, key: value}
                          for combo in combos for value in values]

            cells = []
            worst_level = None
            for combo in combos:
                parameters = {**fixed, **combo}
                request = RenderRequest.from_values(args.scenario, parameters, ())
                with offline_render(repo_root):
                    segment = render_registered(request)
                artifact = store.publish(request, segment)
                audit = json.loads(
                    (artifact.path / "audit.json").read_text(encoding="utf-8"))
                failures = [
                    {"id": check["id"], "kind": check["kind"],
                     "message": check["message"],
                     "observed": dict(check["observed"])}
                    for check in audit["checks"]
                    if check["status"] == "fail"
                ]
                level = artifact.manifest["evidence"]["automatic_level"]
                worst_level = level if worst_level is None else worst_level
                cells.append({
                    "parameters": parameters,
                    "artifact_id": artifact.artifact_id,
                    "passed": artifact.passed,
                    "failures": failures,
                    "gap_contact_sheet": str(
                        artifact.path / "front-gap-contact-sheet.png"),
                })
            result = {
                "scenario": args.scenario,
                "cell_count": len(cells),
                "passed": all(cell["passed"] for cell in cells),
                "evidence_level": worst_level,
                "cells": cells,
            }
            _emit(result, json_output=json_output)
            return 0 if result["passed"] else 1

        if args.command == "run":
            parameters = _parameters(args.parameters)
            inputs = _inputs(args.inputs)
            request = RenderRequest.from_values(args.scenario, parameters, inputs)
            with offline_render(repo_root):
                segment = render_registered(request)
            artifact = store.publish(request, segment)
            result = {
                "artifact_id": artifact.artifact_id,
                "artifact_path": str(artifact.path),
                "passed": artifact.passed,
                "evidence_level": artifact.manifest["evidence"]["automatic_level"],
                "summary_path": str(artifact.path / "summary.md"),
                "previews": {
                    display_id: {
                        "animation": str(artifact.path / f"{display_id}.gif"),
                        "gap_animation": str(
                            artifact.path / f"{display_id}-gap.gif"
                        ),
                        "contact_sheet": str(
                            artifact.path / f"{display_id}-contact-sheet.png"
                        ),
                        "gap_contact_sheet": str(
                            artifact.path / f"{display_id}-gap-contact-sheet.png"
                        ),
                    }
                    for display_id in artifact.manifest["displays"]
                },
                "audit_path": str(artifact.path / "audit.json"),
            }
            _emit(result, json_output=json_output)
            return 0 if artifact.passed else 1

        if args.command == "view":
            regions = {}
            for raw in args.regions:
                name, rect = parse_region(raw)
                if name in regions:
                    raise ValueError(f"duplicate region: {name}")
                regions[name] = rect
            inks = {}
            for raw in args.inks:
                name, colors = parse_ink(raw)
                if name in inks:
                    raise ValueError(f"duplicate ink declaration: {name}")
                inks[name] = colors
            def publish_view(paths: list[Path]) -> tuple[dict, bool]:
                request, segment = load_view_segment(
                    paths,
                    repo_root=repo_root,
                    display_id=args.display,
                    fps=args.fps,
                    scale=args.scale,
                    regions=regions,
                    inks=inks,
                    max_isolated=args.max_isolated,
                )
                artifact = store.publish(request, segment)
                audit = json.loads(
                    (artifact.path / "audit.json").read_text(encoding="utf-8"))
                failures = [
                    {"id": check["id"], "kind": check["kind"],
                     "message": check["message"],
                     "observed": dict(check["observed"]),
                     "frame_indices": list(check["frame_indices"])}
                    for check in audit["checks"]
                    if check["status"] == "fail"
                ]
                result = {
                    "artifact_id": artifact.artifact_id,
                    "artifact_path": str(artifact.path),
                    "passed": artifact.passed,
                    "confidence": segment.displays[0].confidence.value,
                    "scale": request.parameters["scale"],
                    "failures": failures,
                    **({"declaration": emit_declaration(
                        args.emit_declaration,
                        display_id=args.display,
                        regions=regions,
                        inks=inks,
                        max_isolated=args.max_isolated,
                    )} if args.emit_declaration else {}),
                    "summary_path": str(artifact.path / "summary.md"),
                    "audit_path": str(artifact.path / "audit.json"),
                    "previews": {
                        args.display: {
                            "animation": str(
                                artifact.path / f"{args.display}.gif"),
                            "gap_animation": str(
                                artifact.path / f"{args.display}-gap.gif"
                            ),
                            "contact_sheet": str(
                                artifact.path
                                / f"{args.display}-contact-sheet.png"
                            ),
                            "gap_contact_sheet": str(
                                artifact.path
                                / f"{args.display}-gap-contact-sheet.png"
                            ),
                        }
                    },
                }
                return result, artifact.passed

            if args.each:
                # One artifact per input: auditing several candidates is one
                # invocation, not a shell loop with a JSON parse per turn.
                results = []
                all_passed = True
                for path in args.paths:
                    result, passed = publish_view([path])
                    result["input"] = str(path)
                    results.append(result)
                    all_passed = all_passed and passed
                _emit(results, json_output=json_output)
                return 0 if all_passed else 1

            result, passed = publish_view(list(args.paths))
            _emit(result, json_output=json_output)
            return 0 if passed else 1

        if args.command == "asset":
            request, segment = load_asset_segment(
                args.path,
                repo_root=repo_root,
                display_id=args.display,
                section=args.section,
            )
            artifact = store.publish(request, segment)
            result = {
                "artifact_id": artifact.artifact_id,
                "artifact_path": str(artifact.path),
                "passed": artifact.passed,
                "evidence_level": artifact.manifest["evidence"]["automatic_level"],
                "manifest_path": str(artifact.path / "manifest.json"),
                "animation_path": str(artifact.path / f"{args.display}.gif"),
                "gap_animation_path": str(
                    artifact.path / f"{args.display}-gap.gif"
                ),
                "summary_path": str(artifact.path / "summary.md"),
            }
            _emit(result, json_output=json_output)
            return 0 if artifact.passed else 1

        if args.command == "audio":
            from .audio import inspect_snd

            result = inspect_snd(
                args.path, repo_root=repo_root, waveform=args.waveform,
            )
            _emit(result, json_output=json_output)
            return 0

        if args.command == "capture":
            # Deliberately NOT wrapped in offline_render: reading the
            # framebuffer is the entire point, and it is read-only.
            from .capture import load_capture_segment

            request, segment = load_capture_segment(
                tuple(args.displays or ("front", "back")),
            )
            artifact = store.publish(request, segment)
            result = {
                "artifact_id": artifact.artifact_id,
                "artifact_path": str(artifact.path),
                "passed": artifact.passed,
                "evidence_level": artifact.manifest["evidence"]["automatic_level"],
                "summary_path": str(artifact.path / "summary.md"),
                "previews": {
                    display_id: {
                        "contact_sheet": str(
                            artifact.path / f"{display_id}-contact-sheet.png"
                        ),
                        "gap_contact_sheet": str(
                            artifact.path / f"{display_id}-gap-contact-sheet.png"
                        ),
                    }
                    for display_id in artifact.manifest["displays"]
                },
            }
            _emit(result, json_output=json_output)
            return 0 if artifact.passed else 1

        if args.command == "scaffold":
            module, separator, function = args.renderer.partition(":")
            if not separator:
                raise ValueError("--renderer must use MODULE:FUNCTION")
            spec = AdapterScaffoldSpec(
                adapter_id=args.adapter_id,
                renderer_module=module,
                renderer_name=function,
                scenario_name=args.scenario,
                title=args.title,
                description=args.description,
                expected_displays=tuple(args.displays or ("front",)),
            )
            plan = plan_adapter_scaffold(spec)
            if args.write:
                created = create_adapter_scaffold(repo_root, spec)
                result = {
                    "written": True,
                    "created": [str(path) for path in created.created],
                    "next_steps": list(created.next_steps),
                }
            else:
                result = {
                    "written": False,
                    "scenario_id": spec.scenario_id,
                    "files": [str(path) for path in plan.files],
                    "registry_import": plan.registry_import,
                    "registry_constructor": plan.registry_constructor,
                    "notice": "Re-run with --write after reviewing this plan.",
                }
            _emit(result, json_output=json_output)
            return 0

        if args.command == "serve":
            from .server import main as server_main

            server_args = [
                "--host", args.bind,
                "--port", str(args.port),
                "--repo-root", str(repo_root),
                "--data-root", str(data_root),
            ]
            if args.allow_remote:
                server_args.append("--allow-remote")
            for host in args.allowed_hosts:
                server_args.extend(("--allowed-host", host))
            return server_main(server_args)

        if args.command == "session":
            from .journal import SessionJournal

            journal = SessionJournal(data_root / "sessions.sqlite3")
            if args.session_command == "create":
                session_record, event = journal.create_session(
                    args.title, event_id=args.event_id,
                )
                _emit({
                    "session": session_record.as_dict(),
                    "event": event.as_dict(),
                }, json_output=json_output)
                return 0
            if args.session_command == "list":
                _emit({
                    "sessions": [record.as_dict() for record in
                                 journal.list_sessions(limit=args.limit)],
                }, json_output=json_output)
                return 0
            if args.session_command == "show":
                _emit({
                    "session": journal.get_session(args.session_id).as_dict(),
                }, json_output=json_output)
                return 0
            if args.session_command == "events":
                if not 0 <= args.wait <= 3600:
                    raise ValueError("--wait must be between 0 and 3600 seconds")
                deadline = time.monotonic() + args.wait
                events = journal.list_events(
                    args.session_id,
                    after_revision=args.after,
                    limit=args.limit,
                )
                while not events and time.monotonic() < deadline:
                    time.sleep(min(0.20, max(0.0, deadline - time.monotonic())))
                    events = journal.list_events(
                        args.session_id,
                        after_revision=args.after,
                        limit=args.limit,
                    )
                if args.jsonl:
                    for event in events:
                        print(json.dumps(
                            event.as_dict(), sort_keys=True, separators=(",", ":"),
                        ))
                else:
                    session_record = journal.get_session(args.session_id)
                    _emit({
                        "session": session_record.as_dict(),
                        "events": [event.as_dict() for event in events],
                        "next_revision": (
                            events[-1].revision if events else args.after
                        ),
                    }, json_output=json_output)
                return 0
            if args.session_command == "note":
                if args.artifact and args.current_artifact:
                    raise ValueError(
                        "choose --artifact or --current-artifact, not both"
                    )
                artifact_id = (
                    _stored_artifact_id(args.artifact, store)
                    if args.artifact else None
                )
                session_record, event = journal.append_event(
                    args.session_id,
                    expected_revision=args.revision,
                    kind="agent.note",
                    actor="agent",
                    body={"message": args.message},
                    artifact_id=artifact_id,
                    use_current_artifact=args.current_artifact,
                )
                _emit({
                    "session": session_record.as_dict(),
                    "event": event.as_dict(),
                }, json_output=json_output)
                return 0
            if args.session_command == "present":
                artifact_id = _stored_artifact_id(args.artifact, store)
                session_record, event = journal.present_artifact(
                    args.session_id,
                    expected_revision=args.revision,
                    artifact_id=artifact_id,
                    message=args.message,
                    event_id=args.event_id,
                )
                _emit({
                    "session": session_record.as_dict(),
                    "event": event.as_dict(),
                }, json_output=json_output)
                return 0
            if args.session_command == "export":
                sys.stdout.buffer.write(journal.export_jsonl(args.session_id))
                return 0

        if args.command == "gc":
            import time as _time

            from .store_gc import apply_gc, plan_gc

            if args.keep_recent_hours < 0:
                raise ValueError("--keep-recent-hours must not be negative")
            gc_plan = plan_gc(
                data_root,
                now=_time.time(),
                keep_recent_s=args.keep_recent_hours * 3600.0,
            )
            if args.delete:
                apply_gc(data_root, gc_plan)
            _emit({
                "deleted": bool(args.delete),
                "artifacts": list(gc_plan.delete_artifacts),
                "comparisons": list(gc_plan.delete_comparisons),
                "kept_artifact_count": len(gc_plan.keep_artifacts),
                "bytes_reclaimable": gc_plan.bytes_reclaimable,
            }, json_output=json_output)
            return 0

        if args.command == "baseline":
            from .baselines import (
                compare_baselines,
                load_baselines,
                pixel_digests,
                write_baselines,
            )

            def render_digests(scenario_spec: ScenarioSpec):
                parameters = {
                    control.id: control.default
                    for control in scenario_spec.controls
                }
                request = RenderRequest.from_values(scenario_spec.id, parameters)
                with offline_render(repo_root):
                    segment = render_registered(request)
                return request, segment, pixel_digests(segment)

            accepted = load_baselines(repo_root)
            fresh: dict[str, dict[str, str]] = {}

            if args.baseline_command == "update":
                selected = set(args.scenarios)
                known = {scenario.id for scenario in scenarios()}
                unknown = selected - known
                if unknown:
                    raise ValueError(
                        f"unknown scenarios: {', '.join(sorted(unknown))}"
                    )
                updated: list[str] = []
                for scenario in scenarios():
                    if selected and scenario.id not in selected:
                        if scenario.id in accepted:
                            fresh[scenario.id] = accepted[scenario.id]
                        continue
                    _request, _segment, digests = render_digests(scenario)
                    fresh[scenario.id] = digests
                    if accepted.get(scenario.id) != digests:
                        updated.append(scenario.id)
                path = write_baselines(repo_root, fresh)
                _emit({
                    "written": str(path),
                    "scenario_count": len(fresh),
                    "updated": updated,
                }, json_output=json_output)
                return 0

            if args.baseline_command == "check":
                evidence: dict[str, dict[str, str]] = {}
                for scenario in scenarios():
                    request, segment, digests = render_digests(scenario)
                    fresh[scenario.id] = digests
                    if accepted.get(scenario.id, digests) != digests:
                        # Publish the drifted render so the disagreement is
                        # inspectable evidence, not a bare digest mismatch.
                        artifact = store.publish(request, segment)
                        evidence[scenario.id] = {
                            "artifact_id": artifact.artifact_id,
                            "artifact_path": str(artifact.path),
                        }
                result = compare_baselines(accepted, fresh)
                result["drifted_artifacts"] = evidence
                _emit(result, json_output=json_output)
                return 0 if result["ok"] else 1

        if args.command == "inspect":
            artifact = _verified_artifact(args.artifact, store)
            _emit(artifact.manifest, json_output=json_output)
            return 0 if artifact.passed else 1

        if args.command == "compare":
            before = _verified_artifact(args.before, store)
            after = _verified_artifact(args.after, store)
            comparison = ComparisonStore(data_root).publish(
                before.path,
                after.path,
            )
            comparison_result: dict[str, Any] = dict(comparison.summary)
            comparison_result["same_artifact"] = (
                comparison_result["before"] == comparison_result["after"]
            )
            comparison_result["same_frames"] = {
                display_id: value.get("state") == "identical"
                for display_id, value in comparison_result["displays"].items()
            }
            comparison_result["comparison_path"] = str(comparison.path)
            comparison_result["diff_contact_sheets"] = {
                display_id: str(
                    comparison.path / f"{display_id}-diff-contact-sheet.png"
                )
                for display_id, value in comparison.summary["displays"].items()
                if value.get("compared_frame_count", 0)
            }
            _emit(comparison_result, json_output=json_output)
            return 0
    except (KeyError, ValueError, LimitError, ScaffoldCollisionError) as exc:
        if json_output:
            print(json.dumps({"error": str(exc), "kind": "invalid_request"},
                             sort_keys=True, separators=(",", ":")))
        else:
            print(f"busybar-viz: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001 - stable CLI failure boundary
        if json_output:
            print(json.dumps({"error": str(exc), "kind": "runtime_error"},
                             sort_keys=True, separators=(",", ":")))
        else:
            print(f"busybar-viz: {exc}", file=sys.stderr)
        return 3
    return 3

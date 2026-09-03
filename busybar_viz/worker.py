"""Finite subprocess entry point for one registered visual scenario.

The worker accepts exactly one versioned :class:`RenderRequest` on stdin.  It
does not accept Python entrypoints, filesystem inputs, shell commands, or an
environment map.  All executable code comes from the closed adapter registry.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path
from typing import Sequence, TextIO, cast

from .artifacts import ArtifactStore
from .limits import (
    MAX_ARTIFACT_BYTES,
    MAX_JSON_BYTES,
    validate_inputs,
    validate_parameters,
)
from .models import RenderRequest
from .offline import offline_render

_MAX_CAPTURE_CHARS = 32 * 1024


class _BoundedTextSink(io.TextIOBase):
    """Discard renderer chatter after a small diagnostic prefix."""

    def __init__(self, limit: int = _MAX_CAPTURE_CHARS) -> None:
        self.limit = limit
        self.parts: list[str] = []
        self.length = 0
        self.truncated = False

    def writable(self) -> bool:
        return True

    def write(self, value: str) -> int:
        original = len(value)
        remaining = self.limit - self.length
        if remaining > 0:
            kept = value[:remaining]
            self.parts.append(kept)
            self.length += len(kept)
        if original > max(0, remaining):
            self.truncated = True
        return original

    def value(self) -> str:
        suffix = "\n[renderer output truncated]" if self.truncated else ""
        return "".join(self.parts) + suffix


def _apply_resource_limits() -> None:
    """Add fixed worker-side CPU and single-file ceilings where supported."""

    try:
        import resource

        cpu_soft, cpu_hard = resource.getrlimit(resource.RLIMIT_CPU)
        desired_cpu = 60 if cpu_hard < 0 else min(60, cpu_hard)
        resource.setrlimit(resource.RLIMIT_CPU, (desired_cpu, cpu_hard))
        file_soft, file_hard = resource.getrlimit(resource.RLIMIT_FSIZE)
        desired_file = MAX_ARTIFACT_BYTES + 1024 * 1024
        if file_hard >= 0:
            desired_file = min(desired_file, file_hard)
        resource.setrlimit(resource.RLIMIT_FSIZE, (desired_file, file_hard))
    except (ImportError, OSError, ValueError):
        # Windows and some constrained launchers do not expose these limits;
        # the parent still enforces wall time, concurrency, and output budgets.
        pass


def parse_request(raw: bytes) -> RenderRequest:
    if len(raw) > MAX_JSON_BYTES:
        raise ValueError("worker request exceeds the JSON budget")
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("worker request must be one JSON object") from exc
    if not isinstance(value, dict):
        raise ValueError("worker request must be one JSON object")
    request = RenderRequest.from_dict(value)
    validate_parameters(dict(request.parameters))
    validate_inputs(request.inputs)
    # Resolve before starting any renderer work.  This is the security
    # boundary: scenario IDs select only audited, statically registered code.
    from .registry import adapter_for_scenario

    adapter_for_scenario(request.scenario_id)
    return request


def _emit(value: object) -> None:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    )
    # A CLI worker always has its original protocol stream. The cast keeps the
    # runtime path byte-for-byte identical while narrowing sys's GUI-safe type.
    output = cast(TextIO, sys.__stdout__)
    output.write(encoded + "\n")
    output.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--data-root", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    _apply_resource_limits()
    # Read at most one byte over the budget so a manually invoked worker also
    # has a finite input allocation.
    raw = sys.stdin.buffer.read(MAX_JSON_BYTES + 1)
    capture = _BoundedTextSink()
    try:
        repo_root = args.repo_root.resolve()
        data_root = args.data_root.resolve()
        if not (repo_root / "AGENTS.md").is_file() or not (repo_root / "apps").is_dir():
            raise ValueError("repo root is not a BUSY Bar Lab checkout")
        if (data_root / "work").is_symlink():
            raise ValueError("visualizer work path may not be a symlink")
        with (
            offline_render(repo_root),
            contextlib.redirect_stdout(capture),
            contextlib.redirect_stderr(capture),
        ):
            # Registry resolution lazily imports production app modules, some
            # of which intentionally load ``.env`` at import time. Enter the
            # guard before validation so that import cannot hydrate secrets.
            request = parse_request(raw)
            from .registry import render_registered

            segment = render_registered(request)
            artifact = ArtifactStore(data_root, repo_root).publish(request, segment)
        _emit({
            "ok": True,
            "artifact_id": artifact.artifact_id,
            "passed": artifact.passed,
        })
        return 0
    except (KeyError, ValueError) as exc:
        _emit({"ok": False, "kind": "invalid_request", "error": str(exc)})
        return 2
    except Exception as exc:  # noqa: BLE001 - process failure boundary
        _emit({"ok": False, "kind": "render_error", "error": str(exc)})
        return 3


if __name__ == "__main__":
    raise SystemExit(main())

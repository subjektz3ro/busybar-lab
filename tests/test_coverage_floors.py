"""The configured per-file coverage number is the exact enforced floor."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _checker():
    path = ROOT / "scripts" / "check_coverage.py"
    spec = importlib.util.spec_from_file_location("_check_coverage", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _report(path: Path, percent: float) -> Path:
    path.write_text(json.dumps({
        "files": {
            "package/module.py": {
                "summary": {
                    "num_statements": 10,
                    "percent_covered": percent,
                }
            }
        },
        "totals": {
            "num_statements": 10,
            "percent_covered": percent,
        },
    }))
    return path


def test_a_result_below_the_configured_floor_fails(monkeypatch, tmp_path):
    checker = _checker()
    monkeypatch.setattr(checker, "load_floors", lambda: (85.0, {}))
    report = _report(tmp_path / "coverage.json", 84.9)

    assert checker.main(["check_coverage.py", str(report)]) == 1


def test_the_exact_floor_passes_without_hidden_slack(monkeypatch, tmp_path):
    checker = _checker()
    monkeypatch.setattr(checker, "load_floors", lambda: (85.0, {}))
    report = _report(tmp_path / "coverage.json", 85.0)

    assert checker.main(["check_coverage.py", str(report)]) == 0


def test_an_explicit_exception_replaces_the_default(monkeypatch, tmp_path):
    checker = _checker()
    monkeypatch.setattr(
        checker,
        "load_floors",
        lambda: (85.0, {"package/module.py": 60.0}),
    )
    report = _report(tmp_path / "coverage.json", 60.0)

    assert checker.main(["check_coverage.py", str(report)]) == 0

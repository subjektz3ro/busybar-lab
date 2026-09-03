"""Per-file coverage floors, because the average hides the risk.

A single repo-wide number is the wrong instrument here. The suite is large
(21k lines of tests against 18k of source) and the aggregate sat at 80% while
`apps/hello.py` sat at 0% and `skystrip.run` — the whole process lifecycle,
including the `finally` that clears the panel — had one covered statement out
of 154. Averaging is exactly what let that happen.

So: every file must meet `default`, and anything that cannot yet is listed
explicitly in `[tool.coverage-floors.exceptions]` with the number it actually
achieves. The list IS the debt register. Adding a file to it is a deliberate
act that shows up in review; letting a file drift below its own floor fails.

    uv run pytest --cov=... --cov-report=json:coverage.json
    uv run scripts/check_coverage.py coverage.json
"""

from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# Floors are strict. Platform-specific or timing-sensitive files get an
# explicit, deliberately rounded-down exception in pyproject.toml; hiding a
# second tolerance here would make the configured number untrue.


def load_floors() -> tuple[float, dict[str, float]]:
    config = tomllib.loads((ROOT / "pyproject.toml").read_text())
    table = config.get("tool", {}).get("coverage-floors", {})
    return float(table.get("default", 0.0)), {
        name: float(value)
        for name, value in table.get("exceptions", {}).items()
    }


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: check_coverage.py COVERAGE_JSON", file=sys.stderr)
        return 2
    report = json.loads(Path(argv[1]).read_text())
    default, exceptions = load_floors()

    failures: list[str] = []
    slipped: list[str] = []
    unused = set(exceptions)
    for name, data in sorted(report["files"].items()):
        statements = data["summary"]["num_statements"]
        if statements == 0:
            continue
        covered = data["summary"]["percent_covered"]
        floor = exceptions.get(name, default)
        unused.discard(name)
        if covered + 1e-9 < floor:
            failures.append(
                f"  {name}: {covered:.1f}% is below its floor of {floor:.1f}%")
        # An exception that has been comfortably beaten is stale: it keeps a
        # file's real standard artificially low for the next change.
        elif name in exceptions and covered >= floor + 10.0:
            slipped.append(
                f"  {name}: {covered:.1f}% now, floor still {floor:.1f}% "
                f"— raise it")

    for name in sorted(unused):
        slipped.append(f"  {name}: listed as an exception but not measured")

    total = report["totals"]["percent_covered"]
    print(f"coverage {total:.1f}% over {report['totals']['num_statements']} "
          f"statements; default floor {default:.1f}%, "
          f"{len(exceptions)} files exempted")

    if slipped:
        print("\nfloors worth raising (not a failure):")
        print("\n".join(slipped))
    if failures:
        print("\ncoverage fell below its floor:")
        print("\n".join(failures))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

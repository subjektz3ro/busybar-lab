"""Create a new bar app the born-visible way, in one command.

    uv run scripts/new_app.py my_idea
    uv run scripts/new_app.py my_idea --description "What it shows"

Copies ``apps/_template.py`` to ``apps/<name>.py``, registers the app in
``apps.toml`` so barkeep can run it, and includes a commented ``[<name>.viz]``
block ready to enable once the app exposes a raster ``render_visual()`` seam.
Existing files and catalog entries are refused, never overwritten; a failed
manifest update rolls the newly created module back.

Names are lowercase Python module names (``[a-z][a-z0-9_]*``) because the
visualizer's declarative registration imports ``apps.<name>``.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import tomllib
import unicodedata
from pathlib import Path

_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
_TEMPLATE_APP_NAME = 'default="my-idea"'


def find_repo_root(start: Path | None = None) -> Path:
    candidate = (start or Path.cwd()).resolve()
    for directory in (candidate, *candidate.parents):
        if (directory / "AGENTS.md").is_file() and (directory / "apps").is_dir():
            return directory
    raise SystemExit("new_app.py must run inside a BUSY Bar Lab checkout")


def _toml_string(value: str) -> str:
    """Encode one UI line as a TOML basic string without injection."""

    if not value or any(
        unicodedata.category(char) in {"Cc", "Cs", "Zl", "Zp"}
        for char in value
    ):
        raise SystemExit(
            "descriptions must be a non-empty single line without control "
            "characters"
        )
    encoded = json.dumps(value, ensure_ascii=False)
    try:
        decoded = tomllib.loads(f"value = {encoded}\n")["value"]
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise SystemExit("description cannot be represented safely in TOML") from exc
    if decoded != value:  # pragma: no cover - defensive against syntax drift
        raise SystemExit("description did not round-trip through TOML")
    return encoded


def catalog_entry(name: str, description: str) -> str:
    encoded_description = _toml_string(description)
    return f"""
[{name}]
kind = "foreground"
entrypoint = "apps/{name}.py"
description = {encoded_description}

# Uncomment once apps/{name}.py exposes a pure zero-argument render_visual()
# returning {{display: (PIL frames, fps)}}. CI separately runs the declared
# audits and checks accepted pixels for drift. See docs/busybar-viz.md.
# [{name}.viz]
# renderer = "apps.{name}:render_visual"
# displays = ["front"]
"""


def _replace_manifest(path: Path, contents: str) -> None:
    """Atomically replace a manifest while preserving its permission bits."""

    mode = path.stat().st_mode & 0o7777
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent,
    )
    temporary_path = Path(temporary_name)
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            descriptor = -1
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary_path.unlink(missing_ok=True)


def create_app(repo_root: Path, name: str, description: str) -> list[str]:
    if not _NAME.fullmatch(name):
        raise SystemExit(
            f"app names are lowercase module names ([a-z][a-z0-9_]*): {name!r}"
        )
    entry = catalog_entry(name, description)

    app_path = repo_root / "apps" / f"{name}.py"
    if app_path.exists() or app_path.is_symlink():
        raise SystemExit(f"refusing to overwrite existing {app_path}")
    manifest_path = repo_root / "apps.toml"
    manifest = manifest_path.read_text(encoding="utf-8")
    try:
        catalog = tomllib.loads(manifest)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"refusing to edit invalid {manifest_path}: {exc}") from exc
    if name in catalog:
        raise SystemExit(f"apps.toml already declares [{name}]")

    template = (repo_root / "apps" / "_template.py").read_text(encoding="utf-8")
    if template.count(_TEMPLATE_APP_NAME) != 1:
        raise SystemExit(
            "apps/_template.py no longer has exactly one default app-name "
            "marker; refusing to create a misnamed app"
        )
    generated = template.replace(
        _TEMPLATE_APP_NAME, f'default="{name}"', 1,
    )
    next_manifest = manifest.rstrip("\n") + "\n" + entry
    try:
        tomllib.loads(next_manifest)
    except tomllib.TOMLDecodeError as exc:  # pragma: no cover - invariant check
        raise SystemExit(f"generated apps.toml would be invalid: {exc}") from exc

    created = False
    try:
        with app_path.open("x", encoding="utf-8") as app_file:
            created = True
            app_file.write(generated)
            app_file.flush()
            os.fsync(app_file.fileno())
    except FileExistsError as exc:
        raise SystemExit(f"refusing to overwrite existing {app_path}") from exc
    except BaseException:
        if created:
            try:
                app_path.unlink()
            except OSError as rollback_error:
                raise RuntimeError(
                    f"apps.toml update failed and {app_path} could not be rolled back"
                ) from rollback_error
        raise
    try:
        _replace_manifest(manifest_path, next_manifest)
    except BaseException:
        try:
            app_path.unlink()
        except OSError as rollback_error:
            raise RuntimeError(
                f"apps.toml update failed and {app_path} could not be rolled back"
            ) from rollback_error
        raise
    return [
        f"created apps/{name}.py and registered [{name}] in apps.toml",
        f"1. uv run apps/{name}.py --dry-run   # no device needed",
        "2. read .claude/skills/busybar-app/SKILL.md before drawing",
        "3. while iterating: uv run busybar-viz view FRAMES.png --json",
        f"4. expose render_visual() and uncomment [{name}.viz] in apps.toml,",
        f"5. uv run busybar-viz run {name}/default --json   # inspect it",
        "6. uv run busybar-viz doctor --json",
        "7. after acceptance: uv run busybar-viz baseline update",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="lowercase module name, e.g. pomodoro")
    parser.add_argument(
        "--description", default="One line on what this app shows",
    )
    args = parser.parse_args(argv)
    for line in create_app(find_repo_root(), args.name, args.description):
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())

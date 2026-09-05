"""Conservative checkout provenance for production-backed visual scenarios.

A launcher is not the renderer's complete source. Include app/helper Python
and shipped raster assets without importing runtime code or reading owner
configuration. This intentionally invalidates artifact identity for some
unrelated source edits; pixel baselines still change only when frames do.
"""

from pathlib import Path


def app_source_paths(repo_root: Path) -> tuple[str, ...]:
    """Inventory checkout code, not a guessed list of renderer dependencies.

    Keep paths logical: ArtifactStore validates containment and hashes each file.
    Never include .env, config/, state/, caches or arbitrary files under assets/.
    """
    paths = {"busybar_viz/sources.py"}
    if (repo_root / "apps.toml").is_file():
        paths.add("apps.toml")
    for directory in ("apps", "busybar_dev"):
        paths.update(
            path.relative_to(repo_root).as_posix()
            for path in (repo_root / directory).rglob("*.py")
            if path.is_file()
        )
    paths.update(
        path.relative_to(repo_root).as_posix()
        for path in (repo_root / "apps/assets").rglob("*")
        if path.is_file() and path.suffix.lower() in {".png", ".gif"}
    )
    return tuple(sorted(paths))

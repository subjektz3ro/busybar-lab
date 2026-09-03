"""Pure configuration and cache-path policy for the DSN app.

This module deliberately does not read ``os.environ`` or load ``.env``.  It
turns an explicit mapping into one immutable value; ``dsn.py`` owns applying
that value to its long-running process state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import math
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent
VIEW_ORDER = ("network", "instrument", "distance")
NETWORK_STYLES = frozenset({"dishes", "skies", "rows"})

_DEFAULT_POLL_S = 10.0
_DEFAULT_ROTATE_S = 20.0
_DEFAULT_VOICE = "af_nova"
_DEFAULT_VIEW = "network"
_DEFAULT_NETWORK_STYLE = "dishes"


@dataclass(frozen=True)
class DsnConfig:
    """Validated process configuration, independent of ambient environment."""

    poll_s: float
    rotate_s: float
    voice: str
    default_view: str
    network_style: str
    managed_cache_root: Path
    cache_dir: Path
    warnings: tuple[str, ...] = ()


def _positive_seconds(
        raw: str | None, default: float, minimum: float = 1.0,
        ) -> float:
    """A UI ``number`` may be fractional or blank; neither may crash-loop."""
    try:
        value = float(raw or default)
    except (TypeError, ValueError):
        value = default
    return max(minimum, value) if math.isfinite(value) else default


def resolve_managed_cache_root(
        raw: str | None, repo_root: Path,
        ) -> tuple[Path, str]:
    """Resolve the service cache allow-list without making startup fallible."""
    default = repo_root / "cache"
    value = (raw or "").strip()
    if not value:
        return default.resolve(), ""
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = repo_root / candidate
        return candidate.resolve(), ""
    except (OSError, RuntimeError, ValueError):
        return default.resolve(), (
            "BUSYBAR_CACHE_DIR is unusable; using the repository cache root "
            f"{default}")


def resolve_cache_dir(
    raw: str | None,
    repo_root: Path,
    *,
    managed_cache_root: Path | None = None,
    managed: bool = False,
) -> tuple[Path, str]:
    """Resolve DSN's cache directory within its runtime write boundary.

    DSN_CACHE_DIR is the only declared config key that reaches the filesystem,
    and every declared key is settable through Barkeep by a caller allowed to
    reach its API. The write itself is narrow (two fixed filenames, contents
    from NASA) but the mkdir was not: a path anywhere the daemon's user can
    write would be created on the next restart.

    A Barkeep-managed child is narrower still: systemd makes the filesystem
    read-only except for the cache root rendered by install.sh. In that mode an
    app-level DSN_CACHE_DIR may select a descendant of the managed root, never
    a path the service cannot write. To place caches on another volume, rerun
    the installer with BUSYBAR_CACHE_DIR pointing at that owner-controlled
    directory; it will render the matching ReadWritePaths override.

    Outside Barkeep, an absolute path outside the repo is honoured only if it
    already exists. That retains the useful manual-run escape hatch without
    allowing a config write to create an arbitrary host directory tree.
    """
    cache_root = (
        managed_cache_root
        if managed_cache_root is not None
        else repo_root / "cache"
    ).expanduser().resolve()
    default = cache_root / "dsn"
    value = (raw or "").strip()
    if not value:
        return default, ""
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute():
            candidate = (
                cache_root / candidate if managed else repo_root / candidate)
        resolved = candidate.resolve()
    except (OSError, RuntimeError, ValueError):
        # ValueError is the embedded-NUL case. Barkeep's API already rejects
        # those, but .env is hand-editable and runtime configuration must not
        # turn one malformed value into a crash loop with the display dark.
        return default, f"DSN_CACHE_DIR {value!r} is unusable; using {default}"
    if managed:
        if resolved == cache_root or cache_root in resolved.parents:
            return resolved, ""
        return default, (
            f"DSN_CACHE_DIR {value!r} is outside the service-managed cache "
            f"root {cache_root}; rerun deploy/install.sh with "
            "BUSYBAR_CACHE_DIR set if that location is intentional. "
            f"Using {default}")
    root = repo_root.resolve()
    if resolved == root or root in resolved.parents:
        return resolved, ""
    if resolved.is_dir():
        return resolved, ""
    return default, (
        f"DSN_CACHE_DIR {value!r} is outside the checkout and does not exist; "
        f"create it first if you meant it. Using {default}")


def parse_runtime_config(
        values: Mapping[str, str], repo_root: Path = REPO_ROOT,
        ) -> DsnConfig:
    """Validate a configuration mapping without reading or mutating process state."""
    view = (values.get("DSN_VIEW") or _DEFAULT_VIEW).strip().lower()
    if view not in VIEW_ORDER:
        view = _DEFAULT_VIEW
    network_style = (
        values.get("DSN_NETWORK_STYLE") or _DEFAULT_NETWORK_STYLE
    ).strip().lower()
    if network_style not in NETWORK_STYLES:
        network_style = _DEFAULT_NETWORK_STYLE
    managed_cache_root, root_warning = resolve_managed_cache_root(
        values.get("BUSYBAR_CACHE_DIR"), repo_root)
    cache_dir, cache_warning = resolve_cache_dir(
        values.get("DSN_CACHE_DIR"),
        repo_root,
        managed_cache_root=managed_cache_root,
        managed=values.get("BARKEEP_MANAGED") == "1",
    )
    return DsnConfig(
        poll_s=_positive_seconds(values.get("DSN_POLL_S"), _DEFAULT_POLL_S),
        rotate_s=_positive_seconds(
            values.get("DSN_ROTATE_S"), _DEFAULT_ROTATE_S),
        voice=values.get("DSN_VOICE") or _DEFAULT_VOICE,
        default_view=view,
        network_style=network_style,
        managed_cache_root=managed_cache_root,
        cache_dir=cache_dir,
        warnings=tuple(
            warning for warning in (root_warning, cache_warning) if warning),
    )


# Constructed lexically so importing this module does not resolve owner paths.
DEFAULT_DSN_CONFIG = DsnConfig(
    poll_s=_DEFAULT_POLL_S,
    rotate_s=_DEFAULT_ROTATE_S,
    voice=_DEFAULT_VOICE,
    default_view=_DEFAULT_VIEW,
    network_style=_DEFAULT_NETWORK_STYLE,
    managed_cache_root=REPO_ROOT / "cache",
    cache_dir=REPO_ROOT / "cache" / "dsn",
)

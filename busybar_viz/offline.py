"""Best-effort network and environment guards for visualizer renders."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
import os
from pathlib import Path
import re
import socket
import tomllib


_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_SENSITIVE_ENV_PREFIXES = ("BUSYBAR_", "BARKEEP_")
_SENSITIVE_ENV_EXACT = frozenset({"SKYSTRIP_LIGHTNING_WS"})
_SENSITIVE_ENV_SUFFIXES = (
    "_API_KEY",
    "_CONTACT",
    "_PASSWORD",
    "_SECRET",
    "_TOKEN",
)
_VIZ_ENVIRONMENT = {
    "BUSYBAR_VIZ": "1",
    "BUSYBAR_VIZ_OFFLINE": "1",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
}


def registered_app_environment(repo_root: Path) -> set[str]:
    """Return config keys whose personal values must not reach a renderer."""

    try:
        document = tomllib.loads((repo_root / "apps.toml").read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return set()
    keys: set[str] = set()
    for app in document.values():
        if not isinstance(app, dict) or not isinstance(app.get("config"), dict):
            continue
        keys.update(key for key in app["config"] if isinstance(key, str))
    return keys


def _dotenv_keys(repo_root: Path) -> set[str]:
    """Read only key names so app-level ``setdefault`` loaders stay inert."""

    path = repo_root / ".env"
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return set()
    keys: set[str] = set()
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key = line.partition("=")[0].strip()
        if _ENV_KEY_RE.fullmatch(key):
            keys.add(key)
    return keys


def scrubbed_environment(
    source: dict[str, str] | None = None,
    *,
    app_keys: set[str] | None = None,
) -> dict[str, str]:
    """Copy an environment while removing app and device credentials."""

    environment = dict(os.environ if source is None else source)
    for key in tuple(environment):
        if (
            key in _SENSITIVE_ENV_EXACT
            or key in (app_keys or set())
            or key.startswith(_SENSITIVE_ENV_PREFIXES)
            or key.endswith(_SENSITIVE_ENV_SUFFIXES)
        ):
            environment.pop(key, None)
    environment.update(_VIZ_ENVIRONMENT)
    return environment


@contextmanager
def network_disabled() -> Iterator[None]:
    """Fail closed when trusted renderer code accidentally opens a socket.

    This is a correctness guard, not an operating-system sandbox.  Registered
    adapters remain trusted repository code, but ordinary HTTP, UDP, and BUSY
    Bar client paths cannot silently turn an offline proof into live I/O.
    """

    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_sendto = socket.socket.sendto
    original_create_connection = socket.create_connection

    def blocked(*_args, **_kwargs):
        raise OSError("network and BUSY Bar access are disabled in busybar-viz renders")

    socket.socket.connect = blocked  # type: ignore[method-assign]
    socket.socket.connect_ex = blocked  # type: ignore[method-assign]
    socket.socket.sendto = blocked  # type: ignore[method-assign]
    socket.create_connection = blocked
    try:
        yield
    finally:
        socket.socket.connect = original_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = original_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = original_sendto  # type: ignore[method-assign]
        socket.create_connection = original_create_connection


@contextmanager
def offline_render(repo_root: Path) -> Iterator[None]:
    """Keep repository ``.env`` loaders from undoing worker sanitization.

    Production modules in this repository intentionally load ``.env`` with
    ``setdefault`` at import or connection time.  A subprocess-only scrub is
    therefore insufficient: an absent secret is immediately restored from
    disk.  Pre-seeding every local key with an empty value keeps those loaders
    inert for the render, while scenario adapters remain responsible for
    installing deterministic non-personal fixture values.  The full process
    environment is restored afterwards for the direct CLI path.

    This context is process-global by design and is used only by a single CLI
    render or inside a one-request worker process.
    """

    repo_root = repo_root.resolve()
    original = dict(os.environ)
    protected = _dotenv_keys(repo_root) | registered_app_environment(repo_root)
    protected.update(
        key for key in original
        if key in _SENSITIVE_ENV_EXACT
        or key.startswith(_SENSITIVE_ENV_PREFIXES)
        or key.endswith(_SENSITIVE_ENV_SUFFIXES)
    )
    for key in protected:
        os.environ[key] = ""
    os.environ.update(_VIZ_ENVIRONMENT)
    try:
        with network_disabled():
            yield
    finally:
        os.environ.clear()
        os.environ.update(original)

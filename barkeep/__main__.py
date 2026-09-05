"""Entry point: wire registry + supervisor + keepalive + web server, run forever.

    uv run -m barkeep            # http://127.0.0.1:8080

Env: BARKEEP_PORT (8080), BARKEEP_BIND (127.0.0.1), BARKEEP_TLS /
BARKEEP_TLS_CERT / BARKEEP_TLS_KEY (HTTPS, off by default),
BUSYBAR_HOST/-TOKEN via .env.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from pathlib import Path

import uvicorn

import busybar_dev
from .configstore import app_env_path, child_env, read_env_file
from .keepalive import radio_keepalive
from .preview import Preview
from .registry import load_registry
from .server import create_app, exposure_warning
from .statestore import load_state
from .supervisor import Supervisor
from .tls import resolve_tls

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / "config"
STATE_PATH = CONFIG_DIR / "barkeep-state.json"
LOGS_DIR = REPO_ROOT / "logs"
DEFAULT_BIND = "127.0.0.1"
DEFAULT_PORT = 8080


log = logging.getLogger("barkeep")


def configured_bind() -> str:
    """Return an explicitly configured bind address or the local-only default.

    Treating an empty value like an unset one keeps a partially filled `.env`
    from widening the service's network exposure by accident.
    """
    return (os.environ.get("BARKEEP_BIND") or "").strip() or DEFAULT_BIND


def configured_port() -> int:
    """Return the listen port, refusing garbage with a named error."""
    raw = (os.environ.get("BARKEEP_PORT") or "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        raise ValueError(
            f"BARKEEP_PORT={raw!r} is not a port number") from None


async def restore_desired(supervisor, registry, desired) -> None:
    """Start what the operator last chose, skipping what no longer exists.

    Desired state is validated against the live registry: an app renamed or
    dropped from apps.toml must never keep the daemon down, because the web UI
    is the only way to fix it on a headless Pi — and config/ survives
    ship.sh's git reset, so a bad entry would persist across deploys.
    """
    fg = desired.foreground
    spec = registry.get(fg) if fg else None
    if fg and (spec is None or spec.kind != "foreground"):
        log.warning("saved foreground %r is not a registered foreground app; "
                    "starting on standby", fg)
        fg = None
    try:
        await supervisor.set_foreground(fg)
    except (KeyError, ValueError):
        log.exception("could not restore foreground %r", fg)

    for name in sorted(desired.enabled_backgrounds):
        bg = registry.get(name)
        if bg is None or bg.kind != "background":
            log.warning("saved background %r is not registered; leaving it off",
                        name)
            continue
        try:
            await supervisor.enable(name)
        except (KeyError, ValueError):
            log.exception("could not enable %r", name)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    busybar_dev.load_env()

    registry = load_registry(REPO_ROOT / "apps.toml")

    def env_for(name: str) -> dict[str, str]:
        spec = registry[name]
        env = child_env(
            read_env_file(app_env_path(CONFIG_DIR, spec)), os.environ,
            allowed_keys={key.name for key in spec.config},
        )
        env["BARKEEP_MANAGED"] = "1"
        return env

    supervisor = Supervisor(registry, REPO_ROOT, LOGS_DIR, env_for)

    # First boot (no state file) is deliberately STANDBY. The page presents
    # Skystrip's provider limits and linked credits before an operator selects
    # it and starts network polling. A saved foreground still restores exactly
    # as before, so upgrades do not silently dim an established installation.
    desired = load_state(STATE_PATH)

    app = create_app(supervisor, registry, Preview(), CONFIG_DIR, STATE_PATH)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        keeper = None
        if os.environ.get("BARKEEP_KEEPALIVE") == "1":
            # Off by default: the 5Hz ping helped an idle bar at 1am but was
            # measured HARMFUL under load (max latency 10.3s with it vs 475ms
            # without), which is why skystrip dropped it. Opt in only to A/B it.
            keeper = asyncio.create_task(
                radio_keepalive(os.environ.get("BUSYBAR_HOST")))
        try:
            await restore_desired(supervisor, registry, desired)
            yield
        finally:
            if keeper is not None:
                keeper.cancel()
                # CancelledError is a BaseException: suppress(Exception) alone
                # would let it escape and skip the shutdown below, orphaning
                # every app child.
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await keeper
            await supervisor.shutdown()

    app.router.lifespan_context = lifespan

    # Network access is opt-in. An operator who deliberately chooses a
    # non-loopback bind still gets the warning below when no token is set.
    bind = configured_bind()
    warning = exposure_warning(bind, (os.environ.get("BARKEEP_TOKEN") or "").strip())
    if warning:
        log.warning("%s", warning)
    tls = resolve_tls(CONFIG_DIR / "tls")
    if tls:
        log.info("serving HTTPS with certificate %s", tls[0])
    uvicorn.run(app,
                host=bind,
                port=configured_port(),
                log_level="warning",
                ssl_certfile=str(tls[0]) if tls else None,
                ssl_keyfile=str(tls[1]) if tls else None)


if __name__ == "__main__":
    main()

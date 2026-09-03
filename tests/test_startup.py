"""Startup must never be able to keep the daemon down.

config/barkeep-state.json survives ship.sh's `git reset --hard`, so a saved
app that apps.toml no longer registers would otherwise raise inside the
lifespan — and on a headless Pi the web UI is the only way to fix it.
"""

import asyncio
from types import SimpleNamespace

import pytest

import barkeep.__main__ as entrypoint
from barkeep.__main__ import restore_desired
from barkeep.registry import AppSpec
from barkeep.statestore import DesiredState, load_state

REGISTRY = {
    "sky": AppSpec("sky", "foreground", "apps/sky.py", "the sky"),
    "pinger": AppSpec("pinger", "background", "apps/pinger.py", "pings"),
}


class FakeSupervisor:
    def __init__(self):
        self.foreground = None
        self.enabled = set()

    async def set_foreground(self, name):
        if name is not None and name not in REGISTRY:
            raise KeyError(name)
        self.foreground = name

    async def enable(self, name):
        if name not in REGISTRY:
            raise KeyError(name)
        self.enabled.add(name)


async def test_restores_a_valid_lineup():
    sup = FakeSupervisor()
    await restore_desired(sup, REGISTRY, DesiredState("sky", {"pinger"}))
    assert sup.foreground == "sky"
    assert sup.enabled == {"pinger"}


async def test_unregistered_foreground_starts_on_standby():
    sup = FakeSupervisor()
    await restore_desired(sup, REGISTRY, DesiredState("deleted-app", set()))
    assert sup.foreground is None          # started, not wedged


async def test_kind_flip_is_ignored_not_fatal():
    sup = FakeSupervisor()
    # sky is a foreground app; a state file calling it a background must not
    # raise ValueError out of startup.
    await restore_desired(sup, REGISTRY, DesiredState(None, {"sky"}))
    assert sup.enabled == set()


async def test_unregistered_background_is_skipped():
    sup = FakeSupervisor()
    await restore_desired(sup, REGISTRY, DesiredState("sky", {"pinger", "ghost"}))
    assert sup.foreground == "sky"
    assert sup.enabled == {"pinger"}       # the good one still came up


@pytest.mark.parametrize("body", [
    '{"foreground": "sky", "enabled_backgrounds": "pinger"}',   # str, not list
    '{"foreground": 42, "enabled_backgrounds": []}',            # non-str name
])
def test_malformed_state_falls_back_to_defaults(tmp_path, body):
    path = tmp_path / "s.json"
    path.write_text(body)
    assert load_state(path, default_foreground="sky") == DesiredState("sky", set())


class WiredSupervisor:
    def __init__(self, registry, repo_root, logs_dir, env_for):
        self.registry = registry
        self.repo_root = repo_root
        self.logs_dir = logs_dir
        self.child_env = env_for("sky")
        self.foreground = None
        self.enabled = set()
        self.shutdown_calls = 0

    async def set_foreground(self, name):
        self.foreground = name

    async def enable(self, name):
        self.enabled.add(name)

    async def shutdown(self):
        self.shutdown_calls += 1

    def enabled_backgrounds(self):
        return set(self.enabled)


def _wire_main(monkeypatch, tls_env=None):
    captured = {}
    app = SimpleNamespace(router=SimpleNamespace(lifespan_context=None))

    def build_supervisor(*args):
        supervisor = WiredSupervisor(*args)
        captured["supervisor"] = supervisor
        return supervisor

    def fake_load_state(path, default_foreground=None):
        captured["state_path"] = path
        captured["default_foreground"] = default_foreground
        return DesiredState("sky", {"pinger"})

    def fake_create_app(supervisor, registry, preview, config_dir, state_path):
        captured["create_app"] = (
            supervisor,
            registry,
            preview,
            config_dir,
            state_path,
        )
        return app

    def fake_run(server_app, **kwargs):
        captured["uvicorn"] = (server_app, kwargs)

    monkeypatch.setattr(entrypoint.busybar_dev, "load_env", lambda: captured.setdefault("loaded_env", True))
    monkeypatch.setattr(entrypoint, "load_registry", lambda _path: REGISTRY)
    monkeypatch.setattr(entrypoint, "read_env_file", lambda _path: {"APP_ONLY": "yes"})
    monkeypatch.setattr(entrypoint, "Supervisor", build_supervisor)
    monkeypatch.setattr(entrypoint, "load_state", fake_load_state)
    monkeypatch.setattr(entrypoint, "Preview", lambda: "preview")
    monkeypatch.setattr(entrypoint, "create_app", fake_create_app)
    monkeypatch.setattr(entrypoint, "exposure_warning", lambda _bind, _token: "test warning")
    monkeypatch.setattr(entrypoint.uvicorn, "run", fake_run)
    monkeypatch.setenv("BASE_VALUE", "shared")
    monkeypatch.setenv("BARKEEP_BIND", "127.0.0.2")
    monkeypatch.setenv("BARKEEP_PORT", "9090")
    monkeypatch.setenv("BARKEEP_TOKEN", "test-token")
    for var in ("BARKEEP_TLS", "BARKEEP_TLS_CERT", "BARKEEP_TLS_KEY"):
        monkeypatch.delenv(var, raising=False)
    for var, value in (tls_env or {}).items():
        monkeypatch.setenv(var, value)

    entrypoint.main()
    captured["app"] = app
    return captured


def test_main_wires_registry_config_and_uvicorn(monkeypatch):
    captured = _wire_main(monkeypatch)
    supervisor = captured["supervisor"]

    assert captured["loaded_env"] is True
    assert captured["default_foreground"] is None
    # A fresh checkout has no desired-state file. It must reach the UI on
    # STANDBY so Skystrip's provider limits are visible before polling starts.
    assert supervisor.child_env["BASE_VALUE"] == "shared"
    assert supervisor.child_env["APP_ONLY"] == "yes"
    assert supervisor.child_env["BARKEEP_MANAGED"] == "1"
    assert captured["create_app"][2] == "preview"
    assert captured["app"].router.lifespan_context is not None
    assert captured["uvicorn"] == (
        captured["app"],
        {"host": "127.0.0.2", "port": 9090, "log_level": "warning",
         "ssl_certfile": None, "ssl_keyfile": None},
    )


def test_main_serves_https_when_tls_is_configured(monkeypatch, tmp_path):
    cert = tmp_path / "own.crt"
    key = tmp_path / "own.key"
    cert.write_text("cert")
    key.write_text("key")

    captured = _wire_main(monkeypatch, tls_env={
        "BARKEEP_TLS_CERT": str(cert),
        "BARKEEP_TLS_KEY": str(key),
    })

    kwargs = captured["uvicorn"][1]
    assert kwargs["ssl_certfile"] == str(cert)
    assert kwargs["ssl_keyfile"] == str(key)


async def test_lifespan_restores_state_and_shuts_down(monkeypatch):
    captured = _wire_main(monkeypatch)
    app = captured["app"]
    supervisor = captured["supervisor"]

    async with app.router.lifespan_context(app):
        assert supervisor.foreground == "sky"
        assert supervisor.enabled == {"pinger"}
        assert supervisor.shutdown_calls == 0

    assert supervisor.shutdown_calls == 1


async def test_lifespan_cancels_the_opt_in_keepalive(monkeypatch):
    started = asyncio.Event()
    calls = []

    async def fake_keepalive(host):
        calls.append(("start", host))
        started.set()
        try:
            await asyncio.Future()
        finally:
            calls.append(("stop", host))

    monkeypatch.setenv("BARKEEP_KEEPALIVE", "1")
    monkeypatch.setenv("BUSYBAR_HOST", "192.0.2.1")
    monkeypatch.setattr(entrypoint, "radio_keepalive", fake_keepalive)
    captured = _wire_main(monkeypatch)
    app = captured["app"]

    async with app.router.lifespan_context(app):
        await asyncio.wait_for(started.wait(), 1)
        assert calls == [("start", "192.0.2.1")]

    assert calls == [
        ("start", "192.0.2.1"),
        ("stop", "192.0.2.1"),
    ]
    assert captured["supervisor"].shutdown_calls == 1


async def test_restore_errors_are_logged_but_do_not_abort_startup():
    class RejectingSupervisor(FakeSupervisor):
        async def set_foreground(self, name):
            raise KeyError(name)

        async def enable(self, name):
            raise ValueError(name)

    await restore_desired(
        RejectingSupervisor(),
        REGISTRY,
        DesiredState("sky", {"pinger"}),
    )

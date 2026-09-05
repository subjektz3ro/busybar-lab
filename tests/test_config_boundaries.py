"""Config and authentication failures must stop before persistence or spawn."""

from concurrent.futures import ThreadPoolExecutor, TimeoutError
from threading import Event

import pytest
from fastapi.testclient import TestClient

from barkeep.configstore import child_env, parse_env_text, read_env_file, write_env_file
from barkeep.config_service import prepare_config_update
from barkeep import configstore, server
from barkeep.registry import AppSpec, ConfigKey
from barkeep.server import create_app
from barkeep.supervisor import Supervisor


SPEC = AppSpec("example", "foreground", "apps/example.py", "Example", (
    ConfigKey("EXAMPLE_TEXT", "Text", "ready"),
    ConfigKey("EXAMPLE_RATE", "Rate", "10", "number"),
))
SEPARATORS = ("\v", "\f", "\x1c", "\x1d", "\x1e", "\x85", "\u2028", "\u2029")


def client_at(config_dir):
    registry = {SPEC.name: SPEC}
    supervisor = Supervisor(registry, config_dir, config_dir, lambda _name: {})
    app = create_app(supervisor, registry, object(), config_dir,
                     config_dir / "state.json")
    return TestClient(app, base_url="http://127.0.0.1", raise_server_exceptions=False)


@pytest.mark.parametrize("separator", SEPARATORS)
def test_override_writer_rejects_every_parser_line_separator(tmp_path, separator):
    path = tmp_path / "example.env"
    original = {"EXAMPLE_TEXT": "previous"}
    write_env_file(path, original)
    with pytest.raises(ValueError, match="single-line"):
        write_env_file(path, {"EXAMPLE_TEXT": "next" + separator + "PYTHONPATH=injected"})
    assert read_env_file(path) == original


@pytest.mark.parametrize("separator", SEPARATORS)
def test_api_cannot_persist_an_undeclared_env_line(tmp_path, separator):
    client = client_at(tmp_path)
    response = client.put("/api/apps/example/config", json={
        "values": {"EXAMPLE_TEXT": "next" + separator + "PYTHONPATH=injected"},
    })
    assert response.status_code == 422
    assert not (tmp_path / "example.env").exists()


def test_non_ascii_login_is_rejected_without_a_server_error(tmp_path, monkeypatch):
    monkeypatch.setenv("BARKEEP_TOKEN", "expected-test-token")
    response = client_at(tmp_path).post("/api/session", json={"token": "wrong\u2603"})
    assert response.status_code == 401
    assert "set-cookie" not in response.headers


@pytest.mark.parametrize("value", [[], {}, True, 42])
def test_foreground_requires_a_name_or_null(tmp_path, value):
    response = client_at(tmp_path).post("/api/foreground", json={"app": value})
    assert response.status_code == 422
    assert not (tmp_path / "state.json").exists()


def test_overlapping_config_updates_preserve_both_edits(tmp_path, monkeypatch):
    first_entered, release_first = Event(), Event()
    original_write = configstore.write_env_file

    def delayed_write(path, values):
        if not first_entered.is_set():
            first_entered.set()
            assert release_first.wait(5), "first writer was never released"
        original_write(path, values)

    monkeypatch.setattr(configstore, "write_env_file", delayed_write)
    # The old route imported its writer; the service calls the storage module.
    monkeypatch.setattr(server, "write_env_file", delayed_write, raising=False)
    client = client_at(tmp_path)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(client.put, "/api/apps/example/config", json={
            "values": {"EXAMPLE_TEXT": "first"},
        })
        try:
            assert first_entered.wait(5), "first request never reached persistence"
            second = pool.submit(client.put, "/api/apps/example/config", json={
                "values": {"EXAMPLE_RATE": "20"},
            })
            try:
                second.result(timeout=0.5)
            except TimeoutError:
                pass  # The serialized service waits for the first transaction.
        finally:
            release_first.set()
        assert first.result(timeout=5).status_code == 200
        assert second.result(timeout=5).status_code == 200
    assert read_env_file(tmp_path / "example.env") == {
        "EXAMPLE_TEXT": "first", "EXAMPLE_RATE": "20",
    }


def test_spawn_filters_undeclared_keys_in_existing_override_files():
    existing = parse_env_text("EXAMPLE_TEXT=hello\u2028PYTHONPATH=injected\nREMOVED_KEY=old\n")
    base = {"PATH": "system-path", "PYTHONPATH": "owner-managed"}
    env = child_env(existing, base, allowed_keys={key.name for key in SPEC.config})
    assert env == {**base, "EXAMPLE_TEXT": "hello"}
    assert base["PYTHONPATH"] == "owner-managed"


def test_spawn_rejects_invalid_direct_values():
    env = child_env({"EXAMPLE_TEXT": "bad\0value"}, {"EXAMPLE_TEXT": "safe"},
                    allowed_keys={"EXAMPLE_TEXT"})
    assert env == {"EXAMPLE_TEXT": "safe"}


def test_failed_candidate_leaves_input_layers_untouched():
    current = {"EXAMPLE_TEXT": "previous", "EXAMPLE_RATE": "20"}
    submitted = {"EXAMPLE_TEXT": "new", "EXAMPLE_RATE": "nan"}
    shared = {"EXAMPLE_RATE": "10"}
    with pytest.raises(ValueError, match="finite"):
        prepare_config_update(SPEC, submitted, current, shared)
    assert current == {"EXAMPLE_TEXT": "previous", "EXAMPLE_RATE": "20"}
    assert submitted == {"EXAMPLE_TEXT": "new", "EXAMPLE_RATE": "nan"}
    assert shared == {"EXAMPLE_RATE": "10"}

"""First-run failures must produce bounded, private, actionable diagnostics."""

from __future__ import annotations

import json
import logging
import os
import shutil
import ssl
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import httpx2
import pytest
from busylib import BusyBar
from busylib.exceptions import BusyBarAPIError

import busybar_dev
from barkeep.tls import resolve_tls
from deploy import check_setup as diagnostic
from deploy.setup_config import Finding, check_config, settings, web_target

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def config_root(tmp_path):
    shutil.copy2(ROOT / "apps.toml", tmp_path / "apps.toml")
    return tmp_path


@pytest.fixture(autouse=True)
def preserve_logging():
    previous = logging.root.manager.disable
    yield
    logging.disable(previous)


def test_settings_follow_runtime_precedence_without_mutating_environment(config_root):
    (config_root / ".env").write_text(
        'BARKEEP_PORT="9090"\nSKYSTRIP_TZ=Europe/London\n'
    )
    environ = {"BARKEEP_PORT": "8081"}
    loaded = settings(config_root, environ)
    assert loaded["BARKEEP_PORT"] == "8081"
    assert loaded["SKYSTRIP_TZ"] == "Europe/London"
    assert environ == {"BARKEEP_PORT": "8081"}


def test_missing_location_does_not_block_a_dsn_only_install(config_root):
    results = check_config(config_root, {})
    assert not any(r.level == "FAIL" for r in results)
    assert any("DSN needs no location" in r.message for r in results)


@pytest.mark.parametrize(
    "values,key",
    [
        ({"SKYSTRIP_LAT": "51.4769"}, "SKYSTRIP_LAT"),
        ({"SKYSTRIP_LAT": "nan", "SKYSTRIP_LON": "0.0005"}, "SKYSTRIP_LAT"),
        ({"SKYSTRIP_LAT": "91", "SKYSTRIP_LON": "0.0005"}, "SKYSTRIP_LAT"),
        ({"SKYSTRIP_TZ": "private-invalid-timezone"}, "SKYSTRIP_TZ"),
        ({"SKYSTRIP_UNITS": "private-invalid-units"}, "SKYSTRIP_UNITS"),
        ({"BARKEEP_PORT": "private-invalid-port"}, "BARKEEP_PORT"),
        ({"BARKEEP_PORT": "65536"}, "BARKEEP_PORT"),
        ({"BARKEEP_BIND": "http://private.example/path"}, "BARKEEP_BIND"),
        ({"BUSYBAR_HOST": "http://operator:secret@device.example/api"}, "BUSYBAR_HOST"),
        ({"BUSYBAR_HOST": "device.example:invalid"}, "BUSYBAR_HOST"),
        ({"BARKEEP_TLS": "private-invalid-mode"}, "BARKEEP_TLS"),
        ({"BARKEEP_TLS_CERT": "private-missing-file"}, "BARKEEP_TLS_KEY"),
    ],
)
def test_invalid_existing_settings_name_the_key_without_echoing_values(
    config_root, values, key
):
    results = check_config(config_root, values)
    failures = " ".join(r.message for r in results if r.level == "FAIL")
    assert key in failures
    assert "private-" not in failures
    assert "operator:secret" not in failures


def test_skystrip_app_overrides_match_the_real_child_configuration(config_root):
    (config_root / "config").mkdir()
    (config_root / "config" / "skystrip.env").write_text("SKYSTRIP_TZ=Europe/London\n")
    assert not any(
        r.level == "FAIL"
        for r in check_config(config_root, {"SKYSTRIP_TZ": "invalid-private"})
    )
    (config_root / "config" / "skystrip.env").write_text(
        "SKYSTRIP_TZ=invalid-private\n"
    )
    assert any(
        r.level == "FAIL"
        for r in check_config(config_root, {"SKYSTRIP_TZ": "Europe/London"})
    )


@pytest.mark.parametrize(
    "values,url,label",
    [
        ({}, "http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ({"BARKEEP_PORT": "9090"}, "http://127.0.0.1:9090", "http://127.0.0.1:9090"),
        ({"BARKEEP_BIND": "0.0.0.0"}, "http://127.0.0.1:8080", "http://127.0.0.1:8080"),
        ({"BARKEEP_BIND": "::"}, "http://[::1]:8080", "http://[::1]:8080"),
        (
            {"BARKEEP_BIND": "server.example."},
            "http://server.example.:8080",
            "http://<BARKEEP_BIND>:8080",
        ),
        (
            {"BARKEEP_BIND": "server.example"},
            "http://server.example:8080",
            "http://<BARKEEP_BIND>:8080",
        ),
    ],
)
def test_browser_target_uses_configured_bind_and_port(config_root, values, url, label):
    target = web_target(config_root, values)
    assert target.url == url
    assert target.public_label == label


def test_tls_check_does_not_generate_keys_or_weaken_certificate_validation(
    config_root, monkeypatch
):
    monkeypatch.setattr(
        "deploy.setup_config.shutil.which", lambda _name: "/usr/bin/openssl"
    )
    target = web_target(config_root, {"BARKEEP_TLS": "1", "BARKEEP_PORT": "8443"})
    assert target.url == "https://127.0.0.1:8443"
    assert not (config_root / "config").exists()


def test_incomplete_operator_certificate_is_not_misreported_as_http(config_root):
    directory = config_root / "config" / "tls"
    directory.mkdir(parents=True)
    (directory / "barkeep-operator.crt").write_text("not a certificate")
    results = check_config(config_root, {})
    assert any(r.level == "FAIL" and "incomplete" in r.message for r in results)


def test_device_probe_only_opens_and_closes_a_connection(monkeypatch):
    bar = Mock()
    manager = Mock(__enter__=Mock(return_value=bar), __exit__=Mock(return_value=False))
    connect = Mock(return_value=manager)
    monkeypatch.setattr(diagnostic, "connect", connect)
    result = diagnostic.device_probe({})
    assert result.level == "PASS"
    connect.assert_called_once_with()
    assert not bar.mock_calls  # No extra GET, draw, clear, brightness or audio.
    manager.__exit__.assert_called_once()


@pytest.mark.parametrize("code", [401, 403])
def test_device_authentication_is_diagnosed_from_structured_error_not_secret_text(
    monkeypatch, code
):
    cause = BusyBarAPIError("sensitive-response", status_code=code)
    failure = ConnectionError("sensitive-host-and-token")
    failure.__cause__ = cause
    monkeypatch.setattr(diagnostic, "connect", Mock(side_effect=failure))
    result = diagnostic.device_probe({"BUSYBAR_HOST": "device.example"})
    assert result.level == "FAIL"
    assert "denied API access" in result.message
    assert "BUSYBAR_TOKEN" in result.message
    assert "sensitive" not in result.message


@pytest.mark.parametrize(
    "values,expected",
    [({}, "USB data cable"), ({"BUSYBAR_HOST": "device.example"}, "configured bar")],
)
def test_device_transport_failure_has_the_right_connection_advice(
    monkeypatch, values, expected
):
    monkeypatch.setattr(
        diagnostic, "connect", Mock(side_effect=ConnectionError("sensitive-url"))
    )
    result = diagnostic.device_probe(values)
    assert result.level == "FAIL"
    assert expected in result.message
    assert "sensitive-url" not in result.message


def web_with(monkeypatch, handler):
    real_client = httpx.Client
    captured = {}
    elapsed = [0.0]

    def factory(**kwargs):
        captured.update(kwargs)
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(diagnostic.httpx, "Client", factory)
    monkeypatch.setattr(
        diagnostic.time,
        "sleep",
        lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds),
    )
    monkeypatch.setattr(diagnostic.time, "monotonic", lambda: elapsed[0])
    return captured


def test_web_probe_reads_actual_barkeep_page_without_sending_credentials(
    config_root, monkeypatch
):
    requests = []

    def serve(request):
        requests.append(request)
        return httpx.Response(
            200, content=(ROOT / "barkeep/static/index.html").read_bytes()
        )

    options = web_with(monkeypatch, serve)
    result = diagnostic.web_probe(
        config_root, {"BARKEEP_PORT": "9090", "BARKEEP_TOKEN": "private-token"}
    )
    assert result.level == "PASS"
    assert str(requests[0].url) == "http://127.0.0.1:9090/"
    assert requests[0].method == "GET"
    assert "authorization" not in requests[0].headers
    assert "cookie" not in requests[0].headers
    assert options["follow_redirects"] is False
    assert options["trust_env"] is False
    assert options["verify"].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize(
    "status,body,expected",
    [(200, b"other app", "Another page"), (302, b"redirect", "did not serve")],
)
def test_web_probe_rejects_another_service_or_redirect(
    config_root, monkeypatch, status, body, expected
):
    web_with(monkeypatch, lambda _request: httpx.Response(status, content=body))
    result = diagnostic.web_probe(config_root, {})
    assert result.level == "FAIL"
    assert expected in result.message


def test_web_probe_retries_startup_then_reports_an_unreachable_ui(
    config_root, monkeypatch
):
    calls = []

    def refuse(request):
        calls.append(request)
        raise httpx.ConnectError("sensitive-address")

    web_with(monkeypatch, refuse)
    result = diagnostic.web_probe(config_root, {})
    assert 3 < len(calls) <= 32
    assert result.level == "FAIL"
    assert "sensitive" not in result.message


def test_web_probe_allows_a_slow_service_to_finish_starting(config_root, monkeypatch):
    calls = []

    def serve(request):
        calls.append(request)
        if len(calls) < 9:
            raise httpx.ConnectError("still starting")
        return httpx.Response(
            200, content=(ROOT / "barkeep/static/index.html").read_bytes()
        )

    web_with(monkeypatch, serve)
    assert diagnostic.web_probe(config_root, {}).level == "PASS"
    assert len(calls) == 9


def test_probe_worker_timeout_is_bounded_and_does_not_echo_output(
    config_root, monkeypatch
):
    def timeout(command, **kwargs):
        assert kwargs["timeout"] == diagnostic.PROBE_TIMEOUT_S
        assert kwargs["cwd"] == config_root
        raise subprocess.TimeoutExpired(command, 10, output="sensitive-output")

    monkeypatch.setattr(diagnostic.subprocess, "run", timeout)
    result = diagnostic.run_probe("device", config_root, {})
    assert result.level == "FAIL"
    assert "timed out" in result.message
    assert "sensitive" not in result.message


def test_subprocess_timeout_really_terminates_a_worker(config_root, monkeypatch):
    # The child announces readiness via a file, then blocks without hardware.
    package = config_root / "deploy"
    package.mkdir()
    (package / "check_setup.py").write_text(
        "import pathlib, time\npathlib.Path('started').write_text('yes')\ntime.sleep(20)\n"
    )
    monkeypatch.setattr(diagnostic, "PROBE_TIMEOUT_S", 1.0)
    result = diagnostic.run_probe("device", config_root, os.environ)
    assert (config_root / "started").exists()
    assert result.level == "FAIL"
    assert "timed out after 1s" in result.message


@pytest.mark.parametrize(
    "response",
    [
        SimpleNamespace(returncode=1, stdout="private"),
        SimpleNamespace(returncode=0, stdout="private"),
        SimpleNamespace(returncode=0, stdout="[]"),
    ],
)
def test_worker_protocol_failures_never_leak_raw_output(
    config_root, monkeypatch, response
):
    monkeypatch.setattr(diagnostic.subprocess, "run", Mock(return_value=response))
    result = diagnostic.run_probe("web", config_root, {})
    assert result.level == "FAIL"
    assert "private" not in result.message.replace("private values", "")


def test_config_only_cli_never_runs_a_network_probe(config_root, monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "ROOT", config_root)
    monkeypatch.setattr(diagnostic, "settings", lambda _root, _env: {})
    probe = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(diagnostic, "run_probe", probe)
    assert diagnostic.main(["--config-only"]) == 0
    probe.assert_not_called()
    assert "Configuration checks passed" in capsys.readouterr().out


def test_cli_reports_both_links_independently(config_root, monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "ROOT", config_root)
    monkeypatch.setattr(
        diagnostic, "settings", lambda _root, _env: {"BARKEEP_PORT": "9090"}
    )
    probe = Mock(
        side_effect=[Finding("FAIL", "bar unavailable"), Finding("PASS", "web ready")]
    )
    monkeypatch.setattr(diagnostic, "run_probe", probe)
    assert diagnostic.main([]) == 1
    output = capsys.readouterr().out
    assert "[FAIL] bar unavailable" in output
    assert "[PASS] web ready" in output
    assert "http://127.0.0.1:9090" in output
    assert probe.call_count == 2


def test_worker_emits_only_a_structured_finding(config_root, monkeypatch, capsys):
    monkeypatch.setattr(diagnostic, "ROOT", config_root)
    monkeypatch.setattr(diagnostic, "settings", lambda _root, _env: {})
    monkeypatch.setattr(
        diagnostic, "device_probe", lambda _env: Finding("PASS", "device ready")
    )
    assert diagnostic.main(["--worker", "device"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "level": "PASS",
        "message": "device ready",
    }


@pytest.mark.parametrize("key", ["BUSYBAR_TOKEN", "BARKEEP_TOKEN"])
@pytest.mark.parametrize("value", ["secret\r\ninjected", "secret\0", "secret\u2603"])
def test_invalid_header_tokens_are_rejected_without_echo(config_root, key, value):
    messages = " ".join(r.message for r in check_config(config_root, {key: value}))
    assert f"{key} must be single-line ASCII" in messages
    assert "secret" not in messages


def test_network_exposure_warning_does_not_echo_bind_or_token(config_root):
    values = {"BARKEEP_BIND": "private-server.example"}
    results = check_config(config_root, values)
    assert any("without a login token" in r.message for r in results)
    assert "private-server" not in str(results)
    values["BARKEEP_TOKEN"] = "private-token"
    assert not any(
        "without a login token" in r.message for r in check_config(config_root, values)
    )


def test_missing_timezone_and_invalid_optional_lightning_are_warnings(config_root):
    results = check_config(
        config_root,
        {
            "SKYSTRIP_LAT": "51.4769",
            "SKYSTRIP_LON": "0.0005",
            "SKYSTRIP_LIGHTNING_WS": "http://private-secret.example/secret",
        },
    )
    assert not any(r.level == "FAIL" for r in results)
    assert any("will use UTC" in r.message for r in results)
    assert any("live lightning is disabled" in r.message for r in results)
    assert "private-secret" not in str(results)


def test_missing_openssl_is_actionable_before_service_start(config_root, monkeypatch):
    monkeypatch.setattr("deploy.setup_config.shutil.which", lambda _name: None)
    results = check_config(config_root, {"BARKEEP_TLS": "1"})
    assert any(r.level == "FAIL" and "Install openssl" in r.message for r in results)
    assert not (config_root / "config").exists()


def test_invalid_explicit_tls_pair_never_echoes_paths(config_root):
    results = check_config(
        config_root,
        {
            "BARKEEP_TLS_CERT": "private-cert",
            "BARKEEP_TLS_KEY": "private-key",
        },
    )
    assert any(
        r.level == "FAIL" and "unreadable or invalid" in r.message for r in results
    )
    assert "private-cert" not in str(results)
    assert "private-key" not in str(results)


@pytest.fixture
def tls_pair(config_root, monkeypatch):
    if shutil.which("openssl") is None:
        pytest.skip("TLS integration requires openssl")
    monkeypatch.delenv("BARKEEP_TLS_CERT", raising=False)
    monkeypatch.delenv("BARKEEP_TLS_KEY", raising=False)
    monkeypatch.setenv("BARKEEP_TLS", "1")
    return resolve_tls(config_root / "config/tls")


@pytest.mark.parametrize("mode", ["generated", "operator", "explicit"])
def test_tls_diagnostic_accepts_real_runtime_certificates_without_mutating_them(
    config_root, monkeypatch, tls_pair, mode
):
    cert, key = tls_pair
    values = {"BARKEEP_TLS": "1"}
    if mode == "operator":
        cert = cert.rename(cert.with_name("barkeep-operator.crt"))
        key = key.rename(key.with_name("barkeep-operator.key"))
        values = {}  # An uploaded pair enables HTTPS without the flag.
    elif mode == "explicit":
        values.update(BARKEEP_TLS_CERT=str(cert), BARKEEP_TLS_KEY=str(key))
        # Explicit credentials outrank an incomplete uploaded pair.
        (cert.parent / "barkeep-operator.crt").write_text("invalid")
    before = [
        (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in (cert, key)
    ]
    options = web_with(
        monkeypatch,
        lambda _r: httpx.Response(
            200, content=(ROOT / "barkeep/static/index.html").read_bytes()
        ),
    )
    assert diagnostic.web_probe(config_root, values).level == "PASS"
    assert web_target(config_root, values).certificate == cert
    context = options["verify"]
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert context.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert context.cert_store_stats()["x509"] > 0
    # Complete a real TLS handshake in memory: no socket or live service.
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    client_in, client_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = context.wrap_bio(client_in, client_out, server_hostname="127.0.0.1")
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(10):
        for name, peer in (("client", client), ("server", server)):
            try:
                peer.do_handshake()
                completed.add(name)
            except ssl.SSLWantReadError:
                pass
        server_in.write(client_out.read())
        client_in.write(server_out.read())
        if len(completed) == 2:
            break
    assert completed == {"client", "server"}
    assert before == [
        (p.read_bytes(), p.stat().st_mode, p.stat().st_mtime_ns) for p in (cert, key)
    ]


def test_device_probe_wire_contract_is_only_get_version(monkeypatch):
    requests = []

    def serve(request):
        requests.append(request)
        return httpx2.Response(200, json={"api_semver": "1.0.0"})

    monkeypatch.setattr(
        busybar_dev,
        "BusyBar",
        lambda host, **kw: BusyBar(host, transport=httpx2.MockTransport(serve), **kw),
    )
    monkeypatch.setattr(busybar_dev, "load_env", lambda: {})
    monkeypatch.setenv("BUSYBAR_HOST", "device.example")
    monkeypatch.delenv("BUSYBAR_TOKEN", raising=False)
    assert diagnostic.device_probe({"BUSYBAR_HOST": "device.example"}).level == "PASS"
    assert [(r.method, r.url.path) for r in requests] == [("GET", "/api/version")]


@pytest.mark.parametrize("args", [[], ["--device-only"], ["--web-only"]])
def test_invalid_config_stops_before_any_network_probe(
    config_root, monkeypatch, capsys, args
):
    monkeypatch.setattr(diagnostic, "ROOT", config_root)
    monkeypatch.setattr(
        diagnostic, "settings", lambda *_: {"BARKEEP_PORT": "bad-private-port"}
    )
    probe = Mock(side_effect=AssertionError("network forbidden"))
    monkeypatch.setattr(diagnostic, "run_probe", probe)
    assert diagnostic.main(args) == 1
    probe.assert_not_called()
    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "bad-private-port" not in output


@pytest.mark.parametrize(
    "failure", [OSError("private-path"), RuntimeError("private-value")]
)
def test_cli_sanitizes_unexpected_failures_and_restores_logging(
    monkeypatch, capsys, failure
):
    monkeypatch.setattr(diagnostic, "settings", Mock(side_effect=failure))
    previous = logging.root.manager.disable
    assert diagnostic.main([]) == 1
    assert logging.root.manager.disable == previous
    output = capsys.readouterr().out
    assert "[FAIL]" in output
    assert "private-path" not in output and "private-value" not in output


def test_valid_probe_worker_result(config_root, monkeypatch):
    monkeypatch.setattr(
        diagnostic.subprocess,
        "run",
        Mock(
            return_value=SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"level": "PASS", "message": "web ready"}),
            )
        ),
    )
    assert diagnostic.run_probe("web", config_root, {}) == Finding("PASS", "web ready")

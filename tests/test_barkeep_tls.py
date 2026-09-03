"""TLS wiring: encrypt when asked, self-sign by default, never half-configure.

The design point (2026-08-11): an untrusted self-signed certificate still
negotiates real TLS — it closes passive capture of the token — and the same
two env vars let an operator swap in a trusted certificate later. What must
never happen is a partially configured pair silently serving plaintext.
"""

import ssl
import stat
import subprocess
import shutil

import pytest

from barkeep.tls import (
    cert_metadata,
    remove_operator_pair,
    resolve_tls,
    stage_operator_pair,
)
import barkeep.tls as tls_module


requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="the optional self-signed integration needs the openssl command",
)


@pytest.fixture(autouse=True)
def _clean_tls_env(monkeypatch):
    for var in ("BARKEEP_TLS", "BARKEEP_TLS_CERT", "BARKEEP_TLS_KEY"):
        monkeypatch.delenv(var, raising=False)


def test_tls_is_off_by_default(tmp_path):
    assert resolve_tls(tmp_path) is None


@pytest.mark.parametrize("present,absent", [
    ("BARKEEP_TLS_CERT", "BARKEEP_TLS_KEY"),
    ("BARKEEP_TLS_KEY", "BARKEEP_TLS_CERT"),
])
def test_a_half_configured_pair_refuses_startup(
    present, absent, monkeypatch, tmp_path,
):
    """Half a pair must stop the daemon, not quietly serve plaintext."""
    supplied = tmp_path / "half.pem"
    supplied.write_text("not a real pem")
    monkeypatch.setenv(present, str(supplied))

    with pytest.raises(ValueError, match=absent):
        resolve_tls(tmp_path)


def test_an_explicit_pair_is_served_verbatim(monkeypatch, tmp_path):
    """The trusted-certificate upgrade path: point the vars at real files."""
    cert = tmp_path / "trusted.crt"
    key = tmp_path / "trusted.key"
    cert.write_text("cert")
    key.write_text("key")
    monkeypatch.setenv("BARKEEP_TLS_CERT", str(cert))
    monkeypatch.setenv("BARKEEP_TLS_KEY", str(key))
    tls_dir = tmp_path / "generated"

    assert resolve_tls(tls_dir) == (cert, key)
    assert not tls_dir.exists(), "explicit files must not trigger generation"


def test_an_explicit_pair_beats_selfsigned_mode(monkeypatch, tmp_path):
    cert = tmp_path / "trusted.crt"
    key = tmp_path / "trusted.key"
    cert.write_text("cert")
    key.write_text("key")
    monkeypatch.setenv("BARKEEP_TLS", "1")
    monkeypatch.setenv("BARKEEP_TLS_CERT", str(cert))
    monkeypatch.setenv("BARKEEP_TLS_KEY", str(key))
    tls_dir = tmp_path / "generated"

    assert resolve_tls(tls_dir) == (cert, key)
    assert not tls_dir.exists()


def test_a_missing_certificate_file_refuses_startup(monkeypatch, tmp_path):
    key = tmp_path / "trusted.key"
    key.write_text("key")
    missing = tmp_path / "nope.crt"
    monkeypatch.setenv("BARKEEP_TLS_CERT", str(missing))
    monkeypatch.setenv("BARKEEP_TLS_KEY", str(key))

    with pytest.raises(ValueError, match="nope.crt"):
        resolve_tls(tmp_path)


@requires_openssl
def test_selfsigned_mode_generates_a_working_pair(monkeypatch, tmp_path):
    monkeypatch.setenv("BARKEEP_TLS", "1")

    tls = resolve_tls(tmp_path)

    assert tls is not None
    cert, key = tls
    assert cert.is_file() and key.is_file()
    assert cert.is_relative_to(tmp_path) and key.is_relative_to(tmp_path)
    # The pair must actually terminate TLS, not merely exist: loading it into
    # a server context proves valid PEM and that the key matches the cert.
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(cert, key)


@requires_openssl
def test_reused_generated_credentials_restore_private_permissions(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("BARKEEP_TLS", "1")

    _, key = resolve_tls(tmp_path)
    key.chmod(0o644)
    tmp_path.chmod(0o755)
    resolve_tls(tmp_path)

    assert stat.S_IMODE(key.stat().st_mode) == 0o600
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700


@requires_openssl
def test_the_selfsigned_pair_survives_restarts(monkeypatch, tmp_path):
    """Regenerating per boot would re-prompt every browser that trusted it."""
    monkeypatch.setenv("BARKEEP_TLS", "1")

    first_cert, first_key = resolve_tls(tmp_path)
    first_bytes = (first_cert.read_bytes(), first_key.read_bytes())
    second_cert, second_key = resolve_tls(tmp_path)

    assert (second_cert, second_key) == (first_cert, first_key)
    assert (second_cert.read_bytes(), second_key.read_bytes()) == first_bytes


@requires_openssl
def test_a_corrupt_persisted_pair_refuses_startup(monkeypatch, tmp_path):
    monkeypatch.setenv("BARKEEP_TLS", "1")
    cert, _ = resolve_tls(tmp_path)
    cert.write_text("not a certificate")

    with pytest.raises(ValueError, match="unusable"):
        resolve_tls(tmp_path)


def test_missing_openssl_refuses_instead_of_falling_back(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("BARKEEP_TLS", "1")

    def missing(*_args, **_kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(tls_module.subprocess, "run", missing)
    with pytest.raises(ValueError, match="not installed"):
        resolve_tls(tmp_path)


def test_openssl_failure_refuses_instead_of_falling_back(
    monkeypatch, tmp_path,
):
    monkeypatch.setenv("BARKEEP_TLS", "1")

    def failed(*_args, **_kwargs):
        raise subprocess.CalledProcessError(
            1, ["openssl"], stderr="fixture generation failure",
        )

    monkeypatch.setattr(tls_module.subprocess, "run", failed)
    with pytest.raises(ValueError, match="fixture generation failure"):
        resolve_tls(tmp_path)


def test_an_unrecognized_tls_value_refuses_startup(monkeypatch, tmp_path):
    """BARKEEP_TLS=yes silently meaning "off" is the silent-failure pattern
    this repo exists to avoid."""
    monkeypatch.setenv("BARKEEP_TLS", "yes")

    with pytest.raises(ValueError, match="BARKEEP_TLS"):
        resolve_tls(tmp_path)


def _pem_pair(monkeypatch, tmp_path, name):
    """A real, matching PEM pair, made by the module's own generator."""
    monkeypatch.setenv("BARKEEP_TLS", "1")
    cert, key = resolve_tls(tmp_path / name)
    monkeypatch.delenv("BARKEEP_TLS")
    return cert.read_text(), key.read_text()


@requires_openssl
def test_an_uploaded_pair_serves_without_any_env_opt_in(monkeypatch, tmp_path):
    """Uploading through the admin API is itself the explicit opt-in; needing
    a second .env edit would defeat the point of the UI."""
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"

    staged = stage_operator_pair(tls_dir, cert_pem, key_pem)

    assert resolve_tls(tls_dir) == staged


@requires_openssl
def test_an_uploaded_pair_beats_selfsigned_generation(monkeypatch, tmp_path):
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"
    monkeypatch.setenv("BARKEEP_TLS", "1")
    generated = resolve_tls(tls_dir)

    staged = stage_operator_pair(tls_dir, cert_pem, key_pem)

    assert resolve_tls(tls_dir) == staged
    assert staged != generated


@requires_openssl
def test_an_env_pair_still_beats_an_uploaded_pair(monkeypatch, tmp_path):
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"
    stage_operator_pair(tls_dir, cert_pem, key_pem)
    env_cert = tmp_path / "env.crt"
    env_key = tmp_path / "env.key"
    env_cert.write_text(cert_pem)
    env_key.write_text(key_pem)
    monkeypatch.setenv("BARKEEP_TLS_CERT", str(env_cert))
    monkeypatch.setenv("BARKEEP_TLS_KEY", str(env_key))

    assert resolve_tls(tls_dir) == (env_cert, env_key)


@requires_openssl
def test_staging_a_mismatched_pair_rejects_and_writes_nothing(
    monkeypatch, tmp_path,
):
    cert_pem, _ = _pem_pair(monkeypatch, tmp_path, "one")
    _, other_key_pem = _pem_pair(monkeypatch, tmp_path, "two")
    tls_dir = tmp_path / "tls"

    with pytest.raises(ValueError):
        stage_operator_pair(tls_dir, cert_pem, other_key_pem)

    assert resolve_tls(tls_dir) is None, "a rejected upload must leave no trace"


def test_staging_garbage_rejects_and_writes_nothing(tmp_path):
    tls_dir = tmp_path / "tls"

    with pytest.raises(ValueError):
        stage_operator_pair(tls_dir, "not a certificate", "not a key")

    assert resolve_tls(tls_dir) is None


@requires_openssl
def test_a_staged_pair_replaces_the_previous_upload(monkeypatch, tmp_path):
    first_cert, first_key = _pem_pair(monkeypatch, tmp_path, "one")
    second_cert, second_key = _pem_pair(monkeypatch, tmp_path, "two")
    tls_dir = tmp_path / "tls"
    stage_operator_pair(tls_dir, first_cert, first_key)

    cert, key = stage_operator_pair(tls_dir, second_cert, second_key)

    assert cert.read_text() == second_cert
    assert key.read_text() == second_key


@requires_openssl
def test_the_staged_key_is_owner_readable_only(monkeypatch, tmp_path):
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")

    _, key = stage_operator_pair(tmp_path / "tls", cert_pem, key_pem)

    assert stat.S_IMODE(key.stat().st_mode) == 0o600


@requires_openssl
def test_removing_the_uploaded_pair_reverts_resolution(monkeypatch, tmp_path):
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"
    stage_operator_pair(tls_dir, cert_pem, key_pem)

    removed = remove_operator_pair(tls_dir)

    assert removed is True
    assert resolve_tls(tls_dir) is None
    assert remove_operator_pair(tls_dir) is False, "second removal is a no-op"


@requires_openssl
def test_a_half_deleted_uploaded_pair_refuses_startup(monkeypatch, tmp_path):
    """Losing one file of the pair is the on-disk twin of the half-configured
    env vars, and gets the same refusal instead of silent plaintext."""
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"
    _, key = stage_operator_pair(tls_dir, cert_pem, key_pem)
    key.unlink()

    with pytest.raises(ValueError, match=key.name):
        resolve_tls(tls_dir)


@requires_openssl
def test_a_corrupt_uploaded_pair_refuses_startup(monkeypatch, tmp_path):
    """An operator's certificate is never silently discarded or replaced —
    the failure must name the file so they can fix or remove it."""
    cert_pem, key_pem = _pem_pair(monkeypatch, tmp_path, "source")
    tls_dir = tmp_path / "tls"
    cert, _ = stage_operator_pair(tls_dir, cert_pem, key_pem)
    cert.write_text("truncated garbage")

    with pytest.raises(ValueError, match=cert.name):
        resolve_tls(tls_dir)


@requires_openssl
def test_cert_metadata_reports_a_fingerprint(monkeypatch, tmp_path):
    monkeypatch.setenv("BARKEEP_TLS", "1")
    cert, _ = resolve_tls(tmp_path)

    meta = cert_metadata(cert)

    fingerprint = meta["fingerprint_sha256"]
    assert len(fingerprint) == 95          # 32 hex pairs, colon-separated
    assert all(len(part) == 2 for part in fingerprint.split(":"))

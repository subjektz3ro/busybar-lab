"""Resolve barkeep's TLS configuration from the environment.

TLS is off by default because the default bind is loopback. `BARKEEP_TLS=1`
generates a persistent self-signed pair: an untrusted certificate still
negotiates real encryption, which closes passive capture of the token on a
LAN, and `BARKEEP_TLS_CERT`/`BARKEEP_TLS_KEY` are the upgrade path when an
operator wants a certificate clients actually trust. Half a pair is a
configuration error, never a silent fall-back to plaintext.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import ssl
import subprocess
from pathlib import Path

CERT_NAME = "barkeep-selfsigned.crt"
KEY_NAME = "barkeep-selfsigned.key"
OPERATOR_CERT_NAME = "barkeep-operator.crt"
OPERATOR_KEY_NAME = "barkeep-operator.key"
log = logging.getLogger(__name__)


def resolve_tls(tls_dir: Path) -> tuple[Path, Path] | None:
    """Return the (certificate, key) pair to serve, or None for plain HTTP.

    ``tls_dir`` is where a generated self-signed pair lives across restarts;
    explicit ``BARKEEP_TLS_CERT``/``BARKEEP_TLS_KEY`` values bypass
    generation entirely. Any half-configured or unusable state raises
    ValueError so the daemon stops instead of quietly serving plaintext.
    """
    cert = (os.environ.get("BARKEEP_TLS_CERT") or "").strip()
    key = (os.environ.get("BARKEEP_TLS_KEY") or "").strip()
    mode = (os.environ.get("BARKEEP_TLS") or "").strip()

    if cert and not key:
        raise ValueError(
            "BARKEEP_TLS_CERT is set but BARKEEP_TLS_KEY is not; "
            "set both or neither")
    if key and not cert:
        raise ValueError(
            "BARKEEP_TLS_KEY is set but BARKEEP_TLS_CERT is not; "
            "set both or neither")
    if cert:
        pair = (Path(cert), Path(key))
        for path in pair:
            if not path.is_file():
                raise ValueError(f"TLS file does not exist: {path}")
        return pair
    if mode and mode != "1":
        raise ValueError(
            f"BARKEEP_TLS={mode!r} is not recognized; use BARKEEP_TLS=1 for "
            "a self-signed certificate, or point BARKEEP_TLS_CERT and "
            "BARKEEP_TLS_KEY at your own")
    operator = _operator_pair(tls_dir)
    if operator is not None:
        return operator
    if not mode:
        return None
    return _ensure_self_signed(tls_dir)


def _ensure_self_signed(tls_dir: Path) -> tuple[Path, Path]:
    """Generate a self-signed pair once and reuse it forever after.

    Regenerating per boot would re-prompt every browser that had accepted
    the previous certificate, so an existing pair is always kept.
    """
    cert, key = tls_dir / CERT_NAME, tls_dir / KEY_NAME
    if cert.is_file() and key.is_file():
        # The pair persists in an operator-writable runtime directory. Repair
        # permissions on every start and reject corruption or a mismatched
        # replacement here, before handing opaque paths to Uvicorn.
        tls_dir.chmod(0o700)
        key.chmod(0o600)
        _validate_generated_pair(cert, key)
        return cert, key
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)  # the directory holds a private key from birth
    # openssl rather than a new Python dependency: the flags below are the
    # subset common to OpenSSL (the Pi) and LibreSSL (a Mac). No SAN: the
    # certificate is untrusted by design, and browsers warn either way.
    try:
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256",
             "-days", "3650", "-nodes", "-subj", "/CN=barkeep",
             "-keyout", str(key), "-out", str(cert)],
            check=True, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise ValueError(
            "BARKEEP_TLS=1 generates its certificate with the `openssl` "
            "command, which is not installed; install it or supply "
            "BARKEEP_TLS_CERT/BARKEEP_TLS_KEY") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"openssl could not generate a certificate: "
            f"{exc.stderr.strip()}") from exc
    key.chmod(0o600)
    _validate_generated_pair(cert, key)
    return cert, key


def _operator_pair(tls_dir: Path) -> tuple[Path, Path] | None:
    """Return the pair installed through the admin API, if there is one.

    Its presence alone turns HTTPS on: the upload was the operator's explicit
    opt-in, and demanding a second `.env` edit would defeat the admin UI.
    """
    cert, key = tls_dir / OPERATOR_CERT_NAME, tls_dir / OPERATOR_KEY_NAME
    have_cert, have_key = cert.is_file(), key.is_file()
    if not have_cert and not have_key:
        return None
    if have_cert != have_key:
        present, missing = (cert, key) if have_cert else (key, cert)
        raise ValueError(
            f"{present.name} exists without {missing.name}; restore the "
            f"missing file or remove {present} to serve without it")
    tls_dir.chmod(0o700)
    key.chmod(0o600)
    _load_pair(
        cert, key,
        f"the uploaded TLS pair ({cert.name}, {key.name}) is unusable; "
        f"replace it through the admin section or remove it from {tls_dir}")
    return cert, key


def stage_operator_pair(
    tls_dir: Path, cert_pem: str, key_pem: str,
) -> tuple[Path, Path]:
    """Validate a pasted PEM pair before replacing either live file.

    Validation happens on staging files before anything replaces the live
    names, so a rejected upload can never leave the daemon unable to start.
    """
    tls_dir.mkdir(parents=True, exist_ok=True)
    tls_dir.chmod(0o700)
    cert_tmp = tls_dir / (OPERATOR_CERT_NAME + ".staging")
    key_tmp = tls_dir / (OPERATOR_KEY_NAME + ".staging")
    try:
        cert_tmp.write_text(cert_pem)
        key_tmp.unlink(missing_ok=True)
        fd = os.open(key_tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as handle:
            handle.write(key_pem)
        _load_pair(
            cert_tmp, key_tmp,
            "not a usable certificate/key pair — both must be PEM and the "
            "key must match the certificate")
    except Exception:
        cert_tmp.unlink(missing_ok=True)
        key_tmp.unlink(missing_ok=True)
        raise
    cert, key = tls_dir / OPERATOR_CERT_NAME, tls_dir / OPERATOR_KEY_NAME
    os.replace(cert_tmp, cert)
    os.replace(key_tmp, key)
    return cert, key


def remove_operator_pair(tls_dir: Path) -> bool:
    """Drop the uploaded pair; True when there was one to remove."""
    removed = False
    for name in (OPERATOR_CERT_NAME, OPERATOR_KEY_NAME):
        path = tls_dir / name
        if path.is_file():
            path.unlink()
            removed = True
    return removed


def cert_metadata(cert: Path) -> dict[str, str]:
    """Public facts about a certificate: fingerprint always, dates best-effort.

    The SHA-256 fingerprint is what a browser's warning screen shows, so it is
    the value an operator compares before clicking through. Never include the
    key, and never anything derived from it.
    """
    match = re.search(
        r"-----BEGIN CERTIFICATE-----.*?-----END CERTIFICATE-----",
        cert.read_text(), re.DOTALL)
    if match is None:
        raise ValueError(f"no certificate found in {cert}")
    digest = hashlib.sha256(
        ssl.PEM_cert_to_DER_cert(match.group(0))).hexdigest().upper()
    meta = {"fingerprint_sha256":
            ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))}
    try:
        decoded = subprocess.run(
            ["openssl", "x509", "-noout", "-subject", "-enddate",
             "-in", str(cert)],
            check=True, capture_output=True, text=True).stdout
        for line in decoded.splitlines():
            name, _, value = line.partition("=")
            if name == "subject":
                meta["subject"] = value.strip()
            elif name == "notAfter":
                meta["not_after"] = value.strip()
    except (FileNotFoundError, subprocess.CalledProcessError):
        pass  # the fingerprint alone still identifies the certificate
    return meta


def tls_status(tls_dir: Path) -> dict[str, object]:
    """One JSON-able answer for the admin UI: what serves on the next start."""
    env_pinned = bool(
        (os.environ.get("BARKEEP_TLS_CERT") or "").strip()
        or (os.environ.get("BARKEEP_TLS_KEY") or "").strip())
    status: dict[str, object] = {"managed": not env_pinned, "cert": None}
    try:
        pair = resolve_tls(tls_dir)
    except ValueError:
        log.warning("invalid Barkeep TLS configuration", exc_info=True)
        status["source"] = "error"
        status["detail"] = (
            "TLS configuration is invalid; inspect the Barkeep service logs"
        )
        return status
    if pair is None:
        status["source"] = "off"
        return status
    cert = pair[0]
    if env_pinned:
        status["source"] = "env"
    elif cert.name == OPERATOR_CERT_NAME:
        status["source"] = "uploaded"
    else:
        status["source"] = "generated"
    try:
        status["cert"] = cert_metadata(cert)
    except (OSError, ValueError):
        pass  # an env-supplied file may not be readable PEM; source stays true
    return status


def _load_pair(cert: Path, key: Path, message: str) -> None:
    """Prove the PEM files are a matching TLS pair, or raise `message`."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    try:
        context.load_cert_chain(cert, key)
    except (OSError, ssl.SSLError) as exc:
        raise ValueError(f"{message} ({exc})") from exc


def _validate_generated_pair(cert: Path, key: Path) -> None:
    """Prove the persisted generated PEM files are a matching TLS pair."""
    _load_pair(
        cert, key,
        "generated Barkeep TLS certificate/key are unusable; remove "
        f"{cert.parent} and restart to generate a fresh pair")

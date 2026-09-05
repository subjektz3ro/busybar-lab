"""Read-only setup policy; reuse runtime parsers, never echo operator values."""

from __future__ import annotations

import ipaddress
import re
import shutil
import ssl
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from apps.skystrip_app.config import parse_runtime_config
from barkeep.configstore import child_env
from barkeep.registry import load_registry
from barkeep.tls import CERT_NAME, KEY_NAME, OPERATOR_CERT_NAME, OPERATOR_KEY_NAME
from busybar_dev.config import read_env_file


@dataclass(frozen=True)
class Finding:
    level: str
    message: str


@dataclass(frozen=True, repr=False)
class WebTarget:
    url: str
    public_label: str
    certificate: Path | None


def settings(root: Path, environ: Mapping[str, str]) -> dict[str, str]:
    values = read_env_file(root / ".env", strip_quotes=True)
    # Like load_env(): an explicitly set process variable outranks the file.
    values.update(environ)
    return values


def _host(value: str) -> bool:
    try:
        ipaddress.ip_address(value)
        return True
    except ValueError:
        return bool(
            re.fullmatch(r"[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?\.?", value)
        )


def _tls_pair(root: Path, values: Mapping[str, str]) -> tuple[bool, Path | None]:
    """Mirror resolution precedence without generating/chmodding TLS files."""
    cert = (values.get("BARKEEP_TLS_CERT") or "").strip()
    key = (values.get("BARKEEP_TLS_KEY") or "").strip()
    mode = (values.get("BARKEEP_TLS") or "").strip()
    if bool(cert) != bool(key):
        raise ValueError("Set both BARKEEP_TLS_CERT and BARKEEP_TLS_KEY, or neither.")
    if cert:
        pair = (root / cert, root / key)
    else:
        if mode not in {"", "1"}:
            raise ValueError("BARKEEP_TLS must be blank or 1.")
        tls_dir = root / "config" / "tls"
        operator = (tls_dir / OPERATOR_CERT_NAME, tls_dir / OPERATOR_KEY_NAME)
        if any(path.exists() for path in operator):
            if not all(path.is_file() for path in operator):
                raise ValueError("The uploaded TLS certificate/key pair is incomplete.")
            pair = operator
        elif mode == "1":
            pair = (tls_dir / CERT_NAME, tls_dir / KEY_NAME)
            if not all(path.is_file() for path in pair):
                if not shutil.which("openssl"):
                    raise ValueError(
                        "Install openssl to generate Barkeep's HTTPS certificate."
                    )
                return True, None  # Barkeep generates it, not this diagnostic.
        else:
            return False, None
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        # An encrypted key must fail, never prompt/hang inside a diagnostic.
        context.load_cert_chain(*pair, password=lambda: "")
    except (OSError, ValueError, ssl.SSLError) as exc:
        raise ValueError(
            "Barkeep's TLS certificate/key pair is unreadable or invalid."
        ) from exc
    return True, pair[0]


def web_target(root: Path, values: Mapping[str, str]) -> WebTarget:
    raw_port = (values.get("BARKEEP_PORT") or "8080").strip() or "8080"
    try:
        port = int(raw_port)
    except ValueError:
        raise ValueError(
            "BARKEEP_PORT must be a whole number from 1 to 65535."
        ) from None
    if not 1 <= port <= 65535:
        raise ValueError("BARKEEP_PORT must be a whole number from 1 to 65535.")
    bind = (values.get("BARKEEP_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    if not _host(bind):
        raise ValueError(
            "BARKEEP_BIND must be an IP address or hostname, not a URL or port."
        )
    secure, certificate = _tls_pair(root, values)
    # Substitute a local probe address for a wildcard; this never binds.
    host = {"0.0.0.0": "127.0.0.1", "::": "::1"}.get(bind, bind)  # noqa: S104
    bracketed = f"[{host}]" if ":" in host else host
    scheme = "https" if secure else "http"
    label = bracketed if host in {"127.0.0.1", "::1", "localhost"} else "<BARKEEP_BIND>"
    return WebTarget(
        f"{scheme}://{bracketed}:{port}", f"{scheme}://{label}:{port}", certificate
    )


def check_config(root: Path, values: Mapping[str, str]) -> list[Finding]:
    results: list[Finding] = []
    try:
        web_target(root, values)
    except ValueError as exc:
        results.append(Finding("FAIL", str(exc)))  # Only our fixed messages above.

    for key in ("BARKEEP_TOKEN", "BUSYBAR_TOKEN"):
        value = values.get(key) or ""
        if not value.isascii() or any(char in value for char in "\r\n\0"):
            results.append(Finding("FAIL", f"{key} must be single-line ASCII text."))
    bind = (values.get("BARKEEP_BIND") or "127.0.0.1").strip() or "127.0.0.1"
    try:
        local = ipaddress.ip_address(bind).is_loopback
    except ValueError:
        local = bind.lower() == "localhost"
    if not local and not (values.get("BARKEEP_TOKEN") or "").strip():
        results.append(
            Finding(
                "WARN",
                "Barkeep is configured for network access without a login token. Keep BARKEEP_BIND=127.0.0.1, or configure BARKEEP_TOKEN and HTTPS before sharing access.",
            )
        )

    host = (values.get("BUSYBAR_HOST") or "").strip()
    if host:
        try:
            parsed = urlsplit(host if "://" in host else "//" + host)
            valid = (
                parsed.scheme in {"", "http", "https"}
                and parsed.hostname is not None
                and _host(parsed.hostname)
                and parsed.port != 0
                and parsed.path in {"", "/"}
                and not parsed.username
                and not parsed.password
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            results.append(
                Finding(
                    "FAIL",
                    "BUSYBAR_HOST must be the bar's address, not an API path or credential-bearing URL.",
                )
            )
        elif parsed.hostname not in {
            "10.0.4.20",
            "localhost",
            "127.0.0.1",
            "::1",
        } and not values.get("BUSYBAR_TOKEN"):
            results.append(
                Finding(
                    "WARN",
                    "Wi-Fi/LAN needs HTTP API access enabled and its password/PIN in BUSYBAR_TOKEN.",
                )
            )

    try:
        registry = load_registry(root / "apps.toml")
        spec = registry.get("skystrip")
        if spec is not None:
            overrides = read_env_file(root / "config" / "skystrip.env")
            effective = child_env(
                overrides, values, allowed_keys={k.name for k in spec.config}
            )
            config = parse_runtime_config(effective, root)
            if not config.location_set:
                results.append(
                    Finding(
                        "WARN",
                        "Skystrip has no weather location. Set SKYSTRIP_LAT, SKYSTRIP_LON and SKYSTRIP_TZ before selecting it; DSN needs no location.",
                    )
                )
            elif not (effective.get("SKYSTRIP_TZ") or "").strip():
                results.append(
                    Finding(
                        "WARN",
                        "Skystrip will use UTC. Set SKYSTRIP_TZ for your weather location; it is not inferred from coordinates.",
                    )
                )
            if config.errors:
                results.append(
                    Finding(
                        "WARN",
                        "SKYSTRIP_LIGHTNING_WS is invalid; optional live lightning is disabled.",
                    )
                )
    except ValueError as exc:
        # The pure Skystrip parser uses fixed, value-free messages. Other
        # registry/override exceptions may contain private paths or values.
        message = str(exc)
        if not message.startswith(
            (
                "SKYSTRIP_LAT ",
                "SKYSTRIP_LON ",
                "SKYSTRIP_TZ ",
                "SKYSTRIP_UNITS ",
                "SKYSTRIP_CLOCK_INK ",
            )
        ):
            message = "Could not validate apps.toml or the per-app settings."
        results.append(
            Finding(
                "FAIL",
                message
                + " Check .env and config/skystrip.env; app overrides take precedence.",
            )
        )
    except (OSError, UnicodeError):
        results.append(
            Finding(
                "FAIL",
                "Cannot read apps.toml or Skystrip's settings. Check file permissions and plain-text format.",
            )
        )
    if not any(r.level == "FAIL" for r in results):
        results.insert(
            0,
            Finding(
                "PASS", "Configuration checks passed; private values are not printed."
            ),
        )
    return results

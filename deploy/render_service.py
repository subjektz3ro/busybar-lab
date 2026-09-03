#!/usr/bin/env python3
"""Render the systemd unit with the paths selected by the installer.

The checked-in unit is a template because neither a user's home directory nor
the location of ``uv`` is a systemd specifier we can safely guess.  Keep the
escaping here rather than growing a second, subtly different sed program in
both install.sh and ship.sh.
"""

from __future__ import annotations

import argparse
import hashlib
import re
from pathlib import Path


TOKENS = {
    "@WORKING_DIRECTORY@",
    "@UV_EXECUTABLE@",
    "@CONFIG_DIRECTORY@",
    "@LOGS_DIRECTORY@",
    "@CACHE_DIRECTORY@",
    "@STATE_DIRECTORY@",
    "@UV_CACHE_DIRECTORY@",
    "@VENV_DIRECTORY@",
    "@CACHE_ENVIRONMENT@",
    "@STATE_ENVIRONMENT@",
}
TOKEN_PATTERN = re.compile(r"@[A-Z_]+@")
UNIT_CONTRACT_HEADER = "# busybar-unit-contract-sha256="


def _unit_contract_digest(template: bytes, renderer: bytes) -> str:
    """Identify both inputs that determine the installed unit's semantics."""

    payload = b"template\0" + template + b"\0renderer\0" + renderer
    return hashlib.sha256(payload).hexdigest()


def _systemd_quote(value: str) -> str:
    """Quote one unit-file word and neutralise systemd specifiers."""

    if not value or any(character in value for character in "\r\n\0"):
        raise ValueError("systemd paths must be non-empty single-line values")
    escaped = (
        value.replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("%", "%%")
    )
    return f'"{escaped}"'


def _systemd_path(value: str) -> str:
    """Escape one path for a directive that does not accept quoting.

    ``WorkingDirectory=`` parses its raw scalar value as a path, not as a
    shell-like word. On systemd 257 a surrounding quote is retained as the
    first path character, making an otherwise absolute path fail validation.
    Interior spaces and backslashes are ordinary path characters here; only
    ``%`` must be doubled to prevent systemd specifier expansion.
    """

    if not value or any(character in value for character in "\r\n\0"):
        raise ValueError("systemd paths must be non-empty single-line values")
    if value != value.strip():
        raise ValueError("systemd scalar paths cannot begin or end in whitespace")
    if value.endswith("\\"):
        raise ValueError("systemd scalar paths cannot end in a backslash")
    return value.replace("%", "%%")


def render_service(
    template: str,
    *,
    checkout: Path,
    uv_executable: Path,
    cache_directory: Path,
    state_directory: Path,
    uv_cache_directory: Path,
) -> str:
    checkout = checkout.resolve()
    uv_executable = uv_executable.resolve()
    cache_directory = cache_directory.resolve()
    state_directory = state_directory.resolve()
    uv_cache_directory = uv_cache_directory.resolve()
    replacements = {
        "@WORKING_DIRECTORY@": _systemd_path(str(checkout)),
        "@UV_EXECUTABLE@": _systemd_quote(str(uv_executable)),
        "@CONFIG_DIRECTORY@": _systemd_quote(str(checkout / "config")),
        "@LOGS_DIRECTORY@": _systemd_quote(str(checkout / "logs")),
        "@CACHE_DIRECTORY@": _systemd_quote(str(cache_directory)),
        "@STATE_DIRECTORY@": _systemd_quote(str(state_directory)),
        "@UV_CACHE_DIRECTORY@": _systemd_quote(str(uv_cache_directory)),
        "@VENV_DIRECTORY@": _systemd_quote(str(checkout / ".venv")),
        "@CACHE_ENVIRONMENT@": _systemd_quote(
            f"BUSYBAR_CACHE_DIR={cache_directory}"
        ),
        "@STATE_ENVIRONMENT@": _systemd_quote(
            f"BUSYBAR_STATE_DIR={state_directory}"
        ),
    }
    template_tokens = set(TOKEN_PATTERN.findall(template))
    unknown = sorted(template_tokens - TOKENS)
    missing = sorted(TOKENS - template_tokens)
    if unknown or missing:
        details = []
        if unknown:
            details.append(f"unknown tokens: {', '.join(unknown)}")
        if missing:
            details.append(f"missing tokens: {', '.join(missing)}")
        raise ValueError("invalid service template (" + "; ".join(details) + ")")
    # Replace placeholders in one pass. A selected directory may itself
    # contain text such as ``@CACHE_DIRECTORY@``; sequential ``str.replace``
    # calls would reinterpret that path fragment as another template token.
    rendered = TOKEN_PATTERN.sub(
        lambda match: replacements[match.group(0)], template)
    contract_digest = _unit_contract_digest(
        template.encode(), Path(__file__).read_bytes()
    )
    return f"{UNIT_CONTRACT_HEADER}{contract_digest}\n{rendered}"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--uv", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--uv-cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rendered = render_service(
        args.template.read_text(),
        checkout=args.checkout,
        uv_executable=args.uv,
        cache_directory=args.cache_dir,
        state_directory=args.state_dir,
        uv_cache_directory=args.uv_cache_dir,
    )
    args.output.write_text(rendered)


if __name__ == "__main__":
    main()

"""Diagnose configuration, device reachability and Barkeep without changing them.

Run from the checkout: uv run python -m deploy.check_setup
Each network probe is a killable subprocess with a wall-clock deadline.
Nothing draws, changes brightness, plays audio, starts apps or fetches weather.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import ssl
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import Mapping

import httpx

from busybar_dev import connect
from .setup_config import Finding, check_config, settings, web_target

ROOT = Path(__file__).resolve().parents[1]
PROBE_TIMEOUT_S = 10.0
WEB_READY_TIMEOUT_S = 7.0  # Leave time for one final 2s request and worker startup.


def device_probe(values: Mapping[str, str]) -> Finding:
    try:
        with connect():
            pass  # connect() already verifies the API; no draw or status write.
        return Finding(
            "PASS",
            "The bar answered its API. No display or brightness settings were changed.",
        )
    except Exception as exc:
        cause: BaseException | None = exc
        seen: set[int] = set()
        while cause is not None and id(cause) not in seen:
            seen.add(id(cause))
            if getattr(cause, "status_code", None) in {401, 403}:
                return Finding(
                    "FAIL",
                    "The bar denied API access. Enable HTTP API access in its web UI and check BUSYBAR_TOKEN against its password/PIN (not BARKEEP_TOKEN).",
                )
            cause = cause.__cause__ or cause.__context__
        if (values.get("BUSYBAR_HOST") or "").strip():
            return Finding(
                "FAIL",
                "Cannot reach the configured bar. Check BUSYBAR_HOST, HTTP API access, its password/PIN and that this computer can reach the bar's network.",
            )
        return Finding(
            "FAIL",
            "USB bar not reachable. Connect a USB data cable to THIS computer; try another cable/port and open http://10.0.4.20/ here.",
        )


def web_probe(root: Path, values: Mapping[str, str]) -> Finding:
    deadline = time.monotonic() + WEB_READY_TIMEOUT_S
    try:
        while True:
            # Barkeep may finish generating its first certificate after this
            # probe starts. Resolve trust again on retry, without generating it
            # ourselves or ever weakening certificate verification.
            target = web_target(root, values)
            context = ssl.create_default_context()
            if target.certificate:
                # The generated certificate names 'barkeep', not loopback.
                context.load_verify_locations(cafile=str(target.certificate))
                context.check_hostname = False
                context.verify_flags |= ssl.VERIFY_X509_PARTIAL_CHAIN
            try:
                with httpx.Client(
                    timeout=2.0, trust_env=False, verify=context, follow_redirects=False
                ) as client:
                    # Root UI is public even with BARKEEP_TOKEN. Never transmit
                    # a credential or follow a redirect to diagnose readiness.
                    with client.stream("GET", target.url + "/") as response:
                        if response.status_code != 200:
                            return Finding(
                                "FAIL",
                                "The web address responded but did not serve Barkeep's page. Check BARKEEP_PORT, BARKEEP_TLS and any proxy configuration.",
                            )
                        body = bytearray()
                        for chunk in response.iter_bytes(chunk_size=4096):
                            body.extend(chunk)
                            if len(body) >= 65536:
                                break
                        if (
                            b"<title>barkeep</title>" not in body
                            or b'id="foreground-cards"' not in body
                        ):
                            return Finding(
                                "FAIL",
                                "Another page is using Barkeep's web address. Stop the conflicting service or choose a different BARKEEP_PORT.",
                            )
                    return Finding(
                        "PASS",
                        "Barkeep's web page responds on this computer. Remote-browser access still needs a tunnel or deliberate LAN setup.",
                    )
            except httpx.TransportError:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise
                time.sleep(min(0.25, remaining))
    except httpx.TransportError:
        return Finding(
            "FAIL",
            "Barkeep's web page is not reachable. Check that Barkeep is running, its bind/port, and HTTPS certificates if enabled. See docs/troubleshooting.md.",
        )


def run_probe(kind: str, root: Path, values: Mapping[str, str]) -> Finding:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "deploy.check_setup", "--worker", kind],
            cwd=root,
            env=dict(values),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
        )
        if result.returncode != 0:
            raise ValueError("worker failed")
        data = json.loads(result.stdout)
        if data["level"] not in {"PASS", "WARN", "FAIL"} or not isinstance(
            data["message"], str
        ):
            raise ValueError("invalid worker result")
        return Finding(data["level"], data["message"])
    except subprocess.TimeoutExpired:
        return Finding(
            "FAIL",
            f"The {kind} check timed out after {PROBE_TIMEOUT_S:g}s; it was stopped. Check the connection and rerun this diagnostic.",
        )
    except (OSError, ValueError, KeyError, TypeError):
        return Finding(
            "FAIL",
            f"The {kind} check could not complete. Run from the busybar-lab folder after uv sync --locked; no raw errors or private values are printed.",
        )


def _run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--config-only", action="store_true")
    group.add_argument("--device-only", action="store_true")
    group.add_argument("--web-only", action="store_true")
    parser.add_argument("--worker", choices=("device", "web"), help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        values = settings(ROOT, os.environ)
        if args.worker:
            probe = (
                device_probe(values)
                if args.worker == "device"
                else web_probe(ROOT, values)
            )
            print(json.dumps(asdict(probe)))
            return 0
        findings = check_config(ROOT, values)
    except (OSError, UnicodeError, ValueError):
        print(
            "[FAIL] Cannot read setup configuration. Check .env permissions and plain-text format; private values are not printed."
        )
        return 1

    if not args.device_only and not args.web_only:
        for finding in findings:
            print(f"[{finding.level}] {finding.message}")
    if any(f.level == "FAIL" for f in findings):
        if args.device_only or args.web_only:
            print(
                "[FAIL] Fix configuration first: uv run python -m deploy.check_setup --config-only"
            )
        return 1
    if args.config_only:
        return 0
    probes = (
        ("device",)
        if args.device_only
        else ("web",)
        if args.web_only
        else ("device", "web")
    )
    for kind in probes:
        finding = run_probe(kind, ROOT, values)
        findings.append(finding)
        print(f"[{finding.level}] {finding.message}")
    if not args.device_only:
        target = web_target(ROOT, values)
        print(f"On the Barkeep computer: {target.public_label}")
        print("On another computer: follow the SSH-tunnel steps in docs/quickstart.md.")
        print("A browser token prompt asks for BARKEEP_TOKEN, never the bar's PIN.")
    return int(any(f.level == "FAIL" for f in findings))


def main(argv: list[str] | None = None) -> int:
    # Libraries can log request URLs or owner paths. Findings above are the
    # sole output contract, including inside isolated network workers. Restore
    # logging when called in-process by another tool or a test.
    previous = logging.root.manager.disable
    logging.disable(logging.CRITICAL)
    try:
        return _run(argv)
    except Exception:
        print(
            "[FAIL] Setup check could not complete. Check configuration and rerun; raw errors and private values are not printed."
        )
        return 1
    finally:
        logging.disable(previous)


if __name__ == "__main__":
    raise SystemExit(main())

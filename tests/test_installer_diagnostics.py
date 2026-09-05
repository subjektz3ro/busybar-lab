"""Execute the real installer's finishing phase without sudo or a live bar."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "deploy/install.sh"


def finish(tmp_path, *, device=0, web=0, service=1, start=0):
    script = INSTALLER.read_text()
    marker = "# 5. Find the bar."
    tail = marker + script.split(marker, 1)[1]
    uv = tmp_path / "uv"
    uv.write_text(
        "#!/bin/sh\n"
        'case "$*" in\n'
        '  *--device-only) printf "device check\\n"; exit "$DEVICE_RESULT";;\n'
        '  *--web-only) printf "web check\\n"; exit "$WEB_RESULT";;\n'
        "esac\nexit 0\n"
    )
    uv.chmod(0o755)
    prelude = """
set -eu
say() { printf '%s\\n' "$*"; }
run_root() {
  printf 'root: %s\\n' "$*"
  case "$*" in "systemctl start"*) return "$START_RESULT";; esac
}
systemctl() { return 0; }
systemd-analyze() { return 0; }
SERVICE_USER=tester
SERVICE_UNIT=barkeep@tester
SERVICE_WAS_ACTIVE=0
REPO_DIR="$TEST_ROOT"
RUNTIME_CACHE_DIR="$TEST_ROOT/cache"
RUNTIME_STATE_DIR="$TEST_ROOT/state"
UV_CACHE_DIR="$TEST_ROOT/uv-cache"
"""
    return subprocess.run(
        ["bash", "-c", prelude + tail],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "UV_BIN": str(uv),
            "TEST_ROOT": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "INSTALL_SVC": str(service),
            "DEVICE_RESULT": str(device),
            "WEB_RESULT": str(web),
            "START_RESULT": str(start),
        },
        timeout=10,
    )


def test_existing_config_is_checked_before_models_and_service_changes():
    script = INSTALLER.read_text()
    assert script.index("--config-only") < script.index("# 4. Kokoro")


def test_unreachable_bar_does_not_block_host_setup_but_cannot_claim_readiness(tmp_path):
    result = finish(tmp_path, device=1)
    assert result.returncode == 0, result.stderr
    assert "root: systemctl start barkeep@tester" in result.stdout
    assert "Bar connection still needs attention" in result.stdout
    assert "Setup checks passed" not in result.stdout
    assert "The sky is yours" not in result.stdout


def test_web_not_ready_is_not_a_successful_service_install(tmp_path):
    result = finish(tmp_path, web=1)
    assert result.returncode != 0
    assert "Barkeep did not pass its web check" in result.stdout
    assert "journalctl" in result.stdout
    assert "Setup checks passed" not in result.stdout


def test_service_start_failure_has_a_next_step_and_never_claims_readiness(tmp_path):
    result = finish(tmp_path, start=1)
    assert result.returncode != 0
    assert "Barkeep could not start" in result.stdout
    assert "journalctl" in result.stdout
    assert "web check" not in result.stdout
    assert "Setup checks passed" not in result.stdout


def test_success_means_both_checks_completed_after_start(tmp_path):
    result = finish(tmp_path)
    assert result.returncode == 0, result.stderr
    output = result.stdout
    assert output.index("device check") < output.index("root: systemctl start")
    assert output.index("root: systemctl start") < output.index("web check")
    assert "Setup checks passed" in output


@pytest.mark.parametrize("device", [0, 1])
def test_manual_install_does_not_claim_barkeep_has_been_started(tmp_path, device):
    result = finish(tmp_path, device=device, service=0)
    assert result.returncode == 0, result.stderr
    assert "Host setup complete; Barkeep has not been started" in result.stdout
    assert "run -m barkeep" in result.stdout
    assert "web check" not in result.stdout
    assert "Setup checks passed" not in result.stdout

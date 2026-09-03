"""The deploy script's guards, exercised for real.

A deploy script that has never been run against a wrong input is a deploy
script whose error paths are decorative. These build actual git repositories
in a temp directory — a bare 'origin', a clone, real commits — and run
`ship.sh --dry-run` against them, so every guard is checked without ssh, a
host, or a bar.

--dry-run stops after the guards and prints the command it *would* run, which
is exactly the seam that makes this testable.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SHIP = REPO / "deploy" / "ship.sh"
SERVICE = REPO / "deploy" / "barkeep.service"
SERVICE_RENDERER = REPO / "deploy" / "render_service.py"


def unit_contract_hash(checkout: Path) -> str:
    payload = (
        b"template\0"
        + (checkout / "deploy" / "barkeep.service").read_bytes()
        + b"\0renderer\0"
        + (checkout / "deploy" / "render_service.py").read_bytes()
    )
    return hashlib.sha256(payload).hexdigest()


def git(*args: str, cwd: Path, **kw) -> subprocess.CompletedProcess:
    return subprocess.run(("git", *args), cwd=cwd, check=True,
                          capture_output=True, text=True, **kw)


def ship(clone: Path, *args: str, **env) -> subprocess.CompletedProcess:
    """Run the real ship.sh inside a throwaway repo."""
    return subprocess.run(
        ["bash", str(clone / "deploy" / "ship.sh"), *args],
        cwd=clone, capture_output=True, text=True,
        env={"PATH": "/usr/bin:/bin:/usr/local/bin", "HOME": str(clone), **env},
    )


def remote_command(out: subprocess.CompletedProcess) -> str:
    """Recover the exact host-side shell program printed by --dry-run."""
    marker = "would run on example.invalid:\n"
    assert marker in out.stdout
    return "\n".join(
        line.removeprefix("    ")
        for line in out.stdout.split(marker, 1)[1].splitlines()
    )


def write_executable(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(0o755)


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    """A bare origin and a clone of it, with one pushed commit."""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "--initial-branch=main", str(origin), cwd=tmp_path)

    clone = tmp_path / "work"
    git("clone", str(origin), str(clone), cwd=tmp_path)
    git("config", "user.email", "t@example.invalid", cwd=clone)
    git("config", "user.name", "Test", cwd=clone)

    (clone / "deploy").mkdir()
    shutil.copy(SHIP, clone / "deploy" / "ship.sh")
    shutil.copy(SERVICE, clone / "deploy" / "barkeep.service")
    shutil.copy(SERVICE_RENDERER, clone / "deploy" / "render_service.py")
    (clone / "README.md").write_text("hello\n")
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "first", cwd=clone)
    git("push", "-q", "origin", "main", cwd=clone)
    return origin, clone


def test_a_commit_that_is_not_on_origin_is_refused(world):
    """The whole point of the rewrite. If the commit is not on origin the host
    cannot fetch it, and deploying it any other way strands the host on
    something no clean clone can reproduce."""
    _origin, clone = world
    (clone / "new.txt").write_text("unpushed\n")
    git("add", "-A", cwd=clone)
    git("commit", "-qm", "not pushed", cwd=clone)

    out = ship(clone, "--dry-run", "example.invalid")
    assert out.returncode != 0
    assert "not on origin/main" in out.stderr
    # and it must say how to publish the reproducible revision
    assert "git push origin HEAD:main" in out.stderr


def test_a_pushed_commit_deploys(world):
    """The host resets and syncs before any process is restarted."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "example.invalid")
    assert out.returncode == 0, out.stderr
    sha = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    commands = (
        "git fetch --quiet 'origin'",
        "systemctl show --property=FragmentPath",
        "expected_unit_hash=$(",
        f"git show '{sha}:deploy/barkeep.service'",
        f"git show '{sha}:deploy/render_service.py'",
        'sudo systemctl stop "$unit"',
        f"git reset --hard --quiet {sha}",
        '"$uv_bin" sync --locked',
        "verify_kokoro_synthesis",
        'sudo systemctl start "$unit"',
        'systemctl is-active --quiet "$unit"',
    )
    positions = [out.stdout.index(command) for command in commands]
    assert positions == sorted(positions), out.stdout


def test_the_configured_remote_is_fetched_on_the_host(world):
    """The local reproducibility guard and host fetch use the same remote."""
    _origin, clone = world
    git("remote", "rename", "origin", "upstream", cwd=clone)

    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_REMOTE="upstream",
    )

    assert out.returncode == 0, out.stderr
    assert "git fetch --quiet 'upstream'" in out.stdout
    assert "git fetch --quiet 'origin'" not in out.stdout


def test_uv_is_resolved_on_the_host_not_the_deploying_machine(world):
    """Non-login ssh may omit uv's installer location from PATH."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "example.invalid")
    assert out.returncode == 0, out.stderr
    command = remote_command(out)

    assert "command -v uv" in command
    assert 'elif [ -x "$HOME/.local/bin/uv" ]' in command
    assert 'uv_bin="$HOME/.local/bin/uv"' in command
    assert str(clone / ".local/bin/uv") not in command
    assert command.index("command -v uv") < command.index('"$uv_bin" sync --locked')


def test_remote_uv_fallback_runs_before_restart(world, tmp_path):
    """Exercise the printed host program with uv only in ~/.local/bin."""
    _origin, clone = world
    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_PATH=str(clone),
        BUSYBAR_DEPLOY_SERVICE="barkeep@test",
    )
    assert out.returncode == 0, out.stderr

    fake_bin = tmp_path / "bin"
    fake_home = tmp_path / "home"
    log = tmp_path / "calls.log"
    installed_unit = tmp_path / "barkeep@.service"
    expected_hash = unit_contract_hash(clone)
    installed_unit.write_text(
        f"# busybar-unit-contract-sha256={expected_hash}\n[Unit]\n"
    )
    write_executable(fake_bin / "git", 'printf "git %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sudo", 'printf "sudo %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sleep", 'printf "sleep %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "hostname", 'printf "test-host\\n"\n')
    write_executable(
        fake_bin / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = show ] && printf "%s\\n" "$INSTALLED_UNIT"\n'
        'exit 0\n',
    )
    write_executable(
        fake_home / ".local/bin/uv",
        'printf "uv %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = run ] && printf "%s\\n" "$EXPECTED_UNIT_HASH"\n'
        'exit 0\n',
    )

    ran = subprocess.run(
        [shutil.which("bash") or "bash", "-c", remote_command(out)],
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "HOME": str(fake_home),
            "DEPLOY_LOG": str(log),
            "EXPECTED_UNIT_HASH": expected_hash,
            "INSTALLED_UNIT": str(installed_unit),
        },
    )

    assert ran.returncode == 0, ran.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "git fetch --quiet origin"
    assert calls[1] == (
        "systemctl show --property=FragmentPath --value barkeep@test"
    )
    sha = git("rev-parse", "HEAD", cwd=clone).stdout.strip()
    command = remote_command(out)
    assert f"git show '{sha}:deploy/barkeep.service'" in command
    assert f"git show '{sha}:deploy/render_service.py'" in command
    stop_index = calls.index("sudo systemctl stop barkeep@test")
    digest_calls = calls[2:stop_index]
    assert any(
        call.startswith("uv run --no-sync python -c ")
        for call in digest_calls
    )
    # The hash producer and consumer are opposite sides of a shell pipe, so
    # their test-double log writes are concurrent and have no portable order.
    # Once the hash gate completes, every live mutation is strictly serial.
    mutation_calls = calls[stop_index:]
    assert mutation_calls[:3] == [
        "sudo systemctl stop barkeep@test",
        f"git reset --hard --quiet {sha}",
        "uv sync --locked",
    ]
    assert mutation_calls[3].startswith("uv run --no-sync python -c ")
    assert "configured_kokoro_dir" in mutation_calls[3]
    assert "verify_kokoro_synthesis" in mutation_calls[3]
    assert mutation_calls[4:] == [
        "sudo systemctl start barkeep@test",
        "sleep 3",
        "systemctl is-active --quiet barkeep@test",
        "git rev-parse --short HEAD",
    ]


@pytest.mark.parametrize("installed_state", ["missing", "stale"])
def test_missing_or_stale_unit_never_restarts(world, tmp_path, installed_state):
    """Code must not start inside a service sandbox from an older template."""
    _origin, clone = world
    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_PATH=str(clone),
        BUSYBAR_DEPLOY_SERVICE="barkeep@test",
    )
    assert out.returncode == 0, out.stderr

    fake_bin = tmp_path / "bin"
    fake_home = tmp_path / "home"
    log = tmp_path / "calls.log"
    installed_unit = tmp_path / "barkeep@.service"
    expected_hash = unit_contract_hash(clone)
    if installed_state == "stale":
        installed_unit.write_text(
            "# busybar-unit-contract-sha256=" + ("0" * 64) + "\n[Unit]\n"
        )
    write_executable(fake_bin / "git", 'printf "git %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sudo", 'printf "sudo %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(
        fake_bin / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = show ] && printf "%s\\n" "$INSTALLED_UNIT"\n'
        'exit 0\n',
    )
    write_executable(
        fake_home / ".local/bin/uv",
        'printf "uv %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = run ] && printf "%s\\n" "$EXPECTED_UNIT_HASH"\n'
        'exit 0\n',
    )

    ran = subprocess.run(
        [shutil.which("bash") or "bash", "-c", remote_command(out)],
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "HOME": str(fake_home),
            "DEPLOY_LOG": str(log),
            "EXPECTED_UNIT_HASH": expected_hash,
            "INSTALLED_UNIT": str(installed_unit),
        },
    )

    assert ran.returncode != 0
    calls = log.read_text().splitlines()
    assert not any(call.startswith("sudo systemctl start") for call in calls)
    assert not any(call.startswith("git reset") for call in calls)
    assert "checkout and environment were left unchanged" in ran.stderr
    assert "systemctl stop" in ran.stderr
    assert "./deploy/install.sh" in ran.stderr


def test_missing_uv_never_stops_the_running_release(world, tmp_path):
    """A missing tool is found before the live service is touched."""
    _origin, clone = world
    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_PATH=str(clone),
        BUSYBAR_DEPLOY_SERVICE="barkeep@test",
    )
    assert out.returncode == 0, out.stderr

    fake_bin = tmp_path / "bin"
    fake_home = tmp_path / "home"
    log = tmp_path / "calls.log"
    write_executable(fake_bin / "git", 'printf "git %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sudo", 'printf "sudo %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    ran = subprocess.run(
        [shutil.which("bash") or "bash", "-c", remote_command(out)],
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "HOME": str(fake_home),
            "DEPLOY_LOG": str(log),
        },
    )

    assert ran.returncode != 0
    calls = log.read_text().splitlines()
    assert not any(call.startswith("sudo systemctl stop") for call in calls)


def test_failed_sync_leaves_the_service_stopped_not_mixed(world, tmp_path):
    """Once mutation starts, the old supervisor cannot spawn from new files."""
    _origin, clone = world
    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_PATH=str(clone),
        BUSYBAR_DEPLOY_SERVICE="barkeep@test",
    )
    assert out.returncode == 0, out.stderr

    fake_bin = tmp_path / "bin"
    fake_home = tmp_path / "home"
    log = tmp_path / "calls.log"
    installed_unit = tmp_path / "barkeep@.service"
    expected_hash = unit_contract_hash(clone)
    installed_unit.write_text(
        f"# busybar-unit-contract-sha256={expected_hash}\n[Unit]\n"
    )
    write_executable(fake_bin / "git", 'printf "git %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sudo", 'printf "sudo %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(
        fake_bin / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = show ] && printf "%s\\n" "$INSTALLED_UNIT"\n'
        'exit 0\n',
    )
    write_executable(
        fake_home / ".local/bin/uv",
        'printf "uv %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        'if [ "$1" = run ]; then printf "%s\\n" "$EXPECTED_UNIT_HASH"; exit 0; fi\n'
        'exit 23\n',
    )

    ran = subprocess.run(
        [shutil.which("bash") or "bash", "-c", remote_command(out)],
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "HOME": str(fake_home),
            "DEPLOY_LOG": str(log),
            "EXPECTED_UNIT_HASH": expected_hash,
            "INSTALLED_UNIT": str(installed_unit),
        },
    )

    assert ran.returncode == 23
    calls = log.read_text().splitlines()
    assert "sudo systemctl stop barkeep@test" in calls
    assert "uv sync --locked" in calls
    assert not any(call.startswith("sudo systemctl start") for call in calls)


def test_failed_kokoro_smoke_leaves_the_service_stopped(world, tmp_path):
    """A deploy cannot start production with only emergency system speech."""
    _origin, clone = world
    out = ship(
        clone,
        "--dry-run",
        "example.invalid",
        BUSYBAR_DEPLOY_PATH=str(clone),
        BUSYBAR_DEPLOY_SERVICE="barkeep@test",
    )
    assert out.returncode == 0, out.stderr

    fake_bin = tmp_path / "bin"
    fake_home = tmp_path / "home"
    log = tmp_path / "calls.log"
    installed_unit = tmp_path / "barkeep@.service"
    expected_hash = unit_contract_hash(clone)
    installed_unit.write_text(
        f"# busybar-unit-contract-sha256={expected_hash}\n[Unit]\n"
    )
    write_executable(fake_bin / "git", 'printf "git %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(fake_bin / "sudo", 'printf "sudo %s\\n" "$*" >> "$DEPLOY_LOG"\n')
    write_executable(
        fake_bin / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        '[ "$1" = show ] && printf "%s\\n" "$INSTALLED_UNIT"\n'
        'exit 0\n',
    )
    write_executable(
        fake_home / ".local/bin/uv",
        'printf "uv %s\\n" "$*" >> "$DEPLOY_LOG"\n'
        'case "$*" in\n'
        '  *verify_kokoro_synthesis*) exit 42 ;;\n'
        'esac\n'
        '[ "$1" = run ] && printf "%s\\n" "$EXPECTED_UNIT_HASH"\n'
        'exit 0\n',
    )

    ran = subprocess.run(
        [shutil.which("bash") or "bash", "-c", remote_command(out)],
        capture_output=True,
        text=True,
        env={
            "PATH": str(fake_bin),
            "HOME": str(fake_home),
            "DEPLOY_LOG": str(log),
            "EXPECTED_UNIT_HASH": expected_hash,
            "INSTALLED_UNIT": str(installed_unit),
        },
    )

    assert ran.returncode == 1
    calls = log.read_text().splitlines()
    assert "sudo systemctl stop barkeep@test" in calls
    assert "uv sync --locked" in calls
    assert any("verify_kokoro_synthesis" in call for call in calls)
    assert not any(call.startswith("sudo systemctl start") for call in calls)
    assert "required Kokoro speech verification failed" in ran.stderr
    assert "Rerun ./deploy/install.sh" in ran.stderr


def test_the_host_is_told_to_reset_not_merge(world):
    """The host is a deploy target, not a working copy. A merge conflict on a
    machine nobody is sitting at helps nobody."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "example.invalid")
    assert "reset --hard" in out.stdout
    assert "git pull" not in out.stdout
    assert "git merge" not in out.stdout


def test_it_refuses_without_a_host(world):
    """Silently deploying to a default hostname is how you restart someone
    else's server."""
    _origin, clone = world
    out = ship(clone, "--dry-run")
    assert out.returncode != 0
    assert "no host" in out.stderr
    assert "BUSYBAR_DEPLOY_HOST" in out.stderr


def test_the_host_can_be_set_by_environment(world):
    _origin, clone = world
    out = ship(clone, "--dry-run", BUSYBAR_DEPLOY_HOST="pi.invalid")
    assert out.returncode == 0, out.stderr
    assert "pi.invalid" in out.stdout


def test_nothing_is_hardcoded_to_one_deployment(world):
    """Checkout path, unit name, remote and branch are all configurable —
    this used to default to a specific machine on a specific network."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "other.invalid",
               BUSYBAR_DEPLOY_PATH="/srv/bar",
               BUSYBAR_DEPLOY_SERVICE="barkeep@svc")
    assert out.returncode == 0, out.stderr
    assert "cd '/srv/bar'" in out.stdout
    assert "barkeep@svc" in out.stdout
    # and there is no default host at all — see test_it_refuses_without_a_host.
    # The repo-wide identifier sweep lives in tests/test_no_personal_data.py.


def test_a_missing_remote_is_explained_not_crashed(world, tmp_path):
    """A fresh clone with no origin should be told what to do, not hit an
    unbound git error."""
    _origin, clone = world
    git("remote", "remove", "origin", cwd=clone)
    out = ship(clone, "--dry-run", "example.invalid")
    assert out.returncode != 0
    assert "no 'origin' remote" in out.stderr
    assert "git remote add origin <url>" in out.stderr


def test_an_unknown_ref_fails_before_touching_anything(world):
    _origin, clone = world
    out = ship(clone, "--dry-run", "--ref", "no-such-tag", "example.invalid")
    assert out.returncode != 0
    assert "no such commit" in out.stderr


def test_help_stops_at_the_end_of_the_comment_header(world):
    _origin, clone = world
    out = ship(clone, "--help")

    assert out.returncode == 0
    assert "--dry-run" in out.stdout
    assert "--ref" in out.stdout
    assert "set -euo pipefail" not in out.stdout
    assert 'cd "$(dirname "$0")/.."' not in out.stdout


def test_the_script_is_valid_shell():
    """CI checks this too, but a broken deploy script should fail here first."""
    out = subprocess.run(["bash", "-n", str(SHIP)], capture_output=True, text=True)
    assert out.returncode == 0, out.stderr


def test_the_unit_name_is_resolved_on_the_host_not_here(world):
    """Caught by a real deploy, not by these tests, which is why it is here
    now. The default unit is instanced on the user — and it has to be the
    user on the HOST. Interpolating $USER locally deploys under whoever is
    at this laptop; passing '$USER' inside single quotes for the remote
    shell never expands at all, and systemd is handed a unit literally
    named barkeep@$USER."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "example.invalid")
    assert out.returncode == 0, out.stderr
    assert "barkeep@$USER" not in out.stdout, "unit name would not expand"
    # resolved on the far end, from the far end's identity
    assert "id -un" in out.stdout
    assert 'unit="$unit$(id -un)"' in out.stdout


def test_an_explicit_unit_name_is_left_alone(world):
    """Only a trailing '@' means 'append the host's user'."""
    _origin, clone = world
    out = ship(clone, "--dry-run", "example.invalid",
               BUSYBAR_DEPLOY_SERVICE="barkeep@operator")
    assert "unit='barkeep@operator'" in out.stdout


# --- unit hardening --------------------------------------------------------


def _unit() -> str:
    return (Path(__file__).resolve().parents[1]
            / "deploy" / "barkeep.service").read_text()


def test_the_unit_cannot_gain_privileges():
    """Barkeep may be exposed to a LAN and spawns children, while its account
    holds narrowly scoped deploy stop/start rights. Confirmed on a host that
    the installed unit once had none of these protections set."""
    assert "NoNewPrivileges=yes" in _unit()


def test_the_unit_restricts_the_filesystem():
    unit = _unit()
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=read-only" in unit
    assert "PrivateTmp=yes" in unit


def test_everything_the_daemon_writes_stays_writable():
    """ProtectSystem=strict plus ProtectHome=read-only makes the whole home
    read-only, so every path the unit writes must be named.

    An earlier version of this test checked only config/ and logs/ — the
    app's own paths — and the unit shipped without uv's cache. ExecStart is
    `uv run`, which takes a lock in ~/.cache/uv, so the unit loaded cleanly and
    then crash-looped with "Could not acquire lock ... Read-only file system".
    It took the bar down. The list below is what the RUNTIME needs, not what
    the application needs, and that distinction is the whole bug.
    """
    unit = _unit()
    rw = [line for line in unit.splitlines() if line.startswith("ReadWritePaths=")]
    assert rw, "ProtectSystem=strict without ReadWritePaths cannot write config/"
    paths = set(rw[0].split("=", 1)[1].split())
    required = {
        "@CONFIG_DIRECTORY@",
        "@LOGS_DIRECTORY@",
        "@CACHE_DIRECTORY@",
        "@STATE_DIRECTORY@",
        "@UV_CACHE_DIRECTORY@",
        "@VENV_DIRECTORY@",
    }
    assert required <= paths, "the rendered unit would omit a runtime write path"


def test_the_unit_names_the_interpreter_it_actually_runs():
    """The ReadWritePaths above are only correct for `uv run`. If ExecStart
    changes to a plain interpreter, the uv cache entry is dead weight — and
    worse, a different runtime may need a different path."""
    unit = _unit()
    exec_lines = [line for line in unit.splitlines() if line.startswith("ExecStart=")]
    assert exec_lines and "@UV_EXECUTABLE@" in exec_lines[0], (
        "ExecStart no longer uses uv; re-derive ReadWritePaths")
    assert "run --no-sync -m barkeep" in exec_lines[0]

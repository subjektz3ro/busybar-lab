from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parent.parent
INSTALLER = REPO / "deploy" / "install.sh"
SERVICE_TEMPLATE = REPO / "deploy" / "barkeep.service"
SERVICE_RENDERER = REPO / "deploy" / "render_service.py"


def _installer_prelude() -> str:
    script = INSTALLER.read_text()
    prelude, marker, _ = script.partition('say "BUSY Bar Lab installer"')
    assert marker, "installer prelude boundary moved"
    return prelude


def test_installer_writes_url_contacts_as_parser_data_not_shell_code():
    contact = "https://example.invalid/weather?app=skystrip&channel=alerts"
    result = subprocess.run(
        [
            "bash",
            "-c",
            _installer_prelude() + '\nwrite_env_line SKYSTRIP_CONTACT "$1"',
            str(INSTALLER),
            contact,
        ],
        check=True,
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.stdout == f"SKYSTRIP_CONTACT={contact}\n"
    script = INSTALLER.read_text()
    assert ". ./.env" not in script
    assert "source .env" not in script


def test_installer_publishes_new_env_atomically_and_owner_only():
    script = INSTALLER.read_text()
    umask = script.index("umask 077")
    temporary = script.index("mktemp ./.env.install.XXXXXX")
    write = script.index('} > "$BUSYBAR_ENV_TMP"')
    publish = script.index('mv "$BUSYBAR_ENV_TMP" .env')
    migrate = script.index("chmod 600 .env")

    assert umask < temporary < write < publish < migrate
    assert "trap 'rm -f -- \"$BUSYBAR_ENV_TMP\"' EXIT" in script


def test_installer_validates_timezone_and_loops_units_after_sync(tmp_path):
    script = INSTALLER.read_text()
    sync = script.index('"$UV_BIN" sync --locked')
    assert sync < script.index("TZV=$(ask_timezone")
    assert sync < script.index("UNITS=$(ask_units)")
    env = dict(os.environ)
    env["UV_CACHE_DIR"] = str(tmp_path / "uv-cache")
    env["UV_BIN"] = shutil.which("uv") or "uv"

    units = subprocess.run(
        ["bash", "-c", _installer_prelude() + "\nask_units", str(INSTALLER)],
        input="metric\nC\n",
        check=True,
        cwd=REPO,
        capture_output=True,
        env=env,
        text=True,
    )
    assert units.stdout == "c\n"
    assert "units must be f or c" in units.stderr

    timezone = subprocess.run(
        ["bash", "-c", _installer_prelude() + '\nask_timezone "UTC"',
         str(INSTALLER)],
        input=f"{'A' * 300}\nMars/Olympus\nEtc/UTC\n",
        check=True,
        cwd=REPO,
        capture_output=True,
        env=env,
        text=True,
    )
    assert timezone.stdout == "Etc/UTC\n"
    assert "timezone must be an installed IANA name" in timezone.stderr

    detected_env = dict(env)
    detected_env["TZ"] = "Etc/UTC"
    detected = subprocess.run(
        ["bash", "-c", _installer_prelude() + "\nhost_timezone_guess",
         str(INSTALLER)],
        check=True,
        cwd=REPO,
        capture_output=True,
        env=detected_env,
        text=True,
    )
    assert detected.stdout == "Etc/UTC\n"
    assert "detected host default" in script
    assert "UTC is only a placeholder" in script


def test_installer_uses_barkeep_as_the_only_systemd_app_parent():
    script = INSTALLER.read_text()
    assert "deploy/render_service.py" in script
    assert "/etc/systemd/system/barkeep@.service" in script
    assert "deploy/skystrip.service" not in script
    assert 'run_root systemctl enable "$SERVICE_UNIT"' in script
    assert 'run_root systemctl stop "$SERVICE_UNIT"' in script
    assert 'run_root systemctl start "$SERVICE_UNIT"' in script
    assert 'run_root systemctl restart "$SERVICE_UNIT"' not in script


def test_installer_fetches_the_shared_kokoro_model_bank():
    script = INSTALLER.read_text()
    sync = script.index('"$UV_BIN" sync --locked')
    models = script.index("# 4. Kokoro's shared model and voice bank")
    verify = script.index('if ! verify_kokoro_install "$KOKORO_DIR";', models)
    service = script.index("# 6. Control-plane service")

    assert sync < models < verify < service
    assert "--extra kokoro" not in script
    assert "KOKORO_ENABLED" not in script
    assert "kokoro-enabled" not in script
    assert "kokoro-v1.0.onnx" in script
    assert "voices-v1.0.bin" in script
    assert "verify_sha256" in script
    assert ".part" in script
    assert "--retry 3" in script
    assert "curl --help all" in script
    assert "CURL_RETRY_ARGS+=(--retry-all-errors)" in script
    assert 'KOKORO_DIR=$(configured_kokoro_dir)' in script
    assert 'dest="$KOKORO_DIR/$f"' in script
    assert 'part="$KOKORO_DIR/.$f.part"' in script
    assert 'verify_sha256 "$dest" "$expected"' in script
    assert 'verify_sha256 "$part" "$expected"' in script
    assert 'verify_kokoro_install "$KOKORO_DIR"' in script
    assert 'dest="voices/$f"' not in script
    assert "mkdir -p voices" not in script
    assert 'rm -f "$dest"' in script
    assert "download_voices" not in script


def test_installer_gates_linux_to_the_supported_production_runtime():
    script = INSTALLER.read_text()
    gate = script[script.index("linux_runtime_supported()"):
                  script.index("# 1. Dependencies")]

    assert "x86_64|aarch64" in gate
    assert "^glibc\\ ([0-9]+)\\.([0-9]+)" in gate
    assert '[ "$minor" -ge 28 ]' in gate
    assert "requires x86_64 or aarch64 Linux with glibc 2.28+" in gate
    assert script.index("Unsupported Linux production host") < script.index(
        "# 1. Dependencies"
    )


@pytest.mark.parametrize(
    ("architecture", "libc"),
    (("x86_64", "glibc 2.28"), ("aarch64", "glibc 2.39")),
)
def test_installer_accepts_the_supported_linux_runtime(architecture, libc):
    result = subprocess.run(
        [
            "bash",
            "-c",
            _installer_prelude()
            + '\nlinux_runtime_supported "$1" "$2"',
            str(INSTALLER),
            architecture,
            libc,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("architecture", "libc"),
    (("riscv64", "glibc 2.39"), ("x86_64", "glibc 2.27"),
     ("x86_64", "musl 1.2.5")),
)
def test_installer_rejects_unsupported_linux_before_mutation(
    tmp_path,
    architecture,
    libc,
):
    if os.geteuid() == 0:
        pytest.skip("the root guard intentionally exits before platform preflight")

    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    shutil.copy(INSTALLER, checkout / "deploy" / "install.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        "  -s) printf 'Linux\\n' ;;\n"
        f"  -m) printf '{architecture}\\n' ;;\n"
        "esac\n"
    )
    fake_uname.chmod(0o755)
    fake_getconf = fake_bin / "getconf"
    fake_getconf.write_text(f"#!/bin/sh\nprintf '{libc}\\n'\n")
    fake_getconf.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_uv = fake_home / ".local" / "bin" / "uv"
    fake_uv.parent.mkdir(parents=True)
    marker = tmp_path / "uv-ran"
    fake_uv.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    fake_uv.chmod(0o755)

    result = subprocess.run(
        ["bash", str(checkout / "deploy" / "install.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )

    assert result.returncode == 1
    assert "Unsupported Linux production host" in result.stdout
    assert "glibc 2.28+" in result.stdout
    assert not marker.exists()
    assert not (checkout / "config").exists()
    assert not (checkout / "voices").exists()


def test_installer_rejects_other_operating_systems_before_mutation(tmp_path):
    if os.geteuid() == 0:
        pytest.skip("the root guard intentionally exits before platform preflight")

    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    shutil.copy(INSTALLER, checkout / "deploy" / "install.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text("#!/bin/sh\nprintf 'FreeBSD\\n'\n")
    fake_uname.chmod(0o755)
    fake_home = tmp_path / "home"
    fake_uv = fake_home / ".local" / "bin" / "uv"
    fake_uv.parent.mkdir(parents=True)
    marker = tmp_path / "uv-ran"
    fake_uv.write_text(f"#!/bin/sh\ntouch '{marker}'\n")
    fake_uv.chmod(0o755)

    result = subprocess.run(
        ["bash", str(checkout / "deploy" / "install.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
        env={
            **os.environ,
            "HOME": str(fake_home),
            "PATH": f"{fake_bin}:/usr/bin:/bin",
        },
    )

    assert result.returncode == 1
    assert "Unsupported operating system: FreeBSD" in result.stdout
    assert "Linux production and macOS direct development" in result.stdout
    assert not marker.exists()
    assert not (checkout / "config").exists()
    assert not (checkout / "voices").exists()


def test_installer_requires_real_kokoro_synthesis_before_service_install():
    script = INSTALLER.read_text()
    verifier = script[script.index("verify_kokoro_install()"):
                      script.index("linux_runtime_supported()")]
    verify = script.index("Verifying required Kokoro speech")
    service = script.index("# 6. Control-plane service")

    assert "verify_kokoro_synthesis" in verifier
    assert verify < service
    assert "Kokoro speech verification failed" in script[verify:service]
    assert "fallback speech cannot satisfy production" in script[verify:service]
    assert "exit 1" in script[verify:service]


def test_fresh_install_creates_every_private_runtime_directory_before_start():
    script = INSTALLER.read_text()
    create = script.index('for runtime_dir in \\\n')
    start = script.index('run_root systemctl start "$SERVICE_UNIT"')
    assert create < start
    for path in (
        '"$REPO_DIR/config"',
        '"$REPO_DIR/logs"',
        '"$RUNTIME_CACHE_DIR"',
        '"$RUNTIME_STATE_DIR"',
    ):
        assert path in script[create:start]
    assert "umask 077" in script
    assert "chmod 700" in script


def test_service_prerequisites_are_checked_before_installer_mutations():
    script = INSTALLER.read_text()
    preflight = script.index("systemctl show --property=Version")
    sudo = script.index("sudo -v")
    first_mutation = script.index('"$UV_BIN" sync --locked')
    assert preflight < first_mutation
    assert sudo < first_mutation


def test_installer_requires_uv_without_executing_a_remote_bootstrap():
    script = INSTALLER.read_text()
    assert "for tool in git; do" in script
    assert 'command -v curl >/dev/null || missing="$missing curl"' in script
    assert "UV_BIN=$(type -P uv || true)" in script
    assert '[ -x "$HOME/.local/bin/uv" ]; then' in script
    assert 'UV_BIN="$HOME/.local/bin/uv"' in script
    assert 'missing="$missing uv"' in script
    assert "astral.sh/uv/install.sh" not in script
    assert "curl -LsSf" not in script


def test_installer_uses_home_local_uv_for_every_command_when_path_has_none(
    tmp_path,
):
    """A uv discovered only by fallback must survive the entire installer."""
    checkout = tmp_path / "checkout"
    (checkout / "deploy").mkdir(parents=True)
    shutil.copy(INSTALLER, checkout / "deploy" / "install.sh")
    (checkout / ".env").write_text("BARKEEP_BIND=127.0.0.1\n")
    (checkout / ".env").chmod(0o600)

    fake_home = tmp_path / "home"
    fake_uv = fake_home / ".local" / "bin" / "uv"
    fake_uv.parent.mkdir(parents=True)
    fake_uv.write_text(
        "#!/bin/bash\n"
        'printf "%s\\n" "$*" >> "$UV_TEST_LOG"\n'
        'if [ "$1 $2" = "cache dir" ]; then\n'
        '  printf "%s\\n" "$UV_TEST_CACHE"\n'
        "fi\n"
        "exit 0\n"
    )
    fake_uv.chmod(0o755)

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_uname = fake_bin / "uname"
    fake_uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
    fake_uname.chmod(0o755)
    log = tmp_path / "uv.log"
    env = {
        **os.environ,
        "HOME": str(fake_home),
        "PATH": f"{fake_bin}:/usr/bin:/bin",
        "UV_TEST_CACHE": str(tmp_path / "uv-cache"),
        "UV_TEST_LOG": str(log),
    }

    result = subprocess.run(
        ["bash", str(checkout / "deploy" / "install.sh")],
        cwd=checkout,
        capture_output=True,
        text=True,
        env=env,
    )

    assert result.returncode == 0, result.stderr
    calls = log.read_text().splitlines()
    assert calls[0] == "sync --locked"
    assert "cache dir" in calls
    assert calls.count("run --no-sync python -") == 2

    script = INSTALLER.read_text()
    assert '"$UV_BIN" run --no-sync python - "$1"' in script
    assert '"$UV_BIN" run --no-sync python - "$1" "$2"' in script


def test_installer_refuses_root_before_selecting_a_service_user_or_mutating():
    script = INSTALLER.read_text()
    start = script.index('say "BUSY Bar Lab installer"')
    root_guard = script.index(
        'if [ "${EUID:-$(id -u)}" -eq 0 ]; then', start)
    service_user = script.index("SERVICE_USER=$(id -un)")
    first_mutation = script.index('"$UV_BIN" sync --locked')
    guard_body = script[root_guard:script.index("# 1. Dependencies", root_guard)]

    assert root_guard < service_user < first_mutation
    assert "Do not run this installer as root" in guard_body
    assert "unprivileged account" in guard_body
    assert "invokes sudo only" in guard_body


def test_an_offline_bar_does_not_prevent_a_unit_refresh():
    script = INSTALLER.read_text()
    probe_failure = script.index("Bar not reachable yet")
    service_render = script.index('if [ "$INSTALL_SVC" -eq 1 ]', probe_failure)
    assert "exit 1" not in script[probe_failure:service_render]
    assert "supervised apps will retry" in script[probe_failure:service_render]


def test_new_install_is_loopback_only_by_default():
    script = INSTALLER.read_text()
    assert 'write_env_line BARKEEP_BIND "127.0.0.1"' in script


def test_service_renderer_uses_actual_paths_and_escapes_systemd_specifiers(
        tmp_path):
    checkout = tmp_path / "checkout with \\ and 100% @CACHE_DIRECTORY@ bar"
    uv_bin = tmp_path / "tools" / "uv"
    cache = tmp_path / "cache volume"
    state = tmp_path / "state volume"
    uv_cache = tmp_path / "uv cache"
    output = tmp_path / "barkeep.service"
    checkout.mkdir()
    uv_bin.parent.mkdir()
    uv_bin.write_text("uv")

    subprocess.run(
        [
            sys.executable,
            str(SERVICE_RENDERER),
            "--template", str(SERVICE_TEMPLATE),
            "--checkout", str(checkout),
            "--uv", str(uv_bin),
            "--cache-dir", str(cache),
            "--state-dir", str(state),
            "--uv-cache-dir", str(uv_cache),
            "--output", str(output),
        ],
        check=True,
    )

    unit = output.read_text()
    escaped_checkout = str(checkout).replace("%", "%%")
    expected_contract = hashlib.sha256(
        b"template\0"
        + SERVICE_TEMPLATE.read_bytes()
        + b"\0renderer\0"
        + SERVICE_RENDERER.read_bytes()
    ).hexdigest()
    assert unit.startswith(
        f"# busybar-unit-contract-sha256={expected_contract}\n"
    )
    assert "@WORKING_DIRECTORY@" not in unit
    assert f"WorkingDirectory={escaped_checkout}" in unit
    assert 'WorkingDirectory="' not in unit
    assert f'ExecStart="{uv_bin}" run --no-sync -m barkeep' in unit
    assert f'Environment="BUSYBAR_CACHE_DIR={cache}"' in unit
    assert f'Environment="BUSYBAR_STATE_DIR={state}"' in unit
    assert 'ReadWritePaths=' in unit


def test_rendered_service_passes_the_real_systemd_parser(tmp_path):
    systemd_analyze = shutil.which("systemd-analyze")
    if systemd_analyze is None:
        pytest.skip("systemd-analyze is not available on this platform")

    checkout = tmp_path / "checkout"
    uv_bin = Path(shutil.which("true") or "/usr/bin/true")
    cache = tmp_path / "cache"
    state = tmp_path / "state"
    uv_cache = tmp_path / "uv-cache"
    output = tmp_path / "barkeep@.service"
    for directory in (
        checkout,
        checkout / "config",
        checkout / "logs",
        checkout / ".venv",
        cache,
        state,
        uv_cache,
    ):
        directory.mkdir()

    subprocess.run(
        [
            sys.executable,
            str(SERVICE_RENDERER),
            "--template", str(SERVICE_TEMPLATE),
            "--checkout", str(checkout),
            "--uv", str(uv_bin),
            "--cache-dir", str(cache),
            "--state-dir", str(state),
            "--uv-cache-dir", str(uv_cache),
            "--output", str(output),
        ],
        check=True,
    )

    verified = subprocess.run(
        [
            systemd_analyze,
            "verify",
            str(output),
        ],
        capture_output=True,
        text=True,
    )

    assert verified.returncode == 0, verified.stderr


def test_service_renderer_rejects_a_scalar_path_continuation(tmp_path):
    checkout = tmp_path / "checkout\\"
    checkout.mkdir()

    rendered = subprocess.run(
        [
            sys.executable,
            str(SERVICE_RENDERER),
            "--template", str(SERVICE_TEMPLATE),
            "--checkout", str(checkout),
            "--uv", sys.executable,
            "--cache-dir", str(tmp_path / "cache"),
            "--state-dir", str(tmp_path / "state"),
            "--uv-cache-dir", str(tmp_path / "uv-cache"),
            "--output", str(tmp_path / "barkeep@.service"),
        ],
        capture_output=True,
        text=True,
    )

    assert rendered.returncode != 0
    assert "cannot end in a backslash" in rendered.stderr


def test_installer_validates_the_staged_unit_before_replacing_the_live_one():
    script = INSTALLER.read_text()
    rendered = script.index('deploy/render_service.py')
    verified = script.index(
        'systemd-analyze verify "$SERVICE_TMP"',
        rendered,
    )
    installed = script.index(
        'run_root install -m 0644 "$SERVICE_TMP"', verified
    )

    assert rendered < verified < installed
    assert 'SERVICE_TMP="$SERVICE_TMP_DIR/barkeep@.service"' in script
    assert "installed unit and running service were left unchanged" in script


def test_tzdata_keeps_iana_timezones_available_without_an_os_database():
    env = dict(os.environ)
    env["PYTHONTZPATH"] = ""
    result = subprocess.run(
        [sys.executable, "-c",
         "from zoneinfo import ZoneInfo; print(ZoneInfo('Europe/London').key)"],
        check=True,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.stdout == "Europe/London\n"


def test_release_version_lock_and_changelog_agree():
    project = tomllib.loads((REPO / "pyproject.toml").read_text())
    lock = tomllib.loads((REPO / "uv.lock").read_text())
    version = project["project"]["version"]
    locked_project = next(
        package for package in lock["package"]
        if package["name"] == project["project"]["name"]
    )

    assert locked_project["version"] == version
    assert f"## v{version} " in (REPO / "CHANGELOG.md").read_text()
    runtime_dependencies = {
        item["name"] for item in locked_project["dependencies"]
    }
    assert {"httpx", "websockets", "tzdata", "kokoro-onnx"} <= (
        runtime_dependencies
    )
    assert project["project"]["requires-python"] == ">=3.11,<3.14"
    assert lock["requires-python"] == ">=3.11, <3.14"
    assert "optional-dependencies" not in project["project"]
    assert "optional-dependencies" not in locked_project

    kokoro_requirement = next(
        requirement for requirement in project["project"]["dependencies"]
        if requirement.startswith("kokoro-onnx")
    )
    assert "platform_system == 'Linux'" in kokoro_requirement
    assert "platform_machine" not in kokoro_requirement
    locked_kokoro = next(
        dependency for dependency in locked_project["dependencies"]
        if dependency["name"] == "kokoro-onnx"
    )
    assert locked_kokoro["marker"] == "sys_platform == 'linux'"

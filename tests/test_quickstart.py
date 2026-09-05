"""Keep the documented first-run commands executable and safe without a bar.

CI already supplies the locked Python environment. These tests execute the
documented entry points with that interpreter; clone/uv installation is a
separate clean-environment smoke, not a network dependency of this suite.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]


def documented_block(path: str, name: str) -> str:
    text = (ROOT / path).read_text()
    match = re.search(
        rf"<!-- quickstart:{re.escape(name)} -->\s*```bash\n(.*?)\n```",
        text,
        re.DOTALL,
    )
    assert match, f"missing executable {name} example in {path}"
    return match[1]


@pytest.mark.parametrize(
    "name", ["setup", "config", "editor", "config-check", "start", "install"]
)
def test_readme_and_walkthrough_use_the_same_first_commands(name):
    assert documented_block("README.md", name) == documented_block(
        "docs/quickstart.md", name
    )


@pytest.fixture
def clean_checkout(tmp_path):
    """Copy source, never owner env/state, installed packages or generated media."""
    repo = tmp_path / "busybar-lab"
    repo.mkdir()
    for directory in (
        "apps",
        "barkeep",
        "busybar_dev",
        "busybar_viz",
        "scripts",
        "deploy",
    ):
        shutil.copytree(
            ROOT / directory,
            repo / directory,
            ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
        )
    for filename in (
        "AGENTS.md",
        "apps.toml",
        "pyproject.toml",
        "viz-baselines.toml",
        ".env.example",
    ):
        shutil.copy2(ROOT / filename, repo / filename)
    assert not (repo / ".env").exists()
    return repo


def run_offline_python(repo: Path, command: str):
    words = shlex.split(command)
    assert words[:2] == ["uv", "run"], f"not a Python quickstart command: {command}"
    if words[2] == "python":
        assert words[3:5] == ["-m", "deploy.check_setup"]
        arguments = words[3:]
    else:
        assert words[2].endswith(".py")
        arguments = words[2:]
    guard = repo / "test-guard"
    guard.mkdir(exist_ok=True)
    (guard / "sitecustomize.py").write_text(
        "import sys\n"
        "def deny_network(event, args):\n"
        "    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}:\n"
        "        print('QUICKSTART_NETWORK_DENIED', file=sys.stderr, flush=True)\n"
        "        raise RuntimeError('quickstart must not use the network')\n"
        "sys.addaudithook(deny_network)\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("BUSYBAR_", "BARKEEP_", "SKYSTRIP_", "DSN_"))
    }
    env["PYTHONPATH"] = os.pathsep.join((str(guard), str(repo)))
    result = subprocess.run(
        [sys.executable, *arguments],
        cwd=repo,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert "QUICKSTART_NETWORK_DENIED" not in result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    return result


def test_documented_hello_and_preview_work_without_configuration_or_network(
    clean_checkout,
):
    setup = documented_block("README.md", "setup").splitlines()
    assert setup == [
        "git clone https://github.com/subjektz3ro/busybar-lab.git",
        "cd busybar-lab",
        "uv sync --locked",
    ]
    hello = run_offline_python(clean_checkout, documented_block("README.md", "offline"))
    assert "Dry run payload:" in hello.stderr
    assert "HELLO" in hello.stderr

    preview = documented_block("README.md", "preview").splitlines()
    assert preview[0] == "mkdir -p scratch"
    assert len(preview) == 2
    (clean_checkout / "scratch").mkdir()
    result = run_offline_python(clean_checkout, preview[1])
    assert "saved scratch/sky.png" in result.stdout
    assert "SKYSTRIP_LAT/SKYSTRIP_LON are not set" in result.stderr
    with Image.open(clean_checkout / "scratch" / "sky.png") as frame:
        assert frame.format == "PNG"
        assert frame.size == (72 * 8, 16 * 8)
    assert not (clean_checkout / ".env").exists()


def test_documented_configuration_check_works_offline(clean_checkout):
    result = run_offline_python(
        clean_checkout, documented_block("README.md", "config-check")
    )
    assert "[PASS] Configuration checks passed" in result.stdout
    assert "Skystrip has no weather location" in result.stdout
    assert not (clean_checkout / ".env").exists()


def test_readme_contains_the_whole_owner_path_before_optional_demos():
    text = (ROOT / "README.md").read_text()
    quickstart = text.split("## Quickstart", 1)[1].split("## Included apps", 1)[0]
    for step in (
        "Connect your bar",
        "Download and install",
        "Set your weather location",
        "Start Barkeep",
        "Open Barkeep and start Skystrip",
    ):
        assert step in quickstart
    for action in (
        "uv sync --locked",
        "nano .env",
        "uv run -m barkeep",
        "./deploy/install.sh",
        "http://127.0.0.1:8080",
        "click **skystrip**",
    ):
        assert action in quickstart
    assert "--dry-run" not in quickstart
    assert "--preview" not in quickstart
    assert text.index("## Quickstart") < text.index("## Try without a bar")


def test_documented_launch_and_diagnostic_commands_use_production_entrypoints():
    assert documented_block("README.md", "start") == "uv run -m barkeep"
    assert (ROOT / "barkeep" / "__main__.py").is_file()
    assert documented_block("README.md", "install") == "./deploy/install.sh"
    assert (ROOT / "deploy" / "install.sh").stat().st_mode & 0o111
    assert (
        documented_block("docs/troubleshooting.md", "diagnostic")
        == "uv run python -m deploy.check_setup"
    )
    assert documented_block("docs/troubleshooting.md", "hello").splitlines() == [
        "uv run apps/hello.py --dry-run",
        "uv run apps/hello.py",
    ]
    assert (
        documented_block("docs/troubleshooting.md", "clear")
        == "uv run apps/hello.py --clear"
    )


def test_documented_scaffold_registers_an_app_that_can_dry_run(clean_checkout):
    commands = documented_block("README.md", "scaffold").splitlines()
    assert len(commands) == 2
    for command in commands:
        run_offline_python(clean_checkout, command)
    registry = tomllib.loads((clean_checkout / "apps.toml").read_text())
    assert registry["yourapp"]["entrypoint"] == "apps/yourapp.py"
    assert registry["yourapp"]["kind"] == "foreground"
    assert (clean_checkout / "apps" / "yourapp.py").is_file()


def test_documented_config_setup_is_private_and_preserves_existing_values(
    clean_checkout,
):
    command = documented_block("docs/quickstart.md", "config")
    env_path = clean_checkout / ".env"
    for expected in (
        (clean_checkout / ".env.example").read_text(),
        "# Existing operator settings must survive rerunning the guide.\n",
    ):
        if env_path.exists():
            env_path.write_text(expected)
        subprocess.run(["bash", "-e", "-c", command], cwd=clean_checkout, check=True)
        assert env_path.read_text() == expected
        assert env_path.stat().st_mode & 0o777 == 0o600


def heading_ids(text: str) -> set[str]:
    # These guides use plain Markdown headings. Exclude code-block comments.
    prose = re.sub(r"```.*?```", "", text, flags=re.DOTALL)
    headings = re.findall(r"^#{1,6} (.+)$", prose, re.MULTILINE)
    return {
        re.sub(r"\s", "-", re.sub(r"[^\w\s-]", "", heading.lower()))
        for heading in headings
    }


@pytest.mark.parametrize(
    "document",
    [
        "README.md",
        "docs/quickstart.md",
        "docs/troubleshooting.md",
        "docs/gallery.md",
        "docs/README.md",
        "CONTRIBUTING.md",
        "deploy/README.md",
    ],
)
def test_onboarding_links_and_section_targets_resolve(document):
    source = ROOT / document
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", source.read_text()):
        parsed = urlsplit(target)
        if parsed.scheme or parsed.netloc:
            continue
        path = source.parent / unquote(parsed.path) if parsed.path else source
        assert path.exists(), f"{document}: missing link target {target}"
        if parsed.fragment and path.suffix == ".md":
            assert unquote(parsed.fragment) in heading_ids(path.read_text()), (
                f"{document}: missing section target {target}"
            )

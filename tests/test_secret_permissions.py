"""The bar token must not be readable by every local account.

Found on the live host: .env at 0644 holding BUSYBAR_TOKEN and the
coordinates, while barkeep's own config/<app>.env files — written through
mkstemp, which creates 0600 — were correctly restricted. The less sensitive
file was the better protected one, because install.sh wrote the other under
the default umask.
"""

from __future__ import annotations

import re
from pathlib import Path

from busybar_dev import secret_file_warning

ROOT = Path(__file__).resolve().parents[1]


def _device_token_assignment(value: str) -> str:
    """Build a synthetic secret line without committing a literal one."""
    return "BUSYBAR_" + f"TOKEN={value}\n"


def test_a_world_readable_secret_file_is_reported(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_device_token_assignment("abc"))
    env.chmod(0o644)
    warning = secret_file_warning(env)
    assert warning, "0644 must be reported"
    assert "chmod 600" in warning


def test_a_group_readable_secret_file_is_reported(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_device_token_assignment("abc"))
    env.chmod(0o640)
    assert secret_file_warning(env), "group-readable must be reported too"


def test_an_owner_only_secret_file_is_silent(tmp_path):
    env = tmp_path / ".env"
    env.write_text(_device_token_assignment("abc"))
    env.chmod(0o600)
    assert secret_file_warning(env) == ""


def test_a_missing_file_is_silent(tmp_path):
    assert secret_file_warning(tmp_path / "nope.env") == ""


def test_the_warning_never_leaks_the_token(tmp_path):
    env = tmp_path / ".env"
    secret = "supersecretpin"
    env.write_text(
        _device_token_assignment(secret) + "SKYSTRIP_LAT=51.5074\n"
    )
    env.chmod(0o644)
    warning = secret_file_warning(env)
    assert secret not in warning
    assert not re.search(r"\d{1,3}\.\d{4,}", warning), "no coordinates either"


def test_the_installer_never_exposes_the_file_it_writes():
    """Owner-only from the first byte, not merely after a later chmod.

    An earlier version of this test asserted `chmod 600 .env` after the write.
    That is the weaker property: between the redirect and the chmod the file
    exists at the default umask, and an interrupted install leaves it there.
    The installer now writes through a same-directory temp file under
    `umask 077` and renames it into place, so there is no window at all.
    """
    script = (ROOT / "deploy" / "install.sh").read_text()
    assert "umask 077" in script, "the .env write is not umask-protected"
    assert "mktemp" in script, "the .env write is not staged through a temp file"
    assert "mv " in script and ".env" in script, "the .env write is not atomic"
    # And nothing may redirect straight onto the real path.
    assert "} > .env" not in script, (
        "install.sh writes .env directly again; that reintroduces the window")

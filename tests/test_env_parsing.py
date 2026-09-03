"""All entry points share one predictable .env parser.

Three implementations disagreed on quotes and on blank values, and barkeep
called the one that dropped blanks and kept quotes, then handed its own
environment to every child it spawned. A quoted coordinate therefore reached
skystrip as the literal string `"51.51"`, and the child could not correct it
because its own parser used setdefault. `float('"51.51"')` raised at import:
a crash loop with the display dark, and only under barkeep — running the same
app by hand with the same file worked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from barkeep.configstore import child_env, parse_env_text as app_parse
from busybar_dev.config import load_env, parse_env_text, read_env_file

QUOTED = 'SKYSTRIP_LAT="51.51"\nSKYSTRIP_TZ="Europe/London"\n'

# Every key any fixture in this file can introduce. load_env() sets keys via
# os.environ.setdefault, so monkeypatch only restores the ones it was told
# about — deleting SKYSTRIP_LAT and then loading a file that also carries
# SKYSTRIP_LON and SKYSTRIP_TZ leaks those two into every later test. That
# leak broke an unrelated import test three files away, which is exactly how
# expensive this class of bug is to find.
TOUCHED = ("SKYSTRIP_LAT", "SKYSTRIP_LON", "SKYSTRIP_TZ", "SKYSTRIP_CONTACT")


@pytest.fixture(autouse=True)
def _isolate_env():
    """Snapshot and restore, rather than monkeypatch.delenv.

    `monkeypatch.delenv(key, raising=False)` on a key that is ALREADY absent
    registers nothing to undo — so a key that `load_env()` creates during the
    test survives teardown. That is what happened: this file's fixture carries
    SKYSTRIP_LAT and SKYSTRIP_TZ but no SKYSTRIP_LON, and the orphaned LAT made
    an import test three files away fail with "must be configured together".
    """
    saved = {key: os.environ.get(key) for key in TOUCHED}
    for key in TOUCHED:
        os.environ.pop(key, None)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def test_the_hand_edited_env_strips_matched_quotes():
    values = parse_env_text(QUOTED, strip_quotes=True)
    assert values["SKYSTRIP_LAT"] == "51.51"
    assert values["SKYSTRIP_TZ"] == "Europe/London"
    assert float(values["SKYSTRIP_LAT"]) == pytest.approx(51.51)


def test_the_machine_written_override_file_does_not():
    """barkeep's UI writes values verbatim; stripping would break the
    round-trip between what an operator types and what they see back."""
    assert app_parse('SKY_VOICE="hello"')["SKY_VOICE"] == '"hello"'


def test_only_matched_pairs_are_stripped():
    for raw, want in (
        ('K="a"', "a"),
        ("K='a'", "a"),
        ('K="a', '"a'),          # unbalanced stays put
        ('K=a"', 'a"'),
        ('K="', '"'),            # a lone quote is not a pair
        ('K=""', ""),
        ("K=say \"hi\"", 'say "hi"'),
    ):
        assert parse_env_text(raw, strip_quotes=True)["K"] == want, raw


def test_a_blank_value_survives_every_layer():
    """`KEY=` is the documented way to say 'explicitly blank'. The old
    busybar_dev parser dropped it, which made the distinction between blank
    and missing unrepresentable for anything launched through barkeep."""
    assert parse_env_text("SKYSTRIP_CONTACT=")["SKYSTRIP_CONTACT"] == ""
    assert app_parse("SKYSTRIP_CONTACT=")["SKYSTRIP_CONTACT"] == ""


def test_comments_blank_lines_and_bare_keys_are_ignored():
    text = "# a comment\n\nNOVALUE\n  SPACED = value \nK=v\n"
    values = parse_env_text(text)
    assert values == {"SPACED": "value", "K": "v"}


def test_both_launch_paths_agree_on_the_same_file(tmp_path, monkeypatch):
    """The regression itself: what barkeep puts in a child's environment must
    equal what the app would have read for itself."""
    env = tmp_path / ".env"
    env.write_text(QUOTED + "SKYSTRIP_CONTACT=\n")

    # Path A — the app run by hand.
    for key in ("SKYSTRIP_LAT", "SKYSTRIP_TZ", "SKYSTRIP_CONTACT"):
        monkeypatch.delenv(key, raising=False)
    direct = dict(load_env(env))

    # Path B — barkeep loads it, then spawns a child with its environment.
    for key in ("SKYSTRIP_LAT", "SKYSTRIP_TZ", "SKYSTRIP_CONTACT"):
        monkeypatch.delenv(key, raising=False)
    load_env(env)
    spawned = child_env({}, os.environ)

    for key, value in direct.items():
        assert spawned[key] == value, f"{key} differs between launch paths"
    assert spawned["SKYSTRIP_LAT"] == "51.51"


def test_a_quoted_coordinate_no_longer_raises_at_import(tmp_path, monkeypatch):
    """The exact failure: float() on the value barkeep used to pass through."""
    env = tmp_path / ".env"
    env.write_text(QUOTED)
    monkeypatch.delenv("SKYSTRIP_LAT", raising=False)
    load_env(env)
    value = child_env({}, os.environ)["SKYSTRIP_LAT"]
    assert float(value) == pytest.approx(51.51)

    from zoneinfo import ZoneInfo
    ZoneInfo(child_env({}, os.environ)["SKYSTRIP_TZ"])   # must not raise


def test_a_missing_file_is_not_an_error(tmp_path):
    assert load_env(tmp_path / "absent.env") == {}
    assert read_env_file(tmp_path / "absent.env") == {}


def test_existing_environment_wins(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("SKYSTRIP_TZ=UTC\n")
    monkeypatch.setenv("SKYSTRIP_TZ", "Europe/Berlin")
    load_env(env)
    assert os.environ["SKYSTRIP_TZ"] == "Europe/Berlin"


def test_there_is_exactly_one_parser_implementation():
    """Guard against a fourth copy appearing."""
    root = Path(__file__).resolve().parents[1]
    sources = [
        root / "apps" / "skystrip.py",
        root / "apps" / "dsn.py",
        root / "barkeep" / "configstore.py",
        root / "busybar_dev" / "__init__.py",
    ]
    for path in sources:
        text = path.read_text()
        assert "line.partition(\"=\")" not in text, (
            f"{path.name} parses .env lines itself; use busybar_dev.config")

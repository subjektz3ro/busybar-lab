"""Nothing personal in tracked files, enforced rather than remembered.

This repository is meant to be shared. The failure mode is not dramatic — it
is someone's home IP range surviving in a diagram, or a contact address left
in a User-Agent because it was convenient once. Both have happened here.

So the rule is a test. It reads what git actually tracks, which means it also
catches things that were never opened in an editor.
"""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]


def _release_checker():
    path = REPO / "scripts" / "check_public_release.py"
    spec = importlib.util.spec_from_file_location("_privacy_release_checker", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


_PUBLIC_RELEASE = _release_checker()
CONTACT_EMAIL_RULE_NAME = _PUBLIC_RELEASE.CONTACT_EMAIL_RULE_NAME
PRIVATE_IP_RULE_NAME = _PUBLIC_RELEASE.PRIVATE_IP_RULE_NAME
HOSTNAME_RULE_NAME = _PUBLIC_RELEASE.HOSTNAME_RULE_NAME
COORDINATE_RULE_NAME = _PUBLIC_RELEASE.COORDINATE_RULE_NAME
PUBLIC_COORDINATE_FIXTURES = _PUBLIC_RELEASE.PUBLIC_COORDINATE_FIXTURES
content_rule_names = _PUBLIC_RELEASE.content_rule_names
coordinate_matches = _PUBLIC_RELEASE.coordinate_matches
is_public_coordinate_fixture = _PUBLIC_RELEASE.is_public_coordinate_fixture
personal_data_rule_names = _PUBLIC_RELEASE.personal_data_rule_names

# Third-party material retained verbatim is excluded only from the older
# worktree-based structural tests below. The release scanner checks its staged
# blobs and grants only exact rule/path/value exceptions.
VENDORED = ("docs/busylib/", "uv.lock")

SELF = "tests/test_no_personal_data.py"


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files"], cwd=REPO, check=True,
                         capture_output=True, text=True).stdout.split()
    return [Path(f) for f in out
            if not f.startswith(VENDORED) and f != SELF
            and not f.endswith((".png", ".gif", ".snd", ".anim"))]


def read(path: Path) -> str:
    try:
        return (REPO / path).read_text(errors="ignore")
    except (OSError, UnicodeDecodeError):
        return ""


def read_bytes(path: Path) -> bytes:
    try:
        return (REPO / path).read_bytes()
    except OSError:
        return b""


def test_contact_address_detector_uses_the_release_scanner():
    """Contact checks operate on bytes supplied by the index-safe scanner."""
    address = b"operator" + b"@" + b"private.internal"

    assert personal_data_rule_names(
        b"contact=" + address,
        PurePosixPath("README.md"),
    ) == (CONTACT_EMAIL_RULE_NAME,)
    assert personal_data_rule_names(
        b"contact=operator@example.invalid",
        PurePosixPath("README.md"),
    ) == ()


def test_private_ip_detector_uses_the_release_scanner():
    """Private IPv4 and IPv6 literals use the staged-byte detector."""
    private_address = b"192.168." + b"44.9"

    assert personal_data_rule_names(
        b"endpoint=" + private_address,
        PurePosixPath("README.md"),
    ) == (PRIVATE_IP_RULE_NAME,)
    assert personal_data_rule_names(
        b"endpoint=10.0.4.20",
        PurePosixPath("README.md"),
    ) == ()

    private_ipv6_values = (
        b"fc00" + b"::1",
        b"fdff" + b"::1",
        b"[fe80" + b"::1]",
        b"fe80" + b"::1%en0",
        b"[febf" + b"::1%25en0]:8080",
    )
    for address in private_ipv6_values:
        assert PRIVATE_IP_RULE_NAME in personal_data_rule_names(
            b"endpoint=" + address,
            PurePosixPath("README.md"),
        )

    for address in (b"2001:db8::1", b"::1"):
        assert personal_data_rule_names(
            b"endpoint=" + address,
            PurePosixPath("README.md"),
        ) == ()


def test_hostname_detector_uses_only_synthetic_identifiers():
    """LAN names are caught without retaining an operator-host fingerprint."""
    local_host = b"owner-workstation" + b".local"
    home_arpa_host = b"owner-workstation" + b".home." + b"arpa"
    lan_host = b"owner-workstation" + b".lan"

    for host in (local_host, home_arpa_host, lan_host):
        for payload in (
            b"ssh " + host,
            b"deploy to " + host + b".",
            b"deploy to " + host + b" (primary)",
            b"BUSYBAR_HOST=" + host,
        ):
            assert HOSTNAME_RULE_NAME in personal_data_rule_names(
                payload,
                PurePosixPath("README.md"),
            )

    option_commands = (
        b"ssh -p 2222 -i /tmp/key owner-workstation",
        b"ssh -vvvp2222 owner-workstation",
        b"ssh -J owner-jump destination.example",
        b"ssh -o ProxyJump=owner-jump destination.example",
        b"ssh -o 'Hostname owner-workstation' destination.example",
        b"ssh -W owner-target:22 jump.example",
    )
    for command in option_commands:
        assert HOSTNAME_RULE_NAME in personal_data_rule_names(
            command,
            PurePosixPath("README.md"),
        )

    assert personal_data_rule_names(
        b"deploy to the host\n"
        b"BUSYBAR_HOST=device.example\n"
        b'ssh -p 2222 "$HOST"\n'
        b"ssh -J none destination.example",
        PurePosixPath("README.md"),
    ) == ()


def test_apps_do_not_hardcode_a_bar_address():
    """Every device call goes through connect(), which resolves BUSYBAR_HOST.
    That is what lets the same app run against a bar on your desk over USB and
    one on a server across the network."""
    offenders = []
    for path in tracked_files():
        if path.suffix != ".py" or not str(path).startswith(("apps/", "barkeep/")):
            continue
        for line_no, line in enumerate(read(path).splitlines(), 1):
            rules = personal_data_rule_names(
                line.encode(),
                PurePosixPath(path.as_posix()),
            )
            if PRIVATE_IP_RULE_NAME in rules or "busybar.local" in line:
                if line.lstrip().startswith("#"):
                    continue          # explaining the gotcha is fine
                offenders.append(f"{path}:{line_no}: {line.strip()[:90]}")
    assert not offenders, ("an app names a bar directly instead of using "
                           "connect():\n  " + "\n  ".join(offenders))


def test_the_example_env_documents_keys_without_values():
    """.env.example is the contract for what is configurable. It must not
    become a copy of somebody's actual .env."""
    example = REPO / ".env.example"
    assert example.exists(), ".env.example is how a newcomer learns the keys"
    for line_no, line in enumerate(example.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        rules = personal_data_rule_names(
            line.encode(),
            PurePosixPath(".env.example"),
        )
        assert CONTACT_EMAIL_RULE_NAME not in rules, (
            f".env.example:{line_no}: real address"
        )
        assert PRIVATE_IP_RULE_NAME not in rules, (
            f".env.example:{line_no}: real host"
        )
        assert HOSTNAME_RULE_NAME not in rules, (
            f".env.example:{line_no}: operator hostname"
        )


# --- coordinates -----------------------------------------------------------
#
# AGENTS.md says "No addresses, coordinates, hostnames, or tokens in anything
# git tracks." This file enforced three of those four. Coordinates — the item
# listed first — were not checked at all.

def test_no_decimal_coordinates_are_committed():
    """A latitude and longitude together may be somebody's window.

    Unlike the older line-only regex, the release detector also catches
    labelled assignments split across lines. This guard deliberately includes
    its own source; synthetic private examples must therefore be assembled at
    runtime rather than becoming exemptions hidden inside the detector.
    """
    found = []
    paths = [*tracked_files(), Path(SELF)]
    for path in paths:
        data = read_bytes(path)
        for match in coordinate_matches(data):
            if is_public_coordinate_fixture(match.latitude, match.longitude):
                continue
            line_no = data.count(b"\n", 0, match.offset) + 1
            found.append(f"{path}:{line_no}")
    assert not found, ("a coordinate pair is committed:\n  "
                       + "\n  ".join(found))


def test_the_coordinate_detector_still_detects():
    """The guard carries allowlists, and an allowlist is how a detector
    quietly stops detecting. Prove the patterns still fire on the exact
    things they exist to catch, and that each exemption is narrow: the
    luma-coefficient entry must not excuse a real coordinate pair.
    """
    synthetic_lat = b"12." + b"3456"
    synthetic_lon = b"-65." + b"4321"
    samples = (
        synthetic_lat + b", " + synthetic_lon,
        synthetic_lat + b" " + synthetic_lon + b"\n",
        b'{"type":"Point","coordinates":['
        + synthetic_lon + b"," + synthetic_lat + b"]}",
        b"POINT (" + synthetic_lon + b" " + synthetic_lat + b")",
        b"latitude=" + synthetic_lat + b", longitude=" + synthetic_lon,
        b"HOME_LATITUDE = " + synthetic_lat
        + b"\nHOME_LONGITUDE = " + synthetic_lon,
        b"longitude=" + synthetic_lon + b"\nlatitude=" + synthetic_lat,
    )
    for sample in samples:
        matches = coordinate_matches(sample)
        assert matches, "the coordinate detector went blind"
        assert all(
            not is_public_coordinate_fixture(match.latitude, match.longitude)
            for match in matches
        )

    # Every allowlist entry is an exact pair, not a latitude or longitude that
    # can be recombined with a private component.
    for public_lat, public_lon in PUBLIC_COORDINATE_FIXTURES:
        assert is_public_coordinate_fixture(public_lat, public_lon)
        assert not is_public_coordinate_fixture(public_lat, synthetic_lon)
        assert not is_public_coordinate_fixture(synthetic_lat, public_lon)


def test_owner_environment_variants_are_ignored_but_the_template_is_visible():
    """Local env variants stay private; the empty newcomer template ships."""
    for relative in (
        ".env.local",
        ".env.production",
        "operator.env",
        ".playwright-cli/session/state.json",
        ".superpowers/tasks/release-report.md",
    ):
        ignored = subprocess.run(
            ["git", "check-ignore", "--no-index", "--quiet", "--", relative],
            cwd=REPO,
            check=False,
        )
        assert ignored.returncode == 0, f"{relative} is not ignored"

    template = subprocess.run(
        ["git", "check-ignore", "--no-index", "--quiet", "--", ".env.example"],
        cwd=REPO,
        check=False,
    )
    assert template.returncode == 1, ".env.example must remain publishable"


def test_privacy_guard_source_is_safe_for_the_release_rules():
    data = read_bytes(Path(SELF))
    rules = content_rule_names(data, PurePosixPath(SELF))
    assert not {
        CONTACT_EMAIL_RULE_NAME,
        PRIVATE_IP_RULE_NAME,
        HOSTNAME_RULE_NAME,
    } & set(rules)
    matches = coordinate_matches(data)
    assert all(
        is_public_coordinate_fixture(match.latitude, match.longitude)
        for match in matches
    ), COORDINATE_RULE_NAME


def test_the_coordinate_bearing_pollers_never_log_an_exception_verbatim():
    """httpx puts the full request URL in HTTPStatusError.__str__, and these
    three functions are the ones whose URLs carry the coordinates:

        .../alerts/active?point=<lat>%2C<lon>
        .../points/<lat>,<lon>
        .../v1/forecast?latitude=<lat>&longitude=<lon>

    Those lines reach barkeep's log endpoint, which an operator may expose to
    a LAN. They must go through describe_exception, which reports the type and
    status code and never the exception's own message.

    Deliberately scoped rather than repo-wide: a failed asset upload or a
    refused draw carries no coordinates, and banning `exc` everywhere would be
    noise that gets suppressed rather than a rule that holds. The general
    backstop is CoordinateRedactingFilter, installed on the root handlers.
    """
    import ast

    source = read(Path("apps/skystrip.py"))
    tree = ast.parse(source)
    watched = {"poll_alerts", "poll_nws", "poll_radar"}
    offenders = []
    for node in ast.walk(tree):
        name = getattr(node, "name", None)
        if name not in watched:
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            if not (isinstance(func, ast.Attribute)
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "logger"):
                continue
            for arg in call.args[1:]:
                if isinstance(arg, ast.Name) and arg.id == "exc":
                    offenders.append(f"{name}: line {call.lineno}")
    assert not offenders, (
        "a coordinate-bearing poller logs its exception verbatim; use "
        "busybar_dev.config.describe_exception:\n  " + "\n  ".join(offenders))


def test_the_redaction_filter_scrubs_a_record():
    from busybar_dev.config import CoordinateRedactingFilter
    import logging as _logging

    record = _logging.LogRecord(
        "t", _logging.WARNING, __file__, 1,
        "failed for url 'https://api.weather.gov/alerts/active"
        "?point=51.5074%2C-0.1278'", (), None)
    CoordinateRedactingFilter().filter(record)
    assert "51.5074" not in record.getMessage()
    assert "<lat>,<lon>" in record.getMessage()

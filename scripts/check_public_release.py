#!/usr/bin/env python3
"""Fail when the tracked snapshot contains private or generated material.

This is intentionally an index-snapshot check, not a history scanner. Git
chooses both the file list and immutable blob bytes, so an unstaged worktree
replacement cannot hide what the next commit would contain and ignored owner
data is never opened. Nonignored untracked paths fail closed without having
their bytes read. Findings name the rule and path only. Matched content is
never printed.

    uv run scripts/check_public_release.py
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Sequence
from urllib.parse import urlsplit


REPO_ROOT = Path(__file__).resolve().parent.parent

# Every exception is rule-specific and carries its public rationale. Adding a
# fixture here is a reviewable choice; it never exempts that file from content
# scanning or from another path rule.
PATH_RULE_ALLOWLIST: dict[tuple[str, str], str] = {
    ("private-environment-file", ".env.example"): (
        "documented public configuration template; values remain content-scanned"
    ),
}

# RFC 6761 reserves the bare `.example` TLD. Redaction tests deliberately use
# credential-bearing URLs on relay.example; a userinfo URL on any real domain
# still fails. This exception applies only to that one content rule.
PUBLIC_CREDENTIAL_FIXTURE_SUFFIXES = (b".example",)

CONTACT_EMAIL_RULE_NAME = "identity.contact-email"
PRIVATE_IP_RULE_NAME = "network.private-ip"
HOSTNAME_RULE_NAME = "network.operator-hostname"

# These values are public by definition, not merely convenient exceptions.
# Reserved DNS names are safe synthetic fixtures; the BUSY Bar USB/mDNS
# endpoints are identical on every unit and therefore reveal no operator
# network detail.
PUBLIC_DNS_SUFFIXES: dict[bytes, str] = {
    b".example": "RFC 6761 documentation-only namespace",
    b".invalid": "RFC 2606 guaranteed-invalid namespace",
    b".test": "RFC 2606 testing namespace",
    b".localhost": "RFC 6761 loopback namespace",
}
PUBLIC_DNS_HOSTS: dict[bytes, str] = {
    b"example.com": "RFC 2606 documentation domain",
    b"example.net": "RFC 2606 documentation domain",
    b"example.org": "RFC 2606 documentation domain",
    b"localhost": "local loopback name",
    b"busybar.local": "vendor-defined mDNS name shared by every BUSY Bar",
}
PUBLIC_PRIVATE_IPS: dict[bytes, str] = {
    b"10.0.4.20": "vendor-defined USB address shared by every BUSY Bar",
}

# Rule/path/value exceptions are exact and reviewable. Values are assembled so
# this scanner's own source never looks like a match that must exempt itself.
PUBLIC_CONTENT_MATCHES: dict[tuple[str, str, bytes], str] = {
    (
        CONTACT_EMAIL_RULE_NAME,
        "deploy/README.md",
        b"git" + b"@" + b"github.com",
    ): "GitHub SSH transport syntax, not a contact address",
    (
        PRIVATE_IP_RULE_NAME,
        "docs/busylib/README.md",
        b"192.168." + b"1.20",
    ): "verbatim upstream busylib connection example",
    (
        PRIVATE_IP_RULE_NAME,
        "docs/busylib/README.md",
        b"192.168." + b"100.2",
    ): "verbatim upstream busylib discovery example",
    (
        PRIVATE_IP_RULE_NAME,
        "docs/busylib/guides/connecting.md",
        b"192.168." + b"1.20",
    ): "verbatim upstream busylib connection example",
    (
        PRIVATE_IP_RULE_NAME,
        "docs/busylib/api/discovery.md",
        b"192.168." + b"1.20",
    ): "verbatim upstream busylib discovery example",
    (
        PRIVATE_IP_RULE_NAME,
        "tests/test_server.py",
        b"fe80" + b"::1",
    ): "synthetic IPv6 host-and-port parsing fixture",
    (
        PRIVATE_IP_RULE_NAME,
        "tests/test_server.py",
        b"[" + b"fe80" + b"::1]",
    ): "synthetic bracketed IPv6 host-and-port parsing fixture",
    (
        HOSTNAME_RULE_NAME,
        "deploy/ship.sh",
        b"pi" + b".local",
    ): "synthetic command-line example for a LAN deployment target",
    (
        HOSTNAME_RULE_NAME,
        "docs/busybar-viz.md",
        b"review-box" + b".local",
    ): "synthetic LAN review-host example",
    (
        HOSTNAME_RULE_NAME,
        "tests/test_server.py",
        b"pi" + b".local",
    ): "synthetic hostname-normalization fixture",
    (
        HOSTNAME_RULE_NAME,
        "tests/test_viz_cli.py",
        b"review-box" + b".local",
    ): "synthetic allowed-host CLI fixture",
    (
        HOSTNAME_RULE_NAME,
        "tests/test_viz_jobs.py",
        b"device" + b".local",
    ): "synthetic worker-environment fixture",
    (
        HOSTNAME_RULE_NAME,
        "tests/test_viz_server.py",
        b"review-box" + b".local",
    ): "synthetic allowed-host server fixture",
    (
        HOSTNAME_RULE_NAME,
        "busybar_viz/jobs.py",
        b"threading" + b".local",
    ): "Python thread-local API call, not a network hostname",
}

_CONTACT_EMAIL = re.compile(
    rb"(?<![A-Z0-9._%+-])"
    rb"(?P<address>[A-Z0-9._%+-]+@(?P<host>[A-Z0-9.-]+\.[A-Z]{2,}))",
    re.IGNORECASE,
)
_PRIVATE_IP = re.compile(
    rb"(?<![0-9.])(?P<address>"
    rb"(?:10\.\d{1,3}\.\d{1,3}\.\d{1,3}"
    rb"|192\.168\.\d{1,3}\.\d{1,3}"
    rb"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3}))"
    rb"(?![0-9.])"
)
_IPV6_CANDIDATE = re.compile(
    rb"(?<![A-Z0-9])"
    rb"(?P<address>\[?[0-9A-F]{0,4}(?::[0-9A-F]{0,4}){2,}"
    rb"(?:%[A-Z0-9_.-]+)?\]?)"
    rb"(?![A-Z0-9])",
    re.IGNORECASE,
)
_PRIVATE_LAN_HOSTNAME = re.compile(
    rb"(?<![A-Z0-9_.-])"
    rb"(?P<host>[A-Z0-9](?:[A-Z0-9-]{0,62}\.)+"
    rb"(?:local|lan|home\.arpa))\.?(?![A-Z0-9_.-])",
    re.IGNORECASE,
)
_OPERATOR_HOST_ASSIGNMENT = re.compile(
    rb"(?<![A-Z0-9_])"
    rb"(?P<key>BUSYBAR_HOST|BUSYBAR_DEPLOY_HOST|BARKEEP_ALLOWED_HOSTS)"
    rb"(?![A-Z0-9_])[ \t]*=[ \t]*",
)
_SSH_COMMAND_LINE = re.compile(
    rb"(?im)^[ \t]*(?:[$>][ \t]+)?ssh(?P<arguments>[ \t]+[^\r\n]*)$",
)
_SSH_OPTIONS_WITH_VALUE = frozenset("BbcDEeFIiJLlmOoPpQRSWw")
_SSH_HOST_VALUE_OPTIONS = frozenset("JW")

# These settings are credentials (or may contain one) in this project.  The
# general token signatures below cannot recognize a short device PIN, and a
# private relay commonly carries its credential in a URL path or query rather
# than in RFC userinfo.  Keep the project-specific boundary explicit.
PROJECT_TOKEN_KEYS = (b"BARKEEP_TOKEN", b"BUSYBAR_TOKEN")
SENSITIVE_TEMPLATE_KEYS = (
    b"BARKEEP_TOKEN",
    b"BUSYBAR_TOKEN",
    b"SKYSTRIP_LIGHTNING_WS",
)
PUBLIC_PROJECT_TOKEN_PLACEHOLDERS = frozenset({
    b"generate:",
    b"paste-generated-value-here",
})

PROJECT_TOKEN_RULE_NAME = "credential.project-token-value"
LIGHTNING_URL_RULE_NAME = "credential.skystrip-lightning-url"
TEMPLATE_SECRET_RULE_NAME = "template.sensitive-key-must-be-blank"

_DOTENV_ASSIGNMENT = re.compile(
    rb"(?m)^[ \t]*(?:export[ \t]+)?"
    rb"(?P<key>BARKEEP_TOKEN|BUSYBAR_TOKEN|SKYSTRIP_LIGHTNING_WS)"
    rb"[ \t]*=[ \t]*(?P<value>[^\r\n]*)$"
)
_PROJECT_ASSIGNMENT_START = re.compile(
    rb"(?<![A-Z0-9_])"
    rb"(?P<key>BARKEEP_TOKEN|BUSYBAR_TOKEN|SKYSTRIP_LIGHTNING_WS)"
    rb"(?![A-Z0-9_])[ \t]*=[ \t]*"
)

# Exact decimal pairs that are intentionally public. Coordinate exemptions are
# values, never paths: the rest of each file remains subject to every content
# rule, and changing either component creates a release finding.
PUBLIC_COORDINATE_FIXTURES: dict[tuple[bytes, bytes], str] = {
    (b"0.0", b"0.0"): (
        "the documented unset-location sentinel (Null Island), never an operator value"
    ),
    (b"41.9742", b"-87.9073"): (
        "Chicago O'Hare International Airport (KORD), a public landmark used "
        "for Chicago-timezone astronomy and weather tests"
    ),
    (b"51.5074", b"-0.1278"): (
        "London's published city-centre reference point, used by the slippy-map test"
    ),
    (b"51.4769", b"0.0005"): (
        "Royal Observatory Greenwich, a public landmark used for documentation renders"
    ),
    (b"35.68", b"139.69"): (
        "Tokyo's published city-centre reference point, used for horizon-gating tests"
    ),
    (b"40.2413554", b"-4.2480085"): (
        "NASA/JPL Madrid Deep Space Communications Complex test fixture"
    ),
    (b"35.2443523", b"-116.8895382"): (
        "NASA/JPL Goldstone Deep Space Communications Complex test fixture"
    ),
    (b"-35.2209189", b"148.9812673"): (
        "NASA/JPL Canberra Deep Space Communications Complex test fixture"
    ),
    (b"0.2126", b"0.7152"): (
        "the first two Rec. ITU-R BT.709 luma coefficients, not a location"
    ),
}

COORDINATE_RULE_NAME = "location.coordinate-pair"

_RAW_COORDINATE_PAIR = re.compile(
    rb"(?<![0-9.])(?P<lat>[+-]?\d{1,2}\.\d{4,})\s*(?:,|%2C)\s*"
    rb"(?P<lon>[+-]?\d{1,3}\.\d{4,})(?![0-9.])",
    re.IGNORECASE,
)
_GEOJSON_COORDINATE_PAIR = re.compile(
    rb"(?<![A-Z0-9_])(?:[\"']?coordinates[\"']?)(?![A-Z0-9_])"
    rb"[ \t\r\n]*:[ \t\r\n]*\[{1,3}[ \t\r\n]*"
    rb"(?P<lon>[+-]?\d{1,3}\.\d+)[ \t\r\n]*,[ \t\r\n]*"
    rb"(?P<lat>[+-]?\d{1,2}\.\d+)(?![0-9.])",
    re.IGNORECASE,
)
_WKT_COORDINATE_PAIR = re.compile(
    rb"(?<![A-Z0-9_])POINT(?:[ \t]+Z(?:M)?|[ \t]+M)?[ \t\r\n]*\("
    rb"[ \t\r\n]*(?P<lon>[+-]?\d{1,3}\.\d+)[ \t]+"
    rb"(?P<lat>[+-]?\d{1,2}\.\d+)(?![0-9.])",
    re.IGNORECASE,
)
_WHITESPACE_COORDINATE_PAIR = re.compile(
    rb"(?m)^[ \t]*(?P<first>[+-]?\d{1,3}\.\d{4,})[ \t]+"
    rb"(?P<second>[+-]?\d{1,3}\.\d{4,})[ \t]*$"
)
_LABELLED_WHITESPACE_COORDINATE_PAIR = re.compile(
    rb"(?<![A-Z0-9_])(?:lat[ _-]?lon|latitude[ _-]?longitude)"
    rb"(?![A-Z0-9_])[ \t]*[:=][ \t]*[\"']?"
    rb"(?P<lat>[+-]?\d{1,2}\.\d+)[ \t]+"
    rb"(?P<lon>[+-]?\d{1,3}\.\d+)(?![0-9.])",
    re.IGNORECASE,
)
_LATITUDE_LABEL = (
    rb"(?:lat(?:itude)?|[A-Z][A-Z0-9_]*_(?:lat|latitude)|"
    rb"(?:lat|latitude)_[A-Z0-9_]+)"
)
_LONGITUDE_LABEL = (
    rb"(?:lon(?:gitude)?|lng|[A-Z][A-Z0-9_]*_(?:lon|lng|longitude)|"
    rb"(?:lon|lng|longitude)_[A-Z0-9_]+)"
)
_NAMED_VALUE_PREFIX = rb"[\s\"'`]*[:=]\s*[\"']?"
_DECIMAL_VALUE = rb"[+-]?\d{1,3}\.\d+"
_NAMED_LATITUDE_VALUE = re.compile(
    rb"(?<![A-Z0-9_])(?P<label>" + _LATITUDE_LABEL + rb")(?![A-Z0-9_])"
    + _NAMED_VALUE_PREFIX + rb"(?P<value>" + _DECIMAL_VALUE + rb")",
    re.IGNORECASE,
)
_NAMED_LONGITUDE_VALUE = re.compile(
    rb"(?<![A-Z0-9_])(?P<label>" + _LONGITUDE_LABEL + rb")(?![A-Z0-9_])"
    + _NAMED_VALUE_PREFIX + rb"(?P<value>" + _DECIMAL_VALUE + rb")",
    re.IGNORECASE,
)

PRIVATE_OR_GENERATED_DIRS = frozenset({
    ".mypy_cache",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".superpowers",
    ".tox",
    ".venv",
    "__pycache__",
    "build",
    "cache",
    "config",
    "dist",
    "htmlcov",
    "logs",
    "scratch",
    "state",
    "superpowers",
    "voices",
})

PRIVATE_OR_GENERATED_FILES = frozenset({
    ".DS_Store",
    ".skystrip_scene",
    "HANDOFF.md",
    "coverage.json",
    "coverage.xml",
})


@dataclass(frozen=True, order=True)
class Finding:
    rule: str
    path: str


@dataclass(frozen=True)
class ScanResult:
    tracked_count: int
    findings: tuple[Finding, ...]


@dataclass(frozen=True, order=True)
class IndexedEntry:
    path: PurePosixPath
    mode: str
    oid: str


@dataclass(frozen=True)
class ContentRule:
    name: str
    pattern: re.Pattern[bytes]


@dataclass(frozen=True, order=True)
class CoordinateMatch:
    latitude: bytes
    longitude: bytes
    offset: int


@dataclass(frozen=True, order=True)
class _CoordinateComponent:
    family: bytes
    value: bytes
    offset: int


CONTENT_RULES = (
    ContentRule(
        "absolute-home-path.posix",
        re.compile(
            rb"(?<![A-Za-z0-9_.-])/(?:Users|home)/[A-Za-z0-9._-]+"
            rb"(?=$|[/\x00\t\r\n '\"`:;])"
        ),
    ),
    ContentRule(
        "absolute-home-path.root",
        re.compile(rb"(?<![A-Za-z0-9_.-])/root(?=$|[/\x00\t\r\n '\"`:;])"),
    ),
    ContentRule(
        "absolute-home-path.windows",
        re.compile(
            rb"(?<![A-Za-z0-9_.-])(?:[A-Z]:)?[\\/]Users[\\/]"
            rb"[A-Z0-9._ -]+(?=$|[\\/\x00\t\r\n '\"`:;])",
            re.IGNORECASE,
        ),
    ),
    ContentRule(
        "credential.private-key",
        re.compile(
            rb"-{5}BEGIN[ ](?:RSA[ ]|EC[ ]|DSA[ ]|OPENSSH[ ]|ENCRYPTED[ ])?"
            rb"PRIVATE[ ]KEY-{5}"
        ),
    ),
    ContentRule(
        "credential.pgp-private-key",
        re.compile(rb"-{5}BEGIN[ ]PGP[ ]PRIVATE[ ]KEY[ ]BLOCK-{5}"),
    ),
    ContentRule(
        "credential.aws-access-key",
        re.compile(rb"(?<![A-Z0-9])(?:AKIA|ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    ),
    ContentRule(
        "credential.github-token",
        re.compile(
            rb"(?<![A-Za-z0-9_])(?:gh[pousr]_[A-Za-z0-9]{30,255}"
            rb"|github_pat_[A-Za-z0-9_]{30,255})(?![A-Za-z0-9_])"
        ),
    ),
    ContentRule(
        "credential.gitlab-token",
        re.compile(rb"(?<![A-Za-z0-9_-])glpat-[A-Za-z0-9_-]{20,255}"),
    ),
    ContentRule(
        "credential.openai-key",
        re.compile(rb"(?<![A-Za-z0-9_-])sk-(?:proj-)?[A-Za-z0-9_-]{20,255}"),
    ),
    ContentRule(
        "credential.slack-token",
        re.compile(rb"(?<![A-Za-z0-9-])xox[baprs]-[A-Za-z0-9-]{10,255}"),
    ),
    ContentRule(
        "credential.stripe-live-key",
        re.compile(rb"(?<![A-Za-z0-9_])sk_live_[A-Za-z0-9]{16,255}"),
    ),
    ContentRule(
        "credential.google-api-key",
        re.compile(rb"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])"),
    ),
    ContentRule(
        "credential.npm-token",
        re.compile(rb"(?<![A-Za-z0-9_])npm_[A-Za-z0-9]{30,255}"),
    ),
    ContentRule(
        "credential.jwt",
        re.compile(
            rb"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\."
            rb"[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
        ),
    ),
    ContentRule(
        "credential.uri-userinfo",
        re.compile(
            rb"(?i)\b(?:https?|wss?)://[^/\s:@]+:[^/\s@]+@"
            rb"(?P<host>[A-Z0-9.-]+)"
        ),
    ),
)


class InventoryError(RuntimeError):
    """Git could not provide a safe tracked-file inventory."""


INDEXED_BLOB_MODES = frozenset({"100644", "100755", "120000"})
_OBJECT_ID = re.compile(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})")


def _git_paths(root: Path, *selection: str) -> tuple[PurePosixPath, ...]:
    """Return one NUL-delimited Git path selection with safe error handling."""
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), "ls-files", "-z", *selection, "--"],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        # Git's stderr can contain an absolute checkout path. Do not echo it.
        raise InventoryError("git could not enumerate the tracked snapshot")

    raw_paths = completed.stdout.split(b"\0")
    if raw_paths and raw_paths[-1] == b"":
        raw_paths.pop()
    return tuple(
        PurePosixPath(os.fsdecode(raw_path))
        for raw_path in raw_paths
        if raw_path
    )


def indexed_entries(root: Path) -> tuple[IndexedEntry, ...]:
    """Return the stage-zero index entries and their immutable object ids."""
    completed = subprocess.run(
        ["git", "-C", os.fspath(root), "ls-files", "--stage", "-z", "--"],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        # Git's stderr can contain an absolute checkout path. Do not echo it.
        raise InventoryError("git could not enumerate the tracked snapshot")

    records = completed.stdout.split(b"\0")
    if records and records[-1] == b"":
        records.pop()

    entries: list[IndexedEntry] = []
    seen: set[PurePosixPath] = set()
    for record in records:
        header, separator, raw_path = record.partition(b"\t")
        fields = header.split()
        if not separator or len(fields) != 3 or not raw_path:
            raise InventoryError("git returned a malformed tracked snapshot")
        raw_mode, raw_oid, raw_stage = fields
        if (
            not re.fullmatch(rb"[0-7]{6}", raw_mode)
            or not _OBJECT_ID.fullmatch(raw_oid)
            or raw_stage not in (b"0", b"1", b"2", b"3")
        ):
            raise InventoryError("git returned a malformed tracked snapshot")
        if raw_stage != b"0":
            raise InventoryError("git index contains unmerged entries")

        path = PurePosixPath(os.fsdecode(raw_path))
        if path.is_absolute() or ".." in path.parts or path in seen:
            raise InventoryError("git returned an unsafe tracked snapshot")
        seen.add(path)
        entries.append(IndexedEntry(
            path=path,
            mode=raw_mode.decode("ascii"),
            oid=raw_oid.decode("ascii"),
        ))
    return tuple(entries)


def tracked_paths(root: Path) -> tuple[PurePosixPath, ...]:
    """Return paths in the exact candidate Git index snapshot."""
    return tuple(entry.path for entry in indexed_entries(root))


def untracked_paths(root: Path) -> tuple[PurePosixPath, ...]:
    """Return nonignored untracked paths without opening their contents."""
    return _git_paths(root, "--others", "--exclude-standard")


def path_rule_names(path: PurePosixPath) -> tuple[str, ...]:
    """Return every private/generated path rule matched by *path*."""
    name = path.name
    lowered_name = name.lower()
    rules: list[str] = []

    if name.startswith(".coverage"):
        rules.append("generated-coverage-data")
    if name in ("coverage.json", "coverage.xml"):
        rules.append("generated-coverage-report")
    if name == ".env" or name.startswith(".env.") or name.endswith(".env"):
        rules.append("private-environment-file")
    if name.endswith(".log") or ".log." in name:
        rules.append("generated-log-file")
    if name.endswith((".pyc", ".pyo")):
        rules.append("generated-python-bytecode")
    if name.endswith((".whl", ".egg")):
        rules.append("generated-package-archive")
    if lowered_name.endswith((".aiff", ".snd", ".wav")):
        rules.append("generated-audio-artifact")
    if name in PRIVATE_OR_GENERATED_FILES:
        rules.append("private-or-generated-file")
    if any(part in PRIVATE_OR_GENERATED_DIRS for part in path.parts[:-1]):
        rules.append("private-or-generated-directory")
    if any(part.lower().endswith(".egg-info") for part in path.parts[:-1]):
        rules.append("generated-package-metadata")
    if path.parts[:2] == ("docs", "mirror"):
        rules.append("private-vendor-doc-mirror")
    if path == PurePosixPath("docs/api/openapi.yaml"):
        rules.append("private-device-api-snapshot")
    if path.parts[:2] == (".claude", "worktrees"):
        rules.append("private-agent-worktree")

    rendered = path.as_posix()
    return tuple(
        rule for rule in rules
        if (rule, rendered) not in PATH_RULE_ALLOWLIST
    )


def _fixture_match_allowed(rule: ContentRule, match: re.Match[bytes]) -> bool:
    if rule.name != "credential.uri-userinfo":
        return False
    host = match.group("host").lower().rstrip(b".")
    return any(host.endswith(suffix) for suffix in PUBLIC_CREDENTIAL_FIXTURE_SUFFIXES)


def _dotenv_literal_value(raw: bytes) -> bytes:
    """Return a dotenv assignment's semantic literal without exposing it.

    Inline comments count only outside quotes.  This is intentionally a small
    dotenv reader, not a shell evaluator: recognized variable/template forms
    are dealt with separately because their source text is not a credential.
    """
    value = raw.strip()
    if not value:
        return b""
    if value[:1] in (b"\"", b"'"):
        quote = value[:1]
        closing = value.find(quote, 1)
        if closing >= 0:
            remainder = value[closing + 1:].lstrip()
            if not remainder or remainder.startswith(b"#"):
                return value[1:closing]
        return value[1:]
    return re.split(rb"[ \t]+#", value, maxsplit=1)[0].rstrip()


def _is_source_indirection(value: bytes) -> bool:
    """Return whether source contains a variable/placeholder, not its value."""
    return bool(re.fullmatch(
        rb"(?:\$\{?[A-Z_][A-Z0-9_]*\}?|\$\([^)]+\)|"
        rb"\{\{[^{}]+\}\}|<[^<>]+>)",
        value,
    ))


def _embedded_assignment_value(data: bytes, match: re.Match[bytes]) -> bytes:
    """Extract one literal value from source, prose, or a config line.

    Tracked tests and documentation sometimes contain a dotenv assignment
    inside a quoted string.  A physical-line-only matcher would make those a
    blind spot; conversely, treating the closing quote in ``"KEY="`` as the
    opening quote of a value would invent a secret.  The immediate delimiter
    around the key disambiguates those cases, and escaped newlines terminate a
    source-code string value just as real newlines terminate dotenv values.
    """
    position = match.end()
    if position >= len(data):
        return b""

    preceding = data[match.start("key") - 1:match.start("key")]
    if preceding in (b"\"", b"'") and data[position:position + 1] == preceding:
        return b""
    if data[position:position + 2] in (b"\\n", b"\\r"):
        return b""
    if data[position:position + 1] in (b"\r", b"\n", b"#"):
        return b""

    if data[position:position + 1] in (b"\"", b"'"):
        quote = data[position:position + 1]
        closing = data.find(quote, position + 1)
        if closing < 0:
            return data[position + 1:]
        return data[position + 1:closing]

    end = position
    while end < len(data):
        if data[end:end + 1] in (b"\r", b"\n", b"\"", b"'", b"`"):
            break
        if data[end:end + 2] in (b"\\n", b"\\r"):
            break
        if data[end:end + 1] in (b" ", b"\t"):
            break
        end += 1
    return data[position:end].rstrip(b",;)]}")


def _content_match_allowed(
    rule: str,
    path: PurePosixPath | None,
    value: bytes,
) -> bool:
    if path is None:
        return False
    return (rule, path.as_posix(), value.lower()) in PUBLIC_CONTENT_MATCHES


def _dns_name_is_public(host: bytes) -> bool:
    normalized = host.lower().rstrip(b".")
    if normalized in PUBLIC_DNS_HOSTS:
        return True
    return any(normalized.endswith(suffix) for suffix in PUBLIC_DNS_SUFFIXES)


def _parsed_ipv6(raw: bytes) -> ipaddress.IPv6Address | None:
    """Parse an IPv6 literal after removing URI brackets and a zone id."""
    value = raw.strip()
    if value.startswith(b"[") and value.endswith(b"]"):
        value = value[1:-1]
    value = value.split(b"%", 1)[0]
    try:
        parsed = ipaddress.ip_address(value.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, ipaddress.IPv6Address) else None


def _ipv6_is_operator_private(address: ipaddress.IPv6Address) -> bool:
    """Return whether an address is ULA or link-local.

    The byte tests deliberately avoid embedding private address literals in
    this scanner's own tracked source. Documentation space and loopback do not
    match either prefix and remain valid public fixtures.
    """
    first, second = address.packed[:2]
    return (first & 0xFE) == 0xFC or (
        first == 0xFE and (second & 0xC0) == 0x80
    )


def _normalized_hostname(raw: bytes) -> bytes:
    """Extract one hostname-like literal from config or command syntax."""
    value = raw.strip().strip(b"\"'`(){}")
    if not value or _is_source_indirection(value) or value.startswith(b"$"):
        return b""
    if b"://" in value:
        try:
            parsed = urlsplit(value.decode("ascii"))
        except (UnicodeDecodeError, ValueError):
            return value.lower()
        return (parsed.hostname or "").encode("ascii", errors="ignore").lower()
    if b"@" in value:
        value = value.rsplit(b"@", 1)[1]
    if value.startswith(b"[") and b"]" in value:
        value = value[1:value.index(b"]")]
    elif value.count(b":") == 1:
        host, port = value.rsplit(b":", 1)
        if port.isdigit():
            value = host
    return value.lower().rstrip(b".")


def _hostname_is_private(host: bytes) -> bool:
    if not host or _dns_name_is_public(host):
        return False
    address_value = host.split(b"%", 1)[0]
    try:
        ipaddress.ip_address(address_value.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        return True
    return False


def _ssh_command_hostnames(arguments: bytes) -> tuple[bytes, ...]:
    """Extract literal destination and jump hosts from one ssh command line."""
    try:
        words = [
            word.encode("utf-8", errors="surrogateescape")
            for word in shlex.split(
                arguments.decode("utf-8", errors="surrogateescape"),
                comments=False,
                posix=True,
            )
        ]
    except ValueError:
        # An incomplete quoted command is still worth scanning conservatively.
        words = arguments.split()

    hosts: list[bytes] = []
    index = 0
    while index < len(words):
        word = words[index]
        if word == b"--":
            index += 1
            if index < len(words):
                hosts.append(words[index])
            break
        if word.startswith(b"-") and word != b"-":
            options = word[1:]
            value_option = next(
                (
                    chr(option)
                    for option in options
                    if chr(option) in _SSH_OPTIONS_WITH_VALUE
                ),
                None,
            )
            if value_option is None:
                index += 1
                continue
            option_offset = options.index(ord(value_option))
            option_value = options[option_offset + 1:]
            if not option_value:
                index += 1
                if index >= len(words):
                    break
                option_value = words[index]
            if value_option in _SSH_HOST_VALUE_OPTIONS:
                if not (value_option == "J" and option_value.lower() == b"none"):
                    hosts.extend(option_value.split(b","))
            elif value_option == "o":
                key, separator, value = option_value.partition(b"=")
                if not separator:
                    key, separator, value = option_value.partition(b" ")
                if separator and key.lower() in (b"hostname", b"proxyjump"):
                    if value.lower() != b"none":
                        hosts.extend(value.split(b","))
            index += 1
            continue
        hosts.append(word)
        break
    return tuple(hosts)


def personal_data_rule_names(
    data: bytes,
    path: PurePosixPath | None = None,
) -> tuple[str, ...]:
    """Find contact addresses, private IPs, and operator-specific hostnames."""
    found: list[str] = []

    for match in _CONTACT_EMAIL.finditer(data):
        address = match.group("address").lower()
        host = match.group("host").lower()
        if _dns_name_is_public(host):
            continue
        if not _content_match_allowed(CONTACT_EMAIL_RULE_NAME, path, address):
            found.append(CONTACT_EMAIL_RULE_NAME)
            break

    private_ip = False
    for match in _PRIVATE_IP.finditer(data):
        address = match.group("address")
        if address in PUBLIC_PRIVATE_IPS:
            continue
        if not _content_match_allowed(PRIVATE_IP_RULE_NAME, path, address):
            private_ip = True
            break
    if not private_ip:
        for match in _IPV6_CANDIDATE.finditer(data):
            raw_address = match.group("address")
            address = _parsed_ipv6(raw_address)
            if address is None or not _ipv6_is_operator_private(address):
                continue
            if not _content_match_allowed(PRIVATE_IP_RULE_NAME, path, raw_address):
                private_ip = True
                break
    if private_ip:
        found.append(PRIVATE_IP_RULE_NAME)

    private_hostname = False
    for match in _PRIVATE_LAN_HOSTNAME.finditer(data):
        host = match.group("host").lower()
        if _hostname_is_private(host) and not _content_match_allowed(
            HOSTNAME_RULE_NAME, path, host
        ):
            private_hostname = True
            break

    if not private_hostname:
        for match in _OPERATOR_HOST_ASSIGNMENT.finditer(data):
            value = _embedded_assignment_value(data, match)
            if not value or _is_source_indirection(value):
                continue
            for item in value.split(b","):
                host = _normalized_hostname(item)
                if _hostname_is_private(host) and not _content_match_allowed(
                    HOSTNAME_RULE_NAME, path, host
                ):
                    private_hostname = True
                    break
            if private_hostname:
                break

    if not private_hostname:
        for match in _SSH_COMMAND_LINE.finditer(data):
            for raw_host in _ssh_command_hostnames(match.group("arguments")):
                host = _normalized_hostname(raw_host)
                if _hostname_is_private(host) and not _content_match_allowed(
                    HOSTNAME_RULE_NAME, path, host
                ):
                    private_hostname = True
                    break
            if private_hostname:
                break

    if private_hostname:
        found.append(HOSTNAME_RULE_NAME)
    return tuple(found)


def _host_is_public_fixture(host: str | None) -> bool:
    if not host:
        return False
    encoded = host.lower().rstrip(".").encode("ascii", errors="ignore")
    return any(
        encoded.endswith(suffix)
        for suffix in PUBLIC_CREDENTIAL_FIXTURE_SUFFIXES
    )


def _lightning_url_is_private(value: bytes) -> bool:
    """Treat every literal operator relay as private unless it is a fixture.

    Even a credential-free root URL identifies infrastructure chosen by one
    operator.  This repository ships no relay, so there is no legitimate
    production endpoint to publish in a tracked assignment.  Reserved
    ``.example`` hosts remain available for documentation and tests.
    """
    try:
        parsed = urlsplit(value.decode("utf-8"))
        host = parsed.hostname
    except (UnicodeDecodeError, ValueError):
        return True
    if _host_is_public_fixture(host):
        return False
    if parsed.scheme.lower() not in ("ws", "wss") or not host:
        return True
    return True


def project_secret_rule_names(data: bytes) -> tuple[str, ...]:
    """Find literal project credentials in config and quoted assignments."""
    found: set[str] = set()
    for match in _PROJECT_ASSIGNMENT_START.finditer(data):
        key = match.group("key")
        value = _embedded_assignment_value(data, match)
        if not value or _is_source_indirection(value):
            continue
        if key in PROJECT_TOKEN_KEYS:
            if value not in PUBLIC_PROJECT_TOKEN_PLACEHOLDERS:
                found.add(PROJECT_TOKEN_RULE_NAME)
        elif key == b"SKYSTRIP_LIGHTNING_WS":
            if _lightning_url_is_private(value):
                found.add(LIGHTNING_URL_RULE_NAME)
    return tuple(sorted(found))


def template_rule_names(data: bytes) -> tuple[str, ...]:
    """Require every sensitive .env.example key exactly once and empty."""
    values: dict[bytes, list[bytes]] = {
        key: [] for key in SENSITIVE_TEMPLATE_KEYS
    }
    for match in _DOTENV_ASSIGNMENT.finditer(data):
        values[match.group("key")].append(
            _dotenv_literal_value(match.group("value"))
        )
    if any(len(items) != 1 or items[0] for items in values.values()):
        return (TEMPLATE_SECRET_RULE_NAME,)
    return ()


def _coordinate_in_range(latitude: bytes, longitude: bytes) -> bool:
    """Reject decimal lookalikes that cannot name a point on Earth."""
    try:
        lat = float(latitude)
        lon = float(longitude)
    except ValueError:
        return False
    return -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def _coordinate_label_family(label: bytes, axis: str) -> bytes:
    """Reduce HOME_LATITUDE/HOME_LONGITUDE to their shared HOME family."""
    normalized = label.lower()
    axis_names = (b"lat", b"latitude") if axis == "lat" else (
        b"lon", b"lng", b"longitude"
    )
    if normalized in axis_names:
        return b""
    for name in axis_names:
        suffix = b"_" + name
        if normalized.endswith(suffix):
            return normalized[:-len(suffix)]
        prefix = name + b"_"
        if normalized.startswith(prefix):
            return normalized[len(prefix):]
    return normalized


def _named_coordinate_components(
    data: bytes,
    pattern: re.Pattern[bytes],
    axis: str,
) -> tuple[_CoordinateComponent, ...]:
    return tuple(
        _CoordinateComponent(
            family=_coordinate_label_family(match.group("label"), axis),
            value=match.group("value"),
            offset=match.start("value"),
        )
        for match in pattern.finditer(data)
    )


def coordinate_matches(data: bytes) -> tuple[CoordinateMatch, ...]:
    """Find raw and labelled decimal coordinate pairs in tracked bytes.

    Unlabelled pairs require four decimal places to avoid ordinary numeric
    tuples. A whitespace pair must occupy its whole line (or carry an explicit
    lat/lon label), which avoids mistaking ephemeris and tabular values for an
    operator location. GeoJSON and WKT are normalized from their longitude-
    first ordering before the exact public-fixture allowlist is consulted.
    Explicit latitude/longitude labels are strong enough that any decimal
    precision is scanned. Named variables are paired by their shared identifier
    family even when split across a file; bare labels use the nearest opposite
    component, avoiding arbitrary line-count loopholes.
    """
    found: set[CoordinateMatch] = set()
    geojson_value_spans: list[tuple[int, int]] = []
    for match in _GEOJSON_COORDINATE_PAIR.finditer(data):
        latitude = match.group("lat")
        longitude = match.group("lon")
        geojson_value_spans.append(
            (match.start("lon"), match.end("lat"))
        )
        if _coordinate_in_range(latitude, longitude):
            found.add(CoordinateMatch(
                latitude=latitude,
                longitude=longitude,
                offset=match.start("lon"),
            ))

    for match in _RAW_COORDINATE_PAIR.finditer(data):
        if any(
            start <= match.start("lat") and match.end("lon") <= end
            for start, end in geojson_value_spans
        ):
            continue
        latitude = match.group("lat")
        longitude = match.group("lon")
        if _coordinate_in_range(latitude, longitude):
            found.add(CoordinateMatch(
                latitude=latitude,
                longitude=longitude,
                offset=match.start("lat"),
            ))

    for pattern, longitude_first in (
        (_WKT_COORDINATE_PAIR, True),
        (_LABELLED_WHITESPACE_COORDINATE_PAIR, False),
    ):
        for match in pattern.finditer(data):
            latitude = match.group("lat")
            longitude = match.group("lon")
            if _coordinate_in_range(latitude, longitude):
                first_offset = (
                    match.start("lon") if longitude_first
                    else match.start("lat")
                )
                found.add(CoordinateMatch(
                    latitude=latitude,
                    longitude=longitude,
                    offset=first_offset,
                ))

    for match in _WHITESPACE_COORDINATE_PAIR.finditer(data):
        first = match.group("first")
        second = match.group("second")
        if _coordinate_in_range(first, second):
            latitude, longitude = first, second
        elif _coordinate_in_range(second, first):
            # A three-digit first component cannot be latitude, so this is the
            # unambiguous longitude-first serialization used by GIS exports.
            latitude, longitude = second, first
        else:
            continue
        found.add(CoordinateMatch(
            latitude=latitude,
            longitude=longitude,
            offset=match.start("first"),
        ))

    latitudes = _named_coordinate_components(
        data, _NAMED_LATITUDE_VALUE, "lat"
    )
    longitudes = _named_coordinate_components(
        data, _NAMED_LONGITUDE_VALUE, "lon"
    )
    families = {item.family for item in latitudes} & {
        item.family for item in longitudes
    }
    for family in families:
        family_latitudes = [item for item in latitudes if item.family == family]
        family_longitudes = [item for item in longitudes if item.family == family]
        for latitude in family_latitudes:
            longitude = min(
                family_longitudes,
                key=lambda item: abs(item.offset - latitude.offset),
            )
            if _coordinate_in_range(latitude.value, longitude.value):
                found.add(CoordinateMatch(
                    latitude=latitude.value,
                    longitude=longitude.value,
                    offset=min(latitude.offset, longitude.offset),
                ))
        for longitude in family_longitudes:
            latitude = min(
                family_latitudes,
                key=lambda item: abs(item.offset - longitude.offset),
            )
            if _coordinate_in_range(latitude.value, longitude.value):
                found.add(CoordinateMatch(
                    latitude=latitude.value,
                    longitude=longitude.value,
                    offset=min(latitude.offset, longitude.offset),
                ))
    return tuple(sorted(found))


def is_public_coordinate_fixture(latitude: bytes, longitude: bytes) -> bool:
    """Return whether an exact pair has a reviewed public/scientific rationale."""
    return (latitude, longitude) in PUBLIC_COORDINATE_FIXTURES


def content_rule_names(
    data: bytes,
    path: PurePosixPath | None = None,
) -> tuple[str, ...]:
    """Return matching content rules without retaining or exposing matches."""
    found: list[str] = []
    for rule in CONTENT_RULES:
        for match in rule.pattern.finditer(data):
            if not _fixture_match_allowed(rule, match):
                found.append(rule.name)
                break
    found.extend(project_secret_rule_names(data))
    found.extend(personal_data_rule_names(data, path))
    if any(
        not is_public_coordinate_fixture(match.latitude, match.longitude)
        for match in coordinate_matches(data)
    ):
        found.append(COORDINATE_RULE_NAME)
    return tuple(found)


def _tracked_bytes(root: Path, entry: IndexedEntry) -> bytes:
    """Read one indexed blob by object id, never through the working tree."""
    if entry.mode not in INDEXED_BLOB_MODES:
        raise OSError("tracked entry does not name a publishable blob")
    completed = subprocess.run(
        [
            "git",
            "--no-replace-objects",
            "-C",
            os.fspath(root),
            "cat-file",
            "blob",
            entry.oid,
        ],
        check=False,
        capture_output=True,
    )
    if completed.returncode:
        # As above, stderr is intentionally discarded because it may contain
        # checkout details. The finding names only the indexed path.
        raise OSError("git could not read an indexed blob")
    return completed.stdout


def scan_repository(root: Path) -> ScanResult:
    """Scan the indexed candidate and fail on anything left untracked."""
    root = root.resolve()
    entries = indexed_entries(root)
    untracked = untracked_paths(root)
    findings: set[Finding] = set()

    if not entries:
        findings.add(Finding("tracked-snapshot-empty", "."))
    for relative in untracked:
        findings.add(Finding("untracked-path", relative.as_posix()))

    for entry in entries:
        relative = entry.path
        rendered = relative.as_posix()
        for rule in path_rule_names(relative):
            findings.add(Finding(rule, rendered))
        if entry.mode not in INDEXED_BLOB_MODES:
            findings.add(Finding("tracked-entry-unsupported", rendered))
            continue
        try:
            data = _tracked_bytes(root, entry)
        except OSError:
            findings.add(Finding("tracked-entry-unreadable", rendered))
            continue
        if relative == PurePosixPath(".env.example"):
            for rule in template_rule_names(data):
                findings.add(Finding(rule, rendered))
        for rule in content_rule_names(data, relative):
            findings.add(Finding(rule, rendered))

    # Object ids make each read immutable. Rechecking the two mutable
    # inventories closes the remaining race where another process changes the
    # index or creates an untracked file while this gate is running.
    if indexed_entries(root) != entries or untracked_paths(root) != untracked:
        raise InventoryError("git candidate changed during the release scan")

    return ScanResult(len(entries), tuple(sorted(findings)))


def _display_path(path: str) -> str:
    """Quote control characters so a tracked filename cannot forge output."""
    return json.dumps(path, ensure_ascii=True)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="scan the current Git tracked snapshot for release blockers"
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=REPO_ROOT,
        help="repository root (defaults to this script's checkout)",
    )
    args = parser.parse_args(argv)

    try:
        result = scan_repository(args.root)
    except InventoryError as exc:
        print(f"public-release check could not run: {exc}", file=sys.stderr)
        return 2

    if result.findings:
        print(
            f"public-release check failed: {len(result.findings)} finding(s) "
            f"across {result.tracked_count} tracked file(s)",
            file=sys.stderr,
        )
        for finding in result.findings:
            print(
                f"  {finding.rule}: {_display_path(finding.path)}",
                file=sys.stderr,
            )
        print("matched content is intentionally not shown", file=sys.stderr)
        return 1

    print(f"public-release check passed: {result.tracked_count} tracked file(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

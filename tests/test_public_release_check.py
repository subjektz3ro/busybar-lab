"""The public-release gate scans tracked bytes without opening owner data."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _checker():
    path = ROOT / "scripts" / "check_public_release.py"
    spec = importlib.util.spec_from_file_location("_check_public_release", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _checker()


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def _repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q")
    return path


def _track(repo: Path, relative: str, data: bytes = b"public\n") -> None:
    path = repo / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    _git(repo, "add", "-f", "--", relative)


def _rules(checker, repo: Path) -> set[tuple[str, str]]:
    return {(item.rule, item.path) for item in checker.scan_repository(repo).findings}


def test_safe_one_commit_export_passes(checker, tmp_path):
    repo = _repo(tmp_path / "export")
    _track(repo, "README.md")
    _track(
        repo,
        ".env.example",
        b"BARKEEP_TOKEN=\nBUSYBAR_TOKEN=\nSKYSTRIP_LIGHTNING_WS=\n",
    )
    _git(repo, "-c", "user.name=Public Fixture", "-c",
         "user.email=fixture@example.invalid", "commit", "-qm", "public root")

    result = checker.scan_repository(repo)

    assert result.tracked_count == 2
    assert result.findings == ()


def test_worktree_deletion_cannot_hide_an_indexed_private_blob(checker, tmp_path):
    repo = _repo(tmp_path / "export")
    private_path = b"/" + b"home" + b"/synthetic-owner/private-project"
    _track(repo, "stale-private-notes.md", private_path)
    (repo / "stale-private-notes.md").unlink()

    result = checker.scan_repository(repo)

    assert result.tracked_count == 1
    assert result.findings == (
        checker.Finding("absolute-home-path.posix", "stale-private-notes.md"),
    )


def test_unstaged_clean_replacement_cannot_hide_an_indexed_private_blob(
    checker, tmp_path,
):
    repo = _repo(tmp_path / "export")
    private_path = b"/" + b"home" + b"/synthetic-owner/private-project"
    _track(repo, "release-notes.md", private_path)
    (repo / "release-notes.md").write_bytes(b"public replacement\n")

    assert _rules(checker, repo) == {
        ("absolute-home-path.posix", "release-notes.md"),
    }


def test_unstaged_private_replacement_is_not_part_of_the_index_snapshot(
    checker, tmp_path,
):
    repo = _repo(tmp_path / "export")
    _track(repo, "release-notes.md", b"public indexed bytes\n")
    private_path = b"/" + b"home" + b"/synthetic-owner/private-project"
    (repo / "release-notes.md").write_bytes(private_path)

    assert checker.scan_repository(repo).findings == ()


@pytest.mark.parametrize(("rule_name", "private_payload"), [
    (
        "identity.contact-email",
        b"contact=operator" + b"@" + b"private.internal",
    ),
    (
        "network.private-ip",
        b"endpoint=192.168." + b"44.9",
    ),
    (
        "network.private-ip",
        b"endpoint=[fd42" + b"::9%en0]",
    ),
    (
        "network.operator-hostname",
        b"ssh -p 2222 -i /tmp/key owner-workstation",
    ),
])
def test_identity_rules_scan_staged_bytes_not_clean_worktree_replacements(
    checker, tmp_path, rule_name, private_payload,
):
    repo = _repo(tmp_path / "export")
    _track(repo, "operator-notes.txt", private_payload)
    (repo / "operator-notes.txt").write_bytes(b"public replacement\n")

    assert (rule_name, "operator-notes.txt") in _rules(checker, repo)


def test_git_replace_cannot_hide_the_indexed_blob(checker, tmp_path):
    """Local replacement refs are not transferred with the staged object.

    ``git cat-file`` normally honors ``refs/replace``.  A release gate must
    inspect the original object id from the index, because that is the blob a
    clean clone receives even when the maintainer's repository substitutes a
    different object locally.
    """
    repo = _repo(tmp_path / "export")
    private_path = b"/" + b"home" + b"/synthetic-owner/private-project"
    _track(repo, "release-notes.md", private_path)
    private_oid = subprocess.run(
        ["git", "rev-parse", ":release-notes.md"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    safe_oid = subprocess.run(
        ["git", "hash-object", "-w", "--stdin"],
        cwd=repo,
        input=b"public replacement\n",
        check=True,
        capture_output=True,
    ).stdout.decode().strip()
    _git(repo, "replace", private_oid, safe_oid)

    assert _rules(checker, repo) == {
        ("absolute-home-path.posix", "release-notes.md"),
    }


def test_zero_file_git_index_fails_closed(checker, tmp_path):
    repo = _repo(tmp_path / "export")

    result = checker.scan_repository(repo)

    assert result.tracked_count == 0
    assert result.findings == (checker.Finding("tracked-snapshot-empty", "."),)


def test_nonignored_untracked_path_fails_without_being_opened(
    checker, tmp_path, monkeypatch,
):
    repo = _repo(tmp_path / "export")
    _track(repo, ".gitignore", b"ignored/\n")
    untracked = repo / "review-me.txt"
    untracked.write_bytes(b"content must not be opened")
    ignored = repo / "ignored" / "owner.env"
    ignored.parent.mkdir()
    ignored.write_bytes(b"ignored owner content")
    original = checker._tracked_bytes

    def tracked_only(root, entry):
        assert entry.path.as_posix() != "review-me.txt"
        return original(root, entry)

    monkeypatch.setattr(checker, "_tracked_bytes", tracked_only)

    assert _rules(checker, repo) == {("untracked-path", "review-me.txt")}


def test_unexpected_read_failure_still_fails(checker, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "export")
    _track(repo, "README.md")

    def unreadable(_root, _entry):
        raise OSError("synthetic read failure")

    monkeypatch.setattr(checker, "_tracked_bytes", unreadable)

    assert _rules(checker, repo) == {("tracked-entry-unreadable", "README.md")}


@pytest.mark.parametrize("relative", [
    ".coverage",
    ".coverage.host.1234",
    "coverage.json",
    ".env",
    ".env.local",
    "worker.env",
    "config/state.json",
    "logs/app.log",
    "scratch/render.png",
    "build/lib/module.py",
    "dist/package.whl",
    ".mypy_cache/3.11/module.meta.json",
    ".ruff_cache/content",
    ".playwright-cli/session/state.json",
    ".superpowers/tasks/release-report.md",
    "docs/superpowers/plans/internal-plan.md",
    "htmlcov/index.html",
    "docs/mirror/llms.txt",
    "docs/api/openapi.yaml",
    ".claude/worktrees/release-audit/report.md",
    "artifacts/voice.aiff",
    "artifacts/voice.wav",
    "artifacts/voice.snd",
    "busybar_lab.egg-info/PKG-INFO",
])
def test_private_and_generated_tracked_paths_fail(checker, tmp_path, relative):
    repo = _repo(tmp_path / "repo")
    _track(repo, relative)

    findings = checker.scan_repository(repo).findings

    assert findings
    assert {finding.path for finding in findings} == {relative}


@pytest.mark.parametrize("relative", [
    "docs/README.md",
    "docs/api/README.md",
    "docs/busylib/README.md",
    ".claude/skills/busybar-app/SKILL.md",
    "artifacts/README.md",
    "package-info.txt",
])
def test_neighboring_public_paths_remain_publishable(checker, tmp_path, relative):
    repo = _repo(tmp_path / "repo")
    _track(repo, relative)

    assert checker.scan_repository(repo).findings == ()


def test_env_example_path_exception_does_not_skip_content_scan(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    marker = b"-----BEGIN " + b"PRIVATE KEY-----"
    _track(repo, ".env.example", marker)

    rules = _rules(checker, repo)

    assert ("private-environment-file", ".env.example") not in rules
    assert ("credential.private-key", ".env.example") in rules


@pytest.mark.parametrize("key", [b"BARKEEP_TOKEN", b"BUSYBAR_TOKEN"])
def test_nonempty_project_token_assignment_fails(checker, tmp_path, key):
    repo = _repo(tmp_path / "repo")
    synthetic_token = b"4827" + b"1953"
    _track(repo, "operator-notes.txt", key + b"=" + synthetic_token + b"\n")

    assert (checker.PROJECT_TOKEN_RULE_NAME, "operator-notes.txt") in _rules(
        checker, repo
    )


@pytest.mark.parametrize("key", [b"BARKEEP_TOKEN", b"BUSYBAR_TOKEN"])
def test_project_token_inside_a_quoted_config_string_fails(
    checker, tmp_path, key,
):
    repo = _repo(tmp_path / "repo")
    synthetic_token = b"4827" + b"1953"
    payload = b'config = "' + key + b"=" + synthetic_token + b'\\n"\n'
    _track(repo, "fixture.py", payload)

    assert (checker.PROJECT_TOKEN_RULE_NAME, "fixture.py") in _rules(
        checker, repo
    )


def test_empty_assignment_inside_a_quoted_assertion_passes(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    key = b"BUSYBAR_" + b"TOKEN"
    _track(repo, "fixture.py", b'assert "' + key + b'=" in template\n')

    assert checker.scan_repository(repo).findings == ()


def test_blank_and_source_indirected_project_tokens_pass(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(
        repo,
        "public-config.txt",
        b"BARKEEP_TOKEN=\nBUSYBAR_TOKEN=${DEVICE_PIN}\n",
    )

    assert checker.scan_repository(repo).findings == ()


@pytest.mark.parametrize("shape", ["root", "userinfo", "path", "query"])
def test_literal_operator_lightning_assignment_fails(
    checker, tmp_path, shape,
):
    repo = _repo(tmp_path / "repo")
    urls = {
        "root": b"wss://relay.invalid",
        "userinfo": (
            b"wss://" + b"operator:credential" + b"@" + b"relay.invalid"
        ),
        "path": b"wss://relay.invalid/" + b"private-feed",
        "query": b"wss://relay.invalid?" + b"token=credential",
    }
    _track(
        repo,
        "operator-notes.txt",
        b"SKYSTRIP_LIGHTNING_WS=" + urls[shape] + b"\n",
    )

    assert (checker.LIGHTNING_URL_RULE_NAME, "operator-notes.txt") in _rules(
        checker, repo
    )


@pytest.mark.parametrize("shape", ["path", "query"])
def test_private_lightning_url_inside_a_quoted_config_string_fails(
    checker, tmp_path, shape,
):
    repo = _repo(tmp_path / "repo")
    key = b"SKYSTRIP_LIGHTNING_" + b"WS"
    urls = {
        "path": b"wss://relay.invalid/" + b"private-feed",
        "query": b"wss://relay.invalid?" + b"token=credential",
    }
    payload = b'config = "' + key + b"=" + urls[shape] + b'\\n"\n'
    _track(repo, "fixture.py", payload)

    assert (checker.LIGHTNING_URL_RULE_NAME, "fixture.py") in _rules(
        checker, repo
    )


def test_reserved_lightning_fixtures_pass(
    checker, tmp_path,
):
    repo = _repo(tmp_path / "repo")
    _track(
        repo,
        "public-config.txt",
        b"SKYSTRIP_LIGHTNING_WS=wss://relay.example\n",
    )
    _track(
        repo,
        "test-fixture.txt",
        b"SKYSTRIP_LIGHTNING_WS=wss://relay.example/path?token=fake\n",
    )

    assert checker.scan_repository(repo).findings == ()


@pytest.mark.parametrize("problem", ["missing", "nonempty", "duplicate"])
def test_sensitive_env_template_keys_must_be_present_once_and_blank(
    checker, tmp_path, problem,
):
    repo = _repo(tmp_path / "repo")
    values = {
        b"BARKEEP_TOKEN": b"",
        b"BUSYBAR_TOKEN": b"",
        b"SKYSTRIP_LIGHTNING_WS": b"",
    }
    if problem == "missing":
        del values[b"BARKEEP_TOKEN"]
    elif problem == "nonempty":
        values[b"BUSYBAR_TOKEN"] = b"4827" + b"1953"
    payload = b"".join(key + b"=" + value + b"\n" for key, value in values.items())
    if problem == "duplicate":
        payload += b"BARKEEP_TOKEN=\n"
    _track(repo, ".env.example", payload)

    assert (
        checker.TEMPLATE_SECRET_RULE_NAME,
        ".env.example",
    ) in _rules(checker, repo)


@pytest.mark.parametrize("payload,expected", [
    (b"\x00" + b"/" + b"Users" + b"/alice/project\x00",
     "absolute-home-path.posix"),
    (b"\x00" + b"/" + b"home" + b"/alice/project\x00",
     "absolute-home-path.posix"),
    (b"\x00C:" + b"\\Users" + b"\\Alice\\project\x00",
     "absolute-home-path.windows"),
    (b"-----BEGIN " + b"OPENSSH PRIVATE KEY-----",
     "credential.private-key"),
    (b"AK" + b"IA" + b"A" * 16, "credential.aws-access-key"),
    (b"gh" + b"p_" + b"a" * 36, "credential.github-token"),
    (b"sk-" + b"a" * 32, "credential.openai-key"),
    (b"eyJ" + b"a" * 10 + b"." + b"eyJ" + b"b" * 10 + b"." + b"c" * 12,
     "credential.jwt"),
    (b"https://" + b"alice:secret" + b"@" + b"internal.invalid",
     "credential.uri-userinfo"),
])
def test_binary_and_text_signatures_fail(checker, tmp_path, payload, expected):
    repo = _repo(tmp_path / "repo")
    _track(repo, "payload.bin", payload)

    assert (expected, "payload.bin") in _rules(checker, repo)


def test_reserved_example_uri_is_an_explicit_public_fixture(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    fixture = b"wss://" + b"user:password@relay.example/feed"
    _track(repo, "fixture.txt", fixture)

    assert checker.scan_repository(repo).findings == ()


def test_public_identity_values_are_explicit_global_exceptions(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(
        repo,
        "fixture.txt",
        b"contact=user@example.invalid\n"
        b"endpoint=10.0.4.20\n"
        b"documentation_ipv6=2001:db8::1\n"
        b"loopback_ipv6=::1\n"
        b"BUSYBAR_HOST=device.example\n"
        b"device=busybar.local\n",
    )

    assert checker.scan_repository(repo).findings == ()


@pytest.mark.parametrize(("rule_name", "allowed_path", "payload"), [
    (
        "identity.contact-email",
        "deploy/README.md",
        b"remote=git" + b"@" + b"github.com\n",
    ),
    (
        "network.private-ip",
        "docs/busylib/README.md",
        b"host=192.168." + b"1.20\n",
    ),
    (
        "network.private-ip",
        "docs/busylib/api/discovery.md",
        b"Front desk 192.168." + b"1.20\n",
    ),
    (
        "network.private-ip",
        "tests/test_server.py",
        b'assert host_name("fe80' + b'::1") == "fe80' + b'::1"\n',
    ),
    (
        "network.private-ip",
        "tests/test_server.py",
        b'assert host_name("[fe80' + b'::1]:8080") == "fe80' + b'::1"\n',
    ),
    (
        "network.operator-hostname",
        "docs/busybar-viz.md",
        b"review=review-box" + b".local\n",
    ),
])
def test_identity_value_exceptions_are_path_scoped(
    checker, tmp_path, rule_name, allowed_path, payload,
):
    repo = _repo(tmp_path / "repo")
    _track(repo, allowed_path, payload)

    assert checker.scan_repository(repo).findings == ()

    _track(repo, "unreviewed.txt", payload)
    assert (rule_name, "unreviewed.txt") in _rules(checker, repo)


@pytest.mark.parametrize("layout", [
    "raw",
    "whitespace",
    "labelled-whitespace",
    "geojson",
    "wkt",
    "named-inline",
    "named-multiline",
    "named-distant",
    "named-reversed",
])
def test_decimal_coordinate_layouts_fail(checker, tmp_path, layout):
    repo = _repo(tmp_path / "repo")
    latitude = b"12." + b"3456"
    longitude = b"-65." + b"4321"
    payloads = {
        "raw": latitude + b", " + longitude,
        "whitespace": latitude + b" " + longitude + b"\n",
        "labelled-whitespace": b"lat-lon=" + latitude + b" " + longitude,
        "geojson": (
            b'{"type":"Point","coordinates":['
            + longitude + b", " + latitude + b"]}"
        ),
        "wkt": b"POINT (" + longitude + b" " + latitude + b")",
        "named-inline": b"latitude=" + latitude + b", longitude=" + longitude,
        "named-multiline": (
            b"HOME_LATITUDE = " + latitude
            + b"\nHOME_LONGITUDE = " + longitude
        ),
        "named-distant": (
            b"HOME_LATITUDE = " + latitude
            + (b"\nUNRELATED_SETTING=public" * 40)
            + b"\nHOME_LONGITUDE = " + longitude
        ),
        "named-reversed": b"longitude=" + longitude + b"\nlatitude=" + latitude,
    }
    _track(repo, "fixture.txt", payloads[layout])

    assert (checker.COORDINATE_RULE_NAME, "fixture.txt") in _rules(checker, repo)


def test_tabular_numeric_data_is_not_a_whitespace_coordinate(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    first = b"6." + b"29489242661185"
    second = b"-4." + b"6392252"
    _track(
        repo,
        "ephemeris.txt",
        b"2026-Aug-08 06:52:17.184     " + first + b"  " + second + b"\n",
    )

    assert checker.scan_repository(repo).findings == ()


def test_three_digit_longitude_first_whitespace_pair_fails(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    longitude = b"-116." + b"8895"
    latitude = b"35." + b"2443"
    _track(repo, "point.txt", longitude + b" " + latitude + b"\n")

    matches = checker.coordinate_matches((repo / "point.txt").read_bytes())
    assert {(item.latitude, item.longitude) for item in matches} == {
        (latitude, longitude)
    }
    assert (checker.COORDINATE_RULE_NAME, "point.txt") in _rules(checker, repo)


@pytest.mark.parametrize("layout", ["geojson", "wkt", "labelled"])
def test_explicit_coordinate_formats_catch_lower_precision(
    checker, tmp_path, layout,
):
    repo = _repo(tmp_path / "repo")
    latitude = b"12." + b"34"
    longitude = b"-116." + b"89"
    payloads = {
        "geojson": (
            b'{"coordinates": [' + longitude + b", " + latitude + b"]}"
        ),
        "wkt": b"POINT (" + longitude + b" " + latitude + b")",
        "labelled": b"lat-lon=" + latitude + b" " + longitude,
    }
    _track(repo, "point.txt", payloads[layout])

    assert (checker.COORDINATE_RULE_NAME, "point.txt") in _rules(checker, repo)


def test_public_coordinate_fixtures_are_exact_pair_exceptions(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    public_pairs = b"\n".join(
        latitude + b", " + longitude
        for latitude, longitude in checker.PUBLIC_COORDINATE_FIXTURES
    )
    _track(repo, "public-locations.txt", public_pairs)

    assert checker.scan_repository(repo).findings == ()

    private_longitude = b"-65." + b"4321"
    public_latitude = next(iter(checker.PUBLIC_COORDINATE_FIXTURES))[0]
    _track(
        repo,
        "mixed-location.txt",
        b"latitude=" + public_latitude + b", longitude=" + private_longitude,
    )
    assert (
        checker.COORDINATE_RULE_NAME,
        "mixed-location.txt",
    ) in _rules(checker, repo)


def test_geojson_is_normalized_before_public_fixture_review(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    latitude = b"41." + b"9742"
    longitude = b"-87." + b"9073"
    _track(
        repo,
        "public-point.geojson",
        b'{"type":"Point","coordinates":['
        + longitude + b"," + latitude + b"]}",
    )

    matches = checker.coordinate_matches(
        (repo / "public-point.geojson").read_bytes()
    )
    assert {(item.latitude, item.longitude) for item in matches} == {
        (latitude, longitude)
    }
    assert checker.scan_repository(repo).findings == ()


def test_unrelated_coordinate_components_are_not_combined(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    latitude = b"12." + b"3456"
    longitude = b"-65." + b"4321"
    _track(
        repo,
        "separate-components.txt",
        b"HOME_LATITUDE=" + latitude + b"\nOFFICE_LONGITUDE=" + longitude,
    )

    assert checker.scan_repository(repo).findings == ()


def test_privacy_guard_source_is_safe_for_every_release_rule(checker):
    data = (ROOT / "tests" / "test_no_personal_data.py").read_bytes()
    path = checker.PurePosixPath("tests/test_no_personal_data.py")
    rules = set(checker.content_rule_names(data, path))

    assert not {
        checker.CONTACT_EMAIL_RULE_NAME,
        checker.PRIVATE_IP_RULE_NAME,
        checker.HOSTNAME_RULE_NAME,
        checker.COORDINATE_RULE_NAME,
    } & rules


def test_ignored_owner_data_is_never_opened(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(repo, ".gitignore", b".env\nlogs/\n")
    _track(repo, "README.md")
    secret = b"AK" + b"IA" + b"Z" * 16
    (repo / ".env").write_bytes(secret)
    (repo / "logs").mkdir()
    (repo / "logs" / "app.log").write_bytes(secret)

    assert checker.scan_repository(repo).findings == ()


def test_symlinks_are_scanned_without_following_them(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(repo, ".gitignore", b".env\n")
    secret = b"AK" + b"IA" + b"Z" * 16
    (repo / ".env").write_bytes(secret)
    os.symlink(".env", repo / "public-link")
    _git(repo, "add", "--", "public-link")

    assert checker.scan_repository(repo).findings == ()


def test_indexed_symlink_target_wins_over_a_worktree_type_change(
    checker, tmp_path,
):
    repo = _repo(tmp_path / "repo")
    private_target = "/" + "home" + "/synthetic-owner/private-target"
    os.symlink(private_target, repo / "public-link")
    _git(repo, "add", "--", "public-link")
    (repo / "public-link").unlink()
    (repo / "public-link").write_text("public replacement\n")

    assert _rules(checker, repo) == {
        ("absolute-home-path.posix", "public-link"),
    }


def test_gitlink_index_entry_fails_closed(checker, tmp_path):
    repo = _repo(tmp_path / "repo")
    _track(repo, "README.md")
    _git(
        repo,
        "-c", "user.name=Public Fixture",
        "-c", "user.email=fixture@example.invalid",
        "commit", "-qm", "public root",
    )
    oid = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    _git(
        repo,
        "update-index", "--add", "--cacheinfo", "160000", oid,
        "external/checkout",
    )

    assert (
        "tracked-entry-unsupported",
        "external/checkout",
    ) in _rules(checker, repo)


def test_index_change_during_scan_fails_closed(checker, tmp_path, monkeypatch):
    repo = _repo(tmp_path / "repo")
    _track(repo, "README.md")
    original = checker._tracked_bytes
    changed = False

    def mutate_index_after_read(root, entry):
        nonlocal changed
        data = original(root, entry)
        if not changed:
            changed = True
            _track(repo, "late-addition.txt")
        return data

    monkeypatch.setattr(checker, "_tracked_bytes", mutate_index_after_read)

    with pytest.raises(checker.InventoryError, match="candidate changed"):
        checker.scan_repository(repo)


def test_failure_output_never_prints_matching_content(checker, tmp_path, capsys):
    repo = _repo(tmp_path / "repo")
    private_path = b"/" + b"Users" + b"/alice/private-project"
    credential = b"AK" + b"IA" + b"Q" * 16
    _track(repo, "payload.bin", private_path + b"\x00" + credential)

    assert checker.main(["--root", str(repo)]) == 1
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert "payload.bin" in output
    assert "alice" not in output
    assert "private-project" not in output
    assert credential.decode() not in output
    assert "matched content is intentionally not shown" in output

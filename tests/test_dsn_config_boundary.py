"""DSN configuration is explicit runtime state, never an import side effect."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


REPO = Path(__file__).resolve().parents[1]
APP_DIR = REPO / "apps"
CONFIG_KEYS = {
    "BARKEEP_MANAGED",
    "BUSYBAR_CACHE_DIR",
    "DSN_CACHE_DIR",
    "DSN_NETWORK_STYLE",
    "DSN_POLL_S",
    "DSN_ROTATE_S",
    "DSN_VIEW",
    "DSN_VOICE",
}


def _clean_environment() -> dict[str, str]:
    return {key: value for key, value in os.environ.items()
            if key not in CONFIG_KEYS}


def test_dsn_reexports_the_extracted_configuration_boundary():
    sys.path.insert(0, str(APP_DIR))
    import dsn
    import dsn_config

    assert dsn.DsnConfig is dsn_config.DsnConfig
    assert dsn.DEFAULT_DSN_CONFIG is dsn_config.DEFAULT_DSN_CONFIG
    assert dsn.parse_runtime_config is dsn_config.parse_runtime_config
    assert dsn.resolve_cache_dir is dsn_config.resolve_cache_dir
    assert dsn.resolve_managed_cache_root is (
        dsn_config.resolve_managed_cache_root)


def test_plain_import_does_not_load_dotenv_or_inherit_owner_configuration():
    probe = f"""
import json
import sys
from unittest.mock import patch

sys.path.insert(0, {str(APP_DIR)!r})
with patch("busybar_dev.load_env") as load_env, patch("logging.basicConfig") as basic_config:
    import dsn
    print(json.dumps({{
        "load_calls": load_env.call_count,
        "logging_calls": basic_config.call_count,
        "poll": dsn.POLL_S,
        "rotate": dsn.ROTATE_S,
        "voice": dsn.VOICE,
        "view": dsn.DEFAULT_VIEW,
        "style": dsn.DSN_NETWORK_STYLE,
        "cache": str(dsn.CACHE_DIR),
    }}))
"""
    env = _clean_environment()
    env.update({
        "DSN_POLL_S": "99",
        "DSN_ROTATE_S": "88",
        "DSN_VOICE": "owner_voice",
        "DSN_VIEW": "distance",
        "DSN_NETWORK_STYLE": "rows",
        "BUSYBAR_CACHE_DIR": "/owner/cache",
        "DSN_CACHE_DIR": "owner-dsn",
        "BARKEEP_MANAGED": "1",
    })

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    imported = json.loads(result.stdout)
    assert imported == {
        "load_calls": 0,
        "logging_calls": 0,
        "poll": 10.0,
        "rotate": 20.0,
        "voice": "af_nova",
        "view": "network",
        "style": "dishes",
        "cache": str(REPO / "cache" / "dsn"),
    }


def test_explicit_runtime_configuration_loads_and_applies_owner_values(tmp_path):
    managed_cache = tmp_path / "managed cache"
    probe = f"""
import json
import os
import sys

sys.path.insert(0, {str(APP_DIR)!r})
import dsn

calls = []
def fake_load_env():
    calls.append("load")
    os.environ.update({{
        "DSN_POLL_S": "12.5",
        "DSN_ROTATE_S": "33",
        "DSN_VOICE": "bf_alice",
        "DSN_VIEW": "distance",
        "DSN_NETWORK_STYLE": "rows",
        "BUSYBAR_CACHE_DIR": {str(managed_cache)!r},
        "DSN_CACHE_DIR": "dsn-special",
        "BARKEEP_MANAGED": "1",
    }})
    return {{}}

dsn.load_env = fake_load_env
config = dsn.configure_runtime()
print(json.dumps({{
    "calls": calls,
    "frozen": config == dsn.RUNTIME_CONFIG,
    "poll": dsn.POLL_S,
    "rotate": dsn.ROTATE_S,
    "voice": dsn.VOICE,
    "view": dsn.DEFAULT_VIEW,
    "style": dsn.DSN_NETWORK_STYLE,
    "cache": str(dsn.CACHE_DIR),
    "range": str(dsn.RANGE_CACHE),
    "history": str(dsn.HISTORY_PATH),
    "delayed": dsn.FEED_DELAYED_S,
    "stale": dsn.FEED_STALE_S,
    "lease": dsn.LIVE_LEASE_TIMEOUT_S,
}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=_clean_environment(),
        check=True,
        capture_output=True,
        text=True,
    )

    configured = json.loads(result.stdout)
    assert configured == {
        "calls": ["load"],
        "frozen": True,
        "poll": 12.5,
        "rotate": 33.0,
        "voice": "bf_alice",
        "view": "distance",
        "style": "rows",
        "cache": str(managed_cache / "dsn-special"),
        "range": str(managed_cache / "dsn-special" / "dsn_ranges.json"),
        "history": str(managed_cache / "dsn-special" / "dsn_history.jsonl"),
        "delayed": 31.25,
        "stale": 62.5,
        "lease": 43,
    }


def test_mapping_parser_preserves_malformed_value_fallbacks(tmp_path):
    sys.path.insert(0, str(APP_DIR))
    import dsn

    config = dsn.parse_runtime_config({
        "DSN_POLL_S": "not-a-number",
        "DSN_ROTATE_S": "nan",
        "DSN_VIEW": "not-a-view",
        "DSN_NETWORK_STYLE": "not-a-style",
        "BUSYBAR_CACHE_DIR": "\x00bad",
        "DSN_CACHE_DIR": "\x00bad",
        "BARKEEP_MANAGED": "1",
    }, tmp_path)

    assert config.poll_s == 10.0
    assert config.rotate_s == 20.0
    assert config.voice == "af_nova"
    assert config.default_view == "network"
    assert config.network_style == "dishes"
    assert config.managed_cache_root == tmp_path / "cache"
    assert config.cache_dir == tmp_path / "cache" / "dsn"
    assert len(config.warnings) == 2
    assert "BUSYBAR_CACHE_DIR is unusable" in config.warnings[0]
    assert "DSN_CACHE_DIR" in config.warnings[1]

    with pytest.raises(AttributeError):
        config.poll_s = 30.0


def test_cli_applies_runtime_configuration_before_starting(monkeypatch):
    sys.path.insert(0, str(APP_DIR))
    import dsn

    events: list[str] = []

    def configure_logging(**_kwargs):
        events.append("logging")

    def configure():
        events.append("configure")
        return dsn.DEFAULT_DSN_CONFIG

    def run_coroutine(coroutine):
        events.append("run")
        coroutine.close()

    monkeypatch.setattr(dsn, "configure_runtime", configure)
    monkeypatch.setattr(dsn.logging, "basicConfig", configure_logging)
    monkeypatch.setattr(dsn.asyncio, "run", run_coroutine)
    monkeypatch.setattr(sys, "argv", ["dsn.py", "--once"])

    dsn.main()

    assert events == ["logging", "configure", "run"]

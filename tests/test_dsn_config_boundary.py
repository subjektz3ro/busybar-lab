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


def test_runtime_settings_use_the_pure_configuration_owner():
    from typing import get_type_hints

    from apps.dsn_app import config as dsn_config
    from apps.dsn_app import settings as dsn_settings

    assert dsn_settings._config is dsn_config
    assert get_type_hints(dsn_settings.configure_runtime)["return"] is (
        dsn_config.DsnConfig)


def test_settings_apply_one_configuration_to_all_runtime_consumers(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from apps.dsn_app import config, ranges, settings
    from apps.dsn_app.device import display

    # Restore every process setting, including derived leases and cache paths.
    for name, value in vars(settings).copy().items():
        if name.isupper():
            monkeypatch.setattr(settings, name, value)
    values = {"DSN_POLL_S": "12.5", "DSN_ROTATE_S": "33",
              "DSN_VOICE": "bf_alice", "DSN_VIEW": "distance",
              "DSN_NETWORK_STYLE": "rows", "BARKEEP_MANAGED": "1",
              "BUSYBAR_CACHE_DIR": str(tmp_path), "DSN_CACHE_DIR": "custom"}
    events = []
    monkeypatch.setattr(settings, "load_env", lambda: events.append("dotenv"))
    monkeypatch.setattr(settings, "os", SimpleNamespace(environ=values))
    result = settings.configure_runtime()
    assert events == ["dotenv"]
    assert result == config.parse_runtime_config(values)
    assert settings.RUNTIME_CONFIG is result
    assert (settings.POLL_S, settings.ROTATE_S, settings.VOICE,
            settings.DEFAULT_VIEW, settings.DSN_NETWORK_STYLE) == (
                12.5, 33.0, "bf_alice", "distance", "rows")
    assert settings.CACHE_DIR == tmp_path / "custom"
    assert settings.RANGE_CACHE == settings.CACHE_DIR / "dsn_ranges.json"
    assert settings.HISTORY_PATH == settings.CACHE_DIR / "dsn_history.jsonl"
    assert (settings.FEED_DELAYED_S, settings.FEED_STALE_S,
            settings.LIVE_LEASE_TIMEOUT_S) == (31.25, 62.5, 43)
    assert ranges._settings is display._settings is settings
    assert not settings.CACHE_DIR.exists()


def test_plain_import_does_not_load_dotenv_or_inherit_owner_configuration():
    probe = f"""
import json
import sys
from unittest.mock import patch

sys.path.insert(0, {str(APP_DIR)!r})
with patch("busybar_dev.load_env") as load_env, patch("logging.basicConfig") as basic_config:
    from apps.dsn_app import cli as dsn_cli
    from apps.dsn_app import config as dsn_config
    from apps.dsn_app import settings as dsn_settings
    import asyncio
    import logging
    print(json.dumps({{
        "load_calls": load_env.call_count,
        "logging_calls": basic_config.call_count,
        "poll": dsn_settings.POLL_S,
        "rotate": dsn_settings.ROTATE_S,
        "voice": dsn_settings.VOICE,
        "view": dsn_settings.DEFAULT_VIEW,
        "style": dsn_settings.DSN_NETWORK_STYLE,
        "cache": str(dsn_settings.CACHE_DIR),
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
from apps.dsn_app import cli as dsn_cli
from apps.dsn_app import config as dsn_config
from apps.dsn_app import settings as dsn_settings
import asyncio
import logging

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

dsn_settings.load_env = fake_load_env
config = dsn_settings.configure_runtime()
print(json.dumps({{
    "calls": calls,
    "frozen": config == dsn_settings.RUNTIME_CONFIG,
    "poll": dsn_settings.POLL_S,
    "rotate": dsn_settings.ROTATE_S,
    "voice": dsn_settings.VOICE,
    "view": dsn_settings.DEFAULT_VIEW,
    "style": dsn_settings.DSN_NETWORK_STYLE,
    "cache": str(dsn_settings.CACHE_DIR),
    "range": str(dsn_settings.RANGE_CACHE),
    "history": str(dsn_settings.HISTORY_PATH),
    "delayed": dsn_settings.FEED_DELAYED_S,
    "stale": dsn_settings.FEED_STALE_S,
    "lease": dsn_settings.LIVE_LEASE_TIMEOUT_S,
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
    from apps.dsn_app import config as dsn_config

    config = dsn_config.parse_runtime_config({
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
    import asyncio
    import logging

    from apps.dsn_app import cli as dsn_cli
    from apps.dsn_app import config as dsn_config
    from apps.dsn_app import settings as dsn_settings

    events: list[str] = []

    def configure_logging(**_kwargs):
        events.append("logging")

    def configure():
        events.append("configure")
        return dsn_config.DEFAULT_DSN_CONFIG

    def run_coroutine(coroutine):
        events.append("run")
        coroutine.close()

    monkeypatch.setattr(dsn_settings, "configure_runtime", configure)
    monkeypatch.setattr(logging, "basicConfig", configure_logging)
    monkeypatch.setattr(asyncio, "run", run_coroutine)
    monkeypatch.setattr(sys, "argv", ["dsn.py", "--once"])

    dsn_cli.main()

    assert events == ["logging", "configure", "run"]

"""Skystrip configuration is explicit runtime state, never an import effect."""

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
    "BUSYBAR_STATE_DIR",
    "SKYSTRIP_CHRISTMAS",
    "SKYSTRIP_CONTACT",
    "SKYSTRIP_LAT",
    "SKYSTRIP_LIGHTNING_WS",
    "SKYSTRIP_LON",
    "SKYSTRIP_SCENES",
    "SKYSTRIP_STATION",
    "SKYSTRIP_STYLE",
    "SKYSTRIP_TZ",
    "SKYSTRIP_UNITS",
    "SKYSTRIP_VOICE",
}


def _clean_environment() -> dict[str, str]:
    return {
        key: value for key, value in os.environ.items()
        if key not in CONFIG_KEYS
    }


def test_skystrip_reexports_the_extracted_configuration_boundary():
    sys.path.insert(0, str(APP_DIR))
    import skystrip
    import skystrip_config

    assert skystrip.SkystripConfig is skystrip_config.SkystripConfig
    assert (
        skystrip.DEFAULT_SKYSTRIP_CONFIG
        is skystrip_config.DEFAULT_SKYSTRIP_CONFIG
    )
    assert skystrip.parse_runtime_config is skystrip_config.parse_runtime_config
    assert skystrip.resolve_state_root is skystrip_config.resolve_state_root
    assert skystrip._validate_lightning_ws_endpoint is (
        skystrip_config._validate_lightning_ws_endpoint
    )


def test_configuration_module_is_pure_and_accepts_an_explicit_mapping(tmp_path):
    sys.path.insert(0, str(APP_DIR))
    import skystrip_config

    values = {
        "SKYSTRIP_LAT": "51.5074",
        "SKYSTRIP_LON": "-0.1278",
        "SKYSTRIP_TZ": "Europe/London",
        "SKYSTRIP_SCENES": "grove,house",
    }
    config = skystrip_config.parse_runtime_config(values, tmp_path)

    assert config.latitude == 51.5074
    assert config.longitude == -0.1278
    assert str(config.timezone) == "Europe/London"
    assert config.enabled_scenes == ("house", "grove")
    assert config.state_root == tmp_path / "state"


def test_package_import_resolves_the_same_support_boundaries():
    probe = """
import json
import apps.skystrip as skystrip
print(json.dumps({
    "config": skystrip.SkystripConfig.__module__,
    "strike": skystrip._LightningStrike.__module__,
    "timezone": str(skystrip.ZoneInfo("UTC")),
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=_clean_environment(),
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "config": "apps.skystrip_config",
        "strike": "apps.skystrip_lightning",
        "timezone": "UTC",
    }


def test_plain_import_does_not_load_dotenv_or_inherit_owner_configuration():
    probe = f"""
import json
import sys
from unittest.mock import patch

sys.path.insert(0, {str(APP_DIR)!r})
with patch("busybar_dev.load_env") as load_env:
    import skystrip
    print(json.dumps({{
        "load_calls": load_env.call_count,
        "latitude": skystrip.LAT,
        "longitude": skystrip.LON,
        "located": skystrip.LOCATION_SET,
        "timezone": str(skystrip.TZ),
        "units": skystrip.UNITS,
        "style": skystrip.STYLE,
        "voice": skystrip.REPORT_VOICE,
        "christmas": skystrip.CHRISTMAS_WINDOW,
        "station": skystrip.NWS_STATION,
        "user_agent": skystrip.NWS_UA["User-Agent"],
        "lightning": skystrip.LIGHTNING_WS,
        "state_root": str(skystrip.STATE_ROOT),
        "scenes": list(skystrip.ENABLED_SCENES),
    }}))
"""
    env = _clean_environment()
    env.update({
        "BUSYBAR_STATE_DIR": "/owner/state",
        "SKYSTRIP_CHRISTMAS": "off",
        "SKYSTRIP_CONTACT": "owner@example.invalid",
        "SKYSTRIP_LAT": "51.5074",
        "SKYSTRIP_LIGHTNING_WS": "wss://owner.example/secret",
        "SKYSTRIP_LON": "-0.1278",
        "SKYSTRIP_SCENES": "grove",
        "SKYSTRIP_STATION": "KOWN",
        "SKYSTRIP_STYLE": "chicago",
        "SKYSTRIP_TZ": "Europe/London",
        "SKYSTRIP_UNITS": "c",
        "SKYSTRIP_VOICE": "owner_voice",
    })

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=REPO,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "load_calls": 0,
        "latitude": 0.0,
        "longitude": 0.0,
        "located": False,
        "timezone": "UTC",
        "units": "f",
        "style": "plain",
        "voice": "am_michael",
        "christmas": "dec24-26",
        "station": "",
        "user_agent": "skystrip (hobby project)",
        "lightning": None,
        "state_root": str(REPO / "state"),
        "scenes": [
            "house", "skyline", "lakefront", "forest", "grove", "backroads",
        ],
    }


def test_explicit_runtime_configuration_loads_and_applies_owner_values(tmp_path):
    state_root = tmp_path / "managed state"
    probe = f"""
import json
import os
import sys

sys.path.insert(0, {str(APP_DIR)!r})
import skystrip

calls = []
def fake_load_env():
    calls.append("load")
    os.environ.update({{
        "BUSYBAR_STATE_DIR": {str(state_root)!r},
        "SKYSTRIP_CHRISTMAS": "off",
        "SKYSTRIP_CONTACT": "operator@example.invalid",
        "SKYSTRIP_LAT": "51.5074",
        "SKYSTRIP_LIGHTNING_WS": "wss://relay.example/private",
        "SKYSTRIP_LON": "-0.1278",
        "SKYSTRIP_SCENES": "grove,house",
        "SKYSTRIP_STATION": "KTEST",
        "SKYSTRIP_STYLE": "chicago",
        "SKYSTRIP_TZ": "Europe/London",
        "SKYSTRIP_UNITS": "c",
        "SKYSTRIP_VOICE": "bf_alice",
    }})
    return {{}}

skystrip.load_env = fake_load_env
before_name = skystrip.report_asset_name("same report")
config = skystrip.configure_runtime()
print(json.dumps({{
    "calls": calls,
    "frozen": config == skystrip.RUNTIME_CONFIG,
    "latitude": skystrip.LAT,
    "longitude": skystrip.LON,
    "located": skystrip.LOCATION_SET,
    "timezone": str(skystrip.TZ),
    "units": skystrip.UNITS,
    "style": skystrip.STYLE,
    "voice": skystrip.REPORT_VOICE,
    "voice_changes_cache_key": before_name != skystrip.report_asset_name("same report"),
    "christmas": skystrip.CHRISTMAS_WINDOW,
    "station": skystrip.NWS_STATION,
    "user_agent": skystrip.NWS_UA["User-Agent"],
    "lightning_enabled": skystrip.LIGHTNING_WS is not None,
    "state_root": str(skystrip.STATE_ROOT),
    "scene_file": str(skystrip.SCENE_FILE),
    "scenes": list(skystrip.ENABLED_SCENES),
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

    assert json.loads(result.stdout) == {
        "calls": ["load"],
        "frozen": True,
        "latitude": 51.5074,
        "longitude": -0.1278,
        "located": True,
        "timezone": "Europe/London",
        "units": "c",
        "style": "chicago",
        "voice": "bf_alice",
        "voice_changes_cache_key": True,
        "christmas": "off",
        "station": "KTEST",
        "user_agent": "skystrip (operator@example.invalid)",
        "lightning_enabled": True,
        "state_root": str(state_root),
        "scene_file": str(state_root / "skystrip-scene"),
        "scenes": ["house", "grove"],
    }


def test_mapping_parser_is_immutable_and_requires_coordinate_pairs(tmp_path):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    config = skystrip.parse_runtime_config({}, tmp_path)
    with pytest.raises(AttributeError):
        config.units = "c"

    with pytest.raises(ValueError, match="must be configured together"):
        skystrip.parse_runtime_config({"SKYSTRIP_LAT": "51.5074"}, tmp_path)


def test_cli_configures_before_warning_or_runtime(monkeypatch):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    events: list[str] = []

    def configure():
        events.append("configure")
        return skystrip.DEFAULT_SKYSTRIP_CONFIG

    def warning():
        events.append("warning")
        return ""

    def run_coroutine(coroutine):
        events.append("run")
        coroutine.close()

    monkeypatch.setattr(skystrip, "configure_runtime", configure)
    monkeypatch.setattr(skystrip, "warn_if_unlocated", warning)
    monkeypatch.setattr(skystrip.asyncio, "run", run_coroutine)
    monkeypatch.setattr(sys, "argv", ["skystrip.py", "--once"])

    skystrip.main()

    assert events == ["configure", "warning", "run"]
    assert "Programmatic callers must do the same" in (skystrip.run.__doc__ or "")


def _prepare_cli(monkeypatch, skystrip, argv, events):
    monkeypatch.delenv("BARKEEP_MANAGED", raising=False)
    monkeypatch.setattr(
        skystrip, "configure_runtime", lambda: skystrip.DEFAULT_SKYSTRIP_CONFIG
    )
    monkeypatch.setattr(skystrip, "warn_if_unlocated", lambda: "")
    monkeypatch.setattr(sys, "argv", ["skystrip.py", *argv])

    def run_coroutine(coroutine):
        events.append("run")
        coroutine.close()

    monkeypatch.setattr(skystrip.asyncio, "run", run_coroutine)


@pytest.mark.parametrize(
    ("argv", "providers"),
    [([], "Open-Meteo/RainViewer"), (["--report"], "Open-Meteo")],
)
def test_standalone_provider_modes_refuse_before_polling(
    monkeypatch, capsys, argv, providers,
):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    events: list[str] = []
    _prepare_cli(monkeypatch, skystrip, argv, events)

    with pytest.raises(SystemExit) as excinfo:
        skystrip.main()

    assert excinfo.value.code == 2
    assert events == []
    message = capsys.readouterr().err
    assert f"{providers} polling is off" in message
    assert "--enable-network-providers" in message
    assert "does not grant rights" in message


def test_standalone_explicit_provider_opt_in_announces_before_runtime(
    monkeypatch, capsys,
):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    events: list[str] = []
    _prepare_cli(
        monkeypatch, skystrip, ["--enable-network-providers"], events
    )
    notices_before_runtime: list[str] = []

    def run_after_notice(coroutine):
        notices_before_runtime.append(capsys.readouterr().err)
        events.append("run")
        coroutine.close()

    monkeypatch.setattr(skystrip.asyncio, "run", run_after_notice)

    skystrip.main()

    assert events == ["run"]
    message = notices_before_runtime[0]
    assert "Weather data by Open-Meteo.com" in message
    assert "Weather radar data by RainViewer" in message
    assert "https://open-meteo.com/en/terms" in message
    assert "https://www.rainviewer.com/api.html" in message


def test_barkeep_managed_launch_crosses_the_visible_provider_boundary(
    monkeypatch, capsys,
):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    events: list[str] = []
    _prepare_cli(monkeypatch, skystrip, [], events)
    monkeypatch.setenv("BARKEEP_MANAGED", "1")

    skystrip.main()

    assert events == ["run"]
    assert "Weather data by Open-Meteo.com" in capsys.readouterr().err


def test_preview_neither_needs_nor_starts_provider_polling(
    monkeypatch, tmp_path,
):
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    class PreviewFrame:
        def resize(self, *_args, **_kwargs):
            return self

        def save(self, path):
            Path(path).write_bytes(b"offline preview")

    events: list[str] = []
    output = tmp_path / "preview.png"
    _prepare_cli(monkeypatch, skystrip, ["--preview", str(output)], events)
    monkeypatch.setattr(
        skystrip, "render_loop_frames", lambda *_args, **_kwargs: [PreviewFrame()]
    )
    monkeypatch.setattr(
        skystrip, "poll_nws", lambda *_args: pytest.fail("NWS/Open-Meteo polled")
    )
    monkeypatch.setattr(
        skystrip, "poll_radar", lambda *_args: pytest.fail("RainViewer polled")
    )

    skystrip.main()

    assert output.read_bytes() == b"offline preview"
    assert events == []


def test_report_notice_names_only_the_provider_that_mode_calls():
    sys.path.insert(0, str(APP_DIR))
    import skystrip

    args = skystrip.build_parser().parse_args(
        ["--report", "--enable-network-providers"]
    )
    notice = skystrip._provider_notice(args)
    assert notice == f"Skystrip data: {skystrip.OPEN_METEO_NOTICE}"
    assert "RainViewer" not in notice

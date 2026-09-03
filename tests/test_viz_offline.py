"""Offline rendering is enforced by code, not only documented intent."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import socket

import pytest
from PIL import Image

from busybar_viz import registry, worker
from busybar_viz.models import (
    CheckSpec,
    Confidence,
    DisplayTrack,
    RenderedSegment,
)
from busybar_viz.offline import network_disabled, offline_render


DEVICE_TOKEN_KEY = "BUSYBAR_" + "TOKEN"
LIGHTNING_URL_KEY = "SKYSTRIP_LIGHTNING_" + "WS"


def test_network_guard_blocks_common_socket_paths_and_restores_them():
    original_connect = socket.socket.connect
    original_create_connection = socket.create_connection

    with network_disabled():
        with socket.socket() as client:
            with pytest.raises(OSError, match="disabled in busybar-viz"):
                client.connect(("127.0.0.1", 1))
        with pytest.raises(OSError, match="disabled in busybar-viz"):
            socket.create_connection(("127.0.0.1", 1))

    assert socket.socket.connect is original_connect
    assert socket.create_connection is original_create_connection


def test_offline_render_prevents_dotenv_from_restoring_personal_values(
    tmp_path, monkeypatch,
):
    disk_relay = "wss://disk.example/path?token=hidden"
    (tmp_path / ".env").write_text(
        f"{DEVICE_TOKEN_KEY}=from-disk\nSKYSTRIP_TZ=Personal/Zone\n"
        f"{LIGHTNING_URL_KEY}={disk_relay}\n"
    )
    (tmp_path / "apps.toml").write_text(
        "[skystrip]\n[skystrip.config.SKYSTRIP_TZ]\ntype = \"text\"\n"
    )
    monkeypatch.setenv("BUSYBAR_TOKEN", "from-process")
    monkeypatch.setenv(
        "SKYSTRIP_LIGHTNING_WS", "wss://process.example/path?token=hidden"
    )
    monkeypatch.setenv("SAFE_PLATFORM_VALUE", "kept")
    monkeypatch.delenv("SKYSTRIP_TZ", raising=False)

    with offline_render(tmp_path):
        # This is the pattern used by production app dotenv loaders. Empty
        # sentinels keep a missing worker credential from being rehydrated.
        for key, value in {
            DEVICE_TOKEN_KEY: "from-disk",
            "SKYSTRIP_TZ": "Personal/Zone",
            LIGHTNING_URL_KEY: disk_relay,
        }.items():
            os.environ.setdefault(key, value)

        assert os.environ["BUSYBAR_TOKEN"] == ""
        assert os.environ["SKYSTRIP_TZ"] == ""
        assert os.environ["SKYSTRIP_LIGHTNING_WS"] == ""
        assert os.environ["SAFE_PLATFORM_VALUE"] == "kept"
        assert os.environ["BUSYBAR_VIZ_OFFLINE"] == "1"

    assert os.environ["BUSYBAR_TOKEN"] == "from-process"
    assert os.environ["SKYSTRIP_LIGHTNING_WS"].startswith("wss://process.example")
    assert "SKYSTRIP_TZ" not in os.environ
    assert os.environ["SAFE_PLATFORM_VALUE"] == "kept"


def test_worker_enters_offline_context_before_registry_resolution(
    tmp_path, monkeypatch, capfd,
):
    original_connect = socket.socket.connect
    seen: list[tuple[str, str | None, bool]] = []

    def observe(label: str) -> None:
        seen.append((
            label,
            os.environ.get("BUSYBAR_TOKEN"),
            socket.socket.connect is not original_connect,
        ))

    def resolve(_scenario_id):
        observe("resolve")
        return object()

    def render(_request):
        observe("render")
        return RenderedSegment(
            displays=(DisplayTrack(
                "front",
                (Image.new("RGB", (72, 16), (1, 2, 3)),),
                1,
                Confidence.SOURCE_EXACT,
            ),),
            checks=(CheckSpec.create(
                "front-dimensions",
                "frame.dimensions",
                display="front",
                size=(72, 16),
            ),),
            source_paths=("tests/test_viz_offline.py",),
        )

    payload = json.dumps({"scenario_id": "fixture/offline"}).encode()

    class Input:
        buffer = io.BytesIO(payload)

    monkeypatch.setenv("BUSYBAR_TOKEN", "dummy-device-secret")
    monkeypatch.setattr(worker, "_apply_resource_limits", lambda: None)
    monkeypatch.setattr(worker.sys, "stdin", Input())
    monkeypatch.setattr(registry, "adapter_for_scenario", resolve)
    monkeypatch.setattr(registry, "render_registered", render)

    code = worker.main((
        "--repo-root", str(Path(__file__).resolve().parents[1]),
        "--data-root", str(tmp_path / "viz"),
    ))
    emitted = json.loads(capfd.readouterr().out)

    assert code == 0
    assert emitted["ok"] is True
    assert seen == [
        ("resolve", "", True),
        ("render", "", True),
    ]
    assert os.environ["BUSYBAR_TOKEN"] == "dummy-device-secret"
    assert socket.socket.connect is original_connect

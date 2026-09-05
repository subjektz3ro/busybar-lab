"""Boundary cases made visible by the app package split; no hardware or network."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from apps.dsn_app.render import network_data
from apps.skystrip_app import build_info, config
from apps.skystrip_app.device import assets


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        (" Goldstone ", "Goldstone"),
        ("goldstone-dss", "Goldstone"),
        ("Robledo de Chavela", "Madrid"),
        ("Madrid station", "Madrid"),
        ("Tidbinbilla", "Canberra"),
        ("Canberra station", "Canberra"),
        ("unrecognized site", "unrecognized site"),
        ("", ""),
    ],
)
def test_network_site_names_normalize_only_known_aliases(name, expected):
    assert network_data._site_name(name) == expected


@pytest.mark.parametrize(
    ("stdout", "expected"), [("abc123\n", "abc123"), ("", "unknown")]
)
def test_build_revision_resolves_from_the_checkout_not_the_moved_module(
    monkeypatch, stdout, expected
):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(stdout=stdout)

    monkeypatch.setattr(build_info.subprocess, "run", run)
    assert build_info._git_rev() == expected
    assert calls == [
        (
            ["git", "rev-parse", "--short", "HEAD"],
            {
                "cwd": config.REPO_ROOT,
                "capture_output": True,
                "text": True,
                "timeout": 5,
            },
        )
    ]


def test_missing_git_does_not_block_the_app(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise FileNotFoundError("git")

    monkeypatch.setattr(build_info.subprocess, "run", unavailable)
    assert build_info._git_rev() == "unknown"


async def test_skystrip_sweep_is_closed_to_unrecognized_and_durable_assets():
    # A deletion failure must not prevent the next stale generation being reaped.
    names = [
        "sky_ab.anim",
        "train_123.anim",
        "sky_a.png",
        "siren.snd",
        "siren_digest.snd",
        "owner.png",
        "future_format.bin",
    ]
    bar = SimpleNamespace(
        display_clear=AsyncMock(),
        storage_list=AsyncMock(
            return_value=SimpleNamespace(
                list=[SimpleNamespace(name=name) for name in names]
            )
        ),
        storage_remove=AsyncMock(side_effect=[OSError("busy"), None, None, None]),
    )
    await assets.sweep_stale_assets(bar)
    bar.display_clear.assert_awaited_once_with(application_name=assets._limits.APP_NAME)
    prefix = f"/ext/user_assets/{assets._limits.APP_NAME}/"
    bar.storage_list.assert_awaited_once_with(prefix.rstrip("/"))
    assert [call.args for call in bar.storage_remove.await_args_list] == [
        (prefix + name,) for name in names[:4]
    ]


@pytest.mark.parametrize("operation", ["display_clear", "storage_list"])
async def test_skystrip_sweep_failure_is_nonfatal_and_never_deletes_blindly(operation):
    bar = SimpleNamespace(
        display_clear=AsyncMock(), storage_list=AsyncMock(), storage_remove=AsyncMock()
    )
    getattr(bar, operation).side_effect = OSError("unavailable")
    await assets.sweep_stale_assets(bar)
    bar.storage_remove.assert_not_awaited()

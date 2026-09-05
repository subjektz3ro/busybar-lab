"""Shared device rules, and the busylib surface we depend on."""

from __future__ import annotations

import pytest
from busylib import exceptions

from busybar_dev.device import (
    DEVICE_ASSET_FILENAME_MAX,
    asset_path,
    content_asset_name,
    is_refusal,
    storage_file_matches,
)


# --- is_refusal ------------------------------------------------------------


def test_a_409_is_a_refusal():
    """busylib exposes the code as status_code. `http_status` does not exist
    and silently never matches — its own entry in the skill's mistake table."""
    exc = exceptions.BusyBarAPIError("Not drawn due to low priority",
                                     status_code=409)
    assert is_refusal(exc)
    assert not hasattr(exc, "http_status")


def test_an_ordinary_failure_is_not_a_refusal():
    for exc in (
        exceptions.BusyBarAPIError("Upload timeout", status_code=408),
        exceptions.BusyBarAPIError("Failed to open file", status_code=508),
        ConnectionError("usb went away"),
        TimeoutError(),
    ):
        assert not is_refusal(exc), exc


def test_the_legacy_string_fallback_still_matches():
    assert is_refusal(RuntimeError("Not drawn due to low priority"))


# --- storage_file_matches --------------------------------------------------


class _Entry:
    def __init__(self, kind, size):
        self.type, self.size = kind, size


def test_only_a_file_of_the_exact_size_matches():
    assert storage_file_matches(_Entry("file", 10), 10)
    assert not storage_file_matches(_Entry("file", 11), 10)
    assert not storage_file_matches(_Entry("dir", 10), 10)


def test_an_entry_with_no_type_is_not_assumed_to_be_a_file():
    """skystrip's copy defaulted a missing type to 'file'; dsn's did not.
    This is dsn's, which is the safer of the two."""
    class Bare:
        size = 10
    assert not storage_file_matches(Bare(), 10)


def test_an_enum_type_is_unwrapped():
    class Kind:
        value = "file"
    assert storage_file_matches(_Entry(Kind(), 10), 10)


# --- asset naming ----------------------------------------------------------


def test_identical_bytes_give_an_identical_path():
    """The firmware caches by path forever. That is the trap for a mutable
    name and the mechanism for an immutable one."""
    blob = b"\x01\x02\x03"
    assert content_asset_name("tts_", blob, suffix=".snd") == \
        content_asset_name("tts_", blob, suffix=".snd")


def test_different_bytes_never_share_a_path():
    a = content_asset_name("tts_", b"one", suffix=".snd")
    b = content_asset_name("tts_", b"two", suffix=".snd")
    assert a != b


def test_a_name_that_would_exceed_the_device_limit_is_refused():
    with pytest.raises(ValueError, match="device limit"):
        content_asset_name("a" * 40, b"x", suffix=".snd")
    ok = content_asset_name("tts_", b"x", suffix=".snd")
    assert len(ok.encode("ascii")) <= DEVICE_ASSET_FILENAME_MAX


def test_asset_path_is_the_device_layout():
    assert asset_path("skystrip", "sky_1.anim") == \
        "/ext/user_assets/skystrip/sky_1.anim"


# --- one-shot CLI paths are safe offline and during focus sessions -----------
#
# The skill's law 4: "the one-shot --demo / --once CLI paths are exactly what
# the workflow tells you to run on hardware, so an unguarded one greets you
# with a traceback the first time you test during a focus session." Both
# apps/_template.py — the file every new app is copied from — and apps/hello.py
# — the command AGENTS.md names as the stack smoke test — were unguarded, and
# no test executed either file at all.

import importlib.util  # noqa: E402
import sys  # noqa: E402
from pathlib import Path  # noqa: E402

APPS = Path(__file__).resolve().parents[1] / "apps"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(f"_cli_{name}", APPS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class RefusingBar:
    """A bar with a BUSY/CUSTOM session up: every draw and play gets a 409."""

    def __init__(self):
        self.drew = self.played = False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def version(self):
        return type("V", (), {"api_semver": "25.0.0"})()

    def _refuse(self):
        raise exceptions.BusyBarAPIError("Not drawn due to low priority",
                                         status_code=409)

    def display_draw(self, payload):
        self.drew = True
        self._refuse()

    def audio_play(self, **kwargs):
        self.played = True
        self._refuse()

    def assets_upload(self, *a, **k):
        return None

    def display_clear(self, **kwargs):
        return None

    def close(self):
        return None


def test_the_template_yields_to_a_focus_session(monkeypatch, capsys):
    template = _load("_template")
    bar = RefusingBar()
    monkeypatch.setattr(template, "connect", lambda *a, **k: bar)
    config = template.Config(app_name="demo", text="HI", priority=30,
                             dry_run=False)
    template.run(config)          # must not raise
    assert bar.drew, "the guard must not skip the draw entirely"


def test_the_template_still_reports_a_real_failure(monkeypatch):
    template = _load("_template")

    class BrokenBar(RefusingBar):
        def display_draw(self, payload):
            raise exceptions.BusyBarAPIError("Upload timeout", status_code=408)

    monkeypatch.setattr(template, "connect", lambda *a, **k: BrokenBar())
    config = template.Config(app_name="demo", text="HI", priority=30,
                             dry_run=False)
    with pytest.raises(exceptions.BusyBarAPIError):
        template.run(config)


def test_hello_yields_on_a_refused_draw(monkeypatch):
    hello = _load("hello")
    bar = RefusingBar()
    monkeypatch.setattr(hello, "connect", lambda *a, **k: bar)
    monkeypatch.setattr(sys, "argv", ["hello.py"])
    hello.main()                  # must not raise
    assert bar.drew and not bar.played


def test_hello_yields_on_refused_audio(monkeypatch, tmp_path):
    """audio_play refuses during a session the same way a draw does — the
    skill's 'guarding display_draw but not audio_play' mistake."""
    hello = _load("hello")

    class DrawOkAudioRefuses(RefusingBar):
        def display_draw(self, payload):
            self.drew = True

    bar = DrawOkAudioRefuses()
    monkeypatch.setattr(hello, "connect", lambda *a, **k: bar)
    monkeypatch.setattr(hello, "save_screens",
                        lambda *a, **k: (tmp_path / "f.png", tmp_path / "b.png"))
    monkeypatch.setattr(hello, "say_on_bar",
                        lambda *a, **k: bar._refuse())
    monkeypatch.setattr(sys, "argv", ["hello.py", "--say", "hello"])
    hello.main()                  # must not raise


def test_hello_dry_run_never_connects(monkeypatch, caplog):
    hello = _load("hello")

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("--dry-run touched the BUSY Bar")

    monkeypatch.setattr(hello, "connect", unexpected_connect)
    monkeypatch.setattr(
        sys,
        "argv",
        ["hello.py", "--dry-run", "--text", "OFFLINE", "--say", "hello"],
    )

    with caplog.at_level("INFO"):
        hello.main()

    assert "Dry run payload" in caplog.text
    assert "OFFLINE" in caplog.text
    assert "Dry run speech" in caplog.text


def test_skystrip_report_yields_to_a_focus_session(monkeypatch):
    """_play_audio re-raises a refusal on purpose (it must not answer a 409
    with the device-global STOP, which would silence another app). Catching it
    belongs in the CLI path, which had no handler at all — `main()` called
    asyncio.run(report_once()) bare."""
    import asyncio

    sys.path.insert(0, str(APPS))
    from apps.skystrip_app import cli as sky_cli
    from apps.skystrip_app.audio import output as sky_audio_output
    from apps.skystrip_app.audio import report as sky_audio_report
    from apps.skystrip_app.audio import report_policy as sky_audio_report_policy
    from apps.skystrip_app.providers import weather as sky_providers_weather

    async def done(value):
        return value

    class Bar:
        async def aclose(self):
            return None

    async def refuse(*a, **k):
        raise exceptions.BusyBarAPIError("Not drawn due to low priority",
                                         status_code=409)

    played: list[str] = []

    async def scenario():
        real_sleep = asyncio.sleep
        # report_once polls for up to 20s waiting on live feeds; this test is
        # about the refusal, not the wait.
        monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
        monkeypatch.setattr(sky_cli, "aconnect", lambda *a, **k: done(Bar()))
        monkeypatch.setattr(sky_providers_weather, "poll_nws", lambda state: real_sleep(0))
        monkeypatch.setattr(sky_audio_report_policy, "_current_report_text", lambda state: "hi")
        monkeypatch.setattr(sky_audio_report, "_prepare_report_take",
                            lambda bb, state, text: done("r.snd"))

        async def record(bb, state, path, owner, still_valid):
            played.append(path)
            await refuse()

        monkeypatch.setattr(sky_audio_output, "_play_audio", record)
        await sky_cli.report_once()          # must not raise

    asyncio.run(scenario())
    assert played == ["r.snd"], "the guard must not skip the play attempt"


def test_skystrip_report_still_reports_a_real_failure(monkeypatch):
    import asyncio

    sys.path.insert(0, str(APPS))
    from apps.skystrip_app import cli as sky_cli
    from apps.skystrip_app.audio import output as sky_audio_output
    from apps.skystrip_app.audio import report as sky_audio_report
    from apps.skystrip_app.audio import report_policy as sky_audio_report_policy
    from apps.skystrip_app.providers import weather as sky_providers_weather

    async def done(value):
        return value

    class Bar:
        async def aclose(self):
            return None

    async def scenario():
        real_sleep = asyncio.sleep
        monkeypatch.setattr(asyncio, "sleep", lambda *_a, **_k: real_sleep(0))
        monkeypatch.setattr(sky_cli, "aconnect", lambda *a, **k: done(Bar()))
        monkeypatch.setattr(sky_providers_weather, "poll_nws", lambda state: real_sleep(0))
        monkeypatch.setattr(sky_audio_report_policy, "_current_report_text", lambda state: "hi")
        monkeypatch.setattr(sky_audio_report, "_prepare_report_take",
                            lambda bb, state, text: done("r.snd"))

        async def boom(*a, **k):
            raise exceptions.BusyBarAPIError("Upload timeout", status_code=408)

        monkeypatch.setattr(sky_audio_output, "_play_audio", boom)
        await sky_cli.report_once()

    with pytest.raises(exceptions.BusyBarAPIError):
        asyncio.run(scenario())

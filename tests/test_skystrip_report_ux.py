"""Cache-only interaction contracts for Skystrip's spoken weather report."""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from busylib import exceptions
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import cli as sky_cli
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app.audio import report as sky_audio_report
from apps.skystrip_app.audio import report_assets as sky_audio_report_assets
from apps.skystrip_app.audio import report_plain as sky_audio_report_plain
from apps.skystrip_app.audio import report_policy as sky_audio_report_policy
from apps.skystrip_app.device import alerts as sky_device_alerts
from apps.skystrip_app.device import assets as sky_device_assets
from apps.skystrip_app.device import display as sky_device_display
from apps.skystrip_app.device import report_status as sky_device_report_status
from apps.skystrip_app.providers import weather as sky_providers_weather
from busybar_dev import anim
from busybar_dev.pixel_text import text_width

REPORT_TEXT = "A truthful test forecast."


class ReportBar:
    def __init__(self) -> None:
        self.draws = []
        self.operations: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.plays: list[str] = []
        self.removals: list[str] = []
        self.stops = 0
        self.files: dict[str, bytes] = {}

    async def display_draw(self, payload):
        self.draws.append(payload)
        labels = [element.text for element in payload.elements
                  if getattr(element, "text", None) is not None]
        self.operations.append(f"DRAW:{labels[-1] if labels else '-'}")

    async def assets_upload(self, application_name: str, name: str, blob: bytes):
        self.operations.append("UPLOAD")
        self.uploads.append((application_name, name, blob))
        self.files[name] = blob

    async def audio_play(self, *, application_name: str, path: str):
        self.operations.append("PLAY")
        self.plays.append(path)

    async def audio_stop(self):
        self.operations.append("STOP")
        self.stops += 1

    async def storage_remove(self, path: str):
        self.operations.append("REMOVE")
        self.removals.append(path)
        self.files.pop(Path(path).name, None)

    async def storage_list(self, _path: str):
        return SimpleNamespace(list=[
            SimpleNamespace(name=name, size=len(blob), type="file")
            for name, blob in self.files.items()
        ])

    async def aclose(self):
        return None


def _patch_report_text(monkeypatch, text: str = REPORT_TEXT) -> None:
    monkeypatch.setattr(sky_audio_report_plain, "_compose_report", lambda *_args, **_kwargs: text)


def _text_elements(payload):
    return [element for element in payload.elements
            if getattr(element, "text", None) is not None]


def _ids(payload) -> set[str]:
    return {element.id for element in payload.elements}


async def _await_worker(state) -> None:
    task = state.report_prepare_task
    if task is not None:
        await task


@pytest.mark.parametrize("label", [
    sky_limits.REPORT_PREPARING,
    sky_limits.REPORT_READY,
    sky_limits.REPORT_AUDIO_BUSY,
    sky_limits.REPORT_AUDIO_ERROR,
])
def test_every_report_status_is_complete_inside_the_proven_panel_budget(label):
    """These reuse DSN's panel-proven centered <=12-character vocabulary."""
    status = sky_model.ReportStatus(1, 1, label, 99.0)
    elements = sky_device_report_status._report_status_elements(
        status, sky_limits.REPORT_STATUS_TIMEOUT_S)
    text = next(element for element in elements if element.type == "text")
    background = next(element for element in elements
                      if element.type == "rectangle")

    assert text.text == label
    assert text.text.isascii() and len(text.text) <= 12
    assert text_width(text.text) <= 58 < sky_limits.W
    assert (text.font, text.align, text.x, text.y) == (
        "condensed", "center", sky_limits.W // 2, sky_limits.H // 2,
    )
    assert (background.x, background.y, background.width, background.height) == (
        0, 0, sky_limits.W, sky_limits.H,
    )
    assert {element.timeout for element in elements} == {
        sky_limits.REPORT_STATUS_TIMEOUT_S,
    }


async def test_cache_miss_returns_after_preparing_and_never_delayed_autoplays(
        monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    bb = ReportBar()
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_synth(text: str) -> bytes:
        assert text == REPORT_TEXT
        bb.operations.append("SYNTH")
        started.set()
        await release.wait()
        return b"speech"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", slow_synth)
    await asyncio.wait_for(sky_audio_report.weather_report(bb, state), 0.1)
    await asyncio.wait_for(started.wait(), 0.1)

    assert bb.operations == ["DRAW:PREPARING...", "SYNTH"]
    assert bb.uploads == [] and bb.plays == []
    preparing = bb.draws[0]
    assert {element.timeout for element in preparing.elements} == {3}

    release.set()
    await _await_worker(state)

    assert len(bb.uploads) == 1
    assert bb.plays == [], "a completed cold preparation must never autoplay"
    ready = bb.draws[-1]
    assert _text_elements(ready)[-1].text == sky_limits.REPORT_READY
    retired = [element for element in ready.elements
               if element.id in _ids(preparing)]
    assert {element.id for element in retired} == _ids(preparing)
    assert {element.timeout for element in retired} == {1}
    assert state.report_file == bb.uploads[0][1]
    assert state.report_request is None


async def test_next_press_plays_exact_resident_take_immediately(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    bb = ReportBar()
    synth_calls = 0

    async def quick_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"speech"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", quick_synth)
    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)
    cached = state.report_file
    assert cached is not None and bb.plays == []

    await sky_audio_report.weather_report(bb, state)

    assert synth_calls == 1
    assert bb.plays == [cached]
    assert state.report_request is None


async def test_stale_resident_words_prepare_current_text_instead_of_playing(
        monkeypatch):
    _patch_report_text(monkeypatch, "Current weather words.")
    state = sky_model.SkyState(
        report_file="old.snd", report_text="Old weather words.",
        report_files=["old.snd"])
    bb = ReportBar()
    release = asyncio.Event()

    async def gated_synth(_text: str) -> bytes:
        await release.wait()
        return b"new"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", gated_synth)
    await sky_audio_report.weather_report(bb, state)

    assert bb.plays == []
    assert _text_elements(bb.draws[0])[-1].text == sky_limits.REPORT_PREPARING
    worker = state.report_prepare_task
    assert worker is not None
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)


async def test_prepare_failure_retires_preparing_in_error_draw(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    bb = ReportBar()

    async def failed_synth(_text: str) -> bytes:
        raise RuntimeError("voice unavailable")

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", failed_synth)
    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)

    preparing, failure = bb.draws
    old_ids = _ids(preparing)
    retired = [element for element in failure.elements
               if element.id in old_ids]
    replacement = [element for element in failure.elements
                   if element.id not in old_ids]
    assert {element.timeout for element in retired} == {1}
    assert _text_elements(type("P", (), {"elements": replacement})())[0].text == (
        sky_limits.REPORT_AUDIO_ERROR)
    assert bb.uploads == [] and bb.plays == []
    assert state.report_request is None


async def test_worker_cancellation_leaves_only_native_status_lease(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    bb = ReportBar()
    started = asyncio.Event()

    async def parked_synth(_text: str) -> bytes:
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", parked_synth)
    await sky_audio_report.weather_report(bb, state)
    await asyncio.wait_for(started.wait(), 0.1)
    worker = state.report_prepare_task
    assert worker is not None
    worker.cancel()
    await asyncio.gather(worker, return_exceptions=True)

    assert state.report_request is None
    assert len(state.report_statuses) == 1
    remaining = (state.report_statuses[0].expires_at
                 - asyncio.get_running_loop().time())
    assert 0 < remaining <= sky_limits.REPORT_STATUS_TIMEOUT_S
    assert bb.uploads == [] and bb.plays == []


@pytest.mark.parametrize("race", ["navigation", "alert"])
async def test_inflight_preparing_retires_before_newer_intent(monkeypatch, race):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    synth_calls = 0

    async def forbidden_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"unexpected"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", forbidden_synth)

    class BlockFirstDrawBar(ReportBar):
        def __init__(self) -> None:
            super().__init__()
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def display_draw(self, payload):
            if not self.draws:
                self.started.set()
                await self.release.wait()
            await super().display_draw(payload)

    bb = BlockFirstDrawBar()
    report = asyncio.create_task(sky_audio_report.weather_report(bb, state))
    await asyncio.wait_for(bb.started.wait(), 0.1)
    if race == "navigation":
        state.view_generation += 1
    else:
        state.visual_alert = object()
        state.alert_generation += 1
    bb.release.set()
    await report

    assert synth_calls == 0
    preparing, retirement = bb.draws
    assert _ids(retirement) == _ids(preparing)
    assert {element.timeout for element in retirement.elements} == {1}
    assert state.report_statuses == [] and state.report_request is None


async def test_alert_draw_atomically_retires_visible_report_card(monkeypatch):
    state = sky_model.SkyState()
    bb = ReportBar()
    request = sky_audio_report_policy._begin_report_request(state, REPORT_TEXT)
    assert await sky_device_report_status._show_report_status(
        bb, state, request, sky_limits.REPORT_PREPARING)
    status_ids = _ids(bb.draws[0])
    state.visual_alert = object()
    state.alert_generation += 1

    async def resident_alert(*_args, **_kwargs):
        return "alert.anim"

    monkeypatch.setattr(sky_device_assets, "ensure_alert_asset", resident_alert)
    alarm = asyncio.create_task(sky_device_alerts.severe_alarm(bb, state))
    for _ in range(20):
        if len(bb.draws) >= 2:
            break
        await asyncio.sleep(0)
    alarm.cancel()
    await asyncio.gather(alarm, return_exceptions=True)

    alert_draw = bb.draws[1]
    retired = [element for element in alert_draw.elements
               if element.id in status_ids]
    assert {element.id for element in retired} == status_ids
    assert {element.timeout for element in retired} == {1}
    assert alert_draw.elements[-1].id == "alert"
    assert state.report_statuses == []


async def test_repeated_presses_singleflight_through_upload_window(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    synth_calls = 0

    async def quick_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"one take"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", quick_synth)

    class GatedUploadBar(ReportBar):
        def __init__(self) -> None:
            super().__init__()
            self.upload_started = asyncio.Event()
            self.release_upload = asyncio.Event()

        async def assets_upload(self, application_name, name, blob):
            self.uploads.append((application_name, name, blob))
            self.upload_started.set()
            await self.release_upload.wait()

    bb = GatedUploadBar()
    await sky_audio_report.weather_report(bb, state)
    await asyncio.wait_for(bb.upload_started.wait(), 0.1)
    await sky_audio_report.weather_report(bb, state)

    assert synth_calls == 1
    assert len(bb.uploads) == 1
    assert bb.plays == []
    bb.release_upload.set()
    await _await_worker(state)

    assert synth_calls == 1 and len(bb.uploads) == 1
    assert bb.plays == []
    assert _text_elements(bb.draws[-1])[-1].text == sky_limits.REPORT_READY


async def test_preparing_409_starts_no_hidden_worker(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()
    synth_calls = 0

    async def forbidden_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"unexpected"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", forbidden_synth)

    class RefusingBar(ReportBar):
        async def display_draw(self, payload):
            self.draws.append(payload)
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    bb = RefusingBar()
    await sky_audio_report.weather_report(bb, state)

    assert len(bb.draws) == 1 and synth_calls == 0
    assert state.report_prepare_task is None
    assert state.report_statuses == [] and state.report_request is None


async def test_ready_409_keeps_cache_but_never_autoplays(monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState()

    async def quick_synth(_text: str) -> bytes:
        return b"speech"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", quick_synth)

    class RefuseReadyBar(ReportBar):
        async def display_draw(self, payload):
            self.draws.append(payload)
            if len(self.draws) == 2:
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)

    bb = RefuseReadyBar()
    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)

    assert len(bb.uploads) == 1 and bb.plays == []
    assert state.report_file == bb.uploads[0][1]
    assert state.report_request is None


async def test_play_404_removes_each_poisoned_path_and_repairs_in_background(
        monkeypatch):
    _patch_report_text(monkeypatch)
    state = sky_model.SkyState(
        report_file="cached.snd", report_text=REPORT_TEXT,
        report_files=["cached.snd"])
    synth_calls = 0

    async def quick_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"repair"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", quick_synth)

    class UnplayableBar(ReportBar):
        async def audio_play(self, *, application_name: str, path: str):
            self.plays.append(path)
            raise exceptions.BusyBarAPIError("Unplayable", status_code=404)

    bb = UnplayableBar()
    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)
    first_repair = state.report_file
    assert first_repair is not None

    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)

    removed_names = [Path(path).name for path in bb.removals]
    assert bb.plays == ["cached.snd", first_repair]
    assert removed_names == ["cached.snd", first_repair]
    assert synth_calls == len(bb.uploads) == 2
    assert state.report_file == bb.uploads[-1][1]
    assert state.report_file not in removed_names


async def test_report_asset_bound_keeps_current_predecessor_and_play_target():
    state = sky_model.SkyState(
        report_file="a.snd", report_text="a", report_files=["a.snd"],
        audio_owner="report", audio_path="a.snd")
    bb = ReportBar()

    b, _ = await sky_audio_report_assets._ensure_report_take(bb, state, "b", b"b")
    c, _ = await sky_audio_report_assets._ensure_report_take(bb, state, "c", b"c")
    assert state.report_files == ["a.snd", b, c]
    assert len(state.report_files) == 3
    assert bb.removals == []

    previous = state.report_file
    await sky_audio_report_assets._ensure_report_take(bb, state, "d", b"d")
    assert len(state.report_files) == 3
    assert {state.audio_path, previous, state.report_file} == set(
        state.report_files)
    assert "a.snd" not in [Path(path).name for path in bb.removals]

    state.audio_path = state.report_file
    previous = state.report_file
    await sky_audio_report_assets._ensure_report_take(bb, state, "e", b"e")
    assert state.report_files == [previous, state.report_file]
    assert len(state.report_files) == 2


async def test_preparing_deadline_includes_display_lock_and_starts_no_worker(
        monkeypatch):
    _patch_report_text(monkeypatch)
    monkeypatch.setattr(sky_limits, "REPORT_IO_TIMEOUT_S", 0.02)
    state = sky_model.SkyState()
    bb = ReportBar()
    synth_calls = 0

    async def forbidden_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"no"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", forbidden_synth)
    await state.display_lock.acquire()
    try:
        await asyncio.wait_for(sky_audio_report.weather_report(bb, state), 0.1)
    finally:
        state.display_lock.release()

    assert bb.draws == [] and synth_calls == 0
    assert state.report_prepare_task is None
    assert state.report_request is None


async def test_ready_survives_periodic_scene_but_navigation_retires_it(
        monkeypatch):
    _patch_report_text(monkeypatch)
    monkeypatch.setattr(anim, "encode_anim", lambda *_a, **_k: b"anim")
    state = sky_model.SkyState(
        report_file=sky_audio_report_assets.report_asset_name(REPORT_TEXT),
        report_text=REPORT_TEXT,
    )
    bb = ReportBar()
    request = sky_audio_report_policy._begin_report_request(state, REPORT_TEXT)
    assert await sky_device_report_status._show_report_status(
        bb, state, request, sky_limits.REPORT_READY)
    sky_audio_report_policy._finish_report_request(state, request)
    ready_ids = _ids(bb.draws[-1])
    frame = Image.new("RGB", (sky_limits.W, sky_limits.H))

    await sky_device_display.push_scene(
        bb, state, datetime.now(timezone.utc), [frame])
    assert ready_ids.isdisjoint(_ids(bb.draws[-1]))
    assert state.report_statuses

    state.view_generation += 1
    await sky_device_display.push_scene(
        bb, state, datetime.now(timezone.utc), [frame])
    retired = [element for element in bb.draws[-1].elements
               if element.id in ready_ids]
    assert {element.id for element in retired} == ready_ids
    assert {element.timeout for element in retired} == {1}
    assert state.report_statuses == []


@pytest.mark.parametrize("race", ["navigation", "alert", "content"])
async def test_accepted_play_that_turns_stale_is_stopped_in_order(
        monkeypatch, race):
    report_text = {"value": REPORT_TEXT}
    monkeypatch.setattr(sky_audio_report_plain, "_compose_report",
        lambda *_args, **_kwargs: report_text["value"])
    async def parked_synth(_text: str) -> bytes:
        await asyncio.Event().wait()

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", parked_synth)
    cached = sky_audio_report_assets.report_asset_name(REPORT_TEXT)
    state = sky_model.SkyState(
        report_file=cached, report_text=REPORT_TEXT,
        report_files=[cached])

    class DelayedPlayBar(ReportBar):
        def __init__(self):
            super().__init__()
            self.play_started = asyncio.Event()
            self.release_play = asyncio.Event()

        async def audio_play(self, *, application_name: str, path: str):
            self.plays.append(path)
            self.operations.append("PLAY")
            self.play_started.set()
            await self.release_play.wait()

    bb = DelayedPlayBar()
    report = asyncio.create_task(sky_audio_report.weather_report(bb, state))
    await asyncio.wait_for(bb.play_started.wait(), 0.1)
    if race == "navigation":
        state.view_generation += 1
    else:
        if race == "alert":
            state.visual_alert = object()
            state.alert_generation += 1
        else:
            report_text["value"] = "Newer truthful forecast."
    bb.release_play.set()
    await report

    assert bb.operations.index("PLAY") < bb.operations.index("STOP")
    assert bb.stops == 1
    assert state.audio_owner is None and state.audio_path is None
    if race == "content":
        assert _text_elements(bb.draws[-1])[-1].text == (
            sky_limits.REPORT_PREPARING)
        worker = state.report_prepare_task
        assert worker is not None
        worker.cancel()
        await asyncio.gather(worker, return_exceptions=True)
    else:
        assert bb.operations[-2:] == ["PLAY", "STOP"]
        assert state.report_request is None


async def test_ambiguous_upload_is_registered_then_adopted_without_resynth():
    state = sky_model.SkyState()
    bb = ReportBar()
    fname = sky_audio_report_assets.report_asset_name(REPORT_TEXT)

    async def lost_upload(application_name: str, name: str, blob: bytes):
        bb.uploads.append((application_name, name, blob))
        bb.files[name] = blob
        raise TimeoutError("response lost")

    bb.assets_upload = lost_upload
    with pytest.raises(TimeoutError):
        await sky_audio_report_assets._ensure_report_take(bb, state, REPORT_TEXT, b"speech")
    assert state.report_file is None
    assert fname in state.report_retire
    assert state.report_expected_sizes[fname] == len(b"speech")

    adopted = await sky_audio_report_assets._adopt_report_take(bb, state, REPORT_TEXT)
    assert adopted == fname
    assert state.report_file == fname and state.report_text == REPORT_TEXT
    assert fname not in state.report_retire
    assert len(bb.uploads) == 1


async def test_restart_adopts_exact_text_voice_take_and_prunes_old_hashes(
        monkeypatch):
    exact = sky_audio_report_assets.report_asset_name(REPORT_TEXT)
    old = [sky_audio_report_assets.report_asset_name(f"old {i}") for i in range(5)]
    bb = ReportBar()
    bb.files = {name: b"resident" for name in [*old, exact]}
    state = sky_model.SkyState()
    synth_calls = 0

    async def forbidden_synth(_text: str) -> bytes:
        nonlocal synth_calls
        synth_calls += 1
        return b"wrong"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", forbidden_synth)
    adopted = await sky_audio_report._prepare_report_take(bb, state, REPORT_TEXT)

    assert adopted == exact and synth_calls == 0 and bb.uploads == []
    assert state.report_file == exact
    assert len(state.report_files) <= 2
    assert len([name for name in bb.files if sky_audio_report_assets.REPORT_FILE_RE.fullmatch(name)]) <= 2
    assert len(exact.encode("ascii")) <= 31
    assert sky_audio_report_assets.report_asset_name(REPORT_TEXT, voice="voice-a") != (
        sky_audio_report_assets.report_asset_name(REPORT_TEXT, voice="voice-b"))


async def test_play_404_uploads_repair_before_failed_poison_retirement(
        monkeypatch):
    _patch_report_text(monkeypatch)
    poisoned = sky_audio_report_assets.report_asset_name(REPORT_TEXT)
    state = sky_model.SkyState(
        report_file=poisoned, report_text=REPORT_TEXT,
        report_files=[poisoned])

    async def quick_synth(_text: str) -> bytes:
        return b"repair"

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", quick_synth)

    class StickyPoisonBar(ReportBar):
        async def audio_play(self, *, application_name: str, path: str):
            self.plays.append(path)
            raise exceptions.BusyBarAPIError("Unplayable", status_code=404)

        async def storage_remove(self, path: str):
            self.removals.append(path)
            if Path(path).name == poisoned:
                raise exceptions.BusyBarAPIError("open", status_code=508)
            await super().storage_remove(path)

    bb = StickyPoisonBar()
    bb.files[poisoned] = b"bad"
    await sky_audio_report.weather_report(bb, state)
    await _await_worker(state)

    assert len(bb.uploads) == 1
    repair = bb.uploads[0][1]
    assert repair.endswith("_r01.snd") and state.report_file == repair
    assert poisoned in state.report_retire
    assert bb.operations.index("UPLOAD") < len(bb.operations)


async def test_weather_change_during_synth_only_announces_exact_latest_take(
        monkeypatch):
    current = {"text": "Forecast A"}
    monkeypatch.setattr(sky_audio_report_plain, "_compose_report",
        lambda *_args, **_kwargs: current["text"])
    state = sky_model.SkyState()
    bb = ReportBar()
    first_started = asyncio.Event()
    release_first = asyncio.Event()
    calls: list[str] = []

    async def changing_synth(text: str) -> bytes:
        calls.append(text)
        if len(calls) == 1:
            first_started.set()
            await release_first.wait()
        return text.encode()

    monkeypatch.setattr(sky_audio_report, "synth_snd_async", changing_synth)
    await sky_audio_report.weather_report(bb, state)
    await asyncio.wait_for(first_started.wait(), 0.1)
    current["text"] = "Forecast B"
    release_first.set()
    await _await_worker(state)

    ready_labels = [element.text for draw in bb.draws for element in draw.elements
                    if getattr(element, "text", None) == sky_limits.REPORT_READY]
    assert calls == ["Forecast A", "Forecast B"]
    assert ready_labels == [sky_limits.REPORT_READY]
    assert state.report_text == "Forecast B"


@pytest.mark.parametrize(
    ("play_error", "expected"),
    [
        (exceptions.BusyBarAPIError("busy", status_code=409),
         sky_limits.REPORT_AUDIO_BUSY),
        (OSError("lost PLAY response"), sky_limits.REPORT_AUDIO_ERROR),
    ],
)
async def test_cached_play_failure_gets_truthful_terminal_feedback(
        monkeypatch, play_error, expected):
    _patch_report_text(monkeypatch)
    cached = sky_audio_report_assets.report_asset_name(REPORT_TEXT)
    state = sky_model.SkyState(
        report_file=cached, report_text=REPORT_TEXT,
        report_files=[cached])

    class BrokenPlayBar(ReportBar):
        async def audio_play(self, *, application_name: str, path: str):
            self.operations.append("PLAY")
            raise play_error

    bb = BrokenPlayBar()
    await sky_audio_report.weather_report(bb, state)

    assert _text_elements(bb.draws[-1])[-1].text == expected
    if expected == sky_limits.REPORT_AUDIO_ERROR:
        assert bb.stops == 1
    assert state.report_request is None


async def test_unresolved_pending_stop_blocks_cached_report_with_busy(
        monkeypatch):
    _patch_report_text(monkeypatch)
    cached = sky_audio_report_assets.report_asset_name(REPORT_TEXT)
    state = sky_model.SkyState(
        report_file=cached, report_text=REPORT_TEXT,
        report_files=[cached], audio_owner="report-pending",
        audio_path="older.snd", audio_generation=4,
        audio_stop_pending=True)

    class StuckStopBar(ReportBar):
        async def audio_stop(self):
            self.stops += 1
            raise OSError("still uncertain")

    bb = StuckStopBar()
    await sky_audio_report.weather_report(bb, state)

    assert bb.plays == [] and state.audio_stop_pending is True
    assert _text_elements(bb.draws[-1])[-1].text == sky_limits.REPORT_AUDIO_BUSY


async def test_report_cli_path_awaits_prepare_then_plays(monkeypatch):
    bb = ReportBar()

    async def fake_connect():
        return bb

    async def fake_poll(state):
        state.forecast = [{}]
        state.hourly = [(datetime.now(timezone.utc), {})]
        await asyncio.Event().wait()

    monkeypatch.setattr(sky_cli, "aconnect", fake_connect)
    monkeypatch.setattr(sky_providers_weather, "poll_nws", fake_poll)
    _patch_report_text(monkeypatch)
    monkeypatch.setattr(sky_audio_report, "synth_snd_async", lambda _text: asyncio.sleep(0, result=b"cli"))

    await sky_cli.report_once()

    assert len(bb.uploads) == 1
    assert bb.plays == [bb.uploads[0][1]]


def test_report_cli_does_not_wait_for_impossible_nws_forecast_outside_coverage():
    state = sky_model.SkyState()
    state.hourly = [(datetime.now(timezone.utc), {})]

    assert sky_cli._report_inputs_ready(state) is False
    state.nws_point_covered = False
    assert sky_cli._report_inputs_ready(state) is True

    state.nws_point_covered = True
    assert sky_cli._report_inputs_ready(state) is False
    state.forecast = [{}]
    assert sky_cli._report_inputs_ready(state) is True

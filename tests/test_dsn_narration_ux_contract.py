"""End-user contracts for DSN narration preparation and playback feedback.

The disposable Kokoro worker exposes no trustworthy percentage.  These tests
therefore pin the four truthful preparation states and the one device-busy
state without loading a voice, contacting NASA, or touching a BUSY Bar.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import input as dsn_input
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import reconcile as dsn_reconcile
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import settings as dsn_settings
from apps.dsn_app import source as dsn_source
from apps.dsn_app.audio import assets as dsn_audio_assets
from apps.dsn_app.audio import narration as dsn_audio_narration
from apps.dsn_app.audio import output as dsn_audio_output
from apps.dsn_app.audio import policy as dsn_audio_policy
from apps.dsn_app.audio import words as dsn_audio_words
from apps.dsn_app.audio import worker as dsn_audio_worker
from apps.dsn_app.device import display as dsn_device_display


def link(craft: str = "VGR2", dish: str = "DSS43", **changes) -> dsn_source.Link:
    base = dsn_source.Link(
        complex_name="Canberra",
        dish=dish,
        craft=craft,
        elevation=30.0,
        band="X",
        down_bps=20_000.0,
        up_active=True,
        range_km=2.1e10,
        naif=-32,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        azimuth=120.0,
        down_streams=(dsn_source.DownStream("X", 20_000.0, -140.0),),
        up_band="X",
    )
    return replace(base, **changes)


def fresh_state(*links: dsn_source.Link, view: str = "network") -> dsn_model.State:
    now = time.time()
    state = dsn_model.State(links=list(links), view=view)
    state.feed_seeded = True
    state.feed_timestamp_ms = int(now * 1000)
    state.feed_advanced_at = now
    state.freshness = "fresh"
    state.names = {"vgr2": "Voyager 2", "jno": "Juno"}
    state.dish_types = {"DSS43": "70M", "DSS25": "34M"}
    return state


class RecordingBar:
    def __init__(self) -> None:
        self.draws = []
        self.played: list[str] = []
        self.uploads: list[tuple[str, str, bytes]] = []
        self.removed: list[str] = []

    async def display_draw(self, payload):
        self.draws.append(payload)

    async def audio_play(self, *, application_name: str, path: str):
        self.played.append(path)

    async def audio_stop(self):
        return None

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def storage_remove(self, path: str):
        self.removed.append(path)


def readout_labels(bb: RecordingBar) -> list[str]:
    return [element.text for payload in bb.draws for element in payload.elements
            if getattr(element, "text", None) is not None]


def test_user_vocabulary_is_plain_and_never_claims_fake_progress():
    labels = {
        dsn_limits.NARRATION_STARTING,
        dsn_limits.NARRATION_PREPARING,
        dsn_limits.NARRATION_READY,
        dsn_limits.NARRATION_BUSY,
        dsn_limits.NARRATION_ERROR,
    }

    assert labels == {
        "STARTING UP", "PREPARING...", "PRESS START",
        "AUDIO BUSY", "AUDIO ERROR",
    }
    assert all("%" not in label for label in labels)
    assert not labels & {"BAKING", "SYNTH", "UPLOADING", "VOICE WARM"}


def test_terminal_notice_refusals_back_off_without_hammering_the_bar():
    assert [dsn_audio_policy.narration_notice_backoff_s(count) for count in range(7)] == [
        2.0, 2.0, 4.0, 8.0, 16.0, 30.0, 30.0,
    ]


@pytest.mark.parametrize(
    ("cache_ready", "label"),
    [(False, "STARTING UP"), (True, "PREPARING...")],
)
def test_immediate_miss_uses_truthful_startup_or_preparing_state(
        monkeypatch, cache_ready, label):
    selected = link()
    state = fresh_state(selected, view="network")
    state.speech_cache_ready = cache_ready
    bb = RecordingBar()

    async def forbidden(*args, **kwargs):
        raise AssertionError("START attempted synthesis or upload")

    monkeypatch.setattr(dsn_audio_assets, "ensure_speech", forbidden)
    monkeypatch.setattr(dsn_audio_worker, "synth_off_loop", forbidden)

    asyncio.run(asyncio.wait_for(dsn_audio_narration.speak(bb, state, selected), 0.1))

    assert readout_labels(bb) == [label]
    assert bb.played == []
    assert bb.uploads == []
    assert state.narration_request is not None
    assert state.narration_request.key == selected.key
    assert state.narration_request.view == "network"
    # A cold START is an acknowledgement, not a hidden navigation or a hold
    # that makes the global Network board appear stuck for a 40-second bake.
    assert state.view == "network"
    assert state.focus is None
    assert state.narration_focus is None
    assert state.manual_until == 0.0
    assert state.speaking is False


def test_exact_requested_completion_queues_press_start_without_autoplay():
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)
    state.speech[name] = 12.0
    bb = RecordingBar()

    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True
    notice = state.narration_notice
    assert notice == dsn_model.NarrationNotice(
        request.generation, selected.key, name, "network", "PRESS START")
    assert bb.played == []

    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]
    assert bb.played == [], "a completed cold bake must never auto-play"
    assert state.narration_notice is None


def test_startup_cache_adoption_finishes_the_exact_request():
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)

    class CacheBar(RecordingBar):
        async def storage_list(self, path: str):
            entry = type("Entry", (), {
                "type": "file", "name": name, "size": 882_000,
            })()
            return type("Listing", (), {"list": [entry]})()

    asyncio.run(dsn_audio_assets.load_speech_cache(CacheBar(), state))

    assert state.speech_cache_ready is True
    assert name in state.speech
    assert state.narration_request is None
    assert state.narration_notice == dsn_model.NarrationNotice(
        request.generation, selected.key, name, "network", "PRESS START")


def test_moved_or_newer_request_suppresses_old_completion():
    first = link()
    second = link("JNO", "DSS25", naif=-61)
    state = fresh_state(first, second)
    first_name = dsn_audio_assets.speech_name(
        dsn_audio_words.spoken(first, state.names, state.dish_types))
    second_name = dsn_audio_assets.speech_name(
        dsn_audio_words.spoken(second, state.names, state.dish_types))

    moved = dsn_audio_policy.request_narration(state, first, first_name)
    dsn_audio_policy.clear_narration_request(state)             # wheel moved before completion
    state.cursor = 1
    assert dsn_audio_policy.finish_narration_request(
        state, moved, dsn_limits.NARRATION_READY) is False
    assert state.narration_notice is None

    state.cursor = 0
    old = dsn_audio_policy.request_narration(state, first, first_name)
    state.cursor = 1
    current = dsn_audio_policy.request_narration(state, second, second_name)
    assert current.generation > old.generation
    assert dsn_audio_policy.finish_narration_request(
        state, old, dsn_limits.NARRATION_READY) is False
    assert state.narration_request == current
    assert state.narration_notice is None


def test_ready_notice_survives_409_and_stale_notice_is_discarded():
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)
    state.speech[name] = 12.0
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True

    class RefuseOnceBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def display_draw(self, payload):
            self.attempts += 1
            self.draws.append(payload)
            if self.attempts == 1:
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)

    bb = RefuseOnceBar()
    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is False
    assert state.narration_notice is not None
    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert state.narration_notice is None
    assert bb.attempts == 2

    newer = dsn_audio_policy.request_narration(state, selected, name)
    assert dsn_audio_policy.finish_narration_request(
        state, newer, dsn_limits.NARRATION_READY) is True
    old = time.time() - dsn_settings.FEED_STALE_S - 1
    state.feed_advanced_at = old
    state.feed_timestamp_ms = int(old * 1000)
    quiet = RecordingBar()
    assert asyncio.run(dsn_audio_narration.show_narration_notice(quiet, state)) is False
    assert state.narration_notice is None
    assert quiet.draws == []


def test_ready_notice_is_delivered_during_the_same_links_distance_watch():
    selected = link()
    state = fresh_state(selected, view="instrument")
    assert dsn_input.toggle_realtime(state, now=time.time()) is True
    assert state.view == "distance"
    watched = state.current()
    assert watched is not None and watched.key == selected.key
    text = dsn_audio_words.spoken(watched, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, watched, name)
    state.speech[name] = 12.0
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True
    bb = RecordingBar()

    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]
    assert state.narration_notice is None


def test_ready_notice_follows_a_distance_watch_to_its_live_handoff_dish():
    old = link(dish="DSS43")
    state = fresh_state(old, view="instrument")
    assert dsn_input.toggle_realtime(state, now=time.time()) is True
    new = replace(old, dish="DSS25")
    dsn_reconcile.reconcile_links(state, [new], time.time())
    target = dsn_selection.narration_target_link(state)
    assert target is not None and target.key == new.key
    text = dsn_audio_words.spoken(target, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, target, name)
    state.speech[name] = 12.0
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True
    bb = RecordingBar()

    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]
    assert state.narration_notice is None


def test_requested_synth_none_queues_audio_error(monkeypatch):
    selected = link()
    state = fresh_state(selected)
    state.speech_cache_ready = True
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    state.narration_texts[selected.key] = text
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)
    attempted = asyncio.Event()
    backed_off = asyncio.Event()
    park = asyncio.Event()

    async def failed_synth(*args, **kwargs):
        attempted.set()
        return None

    async def controlled_sleep(delay):
        if delay == 2:
            return
        if delay == 30:
            backed_off.set()
            await park.wait()
            return
        raise AssertionError(f"unexpected sleep: {delay}")

    monkeypatch.setattr(dsn_audio_assets, "ensure_speech", failed_synth)
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)

    async def scenario():
        task = asyncio.create_task(dsn_audio_narration.prebake(RecordingBar(), state))
        await asyncio.wait_for(attempted.wait(), 0.1)
        await asyncio.wait_for(backed_off.wait(), 0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert state.narration_request is None
    assert state.narration_notice == dsn_model.NarrationNotice(
        request.generation, selected.key, name, "network", "AUDIO ERROR")
    assert name not in state.speech


def test_requested_upload_failure_queues_audio_error(monkeypatch):
    selected = link()
    state = fresh_state(selected)
    state.speech_cache_ready = True
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    state.narration_texts[selected.key] = text
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)
    attempted = asyncio.Event()
    backed_off = asyncio.Event()
    park = asyncio.Event()

    async def failed_upload(*args, **kwargs):
        attempted.set()
        raise OSError("definite upload failure")

    async def controlled_sleep(delay):
        if delay == 2:
            return
        if delay == 30:
            backed_off.set()
            await park.wait()
            return
        raise AssertionError(f"unexpected sleep: {delay}")

    monkeypatch.setattr(dsn_audio_assets, "ensure_speech", failed_upload)
    monkeypatch.setattr(asyncio, "sleep", controlled_sleep)

    async def scenario():
        task = asyncio.create_task(dsn_audio_narration.prebake(RecordingBar(), state))
        await asyncio.wait_for(attempted.wait(), 0.1)
        await asyncio.wait_for(backed_off.wait(), 0.1)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())

    assert state.narration_request is None
    assert state.narration_notice == dsn_model.NarrationNotice(
        request.generation, selected.key, name, "network", "AUDIO ERROR")


def test_playback_409_shows_audio_busy_once_and_does_not_retry(monkeypatch):
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0

    class BusyAudioBar(RecordingBar):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def audio_play(self, *, application_name: str, path: str):
            self.attempts += 1
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    async def forbidden_sleep(delay):
        raise AssertionError(f"playback 409 retried after sleeping {delay}s")

    monkeypatch.setattr(asyncio, "sleep", forbidden_sleep)
    bb = BusyAudioBar()

    asyncio.run(dsn_audio_narration.speak(bb, state, selected))

    assert bb.attempts == 1
    assert readout_labels(bb) == ["AUDIO BUSY"]
    assert state.speech[name] == 12.0, "a busy device is not a missing asset"
    assert state.narration_focus is None
    assert state.speaking is False


def test_requested_completion_is_delivered_during_the_same_realtime_lock():
    selected = link()
    state = fresh_state(selected, view="distance")
    state.focus = selected.key
    state.realtime_since = time.time()
    state.view_before_lock = "network"
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    request = dsn_audio_policy.request_narration(state, selected, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True
    bb = RecordingBar()

    # The lock can last for hours, and releasing it changes view and invalidates
    # this exact notice.  Completion must therefore be allowed to acknowledge
    # the explicit START while the same link and view are still locked.
    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]
    assert state.narration_notice is None


def test_handoff_watch_completion_validates_against_the_live_narration_target():
    frozen = link()
    live = link(dish="DSS14")
    state = fresh_state(live, view="distance")
    state.watch = dsn_model.Watch(
        link=frozen,
        light_s=frozen.light_s or 10.0,
        started_at=time.time(),
        deadline=time.time() + 10.0,
        generation=1,
        return_view="network",
        live_key=live.key,
        on_air=True,
    )
    state.focus = live.key
    state.realtime_since = time.time()
    text = dsn_audio_words.spoken(live, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    request = dsn_audio_policy.request_narration(state, live, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True
    assert state.current().key == frozen.key
    assert dsn_selection.narration_target_link(state).key == live.key

    bb = RecordingBar()
    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]


def test_late_success_upgrades_a_queued_error_before_it_is_displayed():
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    request = dsn_audio_policy.request_narration(state, selected, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_ERROR) is True

    # A focus session or another interaction can defer the error notice beyond
    # the worker's retry.  Once that exact asset exists, AUDIO ERROR is no
    # longer truthful; the still-current selection is ready to play.
    state.speech[name] = 12.0
    bb = RecordingBar()
    assert asyncio.run(dsn_audio_narration.show_narration_notice(bb, state)) is True
    assert readout_labels(bb) == ["PRESS START"]
    assert state.narration_notice is None


def test_start_selection_gets_enough_dwell_for_background_completion(monkeypatch):
    selected = link()
    state = fresh_state(selected, view="instrument")
    started = asyncio.Event()
    original_speak = dsn_audio_narration.speak

    async def observed_speak(bb, current, requested):
        assert requested is selected
        await original_speak(bb, current, requested)
        started.set()

    monkeypatch.setattr(dsn_audio_narration, "speak", observed_speak)

    class StartBar(RecordingBar):
        async def stream_status_ws(self):
            yield {"updates": [{"input": {"button_event": {
                "button": "START", "action": "PRESS"}}}]}
            await asyncio.Event().wait()

    async def scenario():
        listener = asyncio.create_task(dsn_input.listen_input(StartBar(), state))
        await asyncio.wait_for(started.wait(), 0.1)
        assert state.manual_until >= (
            asyncio.get_running_loop().time() + dsn_limits.MANUAL_DWELL_S - 0.1)
        listener.cancel()
        await asyncio.gather(listener, return_exceptions=True)

    asyncio.run(scenario())


def test_newer_picker_commits_after_an_inflight_terminal_notice():
    first = link()
    second = link("JNO", "DSS25", naif=-61)
    state = fresh_state(first, second)
    text = dsn_audio_words.spoken(first, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    request = dsn_audio_policy.request_narration(state, first, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True

    class OrderedBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.notice_started = asyncio.Event()
            self.release_notice = asyncio.Event()
            self.commits: list[str] = []

        async def display_draw(self, payload):
            label = next(element.text for element in payload.elements
                         if getattr(element, "text", None) is not None)
            if label == dsn_limits.NARRATION_READY:
                self.notice_started.set()
                await self.release_notice.wait()
            self.commits.append(label)

    async def scenario():
        bb = OrderedBar()
        notice = asyncio.create_task(dsn_audio_narration.show_narration_notice(bb, state))
        await asyncio.wait_for(bb.notice_started.wait(), 0.1)

        # Model a wheel move while the device is still answering the notice
        # POST.  Clearing the generation prevents future stale notices; the
        # draw lock must also guarantee the newer picker is physically last.
        dsn_audio_policy.clear_narration_request(state)
        state.cursor = 1
        picker = asyncio.create_task(dsn_device_display.draw_picker(bb, state))
        await asyncio.sleep(0)
        assert not picker.done()

        bb.release_notice.set()
        assert await notice is True
        await picker
        assert bb.commits == ["PRESS START", "JNO 2/2"]

    asyncio.run(scenario())


def test_invalidated_notice_waiting_for_display_lock_never_commits():
    selected = link()
    state = fresh_state(selected)
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    request = dsn_audio_policy.request_narration(state, selected, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True

    async def scenario():
        bb = RecordingBar()
        await state.interactive_draw.acquire()
        notice = asyncio.create_task(dsn_audio_narration.show_narration_notice(bb, state))
        await asyncio.sleep(0)  # notice validated and is waiting for the lock

        # Model a newer input that wins while the notice is queued rather than
        # already inside its POST. Validation and draw must be atomic: the old
        # prompt cannot acquire the lock afterward and obscure newer feedback.
        dsn_audio_policy.clear_narration_request(state)
        state.interactive_draw.release()

        assert await notice is False
        assert readout_labels(bb) == []

    asyncio.run(scenario())


def test_cache_evicted_while_waiting_for_display_lock_becomes_preparing():
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0

    async def scenario():
        bb = RecordingBar()
        await state.interactive_draw.acquire()
        playback = asyncio.create_task(dsn_audio_narration.speak(bb, state, selected))
        await asyncio.sleep(0)  # initial cache hit is now queued on the lock
        state.speech.pop(name)
        state.interactive_draw.release()

        await playback
        assert readout_labels(bb) == ["PREPARING..."]
        assert state.narration_request is not None
        assert state.narration_request.key == selected.key

    asyncio.run(scenario())


def test_explicit_preparation_pins_detail_rotation_until_feedback(monkeypatch):
    first = link()
    second = link("JNO", "DSS25", naif=-61)
    state = fresh_state(first, second, view="instrument")
    request = dsn_audio_policy.request_narration(state, first, "cold.snd")
    state.manual_until = 0.0
    real_sleep = asyncio.sleep
    blocked = asyncio.Event()
    calls = 0

    async def one_rotation_then_block(delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            await blocked.wait()

    monkeypatch.setattr(asyncio, "sleep", one_rotation_then_block)

    async def scenario():
        rotating = asyncio.create_task(dsn_selection.rotate(state))
        await real_sleep(0)
        assert state.cursor == 0

        # The terminal acknowledgement is also exact to this target and must
        # not be invalidated by rotation before the user can read it.
        assert dsn_audio_policy.finish_narration_request(
            state, request, dsn_limits.NARRATION_ERROR) is True
        rotating.cancel()
        await asyncio.gather(rotating, return_exceptions=True)

    asyncio.run(scenario())


def test_cached_start_is_not_followed_by_a_stale_terminal_prompt():
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 0.0
    request = dsn_audio_policy.request_narration(state, selected, name)
    assert dsn_audio_policy.finish_narration_request(
        state, request, dsn_limits.NARRATION_READY) is True

    class OrderedBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.notice_started = asyncio.Event()
            self.release_notice = asyncio.Event()
            self.order: list[str] = []

        async def display_draw(self, payload):
            label = next(element.text for element in payload.elements
                         if getattr(element, "text", None) is not None)
            if label == dsn_limits.NARRATION_READY:
                self.notice_started.set()
                await self.release_notice.wait()
            self.order.append(label)

        async def audio_play(self, *, application_name: str, path: str):
            self.order.append("PLAY")

    async def scenario():
        bb = OrderedBar()
        notice = asyncio.create_task(dsn_audio_narration.show_narration_notice(bb, state))
        await asyncio.wait_for(bb.notice_started.wait(), 0.1)
        playback = asyncio.create_task(dsn_audio_narration.speak(bb, state, selected))
        await asyncio.sleep(0)

        # Instrument/Distance have no post-PLAY craft readout.  If PLAY can
        # pass an older display POST, PRESS START lands afterward and remains
        # over audio that has already begun.
        assert "PLAY" not in bb.order
        bb.release_notice.set()
        await asyncio.gather(notice, playback)
        assert bb.order.index("PRESS START") < bb.order.index("PLAY")

    asyncio.run(scenario())


def test_ambiguous_play_stall_is_bounded_then_stopped(monkeypatch):
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    monkeypatch.setattr(dsn_limits, "INTERACTIVE_IO_TIMEOUT_S", 0.01)

    class StalledPlayBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.order: list[str] = []

        async def audio_play(self, *, application_name: str, path: str):
            try:
                await asyncio.Event().wait()
            finally:
                # Model transport cancellation finishing before a potentially
                # accepted PLAY can be neutralised by STOP.
                await asyncio.sleep(0)
                self.order.append("play settled")

        async def audio_stop(self):
            self.order.append("stop")

    async def scenario():
        bb = StalledPlayBar()
        await asyncio.wait_for(dsn_audio_narration.speak(bb, state, selected), 0.2)
        assert bb.order == ["play settled", "stop"]
        assert readout_labels(bb) == ["AUDIO ERROR"]
        assert state.speaking is False
        assert state.narration_focus is None

    asyncio.run(scenario())


def test_failed_ambiguous_stop_remains_pending_for_a_later_retry(monkeypatch):
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    monkeypatch.setattr(dsn_limits, "INTERACTIVE_IO_TIMEOUT_S", 0.01)

    class RecoveringStopBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.stop_ready = False

        async def audio_play(self, *, application_name: str, path: str):
            await asyncio.Event().wait()

        async def audio_stop(self):
            if not self.stop_ready:
                await asyncio.Event().wait()

    async def scenario():
        bb = RecoveringStopBar()
        await asyncio.wait_for(dsn_audio_narration.speak(bb, state, selected), 0.2)
        assert state.audio_stop_pending is True
        assert state.audio_stop_retry_at > 0

        bb.stop_ready = True
        await dsn_audio_output.stop_audio_bounded(bb, state, "test retry")
        assert state.audio_stop_pending is False
        assert state.audio_stop_retry_at == 0.0

    asyncio.run(scenario())


def test_pending_ambiguous_audio_blocks_a_new_play_until_stop_succeeds(
        monkeypatch):
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0
    state.audio_stop_pending = True
    monkeypatch.setattr(dsn_limits, "INTERACTIVE_IO_TIMEOUT_S", 0.01)

    class UnresolvedAudioBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.plays = 0

        async def audio_stop(self):
            await asyncio.Event().wait()

        async def audio_play(self, *, application_name: str, path: str):
            self.plays += 1

    bb = UnresolvedAudioBar()
    asyncio.run(dsn_audio_narration.speak(bb, state, selected))

    assert bb.plays == 0
    assert state.audio_stop_pending is True
    assert readout_labels(bb) == ["AUDIO BUSY"]


def test_audio_stop_410_means_the_bar_is_already_safely_stopped():
    state = fresh_state(link(), view="instrument")
    state.audio_stop_pending = True
    state.audio_stop_retry_at = 123.0

    class AlreadyStoppedBar(RecordingBar):
        async def audio_stop(self):
            raise exceptions.BusyBarAPIError(
                "No audio is playing", status_code=410)

    asyncio.run(dsn_audio_output.stop_audio_bounded(
        AlreadyStoppedBar(), state, "natural clip end"))

    assert state.audio_stop_pending is False
    assert state.audio_stop_retry_at == 0.0


def test_late_404_does_not_resurrect_intent_after_view_navigation():
    selected = link()
    state = fresh_state(selected, view="instrument")
    text = dsn_audio_words.spoken(selected, state.names, state.dish_types)
    name = dsn_audio_assets.speech_name(text)
    state.speech[name] = 12.0

    class LateMissingBar(RecordingBar):
        def __init__(self):
            super().__init__()
            self.play_started = asyncio.Event()
            self.release_play = asyncio.Event()

        async def audio_play(self, *, application_name: str, path: str):
            self.play_started.set()
            await self.release_play.wait()
            raise exceptions.BusyBarAPIError("asset not found", status_code=404)

    async def scenario():
        bb = LateMissingBar()
        playback = asyncio.create_task(dsn_audio_narration.speak(bb, state, selected))
        await asyncio.wait_for(bb.play_started.wait(), 0.1)

        # OK navigation explicitly invalidates narration UI intent while the
        # request is in flight.  A late device result may still repair cache
        # state, but must not mint a new request in the destination view.
        assert dsn_input.toggle_view(state) == "distance"
        bb.release_play.set()
        await playback

        assert name not in state.speech
        assert state.narration_request is None
        assert state.narration_notice is None
        assert "PREPARING..." not in readout_labels(bb)

    asyncio.run(scenario())

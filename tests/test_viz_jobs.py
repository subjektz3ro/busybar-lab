import os
import subprocess
import threading

import pytest

from busybar_viz.jobs import (
    JobError,
    JobManager,
    JobQueueFull,
    JobState,
    WorkerResult,
    worker_environment,
)
from busybar_viz.models import RenderRequest
from busybar_viz.worker import parse_request

SCENARIO = "conformance/dual-display-input-replay"


def test_job_manager_bounds_pending_work_and_reports_completion(tmp_path):
    release = threading.Event()
    finished = threading.Event()

    def runner(_request, _timeout):
        assert release.wait(2)
        return WorkerResult("a" * 64, True)

    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=2,
        worker_runner=runner,
    )
    try:
        request = RenderRequest.from_values(SCENARIO)
        first = manager.submit("session", request, on_finish=lambda _job: finished.set())
        with pytest.raises(JobQueueFull, match="queue is full"):
            manager.reserve("session", request)
        release.set()
        assert finished.wait(2)
        result = manager.get(first.id)
        assert result.state is JobState.SUCCEEDED
        assert result.artifact_id == "a" * 64
        assert result.passed is True
    finally:
        release.set()
        manager.shutdown()


def test_job_manager_classifies_worker_timeout(tmp_path):
    finished = threading.Event()

    def runner(_request, timeout):
        raise subprocess.TimeoutExpired(["fixed-worker"], timeout)

    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=1,
        worker_runner=runner,
    )
    try:
        job = manager.submit(
            "session",
            RenderRequest.from_values(SCENARIO),
            on_finish=lambda _job: finished.set(),
        )
        assert finished.wait(2)
        result = manager.get(job.id)
        assert result.state is JobState.TIMED_OUT
        assert "timeout" in result.error
    finally:
        manager.shutdown()


def test_terminal_state_is_hidden_until_durable_callback_finishes(tmp_path):
    callback_entered = threading.Event()
    release_callback = threading.Event()
    finished = threading.Event()

    def callback(_record):
        callback_entered.set()
        assert release_callback.wait(2)
        finished.set()

    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult("a" * 64, True),
    )
    try:
        job = manager.submit(
            "session", RenderRequest.from_values(SCENARIO), on_finish=callback,
        )
        assert callback_entered.wait(2)
        assert manager.get(job.id).state is JobState.FINALIZING
        release_callback.set()
        assert finished.wait(2)
        for _ in range(100):
            if manager.get(job.id).state is JobState.SUCCEEDED:
                break
            threading.Event().wait(0.01)
        assert manager.get(job.id).state is JobState.SUCCEEDED
    finally:
        release_callback.set()
        manager.shutdown()


def test_callback_failure_stays_finalizing_until_journal_commit_recovers(tmp_path):
    attempted = threading.Event()
    allow_commit = threading.Event()
    committed = threading.Event()

    def callback(_record):
        attempted.set()
        if not allow_commit.is_set():
            raise RuntimeError("journal unavailable")
        committed.set()

    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult("a" * 64, True),
    )
    try:
        job = manager.submit(
            "session", RenderRequest.from_values(SCENARIO), on_finish=callback,
        )
        assert attempted.wait(2)
        for _ in range(100):
            result = manager.get(job.id)
            if result.notification_error:
                break
            threading.Event().wait(0.01)
        assert result.state is JobState.FINALIZING
        assert result.notification_error == "journal unavailable"
        assert "journal update pending" in result.error

        allow_commit.set()
        assert committed.wait(2)
        for _ in range(100):
            result = manager.get(job.id)
            if result.state is JobState.SUCCEEDED:
                break
            threading.Event().wait(0.01)
        assert result.state is JobState.SUCCEEDED
        assert result.notification_error is None
    finally:
        allow_commit.set()
        manager.shutdown()


def test_shutdown_cancels_a_started_future_before_renderer_work_begins(tmp_path):
    entered_future = threading.Event()
    release_future = threading.Event()
    shutdown_finished = threading.Event()
    runner_called = threading.Event()
    callback_records = []

    def runner(_request, _timeout):
        runner_called.set()
        return WorkerResult("a" * 64, True)

    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=2,
        worker_runner=runner,
    )
    original_execute = manager._execute

    def gated_execute(job_id):
        entered_future.set()
        assert release_future.wait(2)
        original_execute(job_id)

    manager._execute = gated_execute
    job = manager.submit(
        "session",
        RenderRequest.from_values(SCENARIO),
        on_finish=callback_records.append,
    )
    assert entered_future.wait(2)

    def shut_down():
        manager.shutdown()
        shutdown_finished.set()

    thread = threading.Thread(target=shut_down)
    thread.start()
    try:
        for _ in range(100):
            if manager._closed:
                break
            threading.Event().wait(0.01)
        assert manager._closed is True
        release_future.set()
        assert shutdown_finished.wait(2)
        assert runner_called.is_set() is False
        assert manager.get(job.id).state is JobState.CANCELLED
        assert [record.state for record in callback_records] == [JobState.CANCELLED]
    finally:
        release_future.set()
        thread.join(timeout=2)
        manager.shutdown()


def test_worker_environment_scrubs_device_and_app_credentials():
    result = worker_environment(
        {
            "PATH": "/bin",
            "BUSYBAR_HOST": "device.local",
            "BUSYBAR_TOKEN": "device-secret",
            "BUSYBAR_DEPLOY_PASSWORD": "deploy-secret",
            "BARKEEP_PRIVATE": "barkeep-secret",
            "WEATHER_API_KEY": "api-secret",
            "SKYSTRIP_CONTACT": "person@example.invalid",
            "SKYSTRIP_LIGHTNING_WS": "wss://secret.example/path?token=hidden",
            "SKYSTRIP_TZ": "personal/timezone",
            "DSN_VOICE": "personal-voice",
            "SAFE_PLATFORM_VALUE": "kept",
        },
        app_keys={"SKYSTRIP_TZ", "DSN_VOICE"},
    )
    assert result["PATH"] == "/bin"
    assert result["SAFE_PLATFORM_VALUE"] == "kept"
    assert result["BUSYBAR_VIZ"] == result["BUSYBAR_VIZ_OFFLINE"] == "1"
    assert not {
        "BUSYBAR_HOST",
        "BUSYBAR_TOKEN",
        "BUSYBAR_DEPLOY_PASSWORD",
        "BARKEEP_PRIVATE",
        "WEATHER_API_KEY",
        "SKYSTRIP_CONTACT",
        "SKYSTRIP_LIGHTNING_WS",
        "SKYSTRIP_TZ",
        "DSN_VOICE",
    } & result.keys()


def test_stale_publication_work_is_swept_only_when_idle(tmp_path):
    stale = tmp_path / "data" / "work" / "publish-abandoned"
    stale.mkdir(parents=True)
    (stale / "partial.rgb").write_bytes(b"partial")
    os.utime(stale, (0, 0))
    manager = JobManager(
        tmp_path,
        tmp_path / "data",
        max_workers=1,
        max_pending=1,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult("a" * 64, True),
    )
    try:
        assert not stale.exists()
    finally:
        manager.shutdown()


def test_work_symlink_is_rejected_without_deleting_external_staging(tmp_path):
    external = tmp_path / "external"
    stale = external / "publish-must-survive"
    stale.mkdir(parents=True)
    marker = stale / "marker.txt"
    marker.write_text("outside")
    data_root = tmp_path / "data"
    data_root.mkdir()
    (data_root / "work").symlink_to(external, target_is_directory=True)

    with pytest.raises(JobError, match="work path may not be a symlink"):
        JobManager(
            tmp_path,
            data_root,
            max_workers=1,
            max_pending=1,
            timeout_s=2,
            worker_runner=lambda _request, _timeout: WorkerResult("a" * 64, True),
        )

    assert marker.read_text() == "outside"


@pytest.mark.parametrize("field", ["module", "path", "command", "environment"])
def test_worker_request_cannot_select_executable_surfaces(field):
    raw = (
        '{"scenario_id":"conformance/dual-display-input-replay",'
        f'"{field}":"anything"}}'
    ).encode()
    with pytest.raises(ValueError, match="unknown render request fields"):
        parse_request(raw)


def test_worker_rejects_unregistered_scenario():
    with pytest.raises(KeyError, match="unknown scenario"):
        parse_request(b'{"scenario_id":"python/arbitrary"}')

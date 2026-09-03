"""Bounded render-job execution through the registered worker subprocess."""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterable
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Callable

from .limits import MAX_JSON_BYTES, validate_inputs, validate_parameters
from .models import RenderRequest
from .offline import registered_app_environment, scrubbed_environment
from .registry import adapter_for_scenario

MAX_WORKER_RESPONSE_BYTES = 64 * 1024
_JOB_ID_RE = re.compile(r"^job_[0-9a-f]{32}$")


class JobError(ValueError):
    """Base class for stable job-manager errors."""


class JobQueueFull(JobError):
    pass


class JobNotFound(JobError):
    pass


class JobState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    FINALIZING = "finalizing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"

    @property
    def finished(self) -> bool:
        return self in {
            self.SUCCEEDED,
            self.FAILED,
            self.TIMED_OUT,
            self.CANCELLED,
        }


@dataclass(frozen=True, slots=True)
class WorkerResult:
    artifact_id: str
    passed: bool


@dataclass(frozen=True, slots=True)
class JobRecord:
    id: str
    session_id: str
    request: RenderRequest
    state: JobState
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    artifact_id: str | None = None
    passed: bool | None = None
    error: str | None = None
    notification_error: str | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "request": self.request.as_dict(),
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "artifact_id": self.artifact_id,
            "passed": self.passed,
            "error": self.error,
            "notification_error": self.notification_error,
        }


WorkerRunner = Callable[[RenderRequest, float], WorkerResult]
CompletionCallback = Callable[[JobRecord], None]


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_job_id() -> str:
    return f"job_{uuid.uuid4().hex}"


def _registered_app_environment(repo_root: Path) -> set[str]:
    """Compatibility wrapper retained for focused library callers."""

    return registered_app_environment(repo_root)


def worker_environment(
    source: dict[str, str] | None = None,
    *,
    app_keys: set[str] | None = None,
) -> dict[str, str]:
    """Copy the platform environment while removing app/device credentials."""

    return scrubbed_environment(source, app_keys=app_keys)


class JobManager:
    """Own a finite queue and a finite number of worker subprocesses.

    ``worker_runner`` is a trusted constructor-only seam for unit tests.  The
    HTTP layer never exposes it, the Python executable, paths, arguments, or
    environment to a request body.
    """

    def __init__(
        self,
        repo_root: Path,
        data_root: Path,
        *,
        max_workers: int = 2,
        max_pending: int = 8,
        timeout_s: float = 45.0,
        max_history: int = 256,
        worker_runner: WorkerRunner | None = None,
    ) -> None:
        if isinstance(max_workers, bool) or not isinstance(max_workers, int) or max_workers < 1:
            raise ValueError("max_workers must be a positive integer")
        if (
            isinstance(max_pending, bool)
            or not isinstance(max_pending, int)
            or max_pending < max_workers
        ):
            raise ValueError("max_pending must be an integer at least max_workers")
        if not 1 <= timeout_s <= 300:
            raise ValueError("worker timeout must be between 1 and 300 seconds")
        if max_history < max_pending:
            raise ValueError("max_history must be at least max_pending")
        self.repo_root = repo_root.resolve()
        self.data_root = data_root.resolve()
        self.max_pending = max_pending
        self.timeout_s = float(timeout_s)
        self.max_history = max_history
        self._runner = worker_runner or self._run_subprocess
        self._executor = ThreadPoolExecutor(
            max_workers=max_workers,
            thread_name_prefix="busybar-viz",
        )
        self._lock = threading.RLock()
        self._jobs: dict[str, JobRecord] = {}
        self._callbacks: dict[str, CompletionCallback] = {}
        self._futures: dict[str, Future[None]] = {}
        self._processes: dict[str, subprocess.Popen[bytes]] = {}
        self._active = 0
        self._closed = False
        self._shutdown_requested = threading.Event()
        self._worker_job = threading.local()
        self._validate_work_directory()
        self._cleanup_work_directories()

    def _validate_work_directory(self) -> None:
        """Reject a store child symlink before cleanup or publication uses it."""

        work = self.data_root / "work"
        if work.is_symlink():
            raise JobError(f"visualizer work path may not be a symlink: {work}")

    def _validate_request(self, request: RenderRequest) -> None:
        if not isinstance(request, RenderRequest):
            raise JobError("job request must be a RenderRequest")
        validate_parameters(dict(request.parameters))
        validate_inputs(request.inputs)
        adapter_for_scenario(request.scenario_id)
        encoded = json.dumps(
            request.as_dict(),
            separators=(",", ":"),
            sort_keys=True,
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        if len(encoded) > MAX_JSON_BYTES:
            raise JobError("render request exceeds the JSON budget")

    def reserve(
        self,
        session_id: str,
        request: RenderRequest,
        *,
        on_finish: CompletionCallback | None = None,
        job_id: str | None = None,
    ) -> JobRecord:
        self._validate_request(request)
        job_id = job_id or _new_job_id()
        if not _JOB_ID_RE.fullmatch(job_id):
            raise JobError("invalid render job id")
        with self._lock:
            if self._closed:
                raise JobError("job manager is shut down")
            if self._active >= self.max_pending:
                raise JobQueueFull("render queue is full")
            if job_id in self._jobs:
                raise JobError(f"render job already exists: {job_id}")
            self._prune_locked()
            record = JobRecord(
                id=job_id,
                session_id=session_id,
                request=request,
                state=JobState.QUEUED,
                created_at=_now(),
            )
            self._jobs[record.id] = record
            if on_finish is not None:
                self._callbacks[record.id] = on_finish
            self._active += 1
            return record

    def start(self, job_id: str) -> JobRecord:
        with self._lock:
            record = self._job_locked(job_id)
            if record.state is not JobState.QUEUED:
                raise JobError(f"job {job_id} cannot start from {record.state.value}")
            if self._closed:
                raise JobError("job manager is shut down")
            future = self._executor.submit(self._execute, job_id)
            self._futures[job_id] = future
            return record

    def submit(
        self,
        session_id: str,
        request: RenderRequest,
        *,
        on_finish: CompletionCallback | None = None,
    ) -> JobRecord:
        record = self.reserve(session_id, request, on_finish=on_finish)
        try:
            self.start(record.id)
        except BaseException:
            self.cancel(record.id)
            raise
        return record

    def cancel(self, job_id: str, *, notify: bool = True) -> JobRecord:
        callback: CompletionCallback | None = None
        with self._lock:
            record = self._job_locked(job_id)
            if record.state.finished:
                return record
            future = self._futures.get(job_id)
            if future is not None and not future.cancel():
                raise JobError(f"running job {job_id} cannot be cancelled")
            record = replace(record, state=JobState.CANCELLED, finished_at=_now())
            self._jobs[job_id] = record
            self._active -= 1
            callback = self._callbacks.pop(job_id, None) if notify else None
            if not notify:
                self._callbacks.pop(job_id, None)
            self._futures.pop(job_id, None)
        if callback is not None:
            callback(record)
        return record

    def get(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._job_locked(job_id)

    def list(self, *, session_id: str | None = None) -> tuple[JobRecord, ...]:
        with self._lock:
            values: Iterable[JobRecord] = self._jobs.values()
            if session_id is not None:
                values = (record for record in values if record.session_id == session_id)
            return tuple(sorted(values, key=lambda item: (item.created_at, item.id)))

    def _job_locked(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError as exc:
            raise JobNotFound(f"unknown render job: {job_id}") from exc

    def _prune_locked(self) -> None:
        excess = len(self._jobs) - self.max_history + 1
        if excess <= 0:
            return
        finished = [
            record for record in self._jobs.values() if record.state.finished
        ]
        for record in sorted(finished, key=lambda item: (item.finished_at or "", item.id))[:excess]:
            self._jobs.pop(record.id, None)
            self._callbacks.pop(record.id, None)
            self._futures.pop(record.id, None)

    def _execute(self, job_id: str) -> None:
        callback: CompletionCallback | None = None
        cancelled: JobRecord | None = None
        with self._lock:
            queued = self._job_locked(job_id)
            if queued.state is not JobState.QUEUED:
                return
            # A submitted Future can begin running just before shutdown, while
            # still waiting to enter this state transition.  Future.cancel()
            # is then false even though no renderer work has started.  Honor
            # the closed gate here so shutdown cannot accidentally launch a
            # full render and wait for its normal timeout.
            if self._closed:
                cancelled = replace(
                    queued,
                    state=JobState.CANCELLED,
                    finished_at=_now(),
                )
                self._jobs[job_id] = cancelled
                self._active -= 1
                callback = self._callbacks.pop(job_id, None)
                self._futures.pop(job_id, None)
            else:
                running = replace(queued, state=JobState.RUNNING, started_at=_now())
                self._jobs[job_id] = running
        if cancelled is not None:
            if callback is not None:
                try:
                    callback(cancelled)
                except Exception:
                    # Server restart reconciliation records any durable render
                    # request whose shutdown notification could not commit.
                    pass
            return
        self._worker_job.id = job_id
        try:
            result = self._runner(running.request, self.timeout_s)
            completed = replace(
                running,
                state=JobState.SUCCEEDED,
                finished_at=_now(),
                artifact_id=result.artifact_id,
                passed=result.passed,
            )
        except subprocess.TimeoutExpired:
            completed = replace(
                running,
                state=JobState.TIMED_OUT,
                finished_at=_now(),
                error=f"render exceeded the {self.timeout_s:g} second timeout",
            )
        except Exception as exc:  # noqa: BLE001 - job isolation boundary
            completed = replace(
                running,
                state=JobState.FAILED,
                finished_at=_now(),
                error=str(exc)[:2000] or exc.__class__.__name__,
            )
        finally:
            self._worker_job.id = None
        # Keep pollers in a non-terminal state until the corresponding durable
        # session event commits.  Otherwise they can observe "succeeded", stop
        # polling, and refresh the session just before its artifact is promoted.
        with self._lock:
            self._jobs[job_id] = replace(
                completed,
                state=JobState.FINALIZING,
                finished_at=None,
            )
            callback = self._callbacks.pop(job_id, None)
        notification_error: str | None = None
        retry_delay = 0.10
        while callback is not None:
            try:
                callback(completed)
                notification_error = None
                break
            except Exception as exc:
                notification_error = str(exc)[:2000] or exc.__class__.__name__
                with self._lock:
                    self._jobs[job_id] = replace(
                        completed,
                        state=JobState.FINALIZING,
                        finished_at=None,
                        error=(
                            "session journal update pending: " + notification_error
                        )[:2000],
                        notification_error=notification_error,
                    )
                if self._shutdown_requested.wait(retry_delay):
                    break
                retry_delay = min(2.0, retry_delay * 2)
        durable = callback is None or notification_error is None
        with self._lock:
            if durable:
                self._jobs[job_id] = completed
            self._active -= 1
            cleanup_work = self._active == 0
            self._futures.pop(job_id, None)
        if cleanup_work:
            self._cleanup_work_directories()

    def _cleanup_work_directories(self) -> None:
        """Reclaim old staging without touching a concurrent publisher."""

        with self._lock:
            if self._active:
                return
            work = self.data_root / "work"
            if work.is_symlink():
                raise JobError(f"visualizer work path may not be a symlink: {work}")
            if not work.is_dir():
                return
            stale_before = time.time() - max(600.0, self.timeout_s * 2)
            for candidate in work.glob("publish-*"):
                try:
                    stale = candidate.stat().st_mtime < stale_before
                except OSError:
                    continue
                if stale and candidate.is_dir() and not candidate.is_symlink():
                    shutil.rmtree(candidate, ignore_errors=True)

    @staticmethod
    def _kill_process_group(process: subprocess.Popen[bytes]) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            try:
                os.killpg(process.pid, signal.SIGKILL)
                return
            except (ProcessLookupError, PermissionError, OSError):
                pass
        try:
            process.kill()
        except ProcessLookupError:
            pass

    def _run_subprocess(self, request: RenderRequest, timeout_s: float) -> WorkerResult:
        payload = json.dumps(
            request.as_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
        command = [
            sys.executable,
            "-m",
            "busybar_viz.worker",
            "--repo-root",
            str(self.repo_root),
            "--data-root",
            str(self.data_root),
        ]
        with tempfile.TemporaryFile() as stdout_sink, tempfile.TemporaryFile() as stderr_sink:
            process = subprocess.Popen(
                command,
                cwd=self.repo_root,
                stdin=subprocess.PIPE,
                stdout=stdout_sink,
                stderr=stderr_sink,
                env=worker_environment(
                    app_keys=_registered_app_environment(self.repo_root),
                ),
                start_new_session=True,
            )
            job_id = getattr(self._worker_job, "id", None)
            if job_id is not None:
                with self._lock:
                    if self._closed:
                        self._kill_process_group(process)
                    else:
                        self._processes[job_id] = process
            try:
                process.communicate(payload, timeout=timeout_s)
            except subprocess.TimeoutExpired:
                self._kill_process_group(process)
                try:
                    process.communicate(timeout=5)
                except subprocess.TimeoutExpired:
                    if process.stdin is not None:
                        process.stdin.close()
                    process.wait(timeout=5)
                raise
            finally:
                if job_id is not None:
                    with self._lock:
                        self._processes.pop(job_id, None)

            stdout_sink.seek(0, os.SEEK_END)
            stdout_size = stdout_sink.tell()
            stdout_sink.seek(0)
            stdout = stdout_sink.read(MAX_WORKER_RESPONSE_BYTES + 1)
            stderr_sink.seek(0, os.SEEK_END)
            stderr_size = stderr_sink.tell()
            stderr_sink.seek(max(0, stderr_size - MAX_WORKER_RESPONSE_BYTES))
            stderr = stderr_sink.read(MAX_WORKER_RESPONSE_BYTES)
        if stdout_size > MAX_WORKER_RESPONSE_BYTES:
            raise JobError("worker response exceeds the output budget")
        if process.returncode != 0:
            detail = stderr[-MAX_WORKER_RESPONSE_BYTES:].decode("utf-8", "replace").strip()
            try:
                body = json.loads(stdout)
                detail = str(body.get("error") or detail)
            except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                pass
            raise JobError(detail[:2000] or f"worker exited {process.returncode}")
        try:
            body = json.loads(stdout)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise JobError("worker returned invalid JSON") from exc
        if not isinstance(body, dict) or body.get("ok") is not True:
            raise JobError("worker returned an invalid success response")
        artifact_id = body.get("artifact_id")
        passed = body.get("passed")
        if (
            not isinstance(artifact_id, str)
            or len(artifact_id) != 64
            or any(char not in "0123456789abcdef" for char in artifact_id)
            or not isinstance(passed, bool)
        ):
            raise JobError("worker returned invalid artifact metadata")
        return WorkerResult(artifact_id, passed)

    def shutdown(self, *, wait: bool = True) -> None:
        callbacks: list[tuple[CompletionCallback, JobRecord]] = []
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._shutdown_requested.set()
            for process in self._processes.values():
                if process.poll() is None:
                    self._kill_process_group(process)
            for job_id, record in tuple(self._jobs.items()):
                if record.state is not JobState.QUEUED:
                    continue
                future = self._futures.get(job_id)
                if future is None or future.cancel():
                    cancelled = replace(
                        record,
                        state=JobState.CANCELLED,
                        finished_at=_now(),
                    )
                    self._jobs[job_id] = cancelled
                    self._active -= 1
                    callback = self._callbacks.pop(job_id, None)
                    if callback is not None:
                        callbacks.append((callback, cancelled))
        for callback, record in callbacks:
            try:
                callback(record)
            except Exception:
                pass
        self._executor.shutdown(wait=wait, cancel_futures=True)

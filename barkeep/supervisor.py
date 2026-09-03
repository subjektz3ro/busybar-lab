"""Process lifecycle for bar apps: spawn, stop (TERM then KILL), crash backoff,
foreground swap choreography, log capture. Knows nothing about HTTP.

Children run in their own process group (start_new_session) so a stop signal
reaches anything they spawned. sys.executable is used directly — barkeep and
the apps share one uv-managed venv, so `uv run` per child would only add
resolver overhead and lock contention.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from collections import deque
from pathlib import Path
from typing import Callable, Mapping

from .registry import AppSpec

log = logging.getLogger(__name__)

RING_LINES = 1000
LOG_ROTATE_BYTES = 1_000_000


class _Runtime:
    """Mutable per-app state. Internal to the supervisor."""

    def __init__(self, spec: AppSpec):
        self.spec = spec
        self.desired_running = False
        self.proc: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.wake = asyncio.Event()          # interrupts backoff sleeps
        self.ring: deque[str] = deque(maxlen=RING_LINES)
        self.restarts = 0
        self.crash_streak = 0
        self.next_backoff = 0.0              # set from supervisor on crash
        self.started_at: float | None = None


class Supervisor:
    def __init__(
        self,
        registry: dict[str, AppSpec],
        repo_root: Path,
        logs_dir: Path,
        child_env_fn: Callable[[str], Mapping[str, str]],
        *,
        term_grace: float = 10.0,
        backoff_base: float = 5.0,
        backoff_cap: float = 60.0,
        crash_window: float = 30.0,
        crash_streak_limit: int = 3,
    ):
        self.registry = registry
        self.repo_root = repo_root
        self.logs_dir = logs_dir
        self.child_env_fn = child_env_fn
        self.term_grace = term_grace
        self.backoff_base = backoff_base
        self.backoff_cap = backoff_cap
        self.crash_window = crash_window
        self.crash_streak_limit = crash_streak_limit
        self.foreground: str | None = None
        self.switching = False
        self._apps = {name: _Runtime(spec) for name, spec in registry.items()}
        # One lock across whole lifecycle operations. A stop takes seconds
        # (apps get a grace window to clear their draws), and a second click
        # arriving inside that window used to interleave two operations on one
        # runtime — leaving two children alive, or joining a task the other op
        # had already replaced. Reads (status/logs) stay lock-free so the UI's
        # 2s poll never waits behind a swap.
        self._lock = asyncio.Lock()
        self._log_write_failed = False  # warn once, not once per line

    # -- public operations ------------------------------------------------

    async def set_foreground(self, name: str | None) -> None:
        if name is not None:
            self._spec(name, expect_kind="foreground")
        async with self._lock:
            # Re-checked inside the lock: a queued caller may find the work
            # already done by the operation it waited on.
            if name == self.foreground:
                return
            self.switching = True
            try:
                if self.foreground is not None:
                    await self._stop(self._apps[self.foreground])
                self.foreground = name
                if name is not None:
                    self._start(self._apps[name])
            finally:
                self.switching = False

    async def enable(self, name: str) -> None:
        rt = self._apps[self._spec(name, expect_kind="background").name]
        async with self._lock:
            self._start(rt)

    async def disable(self, name: str) -> None:
        rt = self._apps[self._spec(name, expect_kind="background").name]
        async with self._lock:
            await self._stop(rt)

    async def restart(self, name: str) -> None:
        rt = self._apps[name]  # KeyError for unknown apps is the contract
        async with self._lock:
            was_running = rt.desired_running  # capture before _stop clears it
            await self._stop(rt)
            rt.restarts = 0
            rt.crash_streak = 0
            # Restarting a disabled background app must not quietly enable it
            # (the caller persists desired state afterwards).
            if was_running or name == self.foreground:
                self._start(rt)

    async def shutdown(self) -> None:
        # Deliberately outside the lock: this runs after the server stops
        # serving, and waiting on an in-flight swap could stall systemd's stop.
        await asyncio.gather(*(self._stop(rt) for rt in self._apps.values()),
                             return_exceptions=True)

    def enabled_backgrounds(self) -> set[str]:
        return {n for n, rt in self._apps.items()
                if rt.spec.kind == "background" and rt.desired_running}

    def status(self) -> list[dict]:
        rows = []
        for name, rt in self._apps.items():
            # Bound once: status() is lock-free by design (the UI polls it every
            # 2s and must not queue behind a swap), so rt.proc can be replaced
            # by the supervision task between two reads of it.
            proc = rt.proc
            running = proc is not None and proc.returncode is None
            if running:
                state = "running"
            elif rt.desired_running:
                state = "backoff"
            else:
                state = "stopped"
            rows.append({
                "name": name,
                "kind": rt.spec.kind,
                "description": rt.spec.description,
                "status": state,
                "crash_looping": rt.crash_streak >= self.crash_streak_limit,
                "restarts": rt.restarts,
                "pid": proc.pid if proc is not None and running else None,
                "uptime_s": (time.monotonic() - rt.started_at)
                            if running and rt.started_at else None,
            })
        return rows

    def logs(self, name: str, lines: int = 200) -> list[str]:
        return list(self._apps[name].ring)[-lines:]

    # -- internals --------------------------------------------------------

    def _spec(self, name: str, expect_kind: str) -> AppSpec:
        spec = self.registry[name]  # KeyError for unknown apps
        if spec.kind != expect_kind:
            raise ValueError(f"{name} is a {spec.kind} app, not {expect_kind}")
        return spec

    def _start(self, rt: _Runtime) -> None:
        if rt.desired_running:
            return
        rt.desired_running = True
        rt.wake = asyncio.Event()
        rt.next_backoff = self.backoff_base
        rt.task = asyncio.create_task(self._run(rt), name=f"run:{rt.spec.name}")

    async def _terminate(self, rt: _Runtime, proc) -> None:
        """TERM, wait out the grace window, then KILL.

        Shared so a stop that raced the spawn gets exactly what a normal stop
        gets — apps need that window to clear their draws.
        """
        if proc is None or proc.returncode is not None:
            return
        self._signal(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), self.term_grace)
        except asyncio.TimeoutError:
            log.warning("%s ignored SIGTERM for %.1fs; killing",
                        rt.spec.name, self.term_grace)
            self._signal(proc, signal.SIGKILL)

    async def _stop(self, rt: _Runtime) -> None:
        rt.desired_running = False
        rt.wake.set()
        # Capture before any await: a concurrent op must never find us nulling
        # a task it started, and we must not adopt one it did.
        task, proc = rt.task, rt.proc
        await self._terminate(rt, proc)
        if task is not None:
            try:
                await task
            except Exception:  # noqa: BLE001 - a poisoned task must not 500 the API
                log.exception("%s: supervision task raised", rt.spec.name)
            finally:
                if rt.task is task:
                    rt.task = None

    @staticmethod
    def _signal(proc: asyncio.subprocess.Process, sig: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, sig)  # pgid == pid via start_new_session

    async def _run(self, rt: _Runtime) -> None:
        name = rt.spec.name
        while rt.desired_running:
            entry = Path(rt.spec.entrypoint)
            if not entry.is_absolute():
                entry = self.repo_root / entry
            try:
                try:
                    rt.proc = await asyncio.create_subprocess_exec(
                        sys.executable, "-u", str(entry),
                        cwd=self.repo_root,
                        env=dict(self.child_env_fn(name)),
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.STDOUT,
                        start_new_session=True,
                        limit=1 << 20,  # a long traceback line must not raise
                    )
                except OSError as exc:
                    rt.ring.append(f"[barkeep] spawn failed: {exc}")
                    log.error("%s: spawn failed: %s", name, exc)
                    rt.crash_streak += 1
                else:
                    rt.started_at = time.monotonic()
                    log.info("%s: started pid %d", name, rt.proc.pid)
                    if not rt.desired_running:
                        # A stop landed while we were still spawning: it saw
                        # rt.proc as None and signalled nothing, so nobody but
                        # us can stop this child — and _pump below would wait
                        # on it forever.
                        await self._terminate(rt, rt.proc)
                    await self._pump(rt)
                    rc = await rt.proc.wait()
                    lifetime = time.monotonic() - rt.started_at
                    rt.proc = None
                    if not rt.desired_running:
                        log.info("%s: stopped (rc=%s)", name, rc)
                        return
                    rt.restarts += 1
                    if lifetime < self.crash_window:
                        rt.crash_streak += 1
                    else:
                        rt.crash_streak = 0
                        rt.next_backoff = self.backoff_base
                    rt.ring.append(
                        f"[barkeep] exited rc={rc} after {lifetime:.1f}s; "
                        f"restarting in {rt.next_backoff:.0f}s")
                    log.warning("%s: exited rc=%s after %.1fs (streak %d)",
                                name, rc, lifetime, rt.crash_streak)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - supervision must survive
                # Anything unexpected in here (a full disk while writing logs,
                # a StreamReader limit) used to end this task for good: the
                # child kept running unsupervised while the UI still said
                # "running" and every later click 500'd.
                log.exception("%s: supervision loop error", name)
                rt.ring.append(f"[barkeep] supervisor error: {exc!r}; retrying")
                rt.crash_streak += 1
                await self._terminate(rt, rt.proc)  # never respawn over a live child
                rt.proc = None
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(rt.wake.wait(), rt.next_backoff)
            rt.next_backoff = min(rt.next_backoff * 2, self.backoff_cap)

    async def _pump(self, rt: _Runtime) -> None:
        assert rt.proc is not None and rt.proc.stdout is not None
        path = self.logs_dir / f"{rt.spec.name}.log"
        while True:
            line = await rt.proc.stdout.readline()
            if not line:
                return
            text = line.decode(errors="replace").rstrip("\n")
            rt.ring.append(text)
            self._append_log(path, text)

    def _append_log(self, path: Path, text: str) -> None:
        # Open-per-line is fine at bar-app log rates and keeps rotation trivial.
        # Disk trouble must never take supervision down with it — the in-memory
        # ring is what the UI reads, and it is already written.
        try:
            self.logs_dir.mkdir(parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size > LOG_ROTATE_BYTES:
                path.replace(path.with_suffix(".log.1"))
            with path.open("a") as f:
                f.write(text + "\n")
        except OSError as exc:
            if not self._log_write_failed:
                self._log_write_failed = True
                log.warning("log file writes failing (%s); ring buffer only", exc)

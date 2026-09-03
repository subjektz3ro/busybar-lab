import asyncio
import contextlib
import signal
from pathlib import Path

import pytest

from barkeep.registry import AppSpec
from barkeep.supervisor import Supervisor

# Tiny real programs so signals are genuinely exercised.
CHATTY = """import sys, time
print("hello from app", flush=True)
while True: time.sleep(0.05)
"""
POLITE = """import signal, sys, time
signal.signal(signal.SIGTERM, lambda *a: (print("bye", flush=True), sys.exit(0)))
print("ready", flush=True)
while True: time.sleep(0.05)
"""
STUBBORN = """import signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
print("never leaving", flush=True)
while True: time.sleep(0.05)
"""
CRASHER = """import sys
print("dying", flush=True)
sys.exit(1)
"""


def make_sup(tmp_path: Path, **apps) -> Supervisor:
    registry = {}
    for name, (kind, code) in apps.items():
        script = tmp_path / f"{name}.py"
        script.write_text(code)
        registry[name] = AppSpec(name=name, kind=kind, entrypoint=str(script),
                                 description=name)
    return Supervisor(
        registry, repo_root=tmp_path, logs_dir=tmp_path / "logs",
        child_env_fn=lambda name: {"PATH": "/usr/bin:/bin"},
        term_grace=0.5, backoff_base=0.05, backoff_cap=0.2, crash_window=30.0,
    )


@pytest.fixture
async def sup(tmp_path):
    """Build supervisors whose children never outlive the test.

    Every test here spawns real processes; a failing assertion used to skip
    the trailing shutdown() and leak a child (SIGTERM-ignoring ones reparent
    to init) or park pytest forever on a task stuck mid-spawn.
    """
    made: list[Supervisor] = []

    def build(**apps) -> Supervisor:
        s = make_sup(tmp_path, **apps)
        made.append(s)
        return s

    yield build

    for s in made:
        with contextlib.suppress(asyncio.TimeoutError, Exception):
            await asyncio.wait_for(s.shutdown(), 5)
        for rt in s._apps.values():
            if rt.proc is not None and rt.proc.returncode is None:
                Supervisor._signal(rt.proc, signal.SIGKILL)


async def wait_for(predicate, timeout=5.0):
    async with asyncio.timeout(timeout):
        while not predicate():
            await asyncio.sleep(0.02)


def app_row(sup, name):
    return next(r for r in sup.status() if r["name"] == name)


async def test_foreground_runs_and_logs(sup, tmp_path):
    sup = sup(sky=("foreground", CHATTY))
    await sup.set_foreground("sky")
    await wait_for(lambda: "hello from app" in sup.logs("sky"))
    row = app_row(sup, "sky")
    assert row["status"] == "running" and row["pid"]
    assert (tmp_path / "logs" / "sky.log").read_text().startswith("hello")
    await sup.shutdown()
    assert app_row(sup, "sky")["status"] == "stopped"


async def test_swap_stops_old_before_new(sup):
    sup = sup(a=("foreground", POLITE), b=("foreground", POLITE))
    await sup.set_foreground("a")
    # Process creation is not child readiness: under load the status can say
    # running before Python installs POLITE's SIGTERM handler. The child's own
    # marker proves the behavior under test is armed before initiating swap.
    await wait_for(lambda: "ready" in sup.logs("a"))
    # Spy on _start: the outgoing app must already be reaped when the incoming
    # one spawns. End-state assertions alone cannot tell the orders apart, and
    # timestamps can't either — fork+interpreter boot is slower than the old
    # child's SIGTERM turnaround, so "bye" precedes "ready" either way.
    a_alive_when_b_started = []
    orig_start = sup._start

    def spy(rt):
        a = sup._apps["a"]
        a_alive_when_b_started.append(
            a.proc is not None and a.proc.returncode is None)
        return orig_start(rt)
    sup._start = spy

    await sup.set_foreground("b")
    assert a_alive_when_b_started == [False]
    assert sup.foreground == "b"
    assert app_row(sup, "a")["status"] == "stopped"
    # SIGTERM was honored and the clean-exit line was drained. The spy above,
    # not log timing, remains the old-before-new ordering assertion.
    await wait_for(lambda: "bye" in sup.logs("a"))
    await wait_for(lambda: app_row(sup, "b")["status"] == "running")
    await sup.set_foreground(None)  # none = just the stop half
    assert app_row(sup, "b")["status"] == "stopped"
    await sup.shutdown()


async def test_stubborn_child_gets_sigkill(sup):
    sup = sup(mule=("foreground", STUBBORN))
    await sup.set_foreground("mule")
    await wait_for(lambda: app_row(sup, "mule")["status"] == "running")
    await sup.set_foreground(None)  # term_grace=0.5 then SIGKILL
    assert app_row(sup, "mule")["status"] == "stopped"
    await sup.shutdown()


async def test_crash_restarts_then_flags_looping(sup):
    sup = sup(flaky=("background", CRASHER))
    await sup.enable("flaky")
    await wait_for(lambda: app_row(sup, "flaky")["crash_looping"])
    assert app_row(sup, "flaky")["restarts"] >= 3
    await sup.disable("flaky")
    await wait_for(lambda: app_row(sup, "flaky")["status"] == "stopped")
    await sup.shutdown()


async def test_restart_clears_crash_streak(sup):
    sup = sup(flaky=("background", CRASHER))
    await sup.enable("flaky")
    await wait_for(lambda: app_row(sup, "flaky")["crash_looping"])
    await sup.restart("flaky")
    assert app_row(sup, "flaky")["crash_looping"] is False
    await sup.disable("flaky")
    await sup.shutdown()


async def test_stop_during_spawn_window(sup):
    """A stop landing before create_subprocess_exec returns must still stop."""
    s = sup(mule=("foreground", STUBBORN))
    await s.set_foreground("mule")          # returns while _run is mid-spawn
    await asyncio.sleep(0)                  # let _run reach the spawn, no more
    await asyncio.wait_for(s.set_foreground(None), 8.0)
    assert app_row(s, "mule")["status"] == "stopped"
    assert s._apps["mule"].proc is None


async def test_restart_then_standby_both_return(sup):
    """Overlapping lifecycle ops must serialize, not interleave."""
    s = sup(a=("foreground", POLITE))
    await s.set_foreground("a")
    await wait_for(lambda: app_row(s, "a")["status"] == "running")
    await asyncio.wait_for(
        asyncio.gather(s.restart("a"), s.set_foreground(None)), 15.0)
    assert s.foreground is None
    await wait_for(lambda: app_row(s, "a")["status"] == "stopped")
    assert s._apps["a"].proc is None        # nothing left drawing to the bar


async def test_concurrent_foreground_swaps_leave_one_winner(sup):
    s = sup(a=("foreground", POLITE), b=("foreground", POLITE),
            c=("foreground", POLITE))
    await s.set_foreground("a")
    await wait_for(lambda: app_row(s, "a")["status"] == "running")
    await asyncio.wait_for(
        asyncio.gather(s.set_foreground("b"), s.set_foreground("c")), 15.0)
    live = {n for n, rt in s._apps.items() if rt.desired_running}
    assert live == {s.foreground}
    assert s.foreground in ("b", "c")


async def test_unwritable_log_dir_does_not_disturb_the_app(sup, tmp_path):
    """Disk trouble is absorbed where it happens: the app keeps running."""
    (tmp_path / "logs").write_text("not a directory")  # mkdir will raise

    s = sup(sky=("foreground", CHATTY))
    await s.set_foreground("sky")
    await wait_for(lambda: app_row(s, "sky")["status"] == "running")
    await asyncio.sleep(0.3)                  # plenty of doomed log writes
    row = app_row(s, "sky")
    assert row["status"] == "running" and row["restarts"] == 0
    assert "hello from app" in s.logs("sky")  # ring buffer still authoritative
    await asyncio.wait_for(s.set_foreground(None), 8.0)


async def test_supervision_survives_an_unexpected_error(sup, monkeypatch):
    """An unforeseen exception must not end supervision for good."""
    calls = []
    real_pump = Supervisor._pump

    async def boom(self, rt):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("something nobody predicted")
        await real_pump(self, rt)          # behave normally from then on
    monkeypatch.setattr(Supervisor, "_pump", boom)

    s = sup(sky=("foreground", CHATTY))
    await s.set_foreground("sky")
    await wait_for(lambda: len(calls) >= 2, timeout=8.0)   # it retried
    assert not s._apps["sky"].task.done()                  # still supervising
    await asyncio.wait_for(s.set_foreground(None), 8.0)
    assert app_row(s, "sky")["status"] == "stopped"


async def test_restart_of_stopped_background_is_a_noop(sup):
    s = sup(bg=("background", CHATTY))
    await s.restart("bg")
    assert app_row(s, "bg")["status"] == "stopped"
    assert s.enabled_backgrounds() == set()


async def test_kind_enforcement(sup):
    sup = sup(sky=("foreground", CHATTY), bg=("background", CHATTY))
    with pytest.raises(ValueError):
        await sup.set_foreground("bg")
    with pytest.raises(ValueError):
        await sup.enable("sky")
    with pytest.raises(KeyError):
        await sup.set_foreground("nope")
    await sup.shutdown()

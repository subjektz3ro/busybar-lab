"""DSN audio / worker."""

from __future__ import annotations

import asyncio
import sys
import tempfile
import threading
from pathlib import Path

from apps.dsn_app import settings as _settings
from busybar_dev.tts import synth_snd


def _settle(fut: asyncio.Future, error: BaseException | None, value) -> None:
    if fut.done():  # cancelled while we ran
        return
    if error is not None:
        fut.set_exception(error)
    else:
        fut.set_result(value)


async def synth_off_loop(text: str) -> bytes:
    """Synthesise without pinning the app or its event loop.

    Linux is the always-on/Pi path. Kokoro retains roughly a gigabyte of model
    state after one call, so a rare cache miss runs in a disposable child and
    returns that memory to the OS when it exits. The child is also explicitly
    terminated on cancellation. Other platforms keep the daemon-thread path:
    macOS ``say`` is cheap, and a daemon avoids ``asyncio.run()`` waiting for a
    default-executor thread during shutdown.
    """
    if isolate_tts_process():
        return await synth_in_worker(text)

    loop = asyncio.get_running_loop()
    fut: asyncio.Future = loop.create_future()

    def work() -> None:
        try:
            value, error = synth_snd(text, _settings.VOICE), None
        except BaseException as exc:  # noqa: BLE001 - reported to the awaiter
            value, error = None, exc
        try:
            loop.call_soon_threadsafe(_settle, fut, error, value)
        except RuntimeError:
            pass  # loop closed: shutting down

    threading.Thread(target=work, daemon=True, name="dsn-synth").start()
    return await fut


def isolate_tts_process() -> bool:
    """The resident neural-model cost matters on the Linux production host."""
    return sys.platform.startswith("linux")


async def _stop_synth_process(proc) -> None:
    if proc.returncode is not None:
        return
    try:
        proc.terminate()
    except ProcessLookupError:
        await proc.wait()  # let asyncio reap an exit that won the race
        return
    try:
        await asyncio.wait_for(proc.wait(), timeout=2.0)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        await proc.wait()


async def synth_in_worker(text: str) -> bytes:
    """Run one Linux bake in a cancellable process with a private output file."""
    with tempfile.NamedTemporaryFile(
        prefix="dsn-tts-", suffix=".snd", delete=False
    ) as handle:
        output = Path(handle.name)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "busybar_dev.tts_worker",
            "--voice",
            _settings.VOICE,
            "--output",
            str(output),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await proc.communicate(text.encode())
        except asyncio.CancelledError:
            await _stop_synth_process(proc)
            raise
        if proc.returncode:
            detail = stderr.decode(errors="replace").strip()
            if len(detail) > 1200:
                detail = detail[-1200:]
            raise RuntimeError(
                f"isolated TTS exited {proc.returncode}"
                + (f": {detail}" if detail else "")
            )
        pcm = output.read_bytes()
        if not pcm or len(pcm) % 2:
            raise RuntimeError("isolated TTS returned invalid s16 PCM")
        return pcm
    finally:
        if proc is not None and proc.returncode is None:
            await _stop_synth_process(proc)
        output.unlink(missing_ok=True)

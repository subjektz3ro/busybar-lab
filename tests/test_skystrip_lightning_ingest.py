"""Lightning ingestion retries safely without a reconnect hot loop.

The bounded-decoder and coordinate-validation tests that used to live here are
gone on purpose. The lightning rewrite that landed alongside this work replaced
`_lzw_decode` and the strike parser with stricter versions — input, output AND
dictionary bounds, rejection of invalid LZW codes rather than synthesising an
entry, strict JSON typing, and epoch-nanosecond staleness/future-skew checks —
and `tests/test_skystrip_lightning_source.py` covers all of it more thoroughly
than these did. Keeping duplicates against the old API would have been noise.

What remains is what that work does not cover.
"""

from __future__ import annotations

import asyncio
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import skystrip  # noqa: E402
from busybar_dev.device import connect_with_retry  # noqa: E402


# --- reconnect pacing ------------------------------------------------------


def test_a_reconnect_floor_exists_and_is_positive():
    """370 clean-close reconnects were observed in one host's log. The
    clean-close branch reset backoff to 1.0 and reconnected immediately, which
    against a community-run non-commercial service is hammering rather than
    reconnecting (see NOTICE.md)."""
    assert skystrip.RECONNECT_FLOOR_S > 0


def test_the_clean_close_path_waits_before_reconnecting():
    """The floor has to be applied on the CLEAN close path specifically — the
    short-session path already backed off, and that is not the one that fired
    370 times."""
    import inspect

    source = inspect.getsource(skystrip.listen_lightning)
    clean = source.split("closed cleanly", 1)
    assert len(clean) == 2, "the clean-close branch moved; re-check the floor"
    assert "RECONNECT_FLOOR_S" in clean[1], (
        "the clean-close branch reconnects without waiting")


# --- device reconnect ------------------------------------------------------


def test_the_bar_is_waited_for_rather_than_dying():
    """`bb = await aconnect()` had no retry, so a bar not yet on the network at
    host boot killed the process — twelve of those in the deploy host's log."""
    attempts = []

    async def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise ConnectionError("Could not reach the BUSY Bar.")
        return "bar"

    async def scenario():
        stop = asyncio.Event()
        return await connect_with_retry(
            flaky, stop, log=logging.getLogger("t"), base=0.001, cap=0.001)

    assert asyncio.run(scenario()) == "bar"
    assert len(attempts) == 3


def test_a_stop_during_the_wait_exits_promptly():
    """SIGTERM while the bar is absent must not sit out the backoff."""
    async def never():
        raise ConnectionError("no bar")

    async def scenario():
        stop = asyncio.Event()

        async def signal_soon():
            await asyncio.sleep(0.01)
            stop.set()

        asyncio.create_task(signal_soon())
        with pytest.raises(ConnectionError, match="shutting down"):
            await connect_with_retry(
                never, stop, log=logging.getLogger("t"), base=5.0, cap=5.0)

    asyncio.run(asyncio.wait_for(scenario(), timeout=3.0))


def test_an_already_stopped_run_does_not_connect_at_all():
    called = []

    async def counting():
        called.append(1)
        return "bar"

    async def scenario():
        stop = asyncio.Event()
        stop.set()
        with pytest.raises(ConnectionError):
            await connect_with_retry(counting, stop, log=logging.getLogger("t"))

    asyncio.run(scenario())
    assert called == []


def test_the_retry_never_logs_the_exception_verbatim():
    """A connect failure names the host and can carry a token-bearing URL."""
    messages = []

    class Capture(logging.Handler):
        def emit(self, record):
            messages.append(record.getMessage())

    log = logging.getLogger("connect-retry-test")
    log.addHandler(Capture())
    log.setLevel(logging.WARNING)

    async def never():
        raise ConnectionError("secret-token-in-url")

    async def scenario():
        stop = asyncio.Event()
        asyncio.get_running_loop().call_later(0.05, stop.set)
        with pytest.raises(ConnectionError):
            await connect_with_retry(never, stop, log=log, base=0.01, cap=0.01,
                                     describe=lambda e: type(e).__name__)

    asyncio.run(scenario())
    assert messages, "the retry logged nothing at all"
    assert not any("secret-token-in-url" in m for m in messages)

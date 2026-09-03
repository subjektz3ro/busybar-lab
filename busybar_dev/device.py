"""Shared BUSY Bar device rules used by every app.

Everything here is a place where the firmware fails *quietly* — a refused draw
that looks like an exception, an asset path that silently serves stale bytes,
an orphan nothing reaps. Those are exactly the rules worth having one copy of,
because a second copy drifts and the drift is invisible until it is on the
panel.

What is deliberately NOT here: the sweep policies. skystrip and dsn both sweep
`/ext/user_assets/<app>` at startup, but what each considers reclaimable is
genuinely app-specific — skystrip spares a deterministic text+voice report
cache, dsn spares a repair-generation speech cache with its own poison
tracking. Forcing those into one signature would produce a helper with two
apps' worth of exceptions in it. The naming and path rules are shared; the
keep-set stays where the knowledge is.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging

ASSET_ROOT = "/ext/user_assets"

# The firmware's own ceiling on an asset filename, in bytes.
DEVICE_ASSET_FILENAME_MAX = 31


def is_refusal(exc: BaseException) -> bool:
    """True when the device refused outright rather than failing.

    An active BUSY/CUSTOM session answers 409 "Not drawn due to low priority",
    on `audio_play` as well as `display_draw`. 409 is normal operation: yield,
    back off, retry — never escalate priority to fight it.

    busylib exposes the code as `status_code`. `http_status` does not exist and
    silently never matches, which is its own entry in the skill's mistake
    table. The string check stays as a fallback for older payload shapes.
    """
    return (getattr(exc, "status_code", None) == 409
            or "priority" in str(exc).lower())


def storage_file_matches(entry: object, expected_size: int) -> bool:
    """Whether a storage listing entry is the exact file we expect.

    `type` is a Literal['file'] today, but dsn's copy already unwrapped a
    possible enum and skystrip's compared it to a string directly — the two
    had drifted. This is dsn's, which is the safer of the two: a malformed
    entry with no `type` is not treated as a file.
    """
    kind = getattr(entry, "type", None)
    kind = getattr(kind, "value", kind)
    return (str(kind).lower() == "file"
            and getattr(entry, "size", None) == expected_size)


def asset_path(app: str, name: str) -> str:
    """The device path for one of an app's uploaded assets.

    A literal that appeared some thirty times across the two apps. Getting it
    wrong does not raise — the delete simply reaps nothing and the orphan
    stays on flash.
    """
    return f"{ASSET_ROOT}/{app}/{name}"


def content_asset_name(prefix: str, blob: bytes, *, suffix: str,
                       digest_chars: int = 10) -> str:
    """An immutable, content-addressed asset name.

    This is the shape that turns the firmware's cache-by-path from a trap into
    the mechanism: identical bytes produce an identical name, so a hit needs no
    upload, nothing is ever overwritten, and the 508 "file is open" case cannot
    fire. Use it for deterministic content. Mutable scenes want a
    timestamp AND a monotonic counter instead — a bare timestamp modulo wraps.
    """
    digest = hashlib.sha1(blob).hexdigest()[:digest_chars]
    name = f"{prefix}{digest}{suffix}"
    if len(name.encode("ascii")) > DEVICE_ASSET_FILENAME_MAX:
        raise ValueError(
            f"asset filename exceeds the device limit of "
            f"{DEVICE_ASSET_FILENAME_MAX} bytes: {name}")
    return name


CONNECT_RETRY_BASE_S = 2.0
CONNECT_RETRY_CAP_S = 60.0


async def connect_with_retry(aconnect, stop: asyncio.Event, *,
                             log: logging.Logger,
                             describe=repr,
                             base: float = CONNECT_RETRY_BASE_S,
                             cap: float = CONNECT_RETRY_CAP_S):
    """Wait for the bar instead of dying when it is not there yet.

    Every remote FEED in these apps has careful transient-vs-permanent
    backoff — skystrip's poll_nws even separates "outside NWS coverage" (retry
    in six hours) from a cold DNS at boot (fifteen minutes). The one connection
    an app cannot run without had none: `bb = await aconnect()` at the top of
    run(), so a bar not yet on the network at host boot killed the process.
    Twelve of those were sitting in the deploy host's log.

    barkeep does restart it, so this was self-healing — but the panel stayed
    dark for the backoff and the UI said `crash_looping` with a connection
    error, which is not the same thing as "waiting for the bar".

    Honours `stop` so a SIGTERM during the wait exits promptly rather than
    sitting out the delay.
    """
    delay = base
    attempt = 0
    while not stop.is_set():
        try:
            return await aconnect()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - the bar comes and goes
            attempt += 1
            if attempt == 1:
                log.warning("bar unreachable (%s); retrying", describe(exc))
            elif attempt % 10 == 0:
                log.warning("bar still unreachable after %d attempts", attempt)
        if stop.is_set():
            break
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(stop.wait(), delay)
        delay = min(delay * 2, cap)
    raise ConnectionError("gave up waiting for the bar: shutting down")

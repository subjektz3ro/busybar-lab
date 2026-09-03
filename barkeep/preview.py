"""What the bar is showing right now, as PNGs for the web UI.

Sync on purpose: busylib's BusyBar is sync and FastAPI runs `def` routes in
its threadpool. Native-size images; the frontend upscales with CSS
image-rendering: pixelated. Bar unreachable is normal, not fatal — BarOffline
maps to a 503 and the UI shows "bar offline".
"""

from __future__ import annotations

import contextlib
import io
import threading
import time
from collections.abc import Callable

from busylib import BusyBar

import busybar_dev
from busybar_dev.screen import back_image, front_image


class BarOffline(Exception):
    pass


class Preview:
    def __init__(self, connect_fn: Callable[[], BusyBar] = busybar_dev.connect,
                 ttl: float = 2.0, offline_ttl: float = 10.0):
        self._connect_fn = connect_fn
        self._ttl = ttl
        # Connecting to an absent bar costs ~16s of retries. The page polls
        # two displays every 5s, so without a cooldown every browser
        # connection ends up parked in the threadpool and the whole control
        # UI goes dead exactly when the operator needs it. Per display, so a
        # flaky back read (38kB vs the front's 3kB) can't blank a front pane
        # that is working.
        self._offline_ttl = offline_ttl
        self._offline_until: dict[int, float] = {}
        self._bb: BusyBar | None = None
        self._cache: dict[int, tuple[float, bytes]] = {}
        # The route is a sync `def`, so FastAPI runs it in the threadpool, and
        # the UI polls display 0 and 1 every 5s — concurrency is the designed
        # case. Without this, both threads could pass the `self._bb is None`
        # test after a failure, both connect, and the first client would be
        # overwritten with its socket never closed. That leaks a client on
        # every flap of the link, in a daemon that runs for months.
        self._lock = threading.Lock()

    def _client(self) -> BusyBar:
        """The shared client, connecting at most once across threads."""
        with self._lock:
            if self._bb is None:
                self._bb = self._connect_fn()
            return self._bb

    def _drop(self, bb: BusyBar) -> None:
        """Retire a client that failed, unless someone already replaced it."""
        with self._lock:
            ours = self._bb is bb
            if ours:
                self._bb = None                  # reconnect next call
        if not ours:
            return                               # a newer client is live
        with contextlib.suppress(Exception):
            bb.close()                           # don't leak its sockets

    def png(self, display: int) -> bytes:
        with self._lock:
            cached = self._cache.get(display)
            if cached and time.monotonic() - cached[0] < self._ttl:
                return cached[1]
            if time.monotonic() < self._offline_until.get(display, 0.0):
                raise BarOffline("bar offline (cooling down)")
        bb = None
        try:
            bb = self._client()
            # Deliberately outside the lock: the back display is 38 kB against
            # the front's 3 kB, and a slow read of one must not block the other.
            img = front_image(bb) if display == 0 else back_image(bb)
        except Exception as exc:
            if bb is not None:
                self._drop(bb)
            else:
                with self._lock:                 # the connect itself failed
                    self._bb = None
            with self._lock:
                self._offline_until[display] = time.monotonic() + self._offline_ttl
            raise BarOffline(str(exc)) from exc
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png = buf.getvalue()
        with self._lock:
            self._cache[display] = (time.monotonic(), png)
        return png

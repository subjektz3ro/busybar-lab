import io
import time

import pytest
from PIL import Image

from barkeep.preview import BarOffline, Preview


class FakeBar:
    """Duck-types busylib.BusyBar for screen.front_image/back_image."""

    def __init__(self):
        self.calls = 0

    def screen(self, display: int) -> bytes:
        self.calls += 1
        if display == 0:
            return bytes(72 * 16 * 3)          # RGB888 front
        return bytes(160 * 80 // 2)            # L4-packed back


def test_png_roundtrip_and_cache():
    bar = FakeBar()
    p = Preview(connect_fn=lambda: bar, ttl=10.0)
    png = p.png(0)
    img = Image.open(io.BytesIO(png))
    assert img.size == (72, 16)
    p.png(0)
    assert bar.calls == 1                      # second hit served from cache

    back = Image.open(io.BytesIO(p.png(1)))
    assert back.size == (160, 80)


def test_cache_expires():
    bar = FakeBar()
    p = Preview(connect_fn=lambda: bar, ttl=0.0)
    p.png(0)
    p.png(0)
    assert bar.calls == 2


def test_offline_raises_and_reconnects_next_time():
    attempts = []

    def connect():
        attempts.append(1)
        raise ConnectionError("no bar")

    p = Preview(connect_fn=connect, ttl=0.0, offline_ttl=0.0)
    with pytest.raises(BarOffline):
        p.png(0)
    with pytest.raises(BarOffline):
        p.png(0)
    assert len(attempts) == 2                  # fresh connect attempt each call


def test_offline_cooldown_stops_hammering_a_dead_bar():
    """Reconnecting costs ~16s; the UI must not queue those behind each other."""
    attempts = []

    def connect():
        attempts.append(1)
        raise ConnectionError("no bar")

    p = Preview(connect_fn=connect, ttl=0.0, offline_ttl=30.0)
    for _ in range(4):
        with pytest.raises(BarOffline):
            p.png(0)
    assert len(attempts) == 1                  # one attempt, then cooling down


def test_cooldown_is_per_display():
    """A flaky back read must not blank a front pane that is working."""
    class HalfDead:
        def screen(self, display):
            if display == 1:
                raise ConnectionError("back read timed out")
            return bytes(72 * 16 * 3)

    p = Preview(connect_fn=HalfDead, ttl=0.0, offline_ttl=30.0)
    with pytest.raises(BarOffline):
        p.png(1)                               # back fails, arms its cooldown
    assert p.png(0)                            # front still served
    with pytest.raises(BarOffline):
        p.png(1)                               # back still cooling down


def test_offline_closes_the_dead_client():
    class Flaky:
        def __init__(self):
            self.closed = False

        def screen(self, display):
            raise ConnectionError("bar vanished mid-read")

        def close(self):
            self.closed = True

    bar = Flaky()
    p = Preview(connect_fn=lambda: bar, ttl=0.0, offline_ttl=0.0)
    with pytest.raises(BarOffline):
        p.png(0)
    assert bar.closed


# --- concurrency -----------------------------------------------------------


def test_concurrent_misses_connect_once_and_leak_no_client():
    """Two threads racing the same cold cache must share one client.

    The UI polls display 0 and 1 every 5s and the route is a sync `def`, so
    FastAPI runs both in the threadpool at once. Before the lock, both could
    pass the `self._bb is None` test, both connect, and the first client would
    be overwritten with its socket never closed.
    """
    import threading

    from barkeep.preview import Preview

    started = threading.Barrier(2)
    made: list[object] = []
    closed: list[object] = []

    class SlowBar:
        def __init__(self):
            self.screens = 0

        def screen(self, display):
            self.screens += 1
            return b"\x00" * (72 * 16 * 3) if display == 0 else b"\x00" * (160 * 80 * 3)

        def close(self):
            closed.append(self)

    def connect():
        time.sleep(0.05)          # widen the window a real connect would open
        bar = SlowBar()
        made.append(bar)
        return bar

    preview = Preview(connect_fn=connect)

    def fetch(display):
        started.wait()
        preview.png(display)

    threads = [threading.Thread(target=fetch, args=(d,)) for d in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(made) == 1, f"connected {len(made)} times; the client leaked"
    assert closed == []


def test_a_failed_client_is_closed_and_not_clobbered():
    from barkeep.preview import BarOffline, Preview

    closed = []

    class DeadBar:
        def screen(self, display):
            raise OSError("usb went away")

        def close(self):
            closed.append(self)

    bars = [DeadBar()]
    preview = Preview(connect_fn=lambda: bars[-1])
    try:
        preview.png(0)
    except BarOffline:
        pass
    assert len(closed) == 1, "the failed client's sockets were not released"
    assert preview._bb is None

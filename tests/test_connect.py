"""Host resolution prefers the deterministic USB address.

connect() and aconnect() were 15 of 16 statements uncovered each, despite
AGENTS.md making them the single choke point every device call goes through
("nothing in apps/ may hardcode a host"). The USB-before-mDNS ordering in
particular encodes a measured hardware gotcha and had nothing holding it.
"""

from __future__ import annotations

import pytest

import busybar_dev


class FakeBar:
    """Duck-types the busylib clients closely enough for resolution."""

    def __init__(self, host, token=None, fail=()):
        self.host, self.token = host, token
        self._fail = fail
        self.closed = False

    def version(self):
        if self.host in self._fail:
            raise ConnectionError(f"no route to {self.host}")
        return "25.0.0"

    async def aversion(self):
        return self.version()

    def close(self):
        self.closed = True

    async def aclose(self):
        self.closed = True


@pytest.fixture
def bars(monkeypatch):
    """Record every client constructed, and which hosts refuse."""
    made: list[FakeBar] = []
    refuse: set[str] = set()

    def factory(host, token=None):
        bar = FakeBar(host, token, fail=refuse)
        made.append(bar)
        return bar

    class AsyncFactory(FakeBar):
        def __init__(self, host, token=None):
            super().__init__(host, token, fail=refuse)
            made.append(self)

        async def version(self):
            return FakeBar.version(self)

    monkeypatch.setattr(busybar_dev, "BusyBar", factory)
    monkeypatch.setattr(busybar_dev, "AsyncBusyBar", AsyncFactory)
    monkeypatch.setattr(busybar_dev, "UsbController", lambda host: ("usb", host))
    monkeypatch.setattr(busybar_dev, "AsyncUsbController",
                        lambda host: ("ausb", host))
    monkeypatch.setattr(busybar_dev, "load_env", lambda *a, **k: {})
    monkeypatch.delenv("BUSYBAR_HOST", raising=False)
    monkeypatch.delenv("BUSYBAR_TOKEN", raising=False)
    return made, refuse


def test_usb_is_tried_before_mdns(bars):
    """busybar.local resolves to BOTH the USB and Wi-Fi addresses when the bar
    is on Wi-Fi with its API disabled, and each request becomes address
    roulette. The ordering is the fix; this is what holds it."""
    made, _ = bars
    bar = busybar_dev.connect()
    assert bar.host == busybar_dev.USB_HOST
    assert [b.host for b in made] == [busybar_dev.USB_HOST]


def test_mdns_is_the_fallback(bars):
    made, refuse = bars
    refuse.add(busybar_dev.USB_HOST)
    bar = busybar_dev.connect()
    assert bar.host == busybar_dev.MDNS_HOST
    assert [b.host for b in made] == [busybar_dev.USB_HOST, busybar_dev.MDNS_HOST]


def test_an_explicit_host_skips_the_candidates(bars):
    made, _ = bars
    bar = busybar_dev.connect(host="198.51.100.9")
    assert bar.host == "198.51.100.9"
    assert len(made) == 1


def test_the_environment_supplies_the_host(bars, monkeypatch):
    monkeypatch.setenv("BUSYBAR_HOST", "198.51.100.10")
    bar = busybar_dev.connect()
    assert bar.host == "198.51.100.10"


def test_an_explicit_argument_outranks_the_environment(bars, monkeypatch):
    monkeypatch.setenv("BUSYBAR_HOST", "198.51.100.10")
    assert busybar_dev.connect(host="198.51.100.11").host == "198.51.100.11"


def test_the_token_reaches_the_client(bars, monkeypatch):
    monkeypatch.setenv("BUSYBAR_TOKEN", "pin-1234")
    assert busybar_dev.connect().token == "pin-1234"


def test_a_failed_candidate_is_closed_not_leaked(bars):
    made, refuse = bars
    refuse.add(busybar_dev.USB_HOST)
    busybar_dev.connect()
    assert made[0].closed is True


def test_every_host_tried_is_named_in_the_error(bars):
    _, refuse = bars
    refuse.update({busybar_dev.USB_HOST, busybar_dev.MDNS_HOST})
    with pytest.raises(ConnectionError) as excinfo:
        busybar_dev.connect()
    message = str(excinfo.value)
    assert busybar_dev.USB_HOST in message
    assert busybar_dev.MDNS_HOST in message


def test_the_usb_controller_rides_the_same_host(bars):
    """busylib's lazy .usb controller defaults to the USB address; the CLI has
    to follow HTTP or a Wi-Fi bar gets telnet on the wrong address."""
    _, refuse = bars
    refuse.add(busybar_dev.USB_HOST)
    bar = busybar_dev.connect()
    assert bar._usb == ("usb", busybar_dev.MDNS_HOST)


async def test_aconnect_resolves_the_same_way(bars):
    made, refuse = bars
    refuse.add(busybar_dev.USB_HOST)
    bar = await busybar_dev.aconnect()
    assert bar.host == busybar_dev.MDNS_HOST
    assert bar._usb == ("ausb", busybar_dev.MDNS_HOST)
    assert made[0].closed is True


async def test_aconnect_reports_every_host(bars):
    _, refuse = bars
    refuse.update({busybar_dev.USB_HOST, busybar_dev.MDNS_HOST})
    with pytest.raises(ConnectionError):
        await busybar_dev.aconnect()

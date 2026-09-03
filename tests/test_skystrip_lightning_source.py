"""Source-boundary contracts for Skystrip's optional live lightning feed."""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
skystrip = pytest.importorskip("skystrip")


WALL_NOW = 1_800_000_000.0
MONOTONIC_NOW = 50_000.0


def test_skystrip_reexports_the_extracted_lightning_boundary():
    import skystrip_lightning

    assert skystrip._LightningStrike is skystrip_lightning.LightningStrike
    assert skystrip._lzw_decode is skystrip_lightning._lzw_decode
    assert skystrip._decode_lightning_payload is (
        skystrip_lightning._decode_lightning_payload
    )
    assert skystrip._parse_lightning_strike is (
        skystrip_lightning.parse_lightning_strike
    )


def test_lightning_module_has_no_runtime_or_device_dependencies():
    import skystrip_lightning

    source_names = set(skystrip_lightning.__dict__)
    assert "asyncio" not in source_names
    assert "httpx" not in source_names
    assert "busylib" not in source_names
    assert "websockets" not in source_names


def _payload(
    *,
    age_s: float = 2.25,
    lat: object = 51.5074,
    lon: object = -0.1278,
    timestamp: object | None = None,
) -> str:
    if timestamp is None:
        timestamp = int(WALL_NOW * 1_000_000_000 - age_s * 1_000_000_000)
    return json.dumps({"time": timestamp, "lat": lat, "lon": lon})


def _lzw_encode(text: str) -> str:
    """Tiny test encoder matching the feed's historical text framing."""
    dictionary = {chr(i): i for i in range(256)}
    size = 256
    word = ""
    codes: list[int] = []
    for char in text:
        candidate = word + char
        if candidate in dictionary:
            word = candidate
            continue
        codes.append(dictionary[word])
        dictionary[candidate] = size
        size += 1
        word = char
    if word:
        codes.append(dictionary[word])
    return "".join(chr(code) for code in codes)


def test_blank_lightning_endpoint_is_the_safe_default():
    assert skystrip._validate_lightning_ws_endpoint(None) is None
    assert skystrip._validate_lightning_ws_endpoint("") is None
    assert skystrip._validate_lightning_ws_endpoint("   ") is None

    import tomllib

    registry = tomllib.loads(
        (Path(__file__).resolve().parents[1] / "apps.toml").read_text()
    )
    assert "SKYSTRIP_LIGHTNING_WS" not in registry["skystrip"]["config"]
    env_example = (
        Path(__file__).resolve().parents[1] / ".env.example"
    ).read_text().splitlines()
    assert "SKYSTRIP_LIGHTNING_WS=" in env_example
    installer = (
        Path(__file__).resolve().parents[1] / "deploy" / "install.sh"
    ).read_text()
    assert 'write_env_line SKYSTRIP_LIGHTNING_WS ""' in installer
    assert "chmod 600 .env" in installer


def test_secure_websocket_endpoint_may_carry_env_only_credentials():
    for endpoint in (
        "wss://relay.example:443/lightning",
        "wss://user:password@relay.example/lightning?token=secret",
        "wss://relay.example/private-capability-path",
    ):
        assert skystrip._validate_lightning_ws_endpoint(endpoint) == endpoint

    for invalid in (
        "ws://relay.example/lightning",
        "https://relay.example/lightning",
        "wss://",
        "wss://relay.example:0/lightning",
        "wss://relay.example:99999/lightning",
        "wss://relay.example/lightning#fragment",
        " wss://relay.example/lightning",
        "wss://relay.example/lightning\n",
    ):
        with pytest.raises(ValueError, match="secure WebSocket URL") as error:
            skystrip._validate_lightning_ws_endpoint(invalid)
        assert invalid not in str(error.value)


def test_invalid_config_fails_closed_without_disclosing_the_value(
    caplog, tmp_path,
):
    secret = "ws://relay.example/lightning?token=do-not-print"
    config = skystrip.parse_runtime_config(
        {"SKYSTRIP_LIGHTNING_WS": secret}, tmp_path,
    )
    assert config.lightning_ws is None
    previous_config = skystrip.RUNTIME_CONFIG
    try:
        with caplog.at_level(logging.ERROR, logger="skystrip"):
            skystrip.apply_runtime_config(config)
    finally:
        skystrip.apply_runtime_config(previous_config)
    assert "live lightning is disabled" in caplog.text
    assert secret not in caplog.text
    assert "do-not-print" not in caplog.text


def test_plain_and_legacy_lzw_strikes_preserve_source_age():
    plain = _payload()
    expected_observed = MONOTONIC_NOW - 2.25
    for raw in (plain.encode(), _lzw_encode(plain)):
        strike = skystrip._parse_lightning_strike(
            raw,
            wall_now=WALL_NOW,
            monotonic_now=MONOTONIC_NOW,
        )
        assert strike.latitude == pytest.approx(51.5074)
        assert strike.longitude == pytest.approx(-0.1278)
        assert strike.observed_at == pytest.approx(expected_observed)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("lat", None),
        ("lat", "51.5074"),
        ("lat", True),
        ("lat", float("nan")),
        ("lat", float("inf")),
        ("lat", 90.0001),
        ("lon", None),
        ("lon", "-0.1278"),
        ("lon", False),
        ("lon", float("-inf")),
        ("lon", -180.0001),
    ],
)
def test_malformed_or_out_of_domain_coordinates_are_rejected(field, value):
    values = {"lat": 51.5074, "lon": -0.1278, field: value}
    with pytest.raises(ValueError, match="coordinates"):
        skystrip._parse_lightning_strike(
            _payload(lat=values["lat"], lon=values["lon"]),
            wall_now=WALL_NOW,
            monotonic_now=MONOTONIC_NOW,
        )


@pytest.mark.parametrize(
    "timestamp",
    [
        None,
        True,
        "1800000000000000000",
        1_800_000_000.0,
        1_800_000_000,
        1_800_000_000_000,
        1 << 80,
    ],
)
def test_timestamp_must_be_a_bounded_integer_nanosecond_epoch(timestamp):
    # Pass an explicit null rather than letting _payload create its default.
    raw = json.dumps({"time": timestamp, "lat": 51.5074, "lon": -0.1278})
    with pytest.raises(ValueError, match="time"):
        skystrip._parse_lightning_strike(
            raw,
            wall_now=WALL_NOW,
            monotonic_now=MONOTONIC_NOW,
        )


def test_stale_and_future_strikes_are_rejected_before_queueing():
    stale = _payload(age_s=skystrip.LIGHTNING_SOURCE_MAX_AGE_S + 0.001)
    future = _payload(age_s=-skystrip.LIGHTNING_SOURCE_FUTURE_SKEW_S - 0.001)
    for raw in (stale, future):
        with pytest.raises(ValueError, match="stale or in the future"):
            skystrip._parse_lightning_strike(
                raw,
                wall_now=WALL_NOW,
                monotonic_now=MONOTONIC_NOW,
            )


@pytest.mark.parametrize(
    ("wall_now", "monotonic_now"),
    [
        (float("inf"), MONOTONIC_NOW),
        (WALL_NOW, float("nan")),
    ],
)
def test_local_clock_inputs_must_be_finite(wall_now, monotonic_now):
    with pytest.raises(ValueError, match="clocks must be finite"):
        skystrip._parse_lightning_strike(
            _payload(),
            wall_now=wall_now,
            monotonic_now=monotonic_now,
        )


def test_lzw_expansion_and_dictionary_work_are_bounded():
    # A, AA, AAA, ... is the classic quadratic expansion path.
    expanding = "A" + "".join(chr(code) for code in range(256, 280))
    with pytest.raises(ValueError, match="budget"):
        skystrip._lzw_decode(
            expanding,
            max_output_chars=32,
            max_entries=1_000,
        )

    with pytest.raises(ValueError, match="budget"):
        skystrip._lzw_decode(
            "A" * 20,
            max_output_chars=1_000,
            max_entries=260,
        )


@pytest.mark.asyncio
async def test_disabled_listener_never_opens_a_connection(monkeypatch):
    import websockets

    def unexpected_connect(*_args, **_kwargs):
        raise AssertionError("disabled lightning attempted a connection")

    monkeypatch.setattr(skystrip, "LIGHTNING_WS", None)
    monkeypatch.setattr(websockets, "connect", unexpected_connect)
    await skystrip.listen_lightning(skystrip.SkyState())


@pytest.mark.asyncio
async def test_listener_uses_bounded_contract_and_enqueues_valid_source_time(
    monkeypatch, caplog,
):
    import websockets

    endpoint = "wss://relay.example/lightning"
    calls: list[tuple[tuple, dict]] = []

    class TestConnection:
        def __init__(self):
            self.sent: list[str] = []
            self.messages = [
                _payload(lat="do-not-print"),
                _payload(age_s=2.0).encode(),
            ]

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def send(self, message):
            self.sent.append(message)

        def __aiter__(self):
            return self

        async def __anext__(self):
            if not self.messages:
                raise StopAsyncIteration
            return self.messages.pop(0)

    connection = TestConnection()

    def connect(*args, **kwargs):
        calls.append((args, kwargs))
        return connection

    async def stop_after_close(_delay):
        raise asyncio.CancelledError

    state = skystrip.SkyState()
    before = asyncio.get_running_loop().time()
    monkeypatch.setattr(skystrip, "LIGHTNING_WS", endpoint)
    monkeypatch.setattr(skystrip, "LAT", 51.5074)
    monkeypatch.setattr(skystrip, "LON", -0.1278)
    monkeypatch.setattr(skystrip.time, "time", lambda: WALL_NOW)
    monkeypatch.setattr(websockets, "connect", connect)
    monkeypatch.setattr(skystrip.asyncio, "sleep", stop_after_close)

    with caplog.at_level(logging.WARNING, logger="skystrip"):
        with pytest.raises(asyncio.CancelledError):
            await skystrip.listen_lightning(state)

    assert calls == [((endpoint,), {
        "open_timeout": 10,
        "max_size": skystrip.LIGHTNING_FRAME_MAX_BYTES,
        "max_queue": skystrip.LIGHTNING_WS_MAX_QUEUE,
        "logger": skystrip.LIGHTNING_TRANSPORT_LOGGER,
    })]
    assert connection.sent == [skystrip.LIGHTNING_SUBSCRIPTION]
    event = state.flash_queue.get_nowait()
    assert event.distance_km == pytest.approx(0.0)
    assert before - 2.1 <= event.observed_at <= before - 1.9
    assert "discarded invalid frame" in caplog.text
    assert "do-not-print" not in caplog.text


@pytest.mark.asyncio
async def test_connection_errors_never_log_the_endpoint_or_exception_text(
    monkeypatch, caplog,
):
    import websockets

    endpoint = "wss://user:password@relay.example/lightning?token=do-not-print"
    secret_exception = "upstream included token=do-not-print"

    class BrokenConnection:
        async def __aenter__(self):
            raise RuntimeError(secret_exception)

        async def __aexit__(self, *_args):
            return False

    def fail_connect(*_args, **_kwargs):
        return BrokenConnection()

    async def stop_after_log(_delay):
        raise asyncio.CancelledError

    monkeypatch.setattr(skystrip, "LIGHTNING_WS", endpoint)
    monkeypatch.setattr(websockets, "connect", fail_connect)
    monkeypatch.setattr(skystrip.asyncio, "sleep", stop_after_log)

    with caplog.at_level(logging.WARNING, logger="skystrip"):
        with pytest.raises(asyncio.CancelledError):
            await skystrip.listen_lightning(skystrip.SkyState())
    assert "RuntimeError" in caplog.text
    assert endpoint not in caplog.text
    assert secret_exception not in caplog.text
    assert "do-not-print" not in caplog.text

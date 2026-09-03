"""Bounded, pure decoder for Skystrip's optional lightning relay.

The runtime WebSocket and display effects stay in ``skystrip.py``. This module
owns only the untrusted wire boundary: decoding, validation, source-time
checks, and the immutable strike value returned after those checks pass.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import math


LIGHTNING_FRAME_MAX_BYTES = 256 * 1024
LIGHTNING_DECODED_MAX_CHARS = 512 * 1024
LIGHTNING_LZW_MAX_ENTRIES = 65_536
LIGHTNING_SOURCE_MAX_AGE_S = 10.0
LIGHTNING_SOURCE_FUTURE_SKEW_S = 5.0


@dataclass(frozen=True)
class LightningStrike:
    latitude: float
    longitude: float
    observed_at: float


def _lzw_decode(
    data: str,
    *,
    max_output_chars: int = LIGHTNING_DECODED_MAX_CHARS,
    max_entries: int = LIGHTNING_LZW_MAX_ENTRIES,
) -> str:
    """Decode the legacy stream framing within fixed memory/work budgets."""
    if (
        not isinstance(data, str)
        or not data
        or len(data) > LIGHTNING_FRAME_MAX_BYTES
        or max_output_chars < 1
        or max_entries < 257
        or ord(data[0]) > 255
    ):
        raise ValueError("invalid bounded LZW payload")
    try:
        if len(data.encode("utf-8")) > LIGHTNING_FRAME_MAX_BYTES:
            raise ValueError("invalid bounded LZW payload")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid bounded LZW payload") from exc
    dict_size = 256
    dictionary = {i: chr(i) for i in range(dict_size)}
    result = [data[0]]
    output_chars = 1
    word = data[0]
    for char in data[1:]:
        code = ord(char)
        if code in dictionary:
            entry = dictionary[code]
        elif code == dict_size:
            entry = word + word[0]
        else:
            raise ValueError("invalid bounded LZW payload")
        output_chars += len(entry)
        if output_chars > max_output_chars or dict_size >= max_entries:
            raise ValueError("bounded LZW payload exceeds its budget")
        result.append(entry)
        dictionary[dict_size] = word + entry[0]
        dict_size += 1
        word = entry
    return "".join(result)


def _decode_lightning_payload(raw: str | bytes) -> dict:
    """Decode one bounded JSON object from an operator-supplied stream.

    Relays may forward ordinary UTF-8 JSON or the historical LZW text framing.
    Supporting the framing is a wire-compatibility statement, not permission
    to connect directly to a provider's restricted raw servers.
    """
    legacy_lzw = isinstance(raw, str)
    if isinstance(raw, str):
        try:
            encoded_size = len(raw.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise ValueError("lightning frame is not valid UTF-8 text") from exc
        if (
            len(raw) > LIGHTNING_FRAME_MAX_BYTES
            or encoded_size > LIGHTNING_FRAME_MAX_BYTES
        ):
            raise ValueError("lightning frame exceeds its budget")
        text = raw
    elif isinstance(raw, (bytes, bytearray, memoryview)):
        payload = bytes(raw)
        if len(payload) > LIGHTNING_FRAME_MAX_BYTES:
            raise ValueError("lightning frame exceeds its budget")
        text = payload.decode("utf-8")
    else:
        raise ValueError("lightning frame must be text or bytes")

    try:
        decoded = json.loads(text)
    except (json.JSONDecodeError, UnicodeError):
        if not legacy_lzw:
            raise ValueError("lightning frame is not JSON") from None
        try:
            decoded = json.loads(_lzw_decode(text))
        except (json.JSONDecodeError, UnicodeError) as exc:
            raise ValueError("lightning frame is not JSON") from exc
    if not isinstance(decoded, dict):
        raise ValueError("lightning frame must contain one object")
    return decoded


def _strict_json_coordinate(value: object, low: float, high: float) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and low <= number <= high else None


def parse_lightning_strike(
    raw: str | bytes,
    *,
    wall_now: float,
    monotonic_now: float,
) -> LightningStrike:
    """Validate a Blitz-compatible strike and map source time to loop time.

    ``time`` is the documented integer count of nanoseconds since the Unix
    epoch. Receipt time is not observation time: carrying the source's real
    age onto the monotonic axis lets the runtime flash lease reject a strike
    that sat in a relay or reconnect backlog.
    """
    if not math.isfinite(wall_now) or not math.isfinite(monotonic_now):
        raise ValueError("local lightning clocks must be finite")
    payload = _decode_lightning_payload(raw)
    latitude = _strict_json_coordinate(payload.get("lat"), -90.0, 90.0)
    longitude = _strict_json_coordinate(payload.get("lon"), -180.0, 180.0)
    epoch_ns = payload.get("time")
    if latitude is None or longitude is None:
        raise ValueError("lightning coordinates are invalid")
    if isinstance(epoch_ns, bool) or not isinstance(epoch_ns, int):
        raise ValueError("lightning time must be integer epoch nanoseconds")
    # A contemporary epoch-nanosecond value fits in signed 64 bits. This
    # rejects seconds/milliseconds and absurd integers before arithmetic.
    if epoch_ns <= 0 or epoch_ns.bit_length() > 63:
        raise ValueError("lightning time is outside the supported epoch")
    seconds, nanoseconds = divmod(epoch_ns, 1_000_000_000)
    source_unix = seconds + nanoseconds / 1_000_000_000
    age = wall_now - source_unix
    if (
        age < -LIGHTNING_SOURCE_FUTURE_SKEW_S
        or age > LIGHTNING_SOURCE_MAX_AGE_S
    ):
        raise ValueError("lightning time is stale or in the future")
    observed_at = monotonic_now - max(0.0, age)
    return LightningStrike(latitude, longitude, observed_at)

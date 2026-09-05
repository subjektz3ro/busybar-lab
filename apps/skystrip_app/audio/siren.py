"""Skystrip audio / siren."""

from __future__ import annotations

import asyncio
import hashlib
import math
import re
import struct

from busylib import exceptions, types

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from busybar_dev.device import storage_file_matches as _storage_file_matches

_SIREN_PCM_CACHE: bytes | None = None

SIREN_FILES = re.compile(
    r"^siren_(?P<digest>[0-9a-f]{16})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$"
)


def siren_pcm() -> bytes:
    """Reproducible s16le/44.1 kHz alarm tone; no untracked device asset.

    The two-tone sweep is intentionally generated from code and content-hashed
    before upload.  It is an attention signal, not a civil-defense siren or a
    copy of a third-party recording.
    """
    global _SIREN_PCM_CACHE
    if _SIREN_PCM_CACHE is not None:
        return _SIREN_PCM_CACHE
    rate = 44_100
    count = rate * _limits.SIREN_SECONDS
    out = bytearray(count * 2)
    phase = 0.0
    two_pi = 2.0 * math.pi
    fade_samples = int(rate * 0.025)
    for index in range(count):
        # Continuous-phase 620–940 Hz triangle sweep every 1.2 seconds.
        sweep = (index / rate / 1.2) % 1.0
        triangle = 1.0 - abs(2.0 * sweep - 1.0)
        frequency = 620.0 + 320.0 * triangle
        phase = (phase + two_pi * frequency / rate) % two_pi
        edge = min(1.0, index / fade_samples, (count - 1 - index) / fade_samples)
        # A gentle 4 Hz tremolo remains unmistakable without clipping.
        tremolo = 0.74 + 0.26 * math.sin(two_pi * 4.0 * index / rate) ** 2
        sample = int(0.26 * 32767 * edge * tremolo * math.sin(phase))
        struct.pack_into("<h", out, index * 2, sample)
    _SIREN_PCM_CACHE = bytes(out)
    return _SIREN_PCM_CACHE


def _siren_name(digest: str, repair: int = 0) -> str:
    if not 0 <= repair <= 0xFF:
        raise RuntimeError("extreme-weather siren repair generations exhausted")
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    return f"siren_{digest}{suffix}.snd"


def _siren_identity(name: str) -> tuple[str, int] | None:
    match = SIREN_FILES.fullmatch(name)
    if match is None:
        return None
    return match.group("digest"), int(match.group("repair") or "0", 16)


def mark_siren_unplayable(state: _model.SkyState, name: str) -> None:
    """Quarantine a definite PLAY-404 and request an immutable successor."""
    identity = _siren_identity(name)
    if identity is None:
        _limits.logger.error("cannot repair unrecognised siren asset %r", name)
        state.siren_file = None
        state.siren_asset_changed.set()
        return
    _digest, repair = identity
    if repair >= 0xFF:
        _limits.logger.critical("extreme-weather siren repair generations exhausted")
        state.siren_file = None
        state.siren_asset_changed.set()
        return
    state.siren_retire.add(name)
    state.siren_ambiguous.discard(name)
    state.siren_repair = max(state.siren_repair, repair + 1)
    if state.siren_file == name:
        state.siren_file = None
    state.siren_asset_changed.set()


async def _siren_listing(bb) -> list[types.StorageListElement] | None:
    try:
        return (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
    except Exception as exc:  # noqa: BLE001 - caller preserves ambiguity
        _limits.logger.debug("could not inspect siren residency: %s", exc)
        return None


def _defer_siren_retirement(state: _model.SkyState, names: set[str]) -> None:
    """Bound old files without deleting one a prior process may still play."""
    if not names:
        return
    due = asyncio.get_running_loop().time() + _limits.SIREN_RETIRE_GRACE_S
    state.siren_retire.update(names)
    for name in names:
        state.siren_retire_after.setdefault(name, due)


async def retire_siren_assets(bb, state: _model.SkyState) -> None:
    """Best-effort retirement after one complete possible playback window."""
    if state.siren_file is None:
        return
    now = asyncio.get_running_loop().time()
    for stale in list(state.siren_retire):
        if stale == state.siren_file:
            state.siren_retire.discard(stale)
            state.siren_retire_after.pop(stale, None)
            continue
        due = state.siren_retire_after.setdefault(
            stale, now + _limits.SIREN_RETIRE_GRACE_S
        )
        if now < due:
            continue
        try:
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{stale}")
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) != 404:
                _limits.logger.debug("siren retirement deferred for %s: %s", stale, exc)
                state.siren_retire_after[stale] = now + _limits.SIREN_PROVISION_RETRY_S
                continue
        except Exception as exc:  # noqa: BLE001 - successor is ready
            _limits.logger.debug("siren retirement deferred for %s: %s", stale, exc)
            state.siren_retire_after[stale] = now + _limits.SIREN_PROVISION_RETRY_S
            continue
        state.siren_retire.discard(stale)
        state.siren_ambiguous.discard(stale)
        state.siren_retire_after.pop(stale, None)


async def audit_siren_assets(bb, state: _model.SkyState) -> None:
    """Discover obsolete generations even if startup listing was unavailable."""
    if state.siren_file is None:
        return
    files = await _siren_listing(bb)
    if files is None:
        return
    owned = {
        entry.name
        for entry in files
        if _siren_identity(getattr(entry, "name", "")) is not None
        and entry.name != state.siren_file
    }
    _defer_siren_retirement(state, owned)


async def ensure_siren_asset(bb, state: _model.SkyState) -> str | None:
    """Adopt or install one verified immutable siren generation.

    A filename match is insufficient: interrupted writes can leave a partial
    file, while PLAY uses 404 for both missing and unplayable content.  Exact
    size is the safe startup admission check; a definite PLAY-404 advances to
    a discoverable ``_rNN`` path rather than overwriting firmware-cached data.
    """
    if state.siren_file is not None:
        return state.siren_file
    async with state.siren_asset_lock:
        if state.siren_file is not None:
            return state.siren_file

        blob = siren_pcm()
        expected_size = len(blob)
        digest = hashlib.sha256(blob).hexdigest()[:16]
        files = await _siren_listing(bb)
        entries: dict[int, types.StorageListElement] = {}
        owned_names: set[str] = set()
        if files is not None:
            for entry in files:
                identity = _siren_identity(getattr(entry, "name", ""))
                if identity is None:
                    continue
                entry_digest, repair = identity
                owned_names.add(entry.name)
                if entry_digest == digest:
                    entries[repair] = entry

            # Resolve uploads whose response was lost before minting another
            # path.  An absent target is safe to retry; an exact resident file
            # is safe to adopt; a wrong-sized one is poison and advances.
            for ambiguous in list(state.siren_ambiguous):
                identity = _siren_identity(ambiguous)
                if identity is None or identity[0] != digest:
                    state.siren_ambiguous.discard(ambiguous)
                    state.siren_retire.add(ambiguous)
                    continue
                repair = identity[1]
                entry = entries.get(repair)
                if entry is None:
                    state.siren_ambiguous.discard(ambiguous)
                elif _storage_file_matches(entry, expected_size):
                    state.siren_ambiguous.discard(ambiguous)
                else:
                    state.siren_ambiguous.discard(ambiguous)
                    state.siren_retire.add(ambiguous)
                    state.siren_repair = max(state.siren_repair, repair + 1)
        elif state.siren_ambiguous:
            # Do not issue another write while the first may have committed
            # and storage cannot tell us.  The lifetime maintainer retries the
            # listing, so a network outage creates at most one ambiguous path.
            return None

        candidates = [
            (repair, entry)
            for repair, entry in entries.items()
            if repair >= state.siren_repair
            and entry.name not in state.siren_retire
            and _storage_file_matches(entry, expected_size)
        ]
        filename: str | None = None
        chosen_repair = state.siren_repair
        if candidates:
            chosen_repair, chosen = max(candidates, key=lambda item: item[0])
            filename = chosen.name
        else:
            chosen_repair = state.siren_repair
            while True:
                filename = _siren_name(digest, chosen_repair)
                if (
                    filename not in state.siren_retire
                    and filename not in state.siren_ambiguous
                    and chosen_repair not in entries
                ):
                    break
                chosen_repair += 1
                if chosen_repair > 0xFF:
                    _limits.logger.critical(
                        "extreme-weather siren repair generations exhausted"
                    )
                    return None

            try:
                await bb.assets_upload(_limits.APP_NAME, filename, blob)
            except Exception as exc:  # noqa: BLE001 - upload may have committed
                verification = await _siren_listing(bb)
                verified = (
                    None
                    if verification is None
                    else next(
                        (
                            entry
                            for entry in verification
                            if getattr(entry, "name", None) == filename
                        ),
                        None,
                    )
                )
                if verified is not None and _storage_file_matches(
                    verified, expected_size
                ):
                    _limits.logger.warning(
                        "adopting %s after an ambiguous upload result", filename
                    )
                else:
                    if verification is None:
                        state.siren_ambiguous.add(filename)
                    elif verified is not None:
                        state.siren_retire.add(filename)
                        state.siren_repair = max(state.siren_repair, chosen_repair + 1)
                    _limits.logger.error(
                        "extreme-weather siren unavailable; will retry: %s", exc
                    )
                    return None

        assert filename is not None
        state.siren_file = filename
        state.siren_repair = chosen_repair
        state.siren_ambiguous.discard(filename)

        # Only a verified successor makes retirement safe.  This also bounds
        # old digest generations left by future changes to the generated tone.
        _defer_siren_retirement(
            state,
            (owned_names | state.siren_ambiguous | state.siren_retire) - {filename},
        )
        await retire_siren_assets(bb, state)

        _limits.logger.info("extreme-weather siren ready: %s", filename)
        state.siren_asset_changed.set()
        _alerts._signal_alert_change(state)
        return filename


async def maintain_siren_asset(bb, state: _model.SkyState) -> None:
    """Retry provisioning for the daemon lifetime and wake on quarantine."""
    while True:
        state.siren_asset_changed.clear()
        if state.siren_file is None:
            await ensure_siren_asset(bb, state)
        await audit_siren_assets(bb, state)
        await retire_siren_assets(bb, state)
        delays: list[float] = [_limits.SIREN_PROVISION_RETRY_S]
        if state.siren_retire_after:
            now = asyncio.get_running_loop().time()
            delays.append(
                max(
                    0.01,
                    min(state.siren_retire_after.values()) - now,
                )
            )
        timeout = min(delays) if delays else None
        try:
            if timeout is None:
                await state.siren_asset_changed.wait()
            else:
                await asyncio.wait_for(state.siren_asset_changed.wait(), timeout)
        except asyncio.TimeoutError:
            pass

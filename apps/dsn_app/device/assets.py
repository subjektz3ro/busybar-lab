"""DSN device / assets."""

from __future__ import annotations

import asyncio
import hashlib
import re
import time

from busylib import exceptions
from PIL import Image

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app.render import events as _render_events
from busybar_dev import anim
from busybar_dev.device import is_refusal as _is_refusal
from busybar_dev.device import storage_file_matches as _storage_file_matches


def _is_asset_path_failure(exc: Exception) -> bool:
    """A definite missing/corrupt animation, not an ownership refusal."""
    status = getattr(exc, "status_code", None)
    detail = str(exc).lower()
    return (
        status == 404
        or status in {400, 422}
        and any(
            word in detail for word in ("asset", "animation", "path", "file", "invalid")
        )
    )


GENERATION_FILES = re.compile(r"^dsn_[A-Za-z0-9_]+\.anim$")

EVENT_FILES = re.compile(r"^dsnevt_[A-Za-z0-9_]+\.anim$")


def event_asset_name(effect: str, blob: bytes) -> str:
    """A content address that also fits the bar's undocumented 31-byte cap."""
    try:
        code = _limits.EVENT_ASSET_CODES[effect]
    except KeyError as exc:
        raise ValueError(f"unknown DSN event effect: {effect}") from exc
    digest = hashlib.sha256(blob).hexdigest()[:10]
    name = f"dsnevt_{_limits.EVENT_ASSET_VERSION}_{code}_{digest}.anim"
    if len(name.encode("ascii")) > _limits.DEVICE_ASSET_FILENAME_MAX:
        raise ValueError(f"event asset filename exceeds device limit: {name}")
    return name


async def sweep_stale_assets(bb) -> None:
    """Reap generations abandoned by a previous process. The in-memory list
    dies with the process, so without this, flash fills up over months."""
    try:
        await bb.display_clear(application_name=_limits.APP_NAME)
    except exceptions.BusyBarAPIError as exc:
        if not _is_refusal(exc):
            _limits.logger.debug("startup clear failed: %s", exc)
    except Exception as exc:  # noqa: BLE001
        _limits.logger.debug("startup clear failed: %s", exc)
    # Display ownership and app-scoped storage are independent. A routine 409
    # from an active focus session must not disable orphan cleanup.
    try:
        files = (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
        stale = [f.name for f in files if GENERATION_FILES.match(f.name)]
        for name in stale:
            try:
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{name}")
            except Exception:  # noqa: BLE001
                pass
        if stale:
            _limits.logger.info("swept %d stale asset generations", len(stale))
    except Exception as exc:  # noqa: BLE001 - hygiene is never fatal
        _limits.logger.warning("asset storage sweep failed: %s", exc)


async def prepare_event_assets(bb, state: _model.State) -> None:
    """Keep the finite live-event vocabulary resident before it is needed.

    Paths are content-addressed and survive a restart. Dynamic scene
    generations are swept separately; these tiny, reusable assets are only
    replaced when their rendered bytes change. No event path calls this
    function, so an acquisition can never stall behind encoding or upload.
    """
    prepared: dict[str, tuple[str, bytes]] = {}
    for effect in _limits.EVENT_EFFECTS:
        frames, fps, hold = _render_events.render_event_frames(effect)
        blob = encode_native_frames(frames, fps, hold)
        name = event_asset_name(effect, blob)
        prepared[effect] = (name, blob)

    try:
        files = (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
        resident_entries = {entry.name: entry for entry in files}
        resident = set(resident_entries)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - upload can still warm the set
        _limits.logger.debug("event asset scan failed: %s", exc)
        resident_entries = {}
        resident = set()

    pending = set(_limits.EVENT_EFFECTS)
    while pending:
        for effect in tuple(
            effect for effect in _limits.EVENT_EFFECTS if effect in pending
        ):
            name, blob = prepared[effect]
            try:
                existing = resident_entries.get(name)
                if existing is not None and not _storage_file_matches(
                    existing, len(blob)
                ):
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{name}"
                    )
                    resident.discard(name)
                    resident_entries.pop(name, None)
                if name not in resident:
                    await bb.assets_upload(_limits.APP_NAME, name, blob)
                state.event_assets[effect] = name
                resident.add(name)
                pending.remove(effect)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - warm in the background
                if await scene_asset_exists(bb, name, len(blob)):
                    state.event_assets[effect] = name
                    resident.add(name)
                    pending.remove(effect)
                else:
                    _limits.logger.debug("event asset %s not warm yet: %s", effect, exc)
        if pending:
            await asyncio.sleep(30)

    expected = {name for name, _ in prepared.values()}
    for stale in sorted(
        name for name in resident if EVENT_FILES.match(name) and name not in expected
    ):
        try:
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{stale}")
        except Exception as exc:  # noqa: BLE001 - bounded cleanup can retry next boot
            _limits.logger.debug("old event asset retained: %s", exc)
    _limits.logger.info(
        "event grammar warm: %d native animations", len(state.event_assets)
    )


async def prepare_handoff_echo_asset(
    bb,
    state: _model.State,
    event: dict,
) -> tuple[tuple, str] | None:
    """Build/reuse one immutable data-specific Three Skies handoff asset."""
    signature = _render_events.handoff_echo_signature(event)
    if signature is None:
        return None
    filename = state.scene_cache.get(signature)
    if filename is not None:
        state.scene_cache.move_to_end(signature)
        return signature, filename

    frames, fps, hold = _render_events.render_handoff_echo_frames(event)
    blob = encode_native_frames(frames, fps, hold)
    filename = next_scene_filename(state)
    try:
        await bb.assets_upload(_limits.APP_NAME, filename, blob)
    except asyncio.CancelledError:
        raise
    except Exception:
        if not await scene_asset_exists(bb, filename, len(blob)):
            raise
        _limits.logger.info("adopted ambiguous handoff echo upload: %s", filename)
    remember_scene_asset(state, signature, filename)
    await trim_scene_cache(bb, state)
    return signature, filename


def remember_scene_asset(state: _model.State, signature: tuple, filename: str) -> None:
    """Own an immutable uploaded asset before any ambiguous draw can fail."""
    previous = state.scene_cache.pop(signature, None)
    state.scene_cache[signature] = filename
    if previous and previous != filename and previous in state.scene_files:
        state.scene_files.remove(previous)
    if filename not in state.scene_files:
        state.scene_files.append(filename)


def next_scene_filename(state: _model.State) -> str:
    """Immutable per-process generation; never overwrite firmware-owned data."""
    state.scene_gen += 1
    return (
        f"dsn_{state.rt_nonce}_{int(time.time()) % 100000:05d}_{state.scene_gen}.anim"
    )


def encode_native_frames(frames: list[Image.Image], fps: int, hold: int) -> bytes:
    """Let the encoder fold identical frames when one display tick is enough."""
    durations = None if hold == 1 else [hold] * len(frames)
    return anim.encode_anim(frames, fps=fps, durations=durations)


async def trim_scene_cache(bb, state: _model.State) -> None:
    """Bound flash use while keeping the active rotation resident."""
    while len(state.scene_cache) > _limits.SCENE_CACHE_MAX:
        signature, filename = next(iter(state.scene_cache.items()))
        if filename == state.last_scene_filename and len(state.scene_cache) > 1:
            state.scene_cache.move_to_end(signature)
            continue
        try:
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
        except Exception as exc:  # noqa: BLE001 - retry on a later accepted draw
            _limits.logger.debug("scene cache cleanup deferred: %s", exc)
            break
        state.scene_cache.pop(signature, None)
        if filename in state.scene_files:
            state.scene_files.remove(filename)


async def scene_asset_exists(bb, filename: str, expected_size: int) -> bool:
    """Adopt an upload whose success response may have been lost."""
    try:
        entries = (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
    except Exception:  # noqa: BLE001 - the original upload error remains truth
        return False
    for entry in entries:
        if entry.name != filename:
            continue
        return _storage_file_matches(entry, expected_size)
    return False


async def discard_scene_asset(
    bb, state: _model.State, signature: tuple, filename: str
) -> None:
    """Forget a definitely missing/corrupt path so retry mints a generation."""
    if state.scene_cache.get(signature) == filename:
        state.scene_cache.pop(signature, None)
    if state.last_scene_filename == filename:
        state.last_scene_filename = None
        state.last_scene_signature = None
    try:
        await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
    except Exception:  # noqa: BLE001 - a missing file is already discarded
        return
    if filename in state.scene_files:
        state.scene_files.remove(filename)

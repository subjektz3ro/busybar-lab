"""Skystrip audio / report assets."""

from __future__ import annotations

import hashlib
import re

from busylib import exceptions

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings


async def _remove_report_asset(
    bb,
    state: _model.SkyState,
    name: str,
    *,
    force: bool = False,
) -> bool:
    """Remove one exact immutable take, deferring paths still in use."""
    if not force and name in {state.report_file, state.audio_path}:
        return False
    state.report_files[:] = [path for path in state.report_files if path != name]
    try:
        await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{name}")
    except exceptions.BusyBarAPIError as exc:
        if getattr(exc, "status_code", None) != 404:
            state.report_retire.add(name)
            return False
    except Exception:  # noqa: BLE001 - retry on the next successful generation
        state.report_retire.add(name)
        return False
    state.report_retire.discard(name)
    return True


async def _retry_report_retirements(bb, state: _model.SkyState) -> None:
    for name in list(state.report_retire):
        if name in {state.report_file, state.audio_path}:
            continue
        await _remove_report_asset(bb, state, name)


REPORT_FILE_RE = re.compile(
    r"^(?P<base>report_[0-9a-f]{12})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$"
)


def report_asset_name(
    text: str,
    *,
    voice: str | None = None,
    repair: int = 0,
) -> str:
    """Stable immutable path for exact words in the configured voice."""
    if not 0 <= repair <= 0xFF:
        raise ValueError(f"invalid report repair generation: {repair}")
    configured_voice = _settings.REPORT_VOICE if voice is None else voice
    digest = hashlib.sha1(f"{configured_voice}\n{text}".encode()).hexdigest()[:12]
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    name = f"report_{digest}{suffix}.snd"
    if len(name.encode("ascii")) > 31:
        raise ValueError(f"report asset filename exceeds device limit: {name}")
    return name


def _report_file_identity(name: str) -> tuple[str, int] | None:
    match = REPORT_FILE_RE.fullmatch(name)
    if match is None:
        return None
    return f"{match.group('base')}.snd", int(match.group("repair") or "0", 16)


def _current_report_asset_name(state: _model.SkyState, text: str) -> str:
    base = report_asset_name(text)
    return report_asset_name(text, repair=state.report_repairs.get(base, 0))


def _mark_report_unplayable(state: _model.SkyState, name: str, text: str) -> None:
    """Quarantine a definite PLAY-404 and advance its immutable successor."""
    expected_base = report_asset_name(text)
    identity = _report_file_identity(name)
    repair = identity[1] if identity is not None and identity[0] == expected_base else 0
    next_repair = max(state.report_repairs.get(expected_base, 0), repair) + 1
    if next_repair > 0xFF:
        raise RuntimeError(f"report repair generations exhausted for {expected_base}")
    state.report_repairs[expected_base] = next_repair
    state.report_retire.add(name)
    if state.report_file == name:
        state.report_file = None
        state.report_text = None


async def _publish_report_take(
    bb,
    state: _model.SkyState,
    text: str,
    fname: str,
) -> None:
    """Publish one take and keep current, predecessor, and a live PLAY path."""
    prev = state.report_file
    state.report_text = text
    state.report_file = fname
    for name in (prev, fname):
        if name and name not in state.report_files:
            state.report_files.append(name)

    # A corrupt predecessor is safe to retire only after its immutable repair
    # successor is resident. Failed deletion must not block that successor.
    await _retry_report_retirements(bb, state)

    protected = {state.report_file, prev, state.audio_path}
    protected.discard(None)
    limit = 3 if len(protected) == 3 else 2
    while len(state.report_files) > limit:
        stale = next(
            (name for name in state.report_files if name not in protected), None
        )
        if stale is None:
            break
        if not await _remove_report_asset(bb, state, stale):
            break


async def _adopt_report_take(
    bb,
    state: _model.SkyState,
    text: str,
) -> str | None:
    """Adopt the newest resident deterministic take before synthesising."""
    async with state.report_asset_lock:
        if state.report_file is not None and state.report_text == text:
            return state.report_file
        base = report_asset_name(text)
        try:
            entries = (
                await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")
            ).list
        except Exception as exc:  # noqa: BLE001 - cold cache is ordinary
            _limits.logger.debug("report cache scan failed: %s", exc)
            return None

        candidates: list[tuple[int, str, int]] = []
        for entry in entries:
            identity = _report_file_identity(getattr(entry, "name", ""))
            if identity is None:
                continue
            name = entry.name
            if name not in state.report_files:
                state.report_files.append(name)
            if identity[0] != base:
                continue
            repair = identity[1]
            size = int(getattr(entry, "size", 0) or 0)
            expected = state.report_expected_sizes.get(name)
            if size <= 0 or (expected is not None and size != expected):
                state.report_repairs[base] = max(
                    state.report_repairs.get(base, 0), repair + 1
                )
                state.report_retire.add(name)
                continue
            candidates.append((repair, name, size))

        minimum = state.report_repairs.get(base, 0)
        usable = [item for item in candidates if item[0] >= minimum]
        if not usable:
            return None
        repair, fname, _ = max(usable)
        state.report_repairs[base] = repair
        state.report_retire.discard(fname)
        state.report_expected_sizes.pop(fname, None)
        for other_repair, other, _ in candidates:
            if other != fname and other_repair <= repair:
                state.report_retire.add(other)
        await _publish_report_take(bb, state, text, fname)
        _limits.logger.info("adopted resident report take %s", fname)
        return fname


async def _ensure_report_take(
    bb,
    state: _model.SkyState,
    text: str,
    snd: bytes,
) -> tuple[str, bool]:
    """Publish one immutable take and retain a bounded safe predecessor."""
    async with state.report_asset_lock:
        if state.report_file is not None and state.report_text == text:
            return state.report_file, False
        fname = _current_report_asset_name(state, text)
        if fname in state.report_retire:
            # This exact name has an unresolved lost-upload result. Never
            # overwrite it; resolve/remove it before attempting the same path.
            await _remove_report_asset(bb, state, fname, force=True)
            if fname in state.report_retire:
                raise RuntimeError(
                    f"ambiguous report upload is still unresolved: {fname}"
                )
        # A lost upload response can mean the immutable file committed. Track
        # its exact expected size before POST so a later scan can safely adopt.
        state.report_retire.add(fname)
        state.report_expected_sizes[fname] = len(snd)
        try:
            await bb.assets_upload(_limits.APP_NAME, fname, snd)
        except exceptions.BusyBarAPIError:
            state.report_retire.discard(fname)  # definite API rejection
            state.report_expected_sizes.pop(fname, None)
            raise
        state.report_retire.discard(fname)
        state.report_expected_sizes.pop(fname, None)
        await _publish_report_take(bb, state, text, fname)
        return fname, True

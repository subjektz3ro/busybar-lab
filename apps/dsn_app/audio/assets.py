"""DSN audio / assets."""

from __future__ import annotations

import hashlib
import re
from dataclasses import replace

from busylib import exceptions

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app.audio import policy as _audio_policy
from apps.dsn_app.audio import worker as _audio_worker
from busybar_dev.device import storage_file_matches as _storage_file_matches


def speech_name(text: str, voice: str | None = None, *, repair: int = 0) -> str:
    """Filename derived from the line AND the voice reading it.

    The firmware caches assets by path forever, which is usually a trap — here
    it is the whole point. Identical text in the same voice means an identical
    file, so a hit needs no upload at all and survives a restart. Nothing is
    ever overwritten, so the 508 'file is open' trap cannot fire either.

    The voice belongs in the key: without it, changing DSN_VOICE would keep
    serving lines an old narrator recorded, for as long as the cache held them.
    """
    voice = voice or _settings.VOICE
    keyed = f"{voice}\n{text}"
    if not 0 <= repair <= 0xFF:
        raise ValueError(f"invalid speech repair generation: {repair}")
    suffix = "" if repair == 0 else f"_r{repair:02x}"
    name = (
        f"v2_{_voice_tag(voice)}_"
        f"{hashlib.sha1(keyed.encode()).hexdigest()[:10]}{suffix}.snd"
    )
    if len(name.encode("ascii")) > _limits.DEVICE_ASSET_FILENAME_MAX:
        raise ValueError(f"speech asset filename exceeds device limit: {name}")
    return name


def _voice_tag(voice: str) -> str:
    """The voice, flattened into something safe for a filename.

    It goes in the *name* and not just the hash so that a voice change is
    visible on the device: startup can spot lines the previous narrator
    recorded and reclaim the flash instead of stranding ten of them.
    """
    flat = re.sub(r"[^a-z0-9]", "", voice.lower()) or "default"
    # Configurable voice identifiers can exceed the bar's 31-byte filename
    # ceiling, so keep a readable prefix and hash the remainder. Reserve room for the
    # immutable corruption-repair suffix; short common voices retain their
    # established base cache paths exactly.
    fixed = len("v2__") + 10 + len("_rff.snd")
    tag_max = _limits.DEVICE_ASSET_FILENAME_MAX - fixed
    if len(flat) <= tag_max:
        return flat
    digest = hashlib.sha1(flat.encode()).hexdigest()[:6]
    return flat[: tag_max - len(digest)] + digest


# v2: the prefix is a cache generation. Bumping it makes every line baked by
# the older, ungated path unrecognisable, and the sweep below reclaims them.
VOICE_FILES = re.compile(
    r"^v2_(?P<voice>[a-z0-9]+)_(?P<digest>[0-9a-f]{10})"
    r"(?:_r(?P<repair>[0-9a-f]{2}))?\.snd$"
)


def _speech_file_identity(name: str) -> tuple[str, int] | None:
    """Return the stable base path and immutable repair generation."""
    match = VOICE_FILES.fullmatch(name)
    if match is None:
        return None
    base = f"v2_{match.group('voice')}_{match.group('digest')}.snd"
    repair = int(match.group("repair") or "0", 16)
    return base, repair


def speech_asset_name(state: _model.State, text: str) -> str:
    """Newest known immutable device path for this exact voice and text."""
    base = speech_name(text)
    return speech_name(text, repair=state.speech_repairs.get(base, 0))


def mark_speech_unplayable(state: _model.State, name: str) -> None:
    """Quarantine a PLAY-404 path and mint its immutable successor.

    Returns nothing. It was annotated `-> str` and returned the repair
    generation, an int. The only caller discarded it, so nothing broke — but
    the name reads like "gives me the repaired path", and the next caller to
    believe the annotation would have got an integer where a filename goes.
    """
    identity = _speech_file_identity(name)
    if identity is None:
        raise ValueError(f"unrecognised speech asset: {name}")
    base, repair = identity
    next_repair = max(state.speech_repairs.get(base, 0), repair) + 1
    if next_repair > 0xFF:
        raise RuntimeError(f"speech repair generations exhausted for {base}")
    state.speech_repairs[base] = next_repair
    state.speech.pop(name, None)
    state.speech_retire.add(name)


def touch_speech(state: _model.State, name: str) -> float:
    """Mark a cached line as just used, and return its duration.

    Dicts keep insertion order, so re-inserting at the end is what makes
    `next(iter(...))` in trim_speech_cache the *least recently used* entry
    rather than merely the oldest baked one.
    """
    seconds = state.speech.pop(name)
    state.speech[name] = seconds
    return seconds


async def ensure_speech(bb, state: _model.State, text: str) -> tuple[str, float] | None:
    """Bake `text` to a device asset if it isn't already there.

    Serialised: kokoro runs at roughly 1x realtime, and two synths racing on a
    Pi just makes both late.
    """
    name = speech_asset_name(state, text)
    if name in state.speech:
        return name, touch_speech(state, name)
    async with state.synth:
        # A PLAY 404 can advance the repair generation while this bake waits.
        # Re-resolve under the same serialization used for uploads.
        name = speech_asset_name(state, text)
        if name in state.speech:  # baked while we queued
            return name, touch_speech(state, name)
        try:
            pcm = await _audio_worker.synth_off_loop(text)
        except Exception as exc:  # noqa: BLE001 - a missing voice is not fatal
            _limits.logger.warning("synth failed (%s): %s", _settings.VOICE, exc)
            return None
        seconds = len(pcm) / 2 / 44100  # s16le mono 44.1k
        try:
            await bb.assets_upload(_limits.APP_NAME, name, pcm)
        except Exception:
            # An upload timeout is ambiguous: the device may have committed
            # the deterministic path even though the response was lost. Adopt
            # only an exact-size file; otherwise re-raise instead of trying to
            # overwrite a path the firmware may still own.
            try:
                files = (
                    await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")
                ).list
                existing = next((entry for entry in files if entry.name == name), None)
            except Exception:  # noqa: BLE001 - preserve the original failure
                existing = None
            if existing is None or not _storage_file_matches(existing, len(pcm)):
                raise
            _limits.logger.warning("adopting %s after an ambiguous upload result", name)
        state.speech[name] = seconds
        _limits.logger.info(
            "baked %s in %s (%.1fs of audio)", name, _settings.VOICE, seconds
        )
        # Only after the successor is resident is it safe to retire paths that
        # the device reported missing or unplayable. A failed removal leaves
        # an immutable orphan, not a broken current cache entry; startup will
        # try the retirement again after rediscovering the repair generation.
        for old in list(state.speech_retire):
            try:
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{old}")
            except exceptions.BusyBarAPIError as exc:
                if getattr(exc, "status_code", None) != 404:
                    _limits.logger.debug(
                        "speech retirement deferred for %s: %s", old, exc
                    )
                    continue
            except Exception as exc:  # noqa: BLE001 - successor is usable
                _limits.logger.debug("speech retirement deferred for %s: %s", old, exc)
                continue
            state.speech_retire.discard(old)
        await trim_speech_cache(bb, state)
        return name, seconds


async def trim_speech_cache(bb, state: _model.State) -> None:
    """Bounded, least recently used first — the device's flash is far smaller
    than this cache would like to be.

    It used to evict in insertion order on the grounds that this was "close
    enough" to least-used, and it is not: a spacecraft whose data rate churns
    mints a new line every pass, so the line you press START on most often was
    the oldest entry and went first, while the noise that displaced it stayed.
    `touch_speech` on every cache hit is the other half of this.
    """
    while len(state.speech) > _limits.SPEECH_CACHE_MAX:
        protected = {
            speech_asset_name(state, text) for text in state.narration_texts.values()
        }
        # If a corrupt ancestor could not be retired, its repaired successor
        # is the only durable record that the base path is poisoned. Evicting
        # that successor would make a restart adopt the corrupt base again.
        blocked_bases = {
            identity[0]
            for name in state.speech_retire
            if (identity := _speech_file_identity(name)) is not None
        }

        def evictable(name: str) -> bool:
            identity = _speech_file_identity(name)
            return identity is None or identity[0] not in blocked_bases  # noqa: B023 - consumed by the next() calls in this same iteration

        old = next(
            (
                name
                for name in state.speech
                if name not in protected and evictable(name)
            ),
            None,
        )
        if old is None:
            # Preserve the previous hard bound when only ordinary active
            # scripts are protected, but fail safe if every candidate is the
            # sole repaired successor of an unretired corrupt path.
            old = next((name for name in state.speech if evictable(name)), None)
        if old is None:
            _limits.logger.warning(
                "voice cache temporarily exceeds its bound while corrupt "
                "ancestors await retirement (%d entries, bound %d)",
                len(state.speech),
                _limits.SPEECH_CACHE_MAX,
            )
            break
        try:
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{old}")
        except exceptions.BusyBarAPIError as exc:
            if getattr(exc, "status_code", None) != 404:
                _limits.logger.warning(
                    "voice cache trim deferred (%d entries, bound %d): %s",
                    len(state.speech),
                    _limits.SPEECH_CACHE_MAX,
                    exc,
                )
                break
        except Exception as exc:  # noqa: BLE001
            # Keep the mapping: removing it would make the next deterministic
            # bake try to overwrite a file that may still exist and be open.
            #
            # A live device was found holding 49 v2_ files against a bound of
            # 48. These deferral paths are the likeliest explanation and were
            # silent, so the next occurrence could not be told apart from the
            # documented corrupt-ancestor case above. Now it says which.
            _limits.logger.warning(
                "voice cache trim deferred (%d entries, bound %d): %s",
                len(state.speech),
                _limits.SPEECH_CACHE_MAX,
                exc,
            )
            break
        state.speech.pop(old, None)


async def load_speech_cache(bb, state: _model.State) -> None:
    """Adopt voice lines a previous run left on the device.

    Durations are unknown until something plays, so they start at 0 and the
    hold falls back to a fixed guess; the text hash still saves the synth.

    Adopted lines start **cold**, in whatever order the device lists them: use
    order cannot be recovered, because StorageListElement carries only type,
    name and size — there is no mtime to seed
    it from. So the first rotation after a restart re-establishes the LRU
    order, and until it does an adopted line may be evicted ahead of a
    freshly-baked one. That costs one re-synth, not a wrong narration.
    """
    try:
        files = (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
        mine = _voice_tag(_settings.VOICE)
        strangers = 0
        versions: dict[str, list[tuple[int, str, int]]] = {}
        retire: list[str] = []
        for entry in files:
            kind = getattr(
                getattr(entry, "type", None), "value", getattr(entry, "type", None)
            )
            if str(kind).lower() != "file":
                continue
            if not (
                entry.name.startswith(("voice_", "v2_")) and entry.name.endswith(".snd")
            ):
                continue
            match = VOICE_FILES.fullmatch(entry.name)
            if match is None or match.group("voice") != mine:
                # A previous narrator's work, or a name from an older scheme.
                # Either way nothing will ever ask for it again, so it would
                # sit on flash until the end of time. Anything we cannot
                # recognise as ours is reclaimable — that keeps the next
                # change of naming from stranding a cache too.
                strangers += 1
                try:
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{entry.name}"
                    )
                except Exception:  # noqa: BLE001
                    pass
                continue
            size = getattr(entry, "size", 0) or 0
            identity = _speech_file_identity(entry.name)
            if identity is None:
                retire.append(entry.name)
                continue
            base, repair = identity
            if size <= 0:
                if repair < 0xFF:
                    state.speech_repairs[base] = max(
                        state.speech_repairs.get(base, 0), repair + 1
                    )
                retire.append(entry.name)
                continue
            versions.setdefault(base, []).append((repair, entry.name, size))
        for base, candidates in versions.items():
            repair, name, size = max(candidates)
            if state.speech_repairs.get(base, 0) > repair:
                # A higher generation was present but invalid. Older valid
                # bytes cannot become current again after that quarantine.
                retire.extend(candidate_name for _, candidate_name, _ in candidates)
                continue
            if repair:
                state.speech_repairs[base] = max(
                    state.speech_repairs.get(base, 0), repair
                )
            state.speech[name] = size / 2 / 44100
            retire.extend(
                candidate_name
                for _, candidate_name, _ in candidates
                if candidate_name != name
            )
        for name in retire:
            try:
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{name}")
            except Exception:  # noqa: BLE001 - newest generation remains usable
                state.speech_retire.add(name)
            else:
                state.speech_retire.discard(name)
        if state.speech:
            _limits.logger.info(
                "adopted %d cached lines in %s", len(state.speech), _settings.VOICE
            )
        if strangers:
            _limits.logger.info("dropped %d lines from a previous voice", strangers)
        await trim_speech_cache(bb, state)
    except Exception as exc:  # noqa: BLE001 - an empty cache is fine
        _limits.logger.debug("voice cache scan failed: %s", exc)
    finally:
        state.speech_cache_ready = True
        request = state.narration_request
        if request is not None and request.key in state.narration_texts:
            current_name = speech_asset_name(state, state.narration_texts[request.key])
            if current_name != request.name:
                request = replace(request, name=current_name)
                state.narration_request = request
        if (
            request is not None
            and request.name is not None
            and request.name in state.speech
        ):
            if state.narration_priority == request.key:
                state.narration_priority = None
            _audio_policy.finish_narration_request(
                state, request, _limits.NARRATION_READY
            )

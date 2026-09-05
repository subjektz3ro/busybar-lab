from __future__ import annotations

import asyncio
import hashlib
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from busylib import exceptions


sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app.audio import output as sky_audio_output
from apps.skystrip_app.audio import siren as sky_audio_siren


def test_generated_siren_is_deterministic_bar_pcm_without_clipping():
    first = sky_audio_siren.siren_pcm()
    second = sky_audio_siren.siren_pcm()

    assert first is second
    assert len(first) == sky_limits.SIREN_SECONDS * 44_100 * 2
    samples = struct.unpack(f"<{len(first) // 2}h", first)
    peak = max(abs(sample) for sample in samples)
    assert 0.20 * 32_767 <= peak <= 0.27 * 32_767
    assert samples[0] == samples[-1] == 0


@pytest.mark.asyncio
async def test_siren_upload_is_content_addressed_and_reused_per_process():
    class Bar:
        def __init__(self) -> None:
            self.uploads: list[tuple[str, str, bytes]] = []

        async def storage_list(self, _path: str):
            return SimpleNamespace(list=[])

        async def assets_upload(self, application_name: str, name: str, blob: bytes):
            self.uploads.append((application_name, name, blob))

    blob = sky_audio_siren.siren_pcm()
    expected = f"siren_{hashlib.sha256(blob).hexdigest()[:16]}.snd"
    state = sky_model.SkyState()
    bar = Bar()

    assert await sky_audio_siren.ensure_siren_asset(bar, state) == expected
    assert bar.uploads == [(sky_limits.APP_NAME, expected, blob)]
    assert await sky_audio_siren.ensure_siren_asset(bar, state) == expected
    assert len(bar.uploads) == 1


class StorageBar:
    def __init__(self, files: dict[str, bytes] | None = None) -> None:
        self.files = dict(files or {})
        self.operations: list[tuple[str, str]] = []
        self.fail_uploads = 0

    async def storage_list(self, _path: str):
        return SimpleNamespace(list=[
            SimpleNamespace(type="file", name=name, size=len(blob))
            for name, blob in self.files.items()
        ])

    async def assets_upload(self, _application_name: str, name: str, blob: bytes):
        self.operations.append(("upload", name))
        if self.fail_uploads:
            self.fail_uploads -= 1
            raise RuntimeError("transient upload failure")
        if name in self.files:
            raise exceptions.BusyBarAPIError(
                "failed to open file for writing", status_code=508)
        self.files[name] = blob

    async def storage_remove(self, path: str):
        name = Path(path).name
        self.operations.append(("remove", name))
        self.files.pop(name, None)


@pytest.mark.asyncio
async def test_wrong_sized_resident_siren_gets_new_immutable_repair_path(
        monkeypatch):
    assert sky_limits.SIREN_RETIRE_GRACE_S >= sky_limits.SIREN_SECONDS
    monkeypatch.setattr(sky_limits, "SIREN_RETIRE_GRACE_S", 0.01)
    blob = sky_audio_siren.siren_pcm()
    digest = hashlib.sha256(blob).hexdigest()[:16]
    base = f"siren_{digest}.snd"
    repaired = f"siren_{digest}_r01.snd"
    old_digest = "siren_0000000000000000.snd"
    bar = StorageBar({base: b"partial", old_digest: b"old tone"})
    state = sky_model.SkyState()

    assert await sky_audio_siren.ensure_siren_asset(bar, state) == repaired
    assert bar.files[repaired] == blob
    # The old path may still be held by audio started by a crashed process.
    # Provisioning the successor must not delete it in the same operation.
    assert base in bar.files and old_digest in bar.files
    assert not [operation for operation in bar.operations
                if operation[0] == "remove"]

    await asyncio.sleep(0.02)
    await sky_audio_siren.retire_siren_assets(bar, state)
    assert base not in bar.files
    assert old_digest not in bar.files
    assert bar.operations.index(("upload", repaired)) < \
        bar.operations.index(("remove", base))
    assert bar.operations.index(("upload", repaired)) < \
        bar.operations.index(("remove", old_digest))


@pytest.mark.asyncio
async def test_play_404_is_noncommitting_and_repairs_without_global_stop(
        monkeypatch):
    monkeypatch.setattr(sky_limits, "SIREN_RETIRE_GRACE_S", 0.01)
    blob = sky_audio_siren.siren_pcm()
    digest = hashlib.sha256(blob).hexdigest()[:16]
    base = f"siren_{digest}.snd"
    repaired = f"siren_{digest}_r01.snd"
    state = sky_model.SkyState(siren_file=base)

    class UnplayableBar(StorageBar):
        async def audio_play(self, *, application_name: str, path: str):
            raise exceptions.BusyBarAPIError("unplayable", status_code=404)

    bar = UnplayableBar({base: blob})
    with pytest.raises(exceptions.BusyBarAPIError):
        await sky_audio_output._play_audio(bar, state, base, "alert", lambda: True)
    assert state.audio_owner is None
    assert state.audio_stop_pending is False

    sky_audio_siren.mark_siren_unplayable(state, base)
    assert await sky_audio_siren.ensure_siren_asset(bar, state) == repaired
    assert repaired in bar.files and base in bar.files
    await asyncio.sleep(0.02)
    await sky_audio_siren.retire_siren_assets(bar, state)
    assert base not in bar.files


@pytest.mark.asyncio
async def test_daemon_retries_transient_siren_provision_failure(monkeypatch):
    # This contract measures retry scheduling, not first-time PCM generation.
    # Warm the deterministic blob so the test remains valid in isolation.
    sky_audio_siren.siren_pcm()
    bar = StorageBar()
    bar.fail_uploads = 1
    state = sky_model.SkyState()
    monkeypatch.setattr(sky_limits, "SIREN_PROVISION_RETRY_S", 0.01)

    # Production attempts once during startup, which also generates/caches the
    # PCM before the lifetime maintainer starts.
    assert await sky_audio_siren.ensure_siren_asset(bar, state) is None
    assert await sky_audio_siren.ensure_siren_asset(bar, state) is not None
    assert [op for op, _name in bar.operations].count("upload") == 2

    retry_state = sky_model.SkyState()
    attempts = 0
    provisioned = asyncio.Event()

    async def provision(_bar, current):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return None
        current.siren_file = "siren_retry_test.snd"
        provisioned.set()
        return current.siren_file

    monkeypatch.setattr(sky_audio_siren, "ensure_siren_asset", provision)
    maintainer = asyncio.create_task(
        sky_audio_siren.maintain_siren_asset(StorageBar(), retry_state))
    try:
        await asyncio.wait_for(provisioned.wait(), 0.3)
        assert retry_state.siren_file == "siren_retry_test.snd"
        assert attempts == 2
    finally:
        maintainer.cancel()
        await asyncio.gather(maintainer, return_exceptions=True)

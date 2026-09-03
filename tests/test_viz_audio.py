"""Headerless PCM mistakes are silent; the audio inspector makes them loud."""

from __future__ import annotations

import json
import math
import struct
from pathlib import Path

import pytest
from PIL import Image

from busybar_viz import cli
from busybar_viz.audio import inspect_snd, load_samples

SAMPLE_RATE = 44100


def _sine(path: Path, *, seconds: float = 0.5, amplitude: float = 0.5) -> None:
    count = int(SAMPLE_RATE * seconds)
    values = (
        int(amplitude * 32767 * math.sin(2 * math.pi * 440 * i / SAMPLE_RATE))
        for i in range(count)
    )
    path.write_bytes(b"".join(struct.pack("<h", v) for v in values))


def test_inspect_reports_duration_levels_and_identity(tmp_path):
    (tmp_path / "AGENTS.md").write_text("# fixture\n")
    asset = tmp_path / "voice.snd"
    _sine(asset, seconds=0.5, amplitude=0.5)

    result = inspect_snd(asset, repo_root=tmp_path)
    assert result["path"] == "voice.snd"
    assert result["sample_rate"] == SAMPLE_RATE
    assert result["duration_s"] == pytest.approx(0.5, abs=0.001)
    assert result["peak_fraction"] == pytest.approx(0.5, abs=0.01)
    assert 0.3 < result["rms_fraction"] < 0.4  # sine RMS = peak / sqrt(2)
    assert result["clipped_samples"] == 0
    assert result["silent"] is False
    assert len(result["source_sha256"]) == 64


def test_inspect_flags_silence_clipping_and_truncation(tmp_path):
    silent = tmp_path / "silent.snd"
    silent.write_bytes(b"\x00\x00" * 100)
    assert inspect_snd(silent, repo_root=tmp_path)["silent"] is True

    clipped = tmp_path / "clipped.snd"
    clipped.write_bytes(struct.pack("<4h", 32767, -32768, 32767, 0))
    assert inspect_snd(clipped, repo_root=tmp_path)["clipped_samples"] == 3

    truncated = tmp_path / "truncated.snd"
    truncated.write_bytes(b"\x00\x00\x00")
    with pytest.raises(ValueError, match="odd byte length"):
        load_samples(truncated, repo_root=tmp_path)

    wrong = tmp_path / "voice.wav"
    wrong.write_bytes(b"\x00\x00")
    with pytest.raises(ValueError, match="only .snd"):
        load_samples(wrong, repo_root=tmp_path)


def test_waveform_png_draws_the_envelope(tmp_path):
    asset = tmp_path / "voice.snd"
    _sine(asset, seconds=0.1, amplitude=0.9)
    out = tmp_path / "wave.png"

    result = inspect_snd(asset, repo_root=tmp_path, waveform=out)
    assert result["waveform_path"] == str(out)
    image = Image.open(out)
    assert image.size == (720, 160)
    lit = sum(
        1 for x in range(image.width) for y in range(image.height)
        if image.getpixel((x, y)) == (120, 190, 255)
    )
    assert lit > 720  # a 0.9 sine paints tall columns, not a flat line


def test_audio_cli_round_trip(tmp_path, capsys, monkeypatch):
    (tmp_path / "AGENTS.md").write_text("# fixture\n")
    (tmp_path / "apps").mkdir()
    monkeypatch.setattr(cli, "find_repo_root", lambda: tmp_path)
    asset = tmp_path / "voice.snd"
    _sine(asset)

    code = cli.main((
        "audio", str(asset),
        "--waveform", str(tmp_path / "wave.png"), "--json",
    ))
    result = json.loads(capsys.readouterr().out)
    assert code == 0
    assert result["duration_s"] == pytest.approx(0.5, abs=0.001)
    assert Path(result["waveform_path"]).is_file()

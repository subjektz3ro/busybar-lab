"""SKYSTRIP_TTS_SPEED must never crash speech from inside the synth thread."""

from __future__ import annotations

import logging

from busybar_dev import tts


def test_tts_speed_defaults_to_realtime(monkeypatch):
    monkeypatch.delenv("SKYSTRIP_TTS_SPEED", raising=False)

    assert tts.tts_speed() == 1.0


def test_tts_speed_honors_a_valid_value(monkeypatch):
    monkeypatch.setenv("SKYSTRIP_TTS_SPEED", "1.3")

    assert tts.tts_speed() == 1.3


def test_tts_speed_survives_garbage_with_a_warning(monkeypatch, caplog):
    """A bad value used to raise ValueError inside the synth thread, where it
    reads as a Kokoro bug and silently kills the spoken report."""
    monkeypatch.setenv("SKYSTRIP_TTS_SPEED", "fast")

    with caplog.at_level(logging.WARNING):
        speed = tts.tts_speed()

    assert speed == 1.0
    assert "SKYSTRIP_TTS_SPEED" in caplog.text

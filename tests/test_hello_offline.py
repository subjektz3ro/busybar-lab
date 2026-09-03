"""The README's first command must not greet a missing bar with a traceback."""

from __future__ import annotations

import sys

import pytest

from apps import hello


def test_unreachable_bar_is_a_clear_message_not_a_traceback(monkeypatch):
    def refuse():
        raise ConnectionError(
            "Could not reach the BUSY Bar. Is it plugged in over USB?")

    monkeypatch.setattr(hello, "connect", refuse)
    monkeypatch.setattr(sys, "argv", ["hello.py"])

    with pytest.raises(SystemExit) as excinfo:
        hello.main()

    message = str(excinfo.value)
    assert "Could not reach the BUSY Bar" in message
    assert "--dry-run" in message, "must point at the offline path"

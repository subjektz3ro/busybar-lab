"""A service log should carry news, not the same non-event every few minutes."""
from __future__ import annotations

import logging

from busybar_dev import tts


def _busylib_error(message: str) -> logging.LogRecord:
    return logging.LogRecord(
        "busylib.client.base", logging.ERROR, __file__, 1,
        "API error %s request_id=%s: %s (body=%s)",
        (None, "abc123", message, '{"error":"%s"}' % message), None,
    )


def test_kokoro_synthesis_quiets_phonemizer_word_count_chatter():
    logging.getLogger("phonemizer").setLevel(logging.NOTSET)
    tts.quiet_phonemizer()
    assert logging.getLogger("phonemizer").getEffectiveLevel() == logging.ERROR


def test_nothing_to_stop_is_not_an_error_worth_echoing():
    from busybar_dev.config import ExpectedDeviceReplyFilter

    quiet = ExpectedDeviceReplyFilter()
    assert quiet.filter(_busylib_error("No audio is playing")) is False
    assert quiet.filter(_busylib_error("Not drawn due to low priority")) is True
    unrelated = logging.LogRecord("skystrip", logging.ERROR, __file__, 1,
                                  "No audio is playing", (), None)
    assert quiet.filter(unrelated) is True  # only the library's echo is muted


def test_skystrip_logging_setup_installs_both_backstops():
    from apps.skystrip_app import cli
    from busybar_dev.config import CoordinateRedactingFilter, ExpectedDeviceReplyFilter

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    cli.configure_logging()
    assert any(
        isinstance(f, CoordinateRedactingFilter)
        for handler in root.handlers for f in handler.filters
    )
    assert any(
        isinstance(f, ExpectedDeviceReplyFilter)
        for f in logging.getLogger("busylib.client.base").filters
    )
    assert logging.getLogger("busylib").level == logging.WARNING

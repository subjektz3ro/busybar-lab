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
    # phonemizer's own import sets its logger to WARNING; the quieting has
    # to win after that, which is why _kokoro_synth calls it before create().
    logging.getLogger("phonemizer").setLevel(logging.WARNING)
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
    malformed = logging.LogRecord("busylib.client.base", logging.ERROR, __file__, 1,
                                  "API error %s %s", ("only-one-arg",), None)
    assert quiet.filter(malformed) is True  # never break logging


def test_skystrip_logging_setup_installs_both_backstops_on_the_root_handlers():
    from apps.skystrip_app import cli
    from busybar_dev.config import CoordinateRedactingFilter, ExpectedDeviceReplyFilter

    root = logging.getLogger()
    saved = (list(root.handlers), root.level,
             logging.getLogger("busylib").level, logging.getLogger("httpx").level)
    try:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        cli.configure_logging()
        installed = [f for handler in root.handlers for f in handler.filters]
        assert any(isinstance(f, CoordinateRedactingFilter) for f in installed)
        # On the handlers, not on one busylib logger: logger-level filters do
        # not apply to child loggers, so a library refactor would bypass it.
        assert any(isinstance(f, ExpectedDeviceReplyFilter) for f in installed)
        assert logging.getLogger("busylib").level == logging.WARNING
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved[0]:
            root.addHandler(handler)
        root.setLevel(saved[1])
        logging.getLogger("busylib").setLevel(saved[2])
        logging.getLogger("httpx").setLevel(saved[3])

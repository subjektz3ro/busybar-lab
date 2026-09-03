"""Security contracts for Barkeep's process-level startup configuration."""

import pytest

from barkeep.__main__ import configured_bind, configured_port
from barkeep.server import exposure_warning


@pytest.mark.parametrize("configured", [None, "", "   "])
def test_bind_defaults_to_loopback(configured, monkeypatch):
    if configured is None:
        monkeypatch.delenv("BARKEEP_BIND", raising=False)
    else:
        monkeypatch.setenv("BARKEEP_BIND", configured)

    assert configured_bind() == "127.0.0.1"


@pytest.mark.parametrize("configured,expected", [
    ("0.0.0.0", "0.0.0.0"),
    ("::", "::"),
    (" 192.0.2.10 ", "192.0.2.10"),
])
def test_network_bind_requires_an_explicit_value(configured, expected, monkeypatch):
    monkeypatch.setenv("BARKEEP_BIND", configured)

    assert configured_bind() == expected


def test_explicit_unauthenticated_network_bind_is_announced(monkeypatch):
    monkeypatch.setenv("BARKEEP_BIND", "0.0.0.0")

    warning = exposure_warning(configured_bind(), "")

    assert "NO authentication" in warning
    assert "BARKEEP_TOKEN" in warning


@pytest.mark.parametrize("bind", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_every_loopback_form_needs_no_exposure_warning(bind):
    assert exposure_warning(bind, "") == ""


def test_token_keeps_explicit_network_bind_supported(monkeypatch):
    monkeypatch.setenv("BARKEEP_BIND", "0.0.0.0")

    assert exposure_warning(configured_bind(), "configured-token") == ""


@pytest.mark.parametrize("configured,expected", [
    (None, 8080),
    ("", 8080),
    ("  ", 8080),
    ("9090", 9090),
])
def test_port_defaults_to_8080_and_honors_an_explicit_value(
    configured, expected, monkeypatch,
):
    if configured is None:
        monkeypatch.delenv("BARKEEP_PORT", raising=False)
    else:
        monkeypatch.setenv("BARKEEP_PORT", configured)

    assert configured_port() == expected


def test_a_non_numeric_port_refuses_startup(monkeypatch):
    """A typo'd port must be a named error, not an int() traceback."""
    monkeypatch.setenv("BARKEEP_PORT", "eight")

    with pytest.raises(ValueError, match="BARKEEP_PORT"):
        configured_port()

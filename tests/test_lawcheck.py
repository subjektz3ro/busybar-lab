"""Payload law checks catch the silent device rejections before a device."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path

import pytest
from busylib import types

from busybar_dev.lawcheck import check_application_name, check_display_elements

REPO = Path(__file__).resolve().parents[1]


def _load_template():
    spec = importlib.util.spec_from_file_location(
        "lawcheck_template", REPO / "apps" / "_template.py",
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _payload(*texts: str, ids: tuple[str, ...] | None = None) -> types.DisplayElements:
    ids = ids or tuple(f"e{i}" for i in range(len(texts)))
    return types.DisplayElements(
        application_name="law-fixture",
        elements=[
            types.TextElement(
                id=identity, type="text", text=text, font="condensed",
            )
            for identity, text in zip(ids, texts)
        ],
    )


def test_clean_ascii_payload_has_no_findings():
    assert check_display_elements(_payload("HELLO", "72F ~")) == []


def test_non_ascii_text_is_named_with_its_characters():
    findings = check_display_elements(_payload("Zürich", "OK"))
    assert len(findings) == 1
    assert "'ü'" in findings[0]
    assert "'e0'" in findings[0]
    assert "transliterate" in findings[0]


def test_empty_text_is_named_as_a_distinct_api_violation():
    findings = check_display_elements(_payload(""))
    assert len(findings) == 1
    assert "text is empty" in findings[0]


def test_device_identifier_patterns_are_checked():
    assert check_application_name("weather.v2-preview") == []
    assert "application_name 'bad name'" in check_application_name("bad name")[0]

    payload = _payload("OK", ids=("bad id",))
    payload.application_name = "bad/app"
    findings = check_display_elements(payload)
    assert len(findings) == 2
    assert any("application_name" in finding for finding in findings)
    assert any("element id 'bad id'" in finding for finding in findings)


def test_duplicate_ids_are_flagged():
    findings = check_display_elements(
        _payload("A", "B", ids=("status", "status")),
    )
    assert len(findings) == 1
    assert "appears twice" in findings[0]


def test_template_dry_run_fails_on_a_bad_feed_string(caplog):
    template = _load_template()

    clean = template.Config(
        app_name="demo", text="HELLO", priority=30, dry_run=True,
    )
    template.run(clean)  # must not raise

    bad = template.Config(
        app_name="demo", text="Zürich", priority=30, dry_run=True,
    )
    with pytest.raises(SystemExit) as failure:
        template.run(bad)
    assert failure.value.code == 1
    assert any("law check" in record.message for record in caplog.records)


def test_template_never_sends_a_payload_the_device_would_reject(monkeypatch):
    template = _load_template()
    monkeypatch.setattr(
        template, "connect",
        lambda *a, **k: pytest.fail("must refuse before connecting"),
    )
    bad = template.Config(
        app_name="demo", text="Zürich", priority=30, dry_run=False,
    )
    with pytest.raises(SystemExit, match="violates device draw laws"):
        template.run(bad)


class _ClearClient:
    def __init__(self, error: Exception | None = None):
        self.error = error
        self.cleared: list[str] = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def version(self):
        return object()

    def display_clear(self, *, application_name: str):
        if self.error is not None:
            raise self.error
        self.cleared.append(application_name)


class _Refusal(Exception):
    status_code = 409


def test_template_clear_supports_dry_run_and_live_cleanup(monkeypatch, caplog):
    template = _load_template()
    client = _ClearClient()
    monkeypatch.setattr(template, "connect", lambda: client)
    caplog.set_level(logging.INFO)

    template.run(template.Config("demo", "ignored", 30, True, clear=True))
    assert client.cleared == []
    assert any("would clear" in record.message for record in caplog.records)

    template.run(template.Config("demo", "ignored", 30, False, clear=True))
    assert client.cleared == ["demo"]


def test_template_clear_yields_to_a_focus_session(monkeypatch, caplog):
    template = _load_template()
    client = _ClearClient(_Refusal("busy"))
    monkeypatch.setattr(template, "connect", lambda: client)
    caplog.set_level(logging.INFO)

    template.run(template.Config("demo", "ignored", 30, False, clear=True))

    assert client.cleared == []
    assert any("nothing cleared" in record.message for record in caplog.records)


def test_template_clear_validates_name_before_connecting(monkeypatch):
    template = _load_template()
    monkeypatch.setattr(
        template, "connect",
        lambda: pytest.fail("invalid cleanup must not connect"),
    )
    with pytest.raises(SystemExit, match="violates device laws"):
        template.run(
            template.Config("bad app", "ignored", 30, False, clear=True)
        )

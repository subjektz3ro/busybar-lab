"""A damaged history row cannot prevent DSN startup or erase good rows."""

import json

import pytest

from apps.dsn_app import history, model, settings


@pytest.mark.parametrize("row", [
    None, [], 42, "not an event",
    {"event": "appear", "craft": ["VGR2"], "t": 10},
    {"event": "appear", "craft": {"name": "VGR2"}, "t": 10},
    {"event": "appear", "craft": "VGR2", "t": "bad"},
    {"event": "appear", "craft": "VGR2", "t": []},
    {"event": "appear", "craft": "VGR2", "t": True},
    {"event": "appear", "craft": "VGR2", "t": float("nan")},
    {"event": "appear", "craft": "VGR2", "t": float("inf")},
    {"event": "appear", "craft": "VGR2", "t": -1},
    {"event": "appear", "craft": "VGR2", "t": 10**400},
])
def test_malformed_row_does_not_hide_valid_history(tmp_path, monkeypatch, row):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(settings, "HISTORY_PATH", path)
    before = {"event": "appear", "craft": "VGR2", "t": 10}
    after = {"event": "appear", "craft": "VGR2", "t": 20}
    path.write_text("\n".join(json.dumps(item) for item in (before, row, after)))

    state = model.State()
    history.load_history(state)

    assert state.seen == {"vgr2": {"first": 10.0, "last": 20.0, "passes": 2}}


def test_invalid_utf8_is_skipped_per_row(tmp_path, monkeypatch):
    path = tmp_path / "history.jsonl"
    monkeypatch.setattr(settings, "HISTORY_PATH", path)
    path.write_bytes(
        b'{"event":"appear","craft":"VGR2","t":10}\n'
        b'\xff\n'
        b'{"event":"appear","craft":"VGR2","t":20}\n'
    )

    state = model.State()
    history.load_history(state)

    assert state.seen == {"vgr2": {"first": 10.0, "last": 20.0, "passes": 2}}

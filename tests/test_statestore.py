import json

from barkeep.statestore import DesiredState, load_state, save_state


def test_missing_file_gives_default(tmp_path):
    state = load_state(tmp_path / "s.json", default_foreground="sky")
    assert state == DesiredState("sky", set())


def test_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    save_state(path, DesiredState("sky", {"b", "a"}))
    assert load_state(path) == DesiredState("sky", {"a", "b"})
    assert json.loads(path.read_text())["enabled_backgrounds"] == ["a", "b"]


def test_none_foreground_roundtrip(tmp_path):
    path = tmp_path / "s.json"
    save_state(path, DesiredState(None, set()))
    assert load_state(path, default_foreground="sky") == DesiredState(None, set())


def test_corrupt_file_gives_default(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{not json")
    assert load_state(path, default_foreground="sky") == DesiredState("sky", set())

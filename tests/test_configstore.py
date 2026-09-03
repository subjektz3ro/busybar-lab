import pytest

from barkeep.configstore import (
    app_env_path,
    child_env,
    effective_config,
    parse_env_text,
    read_env_file,
    write_env_file,
)
from barkeep.registry import AppSpec, ConfigKey


def spec():
    return AppSpec(
        name="sky", kind="foreground", entrypoint="apps/sky.py", description="d",
        config=(ConfigKey("SKY_VOICE", "voice", "am_michael"),
                ConfigKey("SKY_TZ", "tz", "Etc/UTC")),
    )


def test_parse_env_text_rules():
    text = "# comment\n\nA=1\n  B = two \nnoequals\nC=\n"
    # C= survives: an explicitly blank override is a choice ("anonymous NWS
    # contact"), distinct from the key being absent.
    assert parse_env_text(text) == {"A": "1", "B": "two", "C": ""}


def test_env_file_roundtrip_and_atomicity(tmp_path):
    path = app_env_path(tmp_path / "config", spec())
    assert read_env_file(path) == {}
    write_env_file(path, {"SKY_VOICE": "am_michael", "SKY_TZ": ""})
    assert read_env_file(path) == {"SKY_VOICE": "am_michael", "SKY_TZ": ""}
    assert not list(path.parent.glob("*.tmp"))


def test_write_env_file_refuses_multiline_values(tmp_path):
    """The file becomes a child's environment; one line per key is the format."""
    path = app_env_path(tmp_path / "config", spec())
    with pytest.raises(ValueError):
        write_env_file(path, {"SKY_VOICE": "x\nEVIL=1"})
    with pytest.raises(ValueError):
        write_env_file(path, {"SKY\nVOICE": "x"})
    assert not path.exists()


@pytest.mark.parametrize(
    "name",
    (
        "../escape",
        "sky/../../escape",
        r"sky\..\escape",
        "%2e%2e",
        "%252e%252e",
        ".hidden",
        "Sky",
        "sk\N{CYRILLIC SMALL LETTER U}strip",
        "a" * 33,
    ),
)
def test_app_env_path_refuses_noncanonical_spec_names(tmp_path, name):
    malicious = AppSpec(name, "foreground", "apps/sky.py", "d")

    with pytest.raises(ValueError, match="invalid name"):
        app_env_path(tmp_path / "config", malicious)

    assert not (tmp_path / "escape.env").exists()


def test_effective_config_layering():
    rows = effective_config(spec(), {"SKY_VOICE": "am_michael"}, {"SKY_TZ": "UTC"})
    by_name = {r["name"]: r for r in rows}
    assert by_name["SKY_VOICE"]["value"] == "am_michael"
    assert by_name["SKY_VOICE"]["source"] == "app"
    assert by_name["SKY_TZ"]["value"] == "UTC"
    assert by_name["SKY_TZ"]["source"] == "shared"


def test_effective_config_default_layer():
    rows = effective_config(spec(), {}, {})
    assert all(r["source"] == "default" for r in rows)
    assert rows[0]["value"] == "am_michael"


def test_child_env_overlays_base():
    env = child_env({"A": "app"}, {"A": "base", "B": "keep"})
    assert env == {"A": "app", "B": "keep"}

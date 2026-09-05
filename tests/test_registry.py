from pathlib import Path

import pytest

from barkeep.registry import RegistryError, load_registry
from busybar_dev.tts import DEFAULT_KOKORO_VOICE

GOOD = """
[sky]
kind = "foreground"
entrypoint = "apps/sky.py"
description = "the sky"

[sky.config.SKY_VOICE]
description = "TTS voice"
default = "am_michael"

[sky.config.SKY_UNITS]
description = "units"
choices = ["f", "c"]
default = "f"

[sky.config.SKY_LAT]
description = "latitude"
type = "number"
default = "51.5"
minimum = -90
maximum = 90
requires = ["SKY_TZ"]

[sky.config.SKY_SCENES]
description = "scenes to cycle"
type = "multiselect"
choices = ["house", "forest"]
default = "house,forest"

[sky.config.SKY_CONTACT]
description = "contact"
default = ""
blank_is_value = true

[sky.config.SKY_TZ]
description = "timezone"
default = "UTC"
format = "timezone"

[pinger]
kind = "background"
entrypoint = "apps/pinger.py"
description = "pings"
"""


def write(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "apps.toml"
    p.write_text(text)
    return p


def test_loads_apps_with_config(tmp_path):
    reg = load_registry(write(tmp_path, GOOD))
    assert list(reg) == ["sky", "pinger"]
    sky = reg["sky"]
    assert sky.kind == "foreground"
    assert sky.entrypoint == "apps/sky.py"
    assert sky.config[0].name == "SKY_VOICE"
    assert sky.config[0].default == "am_michael"
    assert sky.config[0].type == "text"
    assert reg["pinger"].config == ()


def test_config_types_and_choices(tmp_path):
    reg = load_registry(write(tmp_path, GOOD))
    by_name = {k.name: k for k in reg["sky"].config}
    assert by_name["SKY_UNITS"].type == "enum"        # inferred from choices
    assert by_name["SKY_UNITS"].choices == ("f", "c")
    assert by_name["SKY_LAT"].type == "number"
    assert by_name["SKY_LAT"].choices == ()
    assert by_name["SKY_CONTACT"].blank_is_value is True
    assert by_name["SKY_LAT"].blank_is_value is False
    assert by_name["SKY_LAT"].minimum == -90
    assert by_name["SKY_LAT"].maximum == 90
    assert by_name["SKY_LAT"].requires == ("SKY_TZ",)
    assert by_name["SKY_TZ"].format == "timezone"


def test_bad_config_type_fails_loud(tmp_path):
    bad = ('[sky]\nkind = "foreground"\nentrypoint = "a.py"\ndescription = "x"\n'
           '[sky.config.K]\ndescription = "k"\ntype = "dropdown"\n')
    with pytest.raises(RegistryError, match="K type must be one of"):
        load_registry(write(tmp_path, bad))


def test_blank_is_value_must_be_boolean(tmp_path):
    bad = ('[sky]\nkind = "foreground"\nentrypoint = "a.py"\n'
           'description = "x"\n[sky.config.K]\ndescription = "k"\n'
           'blank_is_value = "yes"\n')
    with pytest.raises(RegistryError, match="blank_is_value must be boolean"):
        load_registry(write(tmp_path, bad))


@pytest.mark.parametrize(("body", "message"), [
    ('type = "text"\nminimum = 0\n', "bounds require type number"),
    ('type = "number"\nminimum = "0"\n', "bounds must be numbers"),
    ('type = "number"\nminimum = true\n', "bounds must be numbers"),
    ('type = "number"\nminimum = 2\nmaximum = 1\n', "minimum exceeds maximum"),
    ('requires = ["MISSING"]\n', "requires undeclared keys"),
    ('requires = ["K", "K"]\n', "requires contains duplicates"),
    ('format = "hostname"\n', "format must be one of"),
])
def test_invalid_declarative_validation_contract_fails_loud(
    tmp_path, body, message
):
    bad = (
        '[sky]\nkind = "foreground"\nentrypoint = "a.py"\n'
        'description = "x"\n[sky.config.K]\ndescription = "k"\n' + body
    )
    with pytest.raises(RegistryError, match=message):
        load_registry(write(tmp_path, bad))


@pytest.mark.parametrize("ktype", ["enum", "multiselect"])
def test_choice_types_without_choices_fail_loud(tmp_path, ktype):
    bad = ('[sky]\nkind = "foreground"\nentrypoint = "a.py"\ndescription = "x"\n'
           f'[sky.config.K]\ndescription = "k"\ntype = "{ktype}"\n')
    with pytest.raises(RegistryError, match=f"K is {ktype} but lists no choices"):
        load_registry(write(tmp_path, bad))


def test_multiselect_carries_its_choices(tmp_path):
    reg = load_registry(write(tmp_path, GOOD))
    key = next(k for k in reg["sky"].config if k.name == "SKY_SCENES")
    assert key.type == "multiselect"
    assert key.choices == ("house", "forest")


def test_missing_required_field_fails_loud(tmp_path):
    bad = '[sky]\nkind = "foreground"\ndescription = "x"\n'  # no entrypoint
    with pytest.raises(RegistryError, match="sky.*entrypoint"):
        load_registry(write(tmp_path, bad))


@pytest.mark.parametrize(
    "name",
    (
        "../escape",
        "sky/escape",
        r"sky\escape",
        "%2e%2e",
        "%252e%252e",
        ".hidden",
        "Sky",
        "sk\N{CYRILLIC SMALL LETTER U}strip",
        "a" * 33,
    ),
)
def test_unsafe_app_names_fail_before_becoming_paths_or_modules(tmp_path, name):
    bad = (
        f'[{name!r}]\nkind = "foreground"\nentrypoint = "a.py"\n'
        'description = "x"\n'
    )

    with pytest.raises(RegistryError, match="app names must match"):
        load_registry(write(tmp_path, bad))


def test_bad_kind_fails_loud(tmp_path):
    bad = '[sky]\nkind = "sideways"\nentrypoint = "a.py"\ndescription = "x"\n'
    with pytest.raises(RegistryError, match="sky.*kind"):
        load_registry(write(tmp_path, bad))


def test_missing_file_fails_loud(tmp_path):
    with pytest.raises(RegistryError, match="apps.toml"):
        load_registry(tmp_path / "apps.toml")


def test_real_repo_registry_parses():
    repo = Path(__file__).resolve().parent.parent
    reg = load_registry(repo / "apps.toml")
    assert "skystrip" in reg
    assert reg["skystrip"].kind == "foreground"
    skystrip = {key.name: key for key in reg["skystrip"].config}
    assert skystrip["SKYSTRIP_VOICE"].default == DEFAULT_KOKORO_VOICE
    assert "SKYSTRIP_SPEAKER" not in skystrip

    example = dict(
        line.split("=", 1)
        for line in (repo / ".env.example").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    )
    assert example["SKYSTRIP_VOICE"] == DEFAULT_KOKORO_VOICE


# --- config keys stay in step with the code --------------------------------


def _app_env_keys() -> dict[str, set[str]]:
    """Every SKYSTRIP_*/DSN_* key name an app's owned modules mention.

    Any string literal of that shape, not just `os.environ.get("...")` — the
    coordinates are read through a `_coordinate(name, low, high)` helper that
    takes the key as an argument, and a matcher tied to the call shape reports
    them as unread. The explicit module map keeps one app's support modules
    from making another app appear to read its configuration.
    """
    import re

    repo = Path(__file__).resolve().parent.parent
    pattern = re.compile(r'"((?:SKYSTRIP|DSN)_[A-Z0-9_]+)"')
    modules = {
        "skystrip": (
            "skystrip.py", *(str(path.relative_to(repo / "apps"))
                             for path in (repo / "apps" / "skystrip_app").rglob("*.py")),
        ),
        "dsn": ("dsn.py", *(str(path.relative_to(repo / "apps"))
                              for path in (repo / "apps" / "dsn_app").rglob("*.py"))),
    }
    found: dict[str, set[str]] = {}
    for app, prefix in (("skystrip", "SKYSTRIP_"), ("dsn", "DSN_")):
        text = "\n".join(
            (repo / "apps" / module).read_text() for module in modules[app])
        found[app] = {m for m in pattern.findall(text) if m.startswith(prefix)}
    return found


def _declared_secret_exceptions() -> set[str]:
    """Keys deliberately kept OUT of apps.toml because they carry credentials.

    Sourced from busybar_viz.offline rather than hardcoded here: the skill
    requires a secret exception to be registered there so offline workers scrub
    it, and reading the same list means the two cannot drift apart.
    """
    from busybar_viz.offline import _SENSITIVE_ENV_EXACT

    return set(_SENSITIVE_ENV_EXACT)


def test_every_key_an_app_reads_is_declared():
    """An undeclared key still works — children inherit the environment — but
    is invisible and unsettable in the web UI, which is the only way to
    configure an app on a headless Pi.

    The narrow exception is a credential-bearing key, which must NOT be
    declared: Barkeep's config GET returns declared values verbatim to any
    caller allowed to reach its API."""
    repo = Path(__file__).resolve().parent.parent
    reg = load_registry(repo / "apps.toml")
    exempt = _declared_secret_exceptions()
    for app, keys in _app_env_keys().items():
        declared = {key.name for key in reg[app].config}
        missing = sorted(keys - declared - exempt)
        assert not missing, f"{app} reads undeclared keys: {missing}"


def test_a_secret_exception_is_never_also_declared():
    """The exception exists to keep it off Barkeep's config API. Declaring it
    anyway would defeat the whole point."""
    repo = Path(__file__).resolve().parent.parent
    reg = load_registry(repo / "apps.toml")
    exempt = _declared_secret_exceptions()
    for name, spec in reg.items():
        leaked = sorted({k.name for k in spec.config} & exempt)
        assert not leaked, f"{name} declares a secret key: {leaked}"


def test_no_declared_key_is_unread():
    """The other direction. SKYSTRIP_SPEAKER was set in a live .env and read
    by nothing; a declared-but-unread key is the same drift with a UI field
    attached to it."""
    repo = Path(__file__).resolve().parent.parent
    reg = load_registry(repo / "apps.toml")
    read = _app_env_keys()
    for app in ("skystrip", "dsn"):
        declared = {key.name for key in reg[app].config}
        unread = sorted(declared - read[app])
        assert not unread, f"{app} declares keys nothing reads: {unread}"


def _runtime_env_keys() -> set[str]:
    """Every operator-prefixed env key the runtime mentions as a literal.

    Scope is the runtime an operator's .env configures — apps/, barkeep/,
    busybar_dev/ — not tests or dev scripts. A string literal of the right
    shape counts as a read: several keys travel through helpers that take
    the name as an argument.
    """
    import re

    repo = Path(__file__).resolve().parent.parent
    pattern = re.compile(r'"((?:SKYSTRIP|DSN|BARKEEP|BUSYBAR)_[A-Z0-9_]+)"')
    found: set[str] = set()
    for pkg in ("apps", "barkeep", "busybar_dev"):
        for module in sorted((repo / pkg).rglob("*.py")):
            found |= set(pattern.findall(module.read_text()))
    return found


def _env_example_keys() -> set[str]:
    import re

    repo = Path(__file__).resolve().parent.parent
    return {
        m.group(1)
        for line in (repo / ".env.example").read_text().splitlines()
        if (m := re.match(r"([A-Z0-9_]+)=", line))
    }


# barkeep sets this for its children; an operator's .env never should.
_INTERNAL_ENV_KEYS = {"BARKEEP_MANAGED"}


def test_env_example_documents_every_runtime_key():
    """.env.example's header calls itself the complete operator contract.

    This is the test that sentence refers to; without it, BARKEEP_PORT and
    four other live keys were invisible to anyone reading the template."""
    missing = sorted(
        _runtime_env_keys() - _env_example_keys() - _INTERNAL_ENV_KEYS)
    assert not missing, f".env.example is missing runtime keys: {missing}"


def test_env_example_carries_no_dead_keys():
    """A documented key nothing reads is how SKYSTRIP_SPEAKER happened."""
    dead = sorted(_env_example_keys() - _runtime_env_keys())
    assert not dead, f".env.example documents keys nothing reads: {dead}"


def test_no_declared_key_looks_like_a_secret():
    """Barkeep's config GET returns declared values verbatim to API callers.

    A credential must stay in owner-readable .env instead, even though the
    control plane is loopback-only by default and can be token-protected.
    """
    repo = Path(__file__).resolve().parent.parent
    reg = load_registry(repo / "apps.toml")
    suspicious = [
        key.name
        for spec in reg.values()
        for key in spec.config
        if any(word in key.name for word in ("TOKEN", "SECRET", "PASSWORD", "APIKEY"))
    ]
    assert not suspicious, f"secret-looking keys are API-readable: {suspicious}"

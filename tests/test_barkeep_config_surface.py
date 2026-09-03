"""Registry validation metadata reaches Barkeep's generated config editor."""

from pathlib import Path

from barkeep.configstore import effective_config
from barkeep.registry import AppSpec, ConfigKey


ROOT = Path(__file__).resolve().parents[1]


def test_effective_config_rows_include_every_declarative_constraint():
    spec = AppSpec(
        "weather",
        "foreground",
        "apps/weather.py",
        "weather",
        (
            ConfigKey(
                "WEATHER_LAT",
                "latitude",
                "",
                "number",
                minimum=-90.5,
                maximum=90.5,
                requires=("WEATHER_LON",),
            ),
            ConfigKey(
                "WEATHER_LON",
                "longitude",
                "",
                "number",
                requires=("WEATHER_LAT",),
            ),
            ConfigKey(
                "WEATHER_TZ",
                "timezone",
                "UTC",
                format="timezone",
            ),
        ),
    )

    rows = {
        row["name"]: row
        for row in effective_config(spec, {}, {})
    }

    assert rows["WEATHER_LAT"]["value"] == ""
    assert rows["WEATHER_LAT"]["source"] == "default"
    assert rows["WEATHER_LAT"]["minimum"] == -90.5
    assert rows["WEATHER_LAT"]["maximum"] == 90.5
    assert rows["WEATHER_LAT"]["requires"] == ["WEATHER_LON"]
    assert rows["WEATHER_LAT"]["format"] is None
    assert rows["WEATHER_LON"]["minimum"] is None
    assert rows["WEATHER_LON"]["maximum"] is None
    assert rows["WEATHER_LON"]["requires"] == ["WEATHER_LAT"]
    assert rows["WEATHER_TZ"]["format"] == "timezone"


def test_number_bound_markup_is_finite_typed_and_attribute_escaped():
    source = (ROOT / "barkeep" / "static" / "app.js").read_text()

    # Strings cannot smuggle an attribute through a compromised or malformed
    # response, and NaN/Infinity never become invalid browser constraints.
    assert 'typeof k.minimum === "number" && Number.isFinite(k.minimum)' in source
    assert 'typeof k.maximum === "number" && Number.isFinite(k.maximum)' in source
    assert '` min="${escapeAttr(k.minimum)}"`' in source
    assert '` max="${escapeAttr(k.maximum)}"`' in source
    assert 'type="${type}"${step}${bounds}' in source
    assert '` min="${k.minimum}"`' not in source
    assert '` max="${k.maximum}"`' not in source

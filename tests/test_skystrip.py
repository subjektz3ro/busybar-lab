"""Guards for skystrip's host-side logic.

Rendering, weather policy and CLI tests call their production package owners.
Device and provider operations use deterministic fakes; no test here needs a
bar, a provider connection or an owner's configuration.
"""

import asyncio
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import cli as sky_cli
from apps.skystrip_app import config as sky_config
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app import runtime as sky_runtime
from apps.skystrip_app import selection as sky_selection
from apps.skystrip_app import settings as sky_settings
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app import weather_timeline as sky_weather_timeline
from apps.skystrip_app.device import assets as sky_device_assets
from apps.skystrip_app.device import display as sky_device_display
from apps.skystrip_app.providers import lightning as sky_providers_lightning
from apps.skystrip_app.providers import radar as sky_providers_radar
from apps.skystrip_app.providers import weather as sky_providers_weather
from apps.skystrip_app.render import art as sky_render_art
from apps.skystrip_app.render import backroads as sky_render_backroads
from apps.skystrip_app.render import effects as sky_render_effects
from apps.skystrip_app.render import grove as sky_render_grove
from apps.skystrip_app.render import lakefront as sky_render_lakefront
from apps.skystrip_app.render import precipitation as sky_render_precipitation
from apps.skystrip_app.render import scene as sky_render_scene
from apps.skystrip_app.render import season as sky_render_season
from apps.skystrip_app.render import traffic as sky_render_traffic
from apps.skystrip_app.render import vegetation as sky_render_vegetation
from astral import Observer
from busybar_dev.device import is_refusal as _is_refusal
import httpx
import os


def test_runtime_operations_have_explicit_owners():
    from apps.skystrip_app import weather_state as sky_weather_state
    from apps.skystrip_app.device import assets
    from apps.skystrip_app.providers import lightning

    for owner, name in (
        (sky_weather_state, "apply_rain"),
        (sky_providers_radar, "poll_radar"),
        (sky_providers_weather, "poll_nws"),
        (sky_device_display, "push_scene"),
        (assets, "sweep_stale_assets"),
        (lightning, "_coalesce_flashes"),
        (sky_runtime, "run"),
    ):
        operation = getattr(owner, name)
        assert callable(operation)
        assert operation.__module__ == owner.__name__


async def test_once_draws_local_snapshot_without_starting_provider_pollers(
    monkeypatch,
):
    provider_calls: list[str] = []

    async def forbidden_nws(_state):
        provider_calls.append("nws/open-meteo")

    async def forbidden_radar(_state):
        provider_calls.append("rainviewer")

    class USB:
        async def send_command(self, *_args):
            return None

    class Bar:
        usb = USB()

        async def aclose(self):
            return None

    async def connected(*_args, **_kwargs):
        return Bar()

    async def pushed(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sky_providers_weather, "poll_nws", forbidden_nws)
    monkeypatch.setattr(sky_providers_radar, "poll_radar", forbidden_radar)
    monkeypatch.setattr(sky_runtime, "connect_with_retry", connected)
    monkeypatch.setattr(sky_device_display, "push_scene", pushed)
    monkeypatch.setattr(sky_render_scene, "render_loop_frames", lambda *_a, **_k: [])
    monkeypatch.setattr(sky_selection, "load_scene_idx", lambda: 0)

    await sky_runtime.run(once=True)

    assert provider_calls == []


def test_coalesce_flashes_keeps_the_nearest_and_empties_the_queue():
    q = asyncio.Queue()
    for d in (40.0, 3.0, 22.0, 11.0):
        q.put_nowait(d)
    assert sky_providers_lightning._coalesce_flashes(q, 18.0) == 3.0
    assert q.empty()


def test_coalesce_flashes_is_a_noop_on_an_empty_queue():
    q = asyncio.Queue()
    assert sky_providers_lightning._coalesce_flashes(q, 7.5) == 7.5


def test_lightning_queue_is_bounded_and_overflow_collapses_the_burst():
    state = sky_model.SkyState()
    assert state.flash_queue.maxsize == sky_limits.FLASH_QUEUE_MAX

    for distance in range(10, 10 + sky_limits.FLASH_QUEUE_MAX):
        state.flash_queue.put_nowait(float(distance))
    sky_providers_lightning._enqueue_flash(state.flash_queue, 3.0)

    assert state.flash_queue.qsize() == 1
    assert state.flash_queue.get_nowait() == 3.0


def test_weather_state_replace_preserves_the_warning_name():
    """An observation refresh must not downgrade the alarm banner."""
    wx = sky_weather.WeatherState(severe=True, severe_event="Tornado Warning")
    obs = {"cloud_frac": 1.0, "rain": True, "snow": False, "thunder": True,
           "wind_kmh": 30.0, "wind_dir": 180, "temp_c": 21.0,
           "humidity": 88.0, "visibility_m": 8000.0}
    refreshed = replace(wx, **obs)
    assert refreshed.severe is True
    assert refreshed.severe_event == "Tornado Warning"
    assert refreshed.thunder is True          # observation still applied


@pytest.mark.parametrize("name", [
    "sky_00001_7.anim", "tl_140357.anim", "rva_144702.anim",
    "report_213105.snd", "train_120455.anim",
])
def test_sweep_matches_every_versioned_family(name):
    """A family missing from this regex leaks on the device forever."""
    assert sky_device_assets.GENERATION_FILES.match(name), name


@pytest.mark.parametrize("name", [
    "house.png", "siren.snd", "flock_0.png", "sky_a.png", "notes.txt",
])
def test_sweep_spares_durable_assets(name):
    assert not sky_device_assets.GENERATION_FILES.match(name), name


def test_refusal_detection_reads_the_real_busylib_attribute():
    """409 means 'yield and retry'; the old check used a nonexistent field."""
    from busylib import exceptions

    refused = exceptions.BusyBarAPIError("Not drawn due to low priority",
                                         status_code=409)
    assert _is_refusal(refused)
    broken = exceptions.BusyBarAPIError("Failed to open file for writing",
                                        status_code=508)
    assert not _is_refusal(broken)


@pytest.mark.parametrize("raw,expected", [
    (None,                      None),            # unset -> every scene
    ("",                        None),            # blank -> every scene
    ("atlantis,narnia",         None),            # nothing we know -> every scene
    ("forest,house",            ("house", "forest")),        # declared order wins
    (" grove , house ",         ("house", "grove")),         # whitespace tolerated
    ("house,house",             ("house",)),                 # deduped
    ("backroads,atlantis",      ("backroads",)),             # unknown dropped
])
def test_enabled_scenes_parsing(tmp_path, raw, expected):
    values = {} if raw is None else {"SKYSTRIP_SCENES": raw}
    got = sky_config.parse_runtime_config(values, tmp_path).enabled_scenes
    assert got == (sky_config.SCENES if expected is None else expected)
    assert got, "an empty set would divide by zero on the first button press"


def test_cycling_wraps_inside_the_enabled_set(monkeypatch):
    monkeypatch.setattr(sky_settings, "ENABLED_SCENES", ("house", "forest"))
    state = sky_model.SkyState()
    seen = []
    for _ in range(4):
        seen.append(state.scene)
        state.scene_idx = (state.scene_idx + 1) % len(sky_settings.ENABLED_SCENES)
    assert seen == ["house", "forest", "house", "forest"]


def test_disabled_saved_scene_resumes_at_the_first_enabled(monkeypatch, tmp_path):
    scene_file = tmp_path / ".skystrip_scene"
    monkeypatch.setattr(sky_settings, "SCENE_FILE", scene_file)
    monkeypatch.setattr(sky_settings, "ENABLED_SCENES", ("skyline", "grove"))

    scene_file.write_text("grove")                 # still enabled: resumed
    assert sky_selection.load_scene_idx() == 1

    scene_file.write_text("lakefront")             # switched off since: reset
    assert sky_selection.load_scene_idx() == 0

    assert sky_selection.save_scene_idx(1) is True      # saves the NAME, not an index
    assert scene_file.read_text() == "grove"
    assert list(tmp_path.glob("*.tmp")) == []


def test_scene_save_is_atomic_and_reports_a_failed_publish(
        monkeypatch, tmp_path, caplog):
    scene_file = tmp_path / "state" / "skystrip-scene"
    scene_file.parent.mkdir()
    scene_file.write_text("house")
    monkeypatch.setattr(sky_settings, "SCENE_FILE", scene_file)
    monkeypatch.setattr(sky_settings, "ENABLED_SCENES", ("house", "grove"))

    def fail_replace(_source, _destination):
        raise OSError("read-only test boundary")

    monkeypatch.setattr(os, "replace", fail_replace)
    with caplog.at_level("WARNING", logger="skystrip"):
        assert sky_selection.save_scene_idx(1) is False

    assert scene_file.read_text() == "house", "failed publish changed good state"
    assert list(scene_file.parent.glob(".skystrip-scene.*.tmp")) == []
    assert "scene state not persisted" in caplog.text


def test_scene_state_defaults_to_the_managed_state_directory():
    assert sky_settings.SCENE_FILE.name == "skystrip-scene"
    assert sky_settings.SCENE_FILE.parent == sky_config.REPO_ROOT / "state"
    assert sky_config.DEFAULT_SKYSTRIP_CONFIG.state_root == (
        sky_config.REPO_ROOT / "state"
    )


def test_malformed_state_root_cannot_crash_module_startup(tmp_path):
    path, warning = sky_config.resolve_state_root("\x00bad", tmp_path)
    assert path == tmp_path / "state"
    assert "BUSYBAR_STATE_DIR is unusable" in warning


def test_blank_env_values_do_not_crash_config_parsing(tmp_path):
    """A per-app override may now be explicitly blank; float("") must not run."""
    config = sky_config.parse_runtime_config({
        "SKYSTRIP_LAT": "",
        "SKYSTRIP_LON": "",
        "SKYSTRIP_TZ": "",
        "SKYSTRIP_UNITS": "",
        "SKYSTRIP_STYLE": "",
    }, tmp_path)

    assert config.latitude == 0.0
    assert config.longitude == 0.0
    assert config.location_set is False
    assert str(config.timezone) == "UTC"
    assert config.units == "f"
    assert config.style == "plain"


def test_invalid_units_fail_fast_in_runtime_config(tmp_path):
    with pytest.raises(ValueError, match="SKYSTRIP_UNITS must be 'f' or 'c'"):
        sky_config.parse_runtime_config({"SKYSTRIP_UNITS": "kelvin"}, tmp_path)


def test_invalid_timezone_fails_with_bounded_config_error(tmp_path):
    with pytest.raises(
        ValueError, match="SKYSTRIP_TZ must be a valid IANA timezone"
    ):
        sky_config.parse_runtime_config({"SKYSTRIP_TZ": "A" * 300}, tmp_path)


def test_an_unset_location_defaults_to_nowhere_and_says_so(
    monkeypatch, tmp_path,
):
    """Shipping a real city as the fallback makes an unconfigured install look
    like a working one: the sun still rises, the clock still ticks, the clouds
    still move, and every one of them is about somewhere else.

    0,0 is in the Gulf of Guinea. Nobody's window looks out on it, which is
    the point — but obviously-wrong is only useful if it also SAYS so."""
    unlocated = sky_config.parse_runtime_config({}, tmp_path)
    assert unlocated.latitude == 0.0 and unlocated.longitude == 0.0
    assert unlocated.location_set is False
    monkeypatch.setattr(sky_settings, "LOCATION_SET", unlocated.location_set)
    warning = sky_settings.warn_if_unlocated()
    assert "SKYSTRIP_LAT" in warning and "SKYSTRIP_LON" in warning
    assert "not yours" in warning       # says it is wrong, not just what it is

    located = sky_config.parse_runtime_config({
        "SKYSTRIP_LAT": "51.5074",
        "SKYSTRIP_LON": "-0.1278",
    }, tmp_path)
    assert located.location_set is True
    monkeypatch.setattr(sky_settings, "LOCATION_SET", located.location_set)
    assert sky_settings.warn_if_unlocated() == "", "configured installs must stay quiet"


def test_a_malformed_coordinate_is_a_clear_exit_not_a_traceback(
    monkeypatch, tmp_path,
):
    """SKYSTRIP_LAT=51,5074 is a natural European typo. Under systemd a raw
    ValueError out of main() is a restart loop with a dark panel; the failure
    must name the key in a message, not a traceback."""
    monkeypatch.setenv("SKYSTRIP_LAT", "51,5074")
    monkeypatch.setenv("SKYSTRIP_LON", "-0.1278")
    monkeypatch.setattr(
        sys, "argv", ["skystrip.py", "--preview", str(tmp_path / "x.png")])

    with pytest.raises(SystemExit) as excinfo:
        sky_cli.main()

    assert "SKYSTRIP_LAT" in str(excinfo.value)


def test_no_real_location_is_a_default_anywhere():
    """The config surface is the contract. A coordinate default that is a real
    place is a personal detail hiding as a convenience."""
    import tomllib

    registry = tomllib.loads((Path(__file__).resolve().parents[1]
                              / "apps.toml").read_text())
    cfg = registry["skystrip"]["config"]
    assert cfg["SKYSTRIP_LAT"]["default"] == ""
    assert cfg["SKYSTRIP_LON"]["default"] == ""
    assert cfg["SKYSTRIP_TZ"]["default"] == "UTC"


def test_scrubbed_weather_carries_snow_depth():
    """wx_at rebuilds weather for the Time Machine. If depth stops here,
    ground snow is invisible in exactly the view that prompted this work --
    the README GIFs are time-machine renders."""
    from datetime import datetime
    target = datetime(2026, 1, 15, 9, 0, tzinfo=sky_settings.TZ)
    state = sky_model.SkyState()
    state.hourly = [(target, {
        "temp": -4.0, "cloud": 80, "precip": 0.0, "prob": 0, "code": 71,
        "wind": 10.0, "wdir": 270.0, "rh": 80.0, "vis": 16000.0,
        "snow_depth": 0.18,
    })]
    wx = sky_weather_timeline.wx_at(state, target)
    assert wx.snow_depth_m == 0.18


def test_snow_depth_defaults_to_zero_when_the_feed_omits_it():
    """Open-Meteo can return null rows; a missing key must not crash the
    Time Machine or fabricate snow."""
    from datetime import datetime
    target = datetime(2026, 7, 4, 12, 0, tzinfo=sky_settings.TZ)
    state = sky_model.SkyState()
    state.hourly = [(target, {
        "temp": 28.0, "cloud": 10, "precip": 0.0, "prob": 0, "code": 0,
        "wind": 8.0, "wdir": 180.0, "rh": 40.0, "vis": 16000.0,
    })]
    assert sky_weather_timeline.wx_at(state, target).snow_depth_m == 0.0
    assert sky_weather.WeatherState().snow_depth_m == 0.0


async def test_live_nowcast_writes_snow_depth_to_the_live_weather(monkeypatch):
    """THE bug: the bar's live scene renders from state.weather, refreshed
    by poll_nws -- not from wx_at, which only the scrubbed Time Machine and
    --preview ever touch. If poll_nws's Open-Meteo nowcast never sets
    snow_depth_m, snow_tier(wx.snow_depth_m) is 0 forever on the device and
    the scene after a real blizzard looks identical to the scene before it.

    Exercises poll_nws itself -- the coroutine that actually owns
    state.weather -- against a fake transport standing in for the network,
    for exactly one iteration, and checks the field it's supposed to own.
    """
    import httpx as httpx_mod

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        url = str(request.url)
        if request.url.host == "api.weather.gov":
            # A 404 from /points marks this observation/forecast pipeline as
            # outside NWS coverage for the iteration. CAP alert polling is a
            # separate task and is not running in this test.
            return httpx_mod.Response(404, json={"detail": "not found"})
        if request.url.host == "api.open-meteo.com":
            if "current" in request.url.params:  # the nowcast call
                return httpx_mod.Response(200, json={"current": {
                    "time": datetime.now().astimezone().isoformat(),
                    "temperature_2m": -3.5,
                    "precipitation": 0.0, "rain": 0.0, "showers": 0.0,
                    "snow_depth": 0.22,
                    "cloud_cover": 80.0, "weather_code": 71,
                    "wind_speed_10m": 12.0, "wind_direction_10m": 270.0,
                    "relative_humidity_2m": 85.0, "visibility": 9000.0,
                }})
            return httpx_mod.Response(200, json={"hourly": {"time": []}})
        raise AssertionError(f"unexpected request: {url}")

    class _MockClient(httpx_mod.AsyncClient):
        def __init__(self, *a, **kw):
            kw["transport"] = httpx_mod.MockTransport(handler)
            super().__init__(*a, **kw)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)
    monkeypatch.setattr(sky_settings, "NWS_STATION", "")
    # These live on the function object and persist across calls in the same
    # process; clear them so this test doesn't depend on what ran before it.
    monkeypatch.delattr(sky_providers_weather.poll_nws, "_hourly_due", raising=False)
    monkeypatch.delattr(sky_providers_weather.poll_nws, "_forecast_due", raising=False)

    state = sky_model.SkyState()
    assert state.weather.snow_depth_m == 0.0

    with pytest.raises(asyncio.TimeoutError):
        # poll_nws loops forever; one real iteration completes well inside
        # this timeout and then blocks on OBS_INTERVAL_S's sleep (300s).
        await asyncio.wait_for(sky_providers_weather.poll_nws(state), timeout=5)

    assert state.weather.snow_depth_m == 0.22, \
        "nowcast reached temp_c but not snow_depth_m -- ground snow is " \
        "invisible on the actual device"


@pytest.mark.asyncio
async def test_pinned_station_cannot_supply_history_outside_point_coverage(
    monkeypatch,
):
    import httpx as httpx_mod

    requests: list[str] = []

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        url = str(request.url)
        requests.append(url)
        if request.url.path.startswith("/points/"):
            return httpx_mod.Response(404, json={"detail": "not found"})
        if (
            request.url.host == "api.weather.gov"
            and request.url.path.startswith("/stations/")
        ):
            raise AssertionError(
                "a pinned station must not bypass failed point coverage")
        if request.url.host == "api.open-meteo.com":
            if "current" in request.url.params:
                return httpx_mod.Response(200, json={"current": {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "temperature_2m": 20.0,
                    "precipitation": 0.0, "rain": 0.0, "showers": 0.0,
                    "snow_depth": 0.0, "cloud_cover": 10.0,
                    "weather_code": 0, "wind_speed_10m": 5.0,
                    "wind_direction_10m": 180.0,
                    "relative_humidity_2m": 50.0, "visibility": 16000.0,
                }})
            return httpx_mod.Response(200, json={"hourly": {"time": []}})
        raise AssertionError(f"unexpected request: {url}")

    class _MockClient(httpx_mod.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx_mod.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)
    monkeypatch.setattr(sky_settings, "NWS_STATION", "KPIN")
    for attr in ("_hourly_due", "_forecast_due", "_obs_history_due"):
        monkeypatch.delattr(sky_providers_weather.poll_nws, attr, raising=False)

    state = sky_model.SkyState()
    state.obs_history = [
        (datetime.now(timezone.utc), {"rain": True, "snow": False})
    ]
    poller = asyncio.create_task(sky_providers_weather.poll_nws(state))
    try:
        await asyncio.wait_for(state.weather_ready.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.obs_history == []
    assert state.nws_point_covered is False
    assert state.nws_point_checked.is_set()
    assert not any("/stations/KPIN/" in url for url in requests)


@pytest.mark.asyncio
async def test_station_discovery_404_does_not_reclassify_a_covered_point(
    monkeypatch,
):
    """Only `/points` itself is authoritative for the CAP support boundary."""
    import httpx as httpx_mod

    def handler(request: httpx_mod.Request) -> httpx_mod.Response:
        url = str(request.url)
        if request.url.path.startswith("/points/"):
            return httpx_mod.Response(200, json={"properties": {
                "forecast": "https://api.weather.gov/gridpoints/TST/1,1/forecast",
                "observationStations": "https://api.weather.gov/gridpoints/"
                                       "TST/1,1/stations",
            }})
        if request.url.path.endswith("/stations"):
            return httpx_mod.Response(404, json={"detail": "no stations"})
        if (
            request.url.host == "api.weather.gov"
            and request.url.path.startswith("/gridpoints/")
        ):
            return httpx_mod.Response(404, json={"detail": "unavailable"})
        if request.url.host == "api.open-meteo.com":
            if "current" in request.url.params:
                return httpx_mod.Response(200, json={"current": {
                    "time": datetime.now(timezone.utc).isoformat(),
                    "temperature_2m": 20.0,
                    "precipitation": 0.0, "rain": 0.0, "showers": 0.0,
                    "snow_depth": 0.0, "cloud_cover": 10.0,
                    "weather_code": 0, "wind_speed_10m": 5.0,
                    "wind_direction_10m": 180.0,
                    "relative_humidity_2m": 50.0, "visibility": 16000.0,
                }})
            return httpx_mod.Response(200, json={"hourly": {"time": []}})
        raise AssertionError(f"unexpected request: {url}")

    class _MockClient(httpx_mod.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = httpx_mod.MockTransport(handler)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _MockClient)
    monkeypatch.setattr(sky_settings, "NWS_STATION", "")
    for attr in ("_hourly_due", "_forecast_due", "_obs_history_due"):
        monkeypatch.delattr(sky_providers_weather.poll_nws, attr, raising=False)

    state = sky_model.SkyState()
    poller = asyncio.create_task(sky_providers_weather.poll_nws(state))
    try:
        await asyncio.wait_for(state.weather_ready.wait(), timeout=1.0)
        await asyncio.sleep(0)
    finally:
        poller.cancel()
        await asyncio.gather(poller, return_exceptions=True)

    assert state.nws_point_checked.is_set()
    assert state.nws_point_covered is True


@pytest.mark.parametrize("depth,tier", [
    (0.0, 0), (-1.0, 0),          # no snow, and a nonsense negative
    (0.009, 0),                    # below a dusting: bare ground
    (0.01, 1), (0.05, 1),          # a dusting
    (0.08, 2), (0.20, 2),          # properly covered
    (0.25, 3), (2.0, 3),           # deep, and an implausible depth
])
def test_snow_tier_boundaries(depth, tier):
    assert sky_render_precipitation.snow_tier(depth) == tier


def test_snow_tier_survives_a_missing_reading():
    """Open-Meteo returns null for some rows; None must read as bare ground
    rather than raising inside the render loop."""
    assert sky_render_precipitation.snow_tier(None) == 0


def _blank():
    img = Image.new("RGB", (sky_limits.W, 16), (0, 0, 0))
    return img, img.load()


def test_settle_snow_never_fills_a_row():
    """THE failure mode. LEDs sit 1.23mm lit on a 2.2mm pitch, so a filled
    bright row reads as haze rather than surface -- and it looks perfect in
    a preview PNG, which is why this is a test and not a note."""
    for tier in (1, 2, 3):
        img, px = _blank()
        tops = {x: 14 for x in range(sky_limits.W)}
        sky_render_precipitation.settle_snow(px, tops, tier)
        for y in range(16):
            lit = sum(1 for x in range(sky_limits.W) if px[x, y] != (0, 0, 0))
            assert lit < sky_limits.W, f"tier {tier} filled row {y} solid"


def test_settle_snow_never_fills_a_row_even_at_an_unknown_tier():
    """The fallback must be the safest tier, not the most dangerous one:
    a fully lit row IS the haze failure this helper exists to prevent."""
    img, px = _blank()
    tops = {x: 14 for x in range(sky_limits.W)}
    sky_render_precipitation.settle_snow(px, tops, 99)
    for y in range(16):
        lit = sum(1 for x in range(sky_limits.W) if px[x, y] != (0, 0, 0))
        assert lit < sky_limits.W


def test_settle_snow_is_denser_the_deeper_it_gets():
    counts = []
    for tier in (1, 2, 3):
        img, px = _blank()
        tops = {x: 14 for x in range(sky_limits.W)}
        sky_render_precipitation.settle_snow(px, tops, tier)
        counts.append(sum(1 for y in range(16) for x in range(sky_limits.W)
                          if px[x, y] != (0, 0, 0)))
    assert counts[0] < counts[1] < counts[2], counts


def test_settle_snow_draws_nothing_at_tier_zero():
    img, px = _blank()
    sky_render_precipitation.settle_snow(px, {x: 14 for x in range(sky_limits.W)}, 0)
    assert all(px[x, y] == (0, 0, 0)
               for y in range(16) for x in range(sky_limits.W))


def test_settle_snow_follows_an_uneven_surface():
    """Rooftops and banks are not one flat row; snow must sit on whatever
    the scene passes rather than assuming y=14."""
    img, px = _blank()
    tops = {x: (10 if x < 20 else 14) for x in range(sky_limits.W)}
    sky_render_precipitation.settle_snow(px, tops, 3)
    assert any(px[x, 10] != (0, 0, 0) for x in range(20))
    assert all(px[x, 10] == (0, 0, 0) for x in range(20, sky_limits.W))


def test_settle_snow_is_deterministic():
    """Previews and tests must not shimmer between runs."""
    out = []
    for _ in range(2):
        img, px = _blank()
        sky_render_precipitation.settle_snow(px, {x: 14 for x in range(sky_limits.W)}, 2)
        out.append([px[x, y] for y in range(16) for x in range(sky_limits.W)])
    assert out[0] == out[1]


def test_surface_tops_finds_the_first_solid_pixel_per_column():
    """Rooftops, banks and shoulders are all 'the topmost thing that isn't
    sky'. One finder serves all three so each scene doesn't invent its own."""
    img, px = _blank()
    sky = {(0, 0, 0)}
    for x in range(10):
        px[x, 6] = (90, 90, 90)      # a tall building
    for x in range(10, sky_limits.W):
        px[x, 14] = (40, 50, 30)     # ordinary ground
    tops = sky_render_precipitation.surface_tops(px, range(sky_limits.W), range(16), sky)
    assert tops[0] == 6
    assert tops[40] == 14


def test_surface_tops_omits_columns_that_are_all_sky():
    """A gap in the skyline must get no snow floating in mid-air."""
    img, px = _blank()
    px[5, 12] = (90, 90, 90)
    tops = sky_render_precipitation.surface_tops(px, range(sky_limits.W), range(16), {(0, 0, 0)})
    assert tops == {5: 12}


def test_string_lights_leaves_gaps_between_bulbs():
    """A continuous lit line is the haze failure: LEDs are 1.23mm lit on a
    2.2mm pitch, so an unbroken run reads as a smear, not as lights."""
    img, px = _blank()
    points = [(x, 5) for x in range(sky_limits.W)]
    sky_render_season.string_lights(px, points, 0.0)
    lit = [x for x in range(sky_limits.W) if px[x, 5] != (0, 0, 0)]
    assert lit, "drew no bulbs at all"
    assert len(lit) < len(points) / 2, "bulbs are too dense to read as a string"


def test_string_lights_uses_separable_hues():
    """Below ~30% per-channel difference two bulbs are one colour on the
    panel. The point of a light string is that the colours differ. Includes
    the wraparound pair (last next to first) since the palette cycles, and a
    string longer than three bulbs puts them adjacent."""
    for a, b in zip(sky_render_season.XMAS_BULBS, sky_render_season.XMAS_BULBS[1:] + sky_render_season.XMAS_BULBS[:1]):
        assert max(abs(p - q) for p, q in zip(a, b)) >= 76, (a, b)  # 30% of 255


def test_xmas_bulbs_do_not_collide_with_a_scene_colour():
    """XMAS_BULBS[2] (the warm bulb) used to be byte-identical to
    WINDOW_WARM, (255, 190, 90). The lakefront/backroads guard tests
    (test_the_roadside_tree_never_stands_in_the_road,
    test_the_lakefront_tree_never_stands_on_open_water) build `decor =
    set(XMAS_BULBS) | {XMAS_TREE}` and assert none of it ever appears on
    water or road -- but with that collision, `decor` also matched
    ordinary WINDOW_WARM lamp light from the lakefront's own tower
    cluster (see _draw_lakefront), not just actual decorations. Those
    guards passed only because no lamp happened to sit on a guarded row,
    which is a false positive waiting to happen, not a guarantee. Pin the
    absence of the collision directly, against every colour a scene can
    legitimately paint a window."""
    scene_colours = {sky_render_art.WINDOW_WARM, sky_render_art.WINDOW_COOL}
    for bulb in sky_render_season.XMAS_BULBS:
        assert bulb not in scene_colours, \
            f"{bulb} collides with a window/lamp colour"
    assert sky_render_season.XMAS_TREE not in scene_colours


def test_string_lights_twinkle_is_seamless_across_the_loop():
    """The device loops the .anim itself, so phase 0 and phase 1 are adjacent
    frames. A twinkle that does not close its cycle jumps at the seam."""
    img0, px0 = _blank()
    img1, px1 = _blank()
    points = [(x, 5) for x in range(0, sky_limits.W, 2)]
    sky_render_season.string_lights(px0, points, 0.0)
    sky_render_season.string_lights(px1, points, 1.0)
    assert [px0[x, 5] for x in range(sky_limits.W)] == \
           [px1[x, 5] for x in range(sky_limits.W)]


def test_string_lights_follows_an_uneven_line():
    """Rooflines are peaked, not flat; bulbs sit on the points given and
    nowhere else. Asserts BOTH -- that a bulb lands where one is due, and
    that nothing lands off the line. Checking only the second half passes
    even when nothing is drawn at all."""
    img, px = _blank()
    points = [(10, 8), (11, 7), (12, 7), (13, 8), (14, 9)]
    sky_render_season.string_lights(px, points, 0.0)
    drew = 0
    for i, (x, y) in enumerate(points):
        if i % sky_render_season.XMAS_SPACING == 0:
            assert px[x, y] != (0, 0, 0), f"no bulb at point {i} ({x},{y})"
            drew += 1
        for other in range(sky_limits.H):
            if other != y:
                assert px[x, other] == (0, 0, 0), f"bulb off the line at {x}"
    assert drew, "the test asserted nothing"


def test_snowdepth_flag_reaches_the_preview_weather():
    parser = sky_cli.build_parser()
    args = parser.parse_args(["--preview", "x.png", "--snowdepth", "0.3"])
    assert args.snowdepth == 0.3
    assert parser.parse_args(["--preview", "x.png"]).snowdepth == 0.0


def test_house_scene_puts_snow_on_the_ground(monkeypatch):
    """The bug: snow fell but never landed, so the scene after a blizzard
    looked identical to the scene before it.

    render_scene's real signature is (now, wx, seed, *, phase, scene,
    scrubbed) -- the same call render_loop_frames makes for every frame the
    preview and the device actually show, seed=0 as main() uses for --preview.
    """
    from datetime import datetime, timezone
    # Pin the observer rather than lean on LAT/LON's default of 0,0: another
    # test (test_an_unset_location_defaults_to_nowhere_and_says_so) reloads
    # the module with a real location mid-run, and under any test order that
    # isn't "that test happens to run and clean up first" -- or with
    # SKYSTRIP_LAT exported in the shell -- OBSERVER would carry a real
    # coordinate here, elev would land somewhere golden-hour or night skews
    # _ambient(), and this exact-color comparison would flake.
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    # UTC, not sky_settings.TZ: with OBSERVER at 0,0 this puts the sun overhead,
    # which is what keeps _ambient() the identity regardless of local .env.
    when = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)
    bare = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.0)
    deep = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.30)
    a = sky_render_scene.render_scene(when, bare, 0, scene="house")
    b = sky_render_scene.render_scene(when, deep, 0, scene="house")
    assert a != b, "snow depth changed nothing on the ground"

    # Detecting the snow needs more care than it looks, and two earlier
    # versions of this check could not fail.
    #
    # "not black" cannot work: render_scene paints a sky gradient into every
    # row, so row 14 is never (0, 0, 0) even with no snow.
    #
    # Comparing the two frames cannot work either: at tier 3 the house
    # deliberately stops drawing grass tufts, so the frames differ by the
    # ABSENCE of grass whether or not snow was added. Stubbing settle_snow to
    # a no-op still left 29 columns differing.
    #
    # So compare exact colours -- which only holds while _ambient() is the
    # identity, and that is an ENVIRONMENT question. Pinning OBSERVER is not
    # enough: TZ also comes from .env, so the same local clock can map to a
    # different solar instant. Pin the instant in UTC as well; at 0,0 the sun
    # is overhead,
    # the tint is skipped, and settle_snow's colours land untouched.
    pb = b.load()
    snow_colors = {sky_render_art.SNOW_LIT, sky_render_art.SNOW_SHADE}
    ground = sum(1 for x in range(sky_limits.W) if pb[x, 14] in snow_colors)
    assert ground > 0, "deep snow drew nothing on the ground row"
    assert ground < sky_limits.W, "ground row filled solid: reads as haze"


def test_house_snow_never_floats_above_the_tuft(monkeypatch):
    """The bug: the house built tops as 14 - gh, one row above the tuft's
    actual topmost drawn pixel -- and two rows above it once a wind wave
    (wind_kmh >= 5) moved a tall blade's row-13 pixel sideways, stranding
    snow at y=12 over open sky with nothing under it. settle_snow's
    contract is that tops[x] is the topmost pixel actually on the ground;
    for this scene's fringe (rows 13-14 only) that means snow can never
    legitimately land at y=12.

    OBSERVER is pinned (see test_house_scene_puts_snow_on_the_ground) so the
    drawn colors are the raw SNOW_LIT/SNOW_SHADE constants -- otherwise a
    shaded snow pixel would never equal the raw constant and this negative
    check would pass even with the floating-snow bug back in place.
    """
    from datetime import datetime
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    when = datetime(2026, 1, 15, 12, 0, tzinfo=sky_settings.TZ)
    snow_colors = {sky_render_art.SNOW_LIT, sky_render_art.SNOW_SHADE}
    wx = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.12,  # tier 2
                                wind_kmh=10.0, wind_dir=90.0)
    for i in range(8):
        phase = i / 8
        px = sky_render_scene.render_scene(when, wx, 0, phase=phase,
                                    scene="house").load()
        for x in range(2, 47):
            assert px[x, 12] not in snow_colors, \
                f"snow floating at ({x},12), phase={phase}"


@pytest.mark.parametrize("scene", ["forest", "grove", "backroads",
                                    "skyline", "lakefront"])
def test_scene_responds_to_settled_snow(scene):
    """render_scene's real signature is (now, wx, seed, *, phase, scene,
    scrubbed) -- see test_house_scene_puts_snow_on_the_ground. skyline and
    lakefront are last because their "ground" is least like ground:
    rooftops, and banks-not-water."""
    from datetime import datetime
    when = datetime(2026, 1, 15, 12, 0, tzinfo=sky_settings.TZ)
    bare = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.0)
    deep = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.30)
    assert (sky_render_scene.render_scene(when, bare, 0, scene=scene)
            != sky_render_scene.render_scene(when, deep, 0, scene=scene)), \
        f"{scene} ignored settled snow"


def test_snow_never_lands_on_open_water(monkeypatch):
    """Snow on the lake is the single most obvious way this feature can
    look wrong -- lakefront must exclude the water at the surface_tops
    search step, not merely avoid drawing there.

    settle_snow only ever writes into bank_rows (13-15), and those rows
    are NOT all bank -- BEND_WATER_END shows real open water inside them
    (e.g. row 13 is water for x in 0..50, bank only from x=51 on). A
    version of this test that scanned rows 8-12 (all genuinely bank-free
    of snow, since settle_snow never touches them) would pass even with
    the water exclusion completely gutted -- checked and confirmed below.
    Scanning the actual water span of the actual rows snow can reach is
    what makes this test load-bearing.

    OBSERVER is pinned (see test_house_scene_puts_snow_on_the_ground) so a
    shaded snow pixel can't dodge this raw-constant comparison and make the
    water exclusion look intact when it isn't.
    """
    from datetime import datetime
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    when = datetime(2026, 1, 15, 12, 0, tzinfo=sky_settings.TZ)
    wx = sky_weather.WeatherState(temp_c=-2.0, snow_depth_m=0.40)
    px = sky_render_scene.render_scene(when, wx, 0, scene="lakefront").load()
    snow_colors = {sky_render_art.SNOW_LIT, sky_render_art.SNOW_SHADE}
    for y in (13, 14, 15):
        for x in range(sky_render_lakefront.BEND_WATER_END[y]):
            assert px[x, y] not in snow_colors, \
                f"snow settled on open water at ({x},{y})"


@pytest.mark.parametrize("month,winter", [
    (12, True), (1, True), (2, True),
    (3, False), (6, False), (9, False), (11, False),
])
def test_is_winter(month, winter):
    from datetime import datetime
    assert sky_render_grove.is_winter(
        datetime(2026, month, 15, 12, 0, tzinfo=sky_settings.TZ)) is winter


@pytest.mark.parametrize("window,month,day,expected", [
    ("dec24-26",  12, 23, False), ("dec24-26",  12, 24, True),
    ("dec24-26",  12, 25, True),  ("dec24-26",  12, 26, True),
    ("dec24-26",  12, 27, False),
    ("dec25",     12, 24, False), ("dec25",     12, 25, True),
    ("dec25",     12, 26, False),
    ("off",       12, 25, False),                 # off means off, on the day
    ("dec1-26",   12, 1,  True),  ("dec1-26",   11, 30, False),
    ("dec1-26",   12, 27, False),
    ("dec20-jan1", 12, 20, True), ("dec20-jan1", 12, 31, True),
    ("dec20-jan1", 1,  1,  True),                 # crosses the year boundary
    ("dec20-jan1", 1,  2,  False),
    ("dec20-jan1", 6,  15, False),
])
def test_christmas_windows(monkeypatch, window, month, day, expected):
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", window)
    when = datetime(2026, month, day, 12, 0, tzinfo=sky_settings.TZ)
    assert sky_render_season.is_christmas(when) is expected


def test_an_unknown_christmas_window_is_off_not_a_crash(monkeypatch):
    """A hand-edited config.env can hold anything. An unrecognised value must
    not put the app into a crash loop with the display dark."""
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "sometime in winter")
    assert sky_render_season.is_christmas(
        datetime(2026, 12, 25, 12, 0, tzinfo=sky_settings.TZ)) is False


def test_christmas_is_declared_in_the_registry():
    """An undeclared key is invisible in the barkeep editor, which is the only
    way to configure an app on a headless host."""
    import tomllib
    from pathlib import Path
    reg = tomllib.loads((Path(__file__).resolve().parents[1]
                         / "apps.toml").read_text())
    cfg = reg["skystrip"]["config"]["SKYSTRIP_CHRISTMAS"]
    assert cfg["default"] == "dec24-26"
    assert set(cfg["choices"]) == {
        "off", "dec25", "dec24-26", "dec20-jan1", "dec1-26"}


@pytest.mark.parametrize("raw", ["dec24-26", "DEC24-26", " Dec24-26 "])
def test_the_christmas_window_tolerates_case_and_whitespace(tmp_path, raw):
    """A hand-edited env file is the one place this is set without a UI to
    validate it, and an unrecognised value fails silently -- the
    decorations just never appear."""
    config = sky_config.parse_runtime_config({"SKYSTRIP_CHRISTMAS": raw}, tmp_path)
    assert config.christmas_window == "dec24-26"


def test_christmas_preview_flag_overrides_the_date():
    parser = sky_cli.build_parser()
    assert parser.parse_args(["--preview", "x.png"]).christmas is None
    assert parser.parse_args(["--preview", "x.png", "--christmas"]).christmas is True
    assert parser.parse_args(["--preview", "x.png",
                              "--no-christmas"]).christmas is False


def test_forced_christmas_overrides_the_window(monkeypatch):
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "off")
    monkeypatch.setattr(sky_settings, "CHRISTMAS_FORCED", True)
    assert sky_render_season.is_christmas(
        datetime(2026, 7, 4, 12, 0, tzinfo=sky_settings.TZ)) is True
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec25")
    monkeypatch.setattr(sky_settings, "CHRISTMAS_FORCED", False)
    assert sky_render_season.is_christmas(
        datetime(2026, 12, 25, 12, 0, tzinfo=sky_settings.TZ)) is False


def test_the_christmas_window_is_evaluated_in_local_time(monkeypatch):
    """19:00 on the 24th locally is already the 25th in UTC. Fed the raw
    UTC instant, a naive predicate would turn the lights on and off on the
    wrong evening -- and nothing would look broken, just early."""
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec25")
    monkeypatch.setattr(sky_settings, "CHRISTMAS_FORCED", None)
    local_eve = datetime(2026, 12, 24, 19, 0, tzinfo=sky_settings.TZ)
    assert sky_render_season.is_christmas(local_eve) is False
    # the same instant, expressed in UTC, must agree
    assert sky_render_season.is_christmas(local_eve.astimezone(timezone.utc)) is False
    local_day = datetime(2026, 12, 25, 19, 0, tzinfo=sky_settings.TZ)
    assert sky_render_season.is_christmas(local_day) is True
    assert sky_render_season.is_christmas(local_day.astimezone(timezone.utc)) is True


def test_the_house_wears_lights_at_christmas(monkeypatch):
    """render_scene's real signature is (now, wx, seed, *, phase, scene,
    scrubbed) -- see test_house_scene_puts_snow_on_the_ground.
    """
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec24-26")
    wx = sky_weather.WeatherState(temp_c=-2.0)
    # UTC noon at 0,0 keeps _ambient() the identity, so bulb colours land
    # unshaded and can be compared exactly. Pinning OBSERVER alone is not
    # enough: TZ comes from .env, which this worktree has.
    xmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
    plain = datetime(2026, 12, 18, 12, 0, tzinfo=timezone.utc)
    a = sky_render_scene.render_scene(xmas, wx, 0, scene="house")
    b = sky_render_scene.render_scene(plain, wx, 0, scene="house")
    assert a != b, "Christmas changed nothing"

    # "Something changed" is not sufficient -- render_scene paints a sky
    # gradient into every row and a dusk murmuration and cloud puffs can
    # differ frame to frame on their own, so a != b would pass even if the
    # roofline itself grew no lights at all. Pin down the roofline
    # specifically. string_lights only lights every XMAS_SPACINGth point of
    # the 11 roofline columns, so exactly 4 columns (52, 55, 58, 61) should
    # take a bulb from XMAS_BULBS; the other 7 must show whatever the bare
    # roof already drew.
    #
    # The expected colours are NOT the raw XMAS_BULBS tuples -- string_lights
    # also applies a seam-safe twinkle that dims every lit bulb by the same
    # swell factor at a given phase, so e.g. XMAS_BULBS[0] = (235, 40, 40)
    # actually lands as (176, 30, 30) at phase 0.0. Rather than reimplement
    # that swell math by hand here (and silently drift from it if the curve
    # is ever tuned), render the same string onto a blank canvas with the
    # real string_lights at the same phase render_scene used (its default,
    # 0.0) and use THAT as the expected colour -- so this test is checking
    # "render_scene hung the real string on the real roofline", not
    # "render_scene's twinkle matches my arithmetic".
    eaves = [(x, sky_render_art.HOUSE_TOP[x]) for x in sorted(sky_render_art.HOUSE_TOP)]
    lit_points = eaves[::sky_render_season.XMAS_SPACING]
    ref = Image.new("RGB", (sky_limits.W, sky_limits.H))
    refpx = ref.load()
    sky_render_season.string_lights(refpx, eaves, 0.0)
    expected = [(x, y, refpx[x, y]) for x, y in lit_points]
    assert all(c != (0, 0, 0) for _, _, c in expected), \
        "test setup produced no reference bulb colours"

    apx = a.load()
    matches = sum(1 for x, y, c in expected if apx[x, y] == c)
    assert matches == len(lit_points) == 4, (
        f"expected all {len(lit_points)} roofline bulbs lit with the "
        f"string_lights colours, got {matches}")

    # And the non-Christmas frame must show none of those same bulb colours
    # at those same roofline points -- proving the difference IS the lights,
    # not some unrelated frame-to-frame variation.
    bpx = b.load()
    assert not any(bpx[x, y] == c for x, y, c in expected), \
        "non-Christmas frame already shows bulb colours on the roofline"


def test_roofline_is_derived_from_the_sprite():
    """Pasting the coordinates would let the lights drift off the roof the
    first time the artwork moves. Keys off HOUSE_TOP: the lights use the
    same silhouette moonlight already reads, rather than a second copy of
    the same derivation under a different name."""
    for x, y in sky_render_art.HOUSE_TOP.items():
        assert (x, y) in {(px, py) for px, py, _ in sky_render_art.HOUSE_SPRITE}
        assert all(py >= y for px, py, _ in sky_render_art.HOUSE_SPRITE if px == x)


def test_a_lit_tree_stands_at_christmas(monkeypatch):
    """render_scene's real signature is (now, wx, seed, *, phase, scene,
    scrubbed) -- see test_house_scene_puts_snow_on_the_ground.

    The task brief's own draft of this test only asserted the two frames
    differ -- which the brief itself flags as insufficient, and for the
    same reason test_the_house_wears_lights_at_christmas gives:
    render_scene paints a sky gradient into every row and the sun's
    elevation shifts a hair between the two dates on its own, so `a != b`
    passes even with draw_lit_tree stubbed to a no-op. Count actual
    XMAS_TREE/bulb pixels at the tree's own anchor (LAKEFRONT_TREE /
    BACKROADS_TREE) instead.
    """
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec24-26")
    wx = sky_weather.WeatherState(temp_c=-2.0)
    # UTC noon at 0,0 keeps _ambient() the identity (see
    # test_house_scene_puts_snow_on_the_ground), so the tree and its bulbs
    # land unshaded and are byte-for-byte comparable to a reference render.
    xmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
    plain = datetime(2026, 12, 18, 12, 0, tzinfo=timezone.utc)
    anchors = {"lakefront": sky_render_lakefront.LAKEFRONT_TREE,
              "backroads": sky_render_backroads.BACKROADS_TREE}
    decor = set(sky_render_season.XMAS_BULBS) | {sky_render_season.XMAS_TREE}

    for scene, (bx, by) in anchors.items():
        a = sky_render_scene.render_scene(xmas, wx, 0, scene=scene)
        b = sky_render_scene.render_scene(plain, wx, 0, scene=scene)
        assert a != b, f"{scene} ignored Christmas"

        # Render the same tree fresh on a blank canvas at the same phase
        # render_scene used (its default, 0.0), rather than hand-deriving
        # the bulb swell -- same reasoning as the string_lights reference
        # in test_the_house_wears_lights_at_christmas.
        ref = Image.new("RGB", (sky_limits.W, sky_limits.H))
        refpx = ref.load()
        sky_render_season.draw_lit_tree(refpx, bx, by, 0.0)
        footprint = [(bx + dx, by - 3 + dy)
                    for dx, dy in ((0, 0), (-1, 1), (0, 1), (1, 1),
                                   (-1, 2), (0, 2), (1, 2))] + [(bx, by)]
        expected = [(x, y, refpx[x, y]) for x, y in footprint]
        assert all(c != (0, 0, 0) for _, _, c in expected), \
            f"{scene}: test setup produced no reference tree colours"

        # The full 8-pixel footprint (canopy, trunk, both bulbs) lands
        # exactly where draw_lit_tree put it in the reference render.
        apx, bpx = a.load(), b.load()
        matches = sum(1 for x, y, c in expected if apx[x, y] == c)
        assert matches == len(footprint) == 8, (
            f"{scene}: expected the full 8-pixel tree footprint at "
            f"{(bx, by)}, got {matches}/8 matching")
        assert not any(bpx[x, y] == c for x, y, c in expected), \
            f"{scene}: non-Christmas frame already shows tree/bulb colours"

        # And specifically, count XMAS_TREE/XMAS_BULBS pixels the brief
        # asks for: the body cells (bulbs are swell-dimmed off the raw
        # XMAS_BULBS tuples, so only the 5 canopy cells land in `decor`
        # exactly -- see the mutation note on test_the_roadside_tree_...
        # below for why the road test's membership check has that same
        # blind spot for bulbs, and lives with it).
        body_hits = sum(1 for _, _, c in expected if c in decor)
        assert body_hits >= 5, \
            f"{scene}: reference render produced only {body_hits} body pixels"
        found = sum(1 for x, y in footprint if apx[x, y] in decor)
        assert found == body_hits, (
            f"{scene}: expected {body_hits} XMAS_TREE/XMAS_BULBS pixels in "
            f"the footprint, found {found}")


def test_forest_and_grove_render_identically_with_christmas_on_and_off(
        monkeypatch):
    """A physical-panel review showed the woodland star became a smudge.
    It was built and tested for these two scenes exactly like the house's
    lights and the lakefront's tree. On the real panel it read as a shape,
    never a letter, but got lost in an already busy woodland frame. Fixing
    that would mean adding pixels,
    which is this panel's own haze failure, so it was removed rather than
    brightened. Forest and grove ship with NO Christmas treatment at all.

    That is easy for a future edit to quietly undo -- add one `if
    is_christmas(local): px[...] = ...` back into either scene and nothing
    else here would notice. This is the test that would.

    Forces CHRISTMAS_FORCED directly rather than comparing two dates, so
    the only thing that can differ between the two renders is what
    is_christmas() answers -- not weather, season, moon phase, or any
    other date-driven effect a plain-vs-xmas date pair would also pick up.
    """
    wx = sky_weather.WeatherState(temp_c=-2.0)
    now = datetime(2026, 12, 25, 18, 0, tzinfo=sky_settings.TZ)
    for scene in ("forest", "grove"):
        monkeypatch.setattr(sky_settings, "CHRISTMAS_FORCED", True)
        on = sky_render_scene.render_scene(now, wx, 0, scene=scene)
        monkeypatch.setattr(sky_settings, "CHRISTMAS_FORCED", False)
        off = sky_render_scene.render_scene(now, wx, 0, scene=scene)
        assert on == off, (
            f"{scene} changed when Christmas was forced on -- forest and "
            f"grove must ship with no Christmas treatment at all")


def _traffic_band(when, wx):
    top, rows = sky_render_traffic.TRAFFIC_BAND_TOP, sky_render_traffic.TRAFFIC_BAND_ROWS
    scene = sky_render_scene.render_scene(when, wx, 1, phase=0.0, scene="backroads")
    return scene.crop((0, top, sky_limits.W, top + rows))


def _cycles_per_loop(fn, n: int = 400) -> int:
    """How many times a phase-driven signal peaks over one full loop."""
    sig = [fn(i / n) for i in range(n)]
    lo, hi = min(sig), max(sig)
    if hi - lo < 1e-9:
        return 0
    return sum(1 for i in range(n)
               if sig[i] > sig[i - 1] and sig[i] >= sig[(i + 1) % n]
               and (sig[i] - lo) > 0.25 * (hi - lo))


@pytest.mark.parametrize("wind", [10.0, 18.0, 22.0, 36.0, 40.0, 55.0])
def test_everything_that_bends_moves_on_one_wind(wind):
    """The grass may not carry motion the trees do not share.

    The verge's glint used to advance on its own clock — two cycles per
    loop above 18 km/h, three above 36 — while the gust front crossed
    once. So at any real wind the grass shimmered at a rate the poplars
    did not, and the scene read as two things moving independently
    ("the grass and the tree wind don't always move in sync").

    Wind speed may set how HARD the grass glints. It may not set the
    rate: there is one front, and everything reads it.
    """
    front = _cycles_per_loop(lambda ph: sky_render_vegetation.verge_gust(41, ph))
    glint = _cycles_per_loop(
        lambda ph: sky_render_vegetation.verge_shimmer(41, 1, ph, wind, 1.0))
    assert front == 1, f"the front itself is not one crossing: {front}"
    assert glint == front, (
        f"at {wind} km/h the grass glints {glint} times per loop while the "
        f"front crosses {front} — that is the desync viewers noticed")


def test_one_gust_front_crosses_the_whole_lane_in_order():
    """Wind arrives as a single front, so the trees bend in sequence.

    Each poplar used its own phase offset, which made five trees fidget
    independently — "real wind in a field looks more like a single
    gradient right?" is the note this encodes. If the front is coherent
    and travels left to right, each tree must reach its hardest lean
    LATER than the tree to its left.

    Measured off rendered crowns, not off the gust function: asserting
    that verge_gust is monotonic proves nothing about whether the trees
    actually read it.
    """
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState(wind_kmh=22.0, wind_dir=200.0, cloud_frac=0.2)
    frames = sky_render_scene.render_loop_frames(when, wx, 1, scene="backroads")

    def crown_centroid(px, tx):
        # Genuinely green: the status clock's orange (255,130,0) also has
        # more green than blue, and the leftmost poplar's window reaches
        # into the clock corner.
        xs = [x for x in range(max(0, tx - 5), min(sky_limits.W, tx + 6))
              for y in (4, 5)
              if max(px[x, y]) > 40
              and px[x, y][1] > px[x, y][0] and px[x, y][1] > px[x, y][2]]
        return sum(xs) / len(xs) if xs else None

    peaks = []
    for tx in sky_render_backroads.BACKROADS_POPLARS:
        offsets = []
        for i, f in enumerate(frames):
            c = crown_centroid(f.load(), tx)
            if c is not None:
                offsets.append((abs(c - tx), i))
        assert offsets, f"no crown found for the poplar at x={tx}"
        travel = max(o for o, _ in offsets) - min(o for o, _ in offsets)
        assert travel >= 0.5, (
            f"the poplar at x={tx} barely moves ({travel:.2f}px); the wind "
            f"has to be visible where the scene has contrast")
        peaks.append(max(offsets)[1])
    assert peaks == sorted(peaks), (
        f"the trees do not bend in left-to-right order: {peaks} — that is "
        f"five trees fidgeting, not one front crossing the scene")


def test_the_gust_front_is_gone_at_both_ends_of_the_loop():
    """A front still half-on-screen at phase 0 jumps the loop seam."""
    for x in (0, sky_limits.W - 1):
        for phase in (0.0, 1.0):
            assert sky_render_vegetation.verge_gust(x, phase) < 0.004, (
                f"x={x} phase={phase}: the gust is still visible at the "
                f"seam, so the loop join will pop")


def test_the_road_is_a_dark_ribbon_with_sparse_marks():
    """Not a half-and-half dither.

    At two-on/two-off the road row came out 34 dark pixels alternating
    with 33 near-white ones. On a panel whose LEDs are separated by most
    of their own width that is speckle, and a viewer shown it blind
    called it "a path, a fence, water, or nothing".
    """
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState(wind_kmh=10.0, cloud_frac=0.2)
    px = sky_render_scene.render_scene(
        when, wx, 1, phase=0.0, scene="backroads").load()
    row = sky_render_backroads._road_R(0)
    bright = [x for x in range(sky_limits.W) if max(px[x, row]) > 170]
    assert len(bright) < sky_limits.W * 0.34, (
        f"{len(bright)}/{sky_limits.W} road pixels are bright — that is a "
        f"dither, not a dashed centre line")
    assert bright, "the road lost its markings entirely"


def test_traffic_has_left_the_scene_loop():
    """The looping .anim must carry no traffic at all.

    Baked into an 8-second loop the same cars repeated ~7.5 times a
    minute, and the texture seed only turned over every ten minutes, so
    the identical trip ran about 75 times before anything changed. Cars
    now live in a one-shot overlay instead.

    The band is asserted pixel-identical across the whole loop, which
    also protects the overlay: it composites a single snapshot of these
    rows, so anything that animated here would visibly freeze for the
    length of every passing car.
    """
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState(wind_kmh=22.0, wind_dir=200.0)
    top, rows = sky_render_traffic.TRAFFIC_BAND_TOP, sky_render_traffic.TRAFFIC_BAND_ROWS
    frames = sky_render_scene.render_loop_frames(when, wx, 1, scene="backroads")
    first = frames[0].crop((0, top, sky_limits.W, top + rows)).tobytes()
    for i, f in enumerate(frames[1:], 1):
        assert f.crop((0, top, sky_limits.W, top + rows)).tobytes() == first, (
            f"frame {i}: the traffic band moved; it must stay still")


def test_an_episode_starts_and_ends_with_an_empty_road():
    """A one-shot overlay appears and disappears; if a vehicle were mid-road
    at either end it would pop into or out of existence."""
    import random as _random
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    band = _traffic_band(when, sky_weather.WeatherState())
    for trial in range(6):
        rng = _random.Random(trial)
        plan = sky_render_traffic.plan_traffic(rng, 12, False, 3)
        frames = sky_render_traffic.traffic_episode_frames(band, plan, False, (1, 1, 1))
        assert frames[0].tobytes() == band.tobytes(), (
            f"trial {trial}: a vehicle is already on the road in frame 0")
        assert frames[-1].tobytes() == band.tobytes(), (
            f"trial {trial}: a vehicle is still on the road in the last frame")


def test_no_two_episodes_are_alike():
    """Its own entropy per crossing is the whole point — no seed, no
    ten-minute bucket, no repetition."""
    import random as _random
    signatures = set()
    for trial in range(8):
        rng = _random.Random(trial)
        plan = sky_render_traffic.plan_traffic(rng, 12, False, 2)
        signatures.add(tuple(
            (v["kind"], round(v["speed"], 3), round(v["entry_s"], 2), v["far"])
            for v in plan))
    assert len(signatures) == 8, "episodes are repeating themselves"


def test_a_passing_car_goes_behind_the_poplar_trunks():
    """Same law as the freight: the trees stand in front of the road."""
    import random as _random
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    band = _traffic_band(when, sky_weather.WeatherState())
    trunks = frozenset((x, 1) for x in sky_render_backroads.BACKROADS_POPLARS)
    plan = sky_render_traffic.plan_traffic(_random.Random(3), 12, False, 3)
    frames = sky_render_traffic.traffic_episode_frames(band, plan, False, (1, 1, 1),
                                             trunks)
    for i, frame in enumerate(frames):
        for p in trunks:
            assert frame.getpixel(p) == band.getpixel(p), (
                f"frame {i}: a car painted over the trunk at {p}")


def test_traffic_thins_out_overnight():
    """A country road by the clock: rush hums, the small hours are a lone
    pair of headlights."""
    rush = sky_render_traffic.traffic_density(8)[0]
    midday = sky_render_traffic.traffic_density(12)[0]
    evening = sky_render_traffic.traffic_density(20)[0]
    night = sky_render_traffic.traffic_density(2)[0]
    assert rush < midday < evening < night
    assert sky_render_traffic.traffic_density(8)[1] > sky_render_traffic.traffic_density(2)[1]


def test_the_lane_can_be_suppressed_for_a_foreground_mask():
    """`lane=False` renders the same road without its poplars.

    The train overlay needs to know which pixels of the sky band belong to
    the foreground trees so it can keep them on top; diffing two renders is
    how it finds out, without duplicating the lane's geometry.
    """
    from datetime import timezone
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState()
    with_lane = sky_render_scene.render_scene(when, wx, 1, phase=0.0, scene="backroads")
    without = sky_render_scene.render_scene(when, wx, 1, phase=0.0, scene="backroads",
                                    lane=False)
    assert with_lane != without, "lane=False changed nothing"
    differing = {x for x in range(sky_limits.W) for y in range(sky_limits.H)
                 if with_lane.getpixel((x, y)) != without.getpixel((x, y))}
    for trunk_x in sky_render_backroads.BACKROADS_POPLARS:
        assert trunk_x in differing, f"poplar at {trunk_x} survived lane=False"


def test_a_passing_freight_never_paints_over_the_foreground_trees():
    """The trees stand in front; the freight rides a distant ridge. Whatever
    the caller marks as foreground must survive every frame of the crossing,
    or a boxcar slices a tree crown in half as it goes by."""
    import random as _random

    band = Image.new("RGB", (sky_limits.W, 6), (10, 20, 40))
    crown = {(30, 3), (31, 3), (32, 3), (31, 4), (31, 5)}
    px = band.load()
    for x, y in crown:
        px[x, y] = (58, 118, 44)

    frames = sky_render_effects._freight_frames(band, night=False,
                                      rng=_random.Random(7),
                                      foreground=frozenset(crown))
    assert len(frames) > 20
    for i, frame in enumerate(frames):
        fpx = frame.load()
        for x, y in crown:
            assert fpx[x, y] == (58, 118, 44), (
                f"frame {i}: a boxcar painted over the tree at ({x},{y})")
    # ...and the train really did run (otherwise the test proves nothing).
    assert any(frames[i].getpixel((20, 3)) != (10, 20, 40)
               for i in range(len(frames))), "no train crossed the band"


def _poplar_columns_by_row(monkeypatch, when, wx):
    """Which columns each poplar paints, per row.

    Rendered twice — once normally, once with the lane removed — so the
    difference IS the trees. That avoids asserting against foliage colours
    the renderer computes itself, which would only restate the code.
    """
    with_trees = sky_render_scene.render_scene(when, wx, 1, phase=0.0,
                                       scene="backroads").load()
    monkeypatch.setattr(sky_render_backroads, "BACKROADS_POPLARS", ())
    without = sky_render_scene.render_scene(when, wx, 1, phase=0.0,
                                    scene="backroads").load()
    rows = {}
    for y in range(sky_limits.H):
        rows[y] = [x for x in range(sky_limits.W)
                   if with_trees[x, y] != without[x, y]]
    return rows


def test_a_poplar_crosses_the_traffic_band_as_a_trunk_not_a_wall(monkeypatch):
    """Cars must pass BEHIND a trunk, not vanish into foliage.

    The crown used to reach row 11 — the road itself — five pixels wide,
    so 25 of the road's 52 columns were foliage at car height and a 4px
    car disappeared entirely between trees, then 'spawned' out the other
    side (reported from the panel, 2026-08-15). A tree occludes traffic
    with its trunk; its crown belongs in the air.
    """
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "TZ", sky_config.ZoneInfo("UTC"))
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState()
    lane = sky_render_backroads.BACKROADS_POPLARS
    rows = _poplar_columns_by_row(monkeypatch, when, wx)

    road = sky_render_backroads._road_R(0)
    for y in (road - 2, road - 1, road):        # the rows cars occupy
        painted = rows[y]
        assert len(painted) <= 2 * len(lane), (
            f"row {y}: {len(painted)} tree columns across {len(lane)} trees "
            f"— foliage is standing in the traffic band")

    # ...and the crowns must still exist, above the traffic.
    crown_rows = [y for y in range(3, road - 2) if len(rows[y]) >= 3 * len(lane)]
    assert crown_rows, "the poplars lost their crowns"


def test_a_car_is_never_completely_swallowed_by_the_lane(monkeypatch):
    """The user-facing property: on a road this short, no gap between
    occluders may be wide enough to hide a whole car."""
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "TZ", sky_config.ZoneInfo("UTC"))
    when = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
    wx = sky_weather.WeatherState()
    rows = _poplar_columns_by_row(monkeypatch, when, wx)

    body_row = sky_render_backroads._road_R(0) - 1
    occluded = set(rows[body_row])
    longest = run = 0
    for x in range(sky_limits.W):
        run = run + 1 if x in occluded else 0
        longest = max(longest, run)
    assert longest <= 2, (
        f"an unbroken {longest}px occluder sits at car height; a 4px car "
        f"disappears behind it")


def test_the_roadside_tree_never_stands_in_the_road(monkeypatch):
    """Same rule as settled snow: the road is not ground you decorate."""
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec24-26")
    wx = sky_weather.WeatherState(temp_c=-2.0)
    xmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
    decor = set(sky_render_season.XMAS_BULBS) | {sky_render_season.XMAS_TREE}
    for ph in (0.0, 0.25, 0.5, 0.75):
        px = sky_render_scene.render_scene(xmas, wx, 0, phase=ph,
                                   scene="backroads").load()
        for x in range(sky_limits.W):
            y = round(sky_render_backroads._road_R(x))
            for dy in (0, 1):
                if 0 <= y + dy < sky_limits.H:
                    assert px[x, y + dy] not in decor, \
                        f"decoration standing in the road at ({x},{y + dy})"


def test_the_lakefront_tree_never_stands_on_open_water(monkeypatch):
    """Same rule as settled snow, same rule as the road: open water is not
    ground you decorate.

    LAKEFRONT_TREE is a fixed constant, not a per-column scan like
    _road_R -- which makes it MORE rot-prone, not less, since nothing
    re-derives it if BEND_WATER_END or the bank geometry ever moves. This
    test recomputes "is this water" from BEND_WATER_END itself at test
    time (the same live source _draw_lakefront's own settled-snow block
    reads, see water_colors there), the same way
    test_the_roadside_tree_never_stands_in_the_road recomputes "is this
    the road" from _road_R rather than assuming a row -- so a future edit
    to the shoreline is caught here too, not just assumed safe because the
    anchor happened to be chosen correctly once.
    """
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec24-26")
    wx = sky_weather.WeatherState(temp_c=-2.0)
    xmas = datetime(2026, 12, 25, 12, 0, tzinfo=timezone.utc)
    decor = set(sky_render_season.XMAS_BULBS) | {sky_render_season.XMAS_TREE}
    for ph in (0.0, 0.25, 0.5, 0.75):
        px = sky_render_scene.render_scene(xmas, wx, 0, phase=ph,
                                   scene="lakefront").load()
        for y, water_end in sky_render_lakefront.BEND_WATER_END.items():
            for x in range(water_end):
                assert px[x, y] not in decor, \
                    f"decoration standing on open water at ({x},{y})"


def test_the_skyline_shows_holiday_windows(monkeypatch):
    """render_scene's real signature is (now, wx, seed, *, phase, scene,
    scrubbed) -- see test_house_scene_puts_snow_on_the_ground.

    Every other Christmas test in this file pins its render to UTC NOON at
    0,0 so _ambient() lands on the identity and raw constants are
    byte-comparable. That trick doesn't work here: the skyline only lights
    windows at all once it's dark (see _draw_skyline's lit_frac table), so
    noon would leave every window unlit and this test vacuously green. This
    uses 20:00 UTC instead -- after dark at 0,0 in December -- which means
    _ambient() is NOT guaranteed to be the identity a priori. Rather than
    assume it is, this derives the expected bulb colours by running the
    real elevation()/_ambient()/_shade() pipeline the implementation itself
    windows at all once it's dark (see _draw_skyline's lit_frac table), so
    noon would leave every window unlit and this test vacuously green.

    So it uses 20:00 UTC -- after dark at 0,0 in December -- and compares
    against the RAW bulb constants, because that is what the implementation
    writes. Lit windows are emissive: the base warm/cool ones are written
    unshaded, and the festive ones match them deliberately. Shading only the
    festive ones left them ~30% dimmer than their neighbours whenever the
    ambient was not the identity, which on this panel is the difference
    between festive and dirty.
    """
    from datetime import timezone
    monkeypatch.setattr(sky_settings, "OBSERVER",
                        Observer(latitude=0.0, longitude=0.0))
    monkeypatch.setattr(sky_settings, "CHRISTMAS_WINDOW", "dec24-26")
    wx = sky_weather.WeatherState(temp_c=-2.0)
    xmas = datetime(2026, 12, 25, 20, 0, tzinfo=timezone.utc)   # after dark
    plain = datetime(2026, 12, 18, 20, 0, tzinfo=timezone.utc)
    a = sky_render_scene.render_scene(xmas, wx, 0, scene="skyline")
    b = sky_render_scene.render_scene(plain, wx, 0, scene="skyline")
    assert a != b, "skyline ignored Christmas"

    pa, pb = a.load(), b.load()

    # "Something changed" is not sufficient -- render_scene paints a sky
    # gradient into every row and the moon phase/illumination shifts
    # between the two dates on its own, so a != b would pass even if not
    # one window ever changed colour. Confirm the recolouring specifically,
    # by exact bulb colour.
    # Raw, not shaded: the implementation writes festive windows unshaded to
    # match the emissive base windows beside them. Deriving the expected
    # colours through _shade() would pass here only because amb happens to be
    # the identity at this hour, and would fail spuriously the moment anyone
    # moved the test's clock.
    festive_colors = {sky_render_season.XMAS_BULBS[0], sky_render_season.XMAS_BULBS[1]}

    # Confirm the premise first: the plain frame must actually show lit
    # (warm/cool) windows at this hour, or every assertion below would
    # pass vacuously on a dark, unlit tower.
    window_colors = {sky_render_art.WINDOW_WARM, sky_render_art.WINDOW_COOL}
    lit_window_count_b = sum(
        1 for x in range(sky_limits.W) for y in range(sky_limits.H)
        if pb[x, y] in window_colors)
    assert lit_window_count_b > 0, \
        "test setup produced no lit windows -- pick a darker hour"

    festive_hits_a = sum(1 for x in range(sky_limits.W) for y in range(sky_limits.H)
                         if pa[x, y] in festive_colors)
    assert festive_hits_a > 0, "no windows turned red or green at Christmas"

    # And the plain frame must show none of those same bulb colours
    # anywhere in the frame -- proving the difference IS the recolouring,
    # not some unrelated frame-to-frame variation (moonlight, beacons).
    festive_hits_b = sum(1 for x in range(sky_limits.W) for y in range(sky_limits.H)
                         if pb[x, y] in festive_colors)
    assert festive_hits_b == 0, \
        "non-Christmas frame already shows red/green bulb colours"

    # The load-bearing assertion: recolouring must not relight the tower.
    #
    # The brief's own draft of this check counts frame-wide non-black
    # pixels (`px != (0, 0, 0)`) and asserts that total is identical with
    # and without Christmas. Mutation-tested below (Step 4) that check
    # turned out to be exactly the kind of thing this project's standing
    # requirement warns about: it PASSES even when the implementation is
    # mutated to light an extra window, because _draw_skyline never leaves
    # a pixel black in the first place -- the sky gradient, buildings, and
    # street paint every one of the 72*16 pixels, Christmas or not, so the
    # frame-wide non-black count is 1152 either way regardless of what
    # happens to any single window. Counting non-black pixels can only
    # catch a window relighting a pixel that was otherwise black, which
    # never happens here.
    #
    # What actually distinguishes "recolour" from "add" is the count of
    # pixels wearing a WINDOW colour -- warm, cool, or festive -- anywhere
    # in the frame. Recolouring swaps a warm/cool pixel for a festive one
    # 1-for-1, so that count is invariant; adding a new lit window (even
    # one column over, landing on facade rather than black) grows it.
    window_style = {sky_render_art.WINDOW_WARM, sky_render_art.WINDOW_COOL, *festive_colors}
    styled_a = sum(1 for x in range(sky_limits.W) for y in range(sky_limits.H)
                  if pa[x, y] in window_style)
    styled_b = sum(1 for x in range(sky_limits.W) for y in range(sky_limits.H)
                  if pb[x, y] in window_style)
    assert styled_a == styled_b, \
        "Christmas lit extra windows instead of recolouring"

    # Determinism: the same physical windows stay festive from frame to
    # frame (a window flipping colour every frame reads as a fault, not
    # decoration). Which windows are lit at all is independent of phase
    # (win_rng is seeded from `seed`/building index only), so if the
    # festive choice were phase-dependent this would catch it.
    a_mid = sky_render_scene.render_scene(xmas, wx, 0, phase=0.5, scene="skyline")
    pa_mid = a_mid.load()
    festive_a = {(x, y) for x in range(sky_limits.W) for y in range(sky_limits.H)
                if pa[x, y] in festive_colors}
    festive_a_mid = {(x, y) for x in range(sky_limits.W) for y in range(sky_limits.H)
                     if pa_mid[x, y] in festive_colors}
    assert festive_a == festive_a_mid, \
        "festive windows moved between phases -- not seeded independently"


# --- Rain -------------------------------------------------------------------
#
# All of these exist because the panel shipped raining only on its right half
# for a ten-minute stretch on 2026-08-09. Coverage used to be a lottery that
# drop count paid for; it is now a guarantee, and intensity rides elsewhere.

def _rain_wx(tier=1, **kw):
    return sky_weather.WeatherState(rain=True, rain_tier=tier, **kw)


def _drop_columns(wx, seed, phase=0.0):
    img, px = _blank()
    sky_render_precipitation.draw_rain(px, wx, seed, phase)
    return {x for x in range(sky_limits.W) for y in range(sky_limits.H)
            if px[x, y] != (0, 0, 0)}


def test_rain_fills_every_column_bucket_at_every_tier():
    """THE bug. Five drops sampled across 72 columns put all five on one side
    ~3% of the time and looked one-sided ~38% of the time -- and the seed is
    frozen for ten minutes, so it never averaged out."""
    for tier in (0, 1, 2):
        drops = sky_render_art.RAIN_TIERS[tier][0]
        for seed in range(200):
            cols = _drop_columns(_rain_wx(tier), seed)
            for i in range(drops):
                lo, hi = i * sky_limits.W // drops, (i + 1) * sky_limits.W // drops
                assert any(lo <= c < hi for c in cols), \
                    f"tier {tier} seed {seed}: no drop in columns {lo}-{hi}"


def test_rain_never_leaves_half_the_panel_dry():
    """The user-visible symptom, asserted directly rather than via buckets."""
    for tier in (0, 1, 2):
        for seed in range(200):
            cols = _drop_columns(_rain_wx(tier), seed)
            left = sum(1 for c in cols if c < sky_limits.W // 2)
            right = len(cols) - left
            assert left and right, f"tier {tier} seed {seed}: one side bare"


def test_rain_column_gaps_stay_bounded():
    """A bucket can be occupied at its edges and still leave a visible hole."""
    for tier in (0, 1, 2):
        drops = sky_render_art.RAIN_TIERS[tier][0]
        bucket = sky_limits.W / drops
        for seed in range(100):
            cols = sorted(_drop_columns(_rain_wx(tier), seed))
            gaps = [b - a for a, b in zip(cols, cols[1:])]
            assert max(gaps) <= 2 * bucket, \
                f"tier {tier} seed {seed}: {max(gaps)}px gap"


def test_rain_loop_seam_is_invisible():
    """`crossings` must be a whole number or the device's loop visibly jumps
    once every eight seconds. This is what pins RAIN_TIERS to 4/8/16."""
    for tier in (0, 1, 2):
        start, end = _blank(), _blank()
        sky_render_precipitation.draw_rain(start[1], _rain_wx(tier), 3, 0.0)
        sky_render_precipitation.draw_rain(end[1], _rain_wx(tier), 3, 1.0)
        assert start[0].tobytes() == end[0].tobytes(), \
            f"tier {tier}: phase 1.0 does not land back on phase 0.0"


def test_rain_falls_at_a_constant_rate():
    """A jitter in the per-frame step reads as stuttering rain. int() on an
    inexact i/n phase gave tier 2 a 2-3-4 step instead of a steady 3."""
    for tier in (0, 1, 2):
        crossings = sky_render_art.RAIN_TIERS[tier][1]
        span = sky_limits.H - 1
        steps = set()
        for i in range(80):
            a = round((i / 80) * crossings * span)
            b = round(((i + 1) / 80) * crossings * span)
            steps.add(b - a)
        assert max(steps) - min(steps) <= 1, f"tier {tier} steps {steps}"


SKIES = {"overcast": (79, 91, 104), "partly": (198, 200, 188),
         "clear": (255, 251, 230), "night": (12, 16, 30),
         "storm": (48, 54, 58)}


def test_streak_clears_the_contrast_floor_on_every_sky():
    """sky+55 clamps to white and lands at +0..11% on a bright sky -- rain
    that is perfectly spread and still invisible. Brightness deltas under
    ~30% per channel do not read on this panel."""
    for label, sky in SKIES.items():
        streak = sky_render_precipitation._streak_color(sky)
        for chan, (b, s) in enumerate(zip(sky, streak)):
            delta = (s - b) / max(b, 1)
            assert abs(delta) >= 0.30, \
                f"{label} channel {chan}: {delta:+.0%} is under the floor"


def test_streak_flips_polarity_rather_than_always_brightening():
    """Bright sky -> darker rain; night -> brighter. One direction cannot
    serve both."""
    assert sum(sky_render_precipitation._streak_color(SKIES["clear"])) < sum(SKIES["clear"])
    assert sum(sky_render_precipitation._streak_color(SKIES["night"])) > sum(SKIES["night"])


def test_rain_intensity_rises_with_tier():
    """Flux -- how much falls past a row per second -- is the channel that
    carries intensity now, and it must be strictly ordered."""
    flux = [drops * crossings
            for drops, crossings, _ in
            (sky_render_art.RAIN_TIERS[t] for t in (0, 1, 2))]
    assert flux[0] < flux[1] < flux[2], flux
    speeds = [sky_render_art.RAIN_TIERS[t][1] for t in (0, 1, 2)]
    assert speeds[0] < speeds[1] < speeds[2], speeds
    lengths = [sky_render_art.RAIN_TIERS[t][2] for t in (0, 1, 2)]
    assert lengths[0] < lengths[1] < lengths[2], lengths


def test_rain_never_fills_a_row():
    """The haze failure: a solid lit row reads as fog, not as rain."""
    for tier in (0, 1, 2):
        for seed in range(50):
            for phase in (0.0, 0.25, 0.5, 0.75):
                img, px = _blank()
                sky_render_precipitation.draw_rain(px, _rain_wx(tier, wind_kmh=30), seed, phase)
                for y in range(sky_limits.H):
                    lit = sum(1 for x in range(sky_limits.W)
                              if px[x, y] != (0, 0, 0))
                    assert lit < sky_limits.W, f"tier {tier} row {y} solid"


def test_a_storm_is_never_a_drizzle():
    """Storms floor the tier rather than pinning it. Pinning to 2 erased the
    observed intensity once scrubbing began replaying real history -- a
    station reporting plain "Thunderstorms and Rain" drew as a downpour."""
    storm = sky_weather.WeatherState(rain=False, thunder=True)
    assert storm.stormy
    assert sky_render_precipitation.is_raining(storm)
    assert sky_render_precipitation._rain_tier(storm) == 1          # moderate, not pinned up
    assert sky_render_precipitation._rain_tier(
        sky_weather.WeatherState(rain=True, rain_tier=0, thunder=True)) == 1
    assert sky_render_precipitation._rain_tier(
        sky_weather.WeatherState(rain=True, rain_tier=2, thunder=True)) == 2


def test_snow_suppresses_rain():
    both = sky_weather.WeatherState(rain=True, snow=True)
    assert not sky_render_precipitation.is_raining(both)


def test_an_out_of_range_tier_does_not_crash():
    """rain_tier arrives from a remote radar feed, so bound it at the edge."""
    for tier in (-1, 3, 99):
        assert sky_render_precipitation._rain_tier(_rain_wx(tier)) in (0, 1, 2)


def test_raining_scenes_get_the_double_rate_loop():
    """At 40 frames a downpour jumps 6 of 16 rows per frame and strobes."""
    now = datetime(2026, 8, 9, 17, 0, tzinfo=timezone.utc)
    wet = sky_render_scene.render_loop_frames(now, _rain_wx(2), 1, scene="house")
    dry = sky_render_scene.render_loop_frames(
        now, sky_weather.WeatherState(), 1, scene="house")
    assert len(wet) == 80, len(wet)
    assert len(dry) == sky_limits.ANIM_FRAMES, len(dry)


def test_snow_keeps_the_single_rate_loop():
    """Snow drifts at 0.75 rows/frame already; it does not need the frames."""
    now = datetime(2026, 8, 9, 17, 0, tzinfo=timezone.utc)
    snowy = sky_render_scene.render_loop_frames(
        now, sky_weather.WeatherState(snow=True), 1, scene="house")
    assert len(snowy) == sky_limits.ANIM_FRAMES, len(snowy)


# --- Snow -------------------------------------------------------------------
#
# Snow was never visibly broken: at 0.75 rows/frame int() and round() agree,
# flakes clear the contrast floor by 100%+, and 12 of them clump onto one side
# in only ~0.7% of seeds. These lock in the guarantees anyway, so that raising
# SNOW_CROSSINGS cannot quietly reintroduce what draw_rain had to fix.

def _snow_columns(seed, phase=0.0):
    img, px = _blank()
    sky_render_precipitation.draw_snow(px, seed, phase)
    return {x for x in range(sky_limits.W) for y in range(sky_limits.H)
            if px[x, y] != (0, 0, 0)}


def test_snow_never_leaves_half_the_panel_bare():
    for seed in range(200):
        cols = _snow_columns(seed)
        left = sum(1 for c in cols if c < sky_limits.W // 2)
        assert left and len(cols) - left, f"seed {seed}: one side bare"


def test_snow_column_gaps_stay_bounded():
    bucket = sky_limits.W / sky_render_precipitation.SNOW_FLAKES
    for seed in range(200):
        for phase in (0.0, 0.25, 0.5, 0.75):
            cols = sorted(_snow_columns(seed, phase))
            gaps = [b - a for a, b in zip(cols, cols[1:])]
            assert max(gaps) <= 2 * bucket + 1, \
                f"seed {seed} phase {phase}: {max(gaps)}px gap"


def test_snow_loop_seam_is_invisible():
    """Both the fall and the sway must land back where they started."""
    for seed in (0, 7, 99):
        start, end = _blank(), _blank()
        sky_render_precipitation.draw_snow(start[1], seed, 0.0)
        sky_render_precipitation.draw_snow(end[1], seed, 1.0)
        assert start[0].tobytes() == end[0].tobytes(), f"seed {seed}: seam"


def test_snow_falls_at_a_constant_rate():
    span = sky_limits.H - 1
    n = sky_limits.ANIM_FRAMES
    steps = {round(((i + 1) / n) * sky_render_precipitation.SNOW_CROSSINGS * span)
             - round((i / n) * sky_render_precipitation.SNOW_CROSSINGS * span)
             for i in range(n)}
    assert max(steps) - min(steps) <= 1, steps


def test_snow_crossings_stay_a_whole_number():
    """The loop seam depends on it, exactly as rain's does."""
    assert sky_render_precipitation.SNOW_CROSSINGS == int(sky_render_precipitation.SNOW_CROSSINGS)


def test_flakes_clear_the_contrast_floor_against_the_snow_sky():
    """Snow darkens its own sky (dim=0.6), which is what buys the flakes
    their contrast. A change to that dimming would silently erase them."""
    now = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    for cloud in (0.4, 0.9):
        wx = sky_weather.WeatherState(cloud_frac=cloud, snow=True, temp_c=-3.0)
        frame = sky_render_scene.render_scene(now, wx, 12345, phase=0.0,
                                      scene="lakefront")
        p = frame.load()
        for y in (1, 4, 8):
            sky = p[36, y]
            for chan, (b, f) in enumerate(zip(sky, sky_render_precipitation.SNOW_FLAKE_COLOR)):
                delta = (f - b) / max(b, 1)
                assert abs(delta) >= 0.30, \
                    f"cloud {cloud} row {y} channel {chan}: {delta:+.0%}"


def test_snow_still_suppresses_rain_in_the_composed_frame():
    """The two must never fall together; snow owns the panel when it does.

    Compared across rain_tier rather than against a rain-free frame: wx.rain
    legitimately tints the palette even when no drop is drawn, so an equality
    against snow-only would be asserting the wrong thing. Tier changes only
    the drops, so if any drew, drizzle and downpour would differ here.
    """
    now = datetime(2026, 1, 15, 14, 0, tzinfo=timezone.utc)
    frames = []
    for tier in (0, 2):
        wx = sky_weather.WeatherState(cloud_frac=0.9, rain=True, snow=True,
                                   rain_tier=tier, temp_c=-3.0)
        assert not sky_render_precipitation.is_raining(wx)
        frames.append(sky_render_scene.render_scene(
            now, wx, 5, phase=0.0, scene="lakefront").tobytes())
    assert frames[0] == frames[1], "rain drew underneath the snow"

"""Build the demo GIFs in docs/media/ — reproducible, not hand-made.

    uv run scripts/make_demo_gifs.py

Frames are rendered by the apps' production renderers. The one exception is
labelled below: dsn's wheel pop-up is a device TEXT element in the firmware's
own `condensed` font, which cannot be rendered host-side, so that clip
approximates it with the app's pixel font.

Why not screen-record the panel? The device's screen-readback API returns a
static snapshot that does not advance with `.anim` playback — 60 captures over
24 seconds came back with 47 frames byte-identical to their predecessor and a
maximum of 2 pixels changed. The bar was visibly animating the whole time.
Readback shows you the composited scene, not the frame currently on the LEDs.

The app GIFs use fixed timestamps and explicit public configuration fixtures.
With the locked dependency set, `--check` proves the checked-in bytes match a
fresh render. Barkeep's optional clip is the exception: it is captured live
from an explicitly supplied host, so it differs every run by design.

Every frame is drawn with the LED gaps simulated: the panel's LEDs are 1.23 mm
lit on a 2.2 mm pitch, so a naive upscale (solid adjacent squares) flatters the
design in a way the hardware never will.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

from busybar_viz.panel import panelise

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "apps"))
OUT = ROOT / "docs" / "media"
APP_GIFS = (
    "dsn-browsing.gif",
    "dsn-focus.gif",
    "dsn-instrument.gif",
    "dsn-network.gif",
    "dsn-picker.gif",
    "dsn-realtime.gif",
    "dsn-skies.gif",
    "skystrip-christmas.gif",
    "skystrip-day.gif",
    "skystrip-seasons.gif",
    "skystrip-weather.gif",
)

# Preserve this script's established documentation presentation.  The shared
# visualizer defaults model the measured 10:8 package spacing on black; these
# older demo assets intentionally used a more compact 6:3 grid on charcoal.
DEMO_LED_SIZE = 6
DEMO_GAP_SIZE = 3
DEMO_BACKGROUND = (10, 10, 12)


def save_gif(name: str, frames: list[Image.Image], ms: int) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    shots = [
        panelise(
            frame,
            led_size=DEMO_LED_SIZE,
            gap_size=DEMO_GAP_SIZE,
            background=DEMO_BACKGROUND,
            package_offset=0,
        )
        for frame in frames
    ]
    path = OUT / name
    shots[0].save(path, save_all=True, append_images=shots[1:],
                  duration=ms, loop=0, optimize=True)
    kb = path.stat().st_size / 1024
    try:
        display_path = path.relative_to(ROOT)
    except ValueError:
        display_path = path
    print(f"  {display_path}  {len(shots)} frames  {kb:.0f} KB")


def dsn_gifs() -> None:
    from apps.dsn_app import config as dsn_config
    from apps.dsn_app import limits as dsn_limits
    from apps.dsn_app import settings as dsn_settings
    from apps.dsn_app import source as dsn_source
    from apps.dsn_app.render import distance as dsn_render_distance
    from apps.dsn_app.render import instrument as dsn_render_instrument
    from apps.dsn_app.render import network_dishes as dsn_render_network_dishes
    from apps.dsn_app.render import network_skies as dsn_render_network_skies
    from apps.dsn_app.render import text as dsn_render_text

    # Never inherit a maintainer's dotenv or a prior in-process configuration.
    dsn_settings.apply_runtime_config(dsn_config.parse_runtime_config({}))
    names = {"vgr2": "Voyager 2", "mro": "Mars Reconnaissance Orbiter"}
    # DSS-43 is a 70 m dish and DSS-36 a 34 m one. This map was built and then
    # never passed, so every demo GIF rendered both at the unknown-size default
    # — the dish icon is size-aware and was drawing a fact it had been given.
    types = {"DSS43": "70M", "DSS36": "34M"}
    voyager = dsn_source.Link("Canberra", "DSS43", "VGR2", 32, "X", 160, True,
                       2.1e10, 0.0, down_dbm=-155.0, up_kw=18.0, streams=1)
    mars = dsn_source.Link("Canberra", "DSS36", "MRO", 30, "X", 1.5e6, True,
                    2.95e8, 0.0, down_dbm=-120.0, up_kw=5.0, streams=1)

    when = datetime(2026, 8, 6, 18, 30, tzinfo=timezone.utc)
    frames, fps, _ = dsn_render_distance.render_frames(voyager, when, names, dish_types=types)
    save_gif("dsn-browsing.gif", frames, int(1000 / fps))

    # Real time: ONE message, followed from the instant of the lock. Sampled
    # every 27 minutes of real time, because that is what a single pixel of
    # travel costs at Voyager's distance. The clock does not move: it is that
    # message's departure time, and it holds until the message lands.
    light = voyager.light_s
    lock = 1_780_000_000.0
    step = light / (dsn_limits.TRACK1 - dsn_limits.TRACK0)
    creep = [dsn_render_distance.render_frames(voyager,
                               datetime.fromtimestamp(lock + i * step, timezone.utc),
                               names, realtime_since=lock,
                               dish_types=types)[0][0]
             for i in range(46)]
    save_gif("dsn-realtime.gif", creep, 110)

    # The pop-up that rides the wheel, then the scene it commits to.
    # APPROXIMATION: the real pop-up is a device TextElement in the firmware's
    # `condensed` font. There is no host-side renderer for that font, so this
    # stands in with the app's own. Shape and timing are right; the letterforms
    # are not the ones on the panel.
    links = [("JNO", 1), ("SOHO", 2), ("MRO", 3), ("VGR2", 4), ("LUCY", 5)]
    picker = []
    for craft, idx in links:
        img = Image.new("RGB", (dsn_limits.W, dsn_limits.H), (0, 0, 0))
        dsn_render_text._text(img.load(), 4, 5, f"{craft} {idx}/5", (255, 217, 140))
        picker.extend([img] * 3)
    picker.extend(dsn_render_distance.render_frames(mars, when, names, dish_types=types)[0][:20])
    save_gif("dsn-picker.gif", picker, 150)

    # The Network view: the default. A literal site -> physical dish -> live
    # link-count roster of what all three complexes are doing at once, which
    # is the thing the Distance view above deliberately cannot show, because
    # it follows one message.
    roster = [
        dsn_source.Link("Goldstone", "DSS14", "VGR1", 24, "X", 160, True, 2.5e10,
                 down_dbm=-156.0, up_kw=20.0, streams=1, azimuth=218.0),
        # Two craft on one aperture (MSPA), so Focus below has co-dish links
        # to show rather than a dish carrying one contact.
        dsn_source.Link("Goldstone", "DSS24", "MRO", 41, "X", 2.0e6, True, 2.9e8,
                 down_dbm=-119.0, up_kw=5.0, streams=2, mspa=True,
                 azimuth=143.0),
        dsn_source.Link("Goldstone", "DSS24", "ODY", 41, "X", 1.2e5, False, 2.9e8,
                 down_dbm=-127.0, streams=1, mspa=True, azimuth=143.0),
        dsn_source.Link("Goldstone", "DSS26", "MVN", 55, "X", 5.6e5, False, 3.0e8,
                 down_dbm=-124.0, streams=1, azimuth=97.0),
        dsn_source.Link("Madrid", "DSS63", "VGR2", 18, "X", 160, True, 2.1e10,
                 down_dbm=-155.0, up_kw=18.0, streams=1, azimuth=64.0),
        dsn_source.Link("Madrid", "DSS54", "JWST", 62, "K", 2.8e7, False, 1.5e6,
                 down_dbm=-118.0, streams=2, azimuth=181.0),
        dsn_source.Link("Canberra", "DSS43", "JNO", 33, "X", 0, True, 9.4e8,
                 down_dbm=-141.0, up_kw=20.0, streams=1, azimuth=305.0),
        dsn_source.Link("Canberra", "DSS36", "TESS", 66, "S", 1.6e4, False, 3.0e5,
                 down_dbm=-131.0, streams=1, azimuth=12.0),
    ]
    frames, fps, _ = dsn_render_network_dishes.render_dish_network_frames(roster)
    save_gif("dsn-network.gif", frames, int(1000 / fps))

    # Selected-dish Focus: what a rested wheel opens. One physical dish, its
    # real aim, and every link sharing it — the thing the roster above counts
    # but cannot show. Goldstone's DSS24 is carrying two.
    focus_key = roster[1].key            # Goldstone DSS24, carrying two
    frames, fps, _ = dsn_render_network_dishes.render_dish_focus_frames(roster, names=names,
                                                  selected_key=focus_key)
    save_gif("dsn-focus.gif", frames, int(1000 / fps))

    # Three Skies: the same network as three local horizons, one per complex,
    # so elevation is read as height rather than as a number.
    frames, fps, _ = dsn_render_network_skies.render_three_skies_frames(roster, names=names)
    save_gif("dsn-skies.gif", frames, int(1000 / fps))

    # The Instrument view: one selected antenna and contact, in detail. The
    # RF lanes move because the numbers behind them do.
    frames, fps, _ = dsn_render_instrument.render_instrument_frames(voyager, names=names)
    save_gif("dsn-instrument.gif", frames, int(1000 / fps))


def skystrip_gifs() -> None:
    from apps.skystrip_app import config as sky_config
    from apps.skystrip_app import settings as sky_settings
    from apps.skystrip_app import weather as sky_weather
    from apps.skystrip_app.render import scene as sky_render_scene

    # Greenwich is a recognisable public fixture, not the maintainer's location.
    # Supplying every location field also prevents import defaults or owner
    # dotenv values from changing documentation output.
    public_fixture = sky_config.parse_runtime_config({
        "SKYSTRIP_LAT": "51.4769",
        "SKYSTRIP_LON": "0.0005",
        "SKYSTRIP_TZ": "Europe/London",
        "SKYSTRIP_UNITS": "c",
        "SKYSTRIP_STYLE": "plain",
    })
    sky_settings.apply_runtime_config(public_fixture)

    # A whole day in one loop: solstice dawn to the following dawn, every
    # half hour. The sun's position is real astronomy, so the gradient, the
    # stars appearing and the moon are all doing what they would that day.
    clear = sky_weather.WeatherState(cloud_frac=0.15, temp_c=24.0, wind_kmh=8.0)
    start = datetime(2026, 6, 21, 4, 0, tzinfo=sky_settings.TZ)
    frames = [sky_render_scene.render_scene(start + timedelta(minutes=30 * i),
                                    clear, seed=7, scene="house")
              for i in range(48)]
    save_gif("skystrip-day.gif", frames, 120)

    # The seasons. Same scene, same hour, same weather — only the date moves,
    # so every difference you see is the calendar: the broadleaf crowns going
    # to autumn quilt, bare winter lattice, fresh spring green, and the summer
    # field palette. Two frames a month so precipitation-free air still drifts.
    # Ground snow belongs in this one. On the device the depth is whatever
    # Open-Meteo says is really lying outside, so a bare February is bare --
    # but a seasons loop that shows snowless Decembers is showing the calendar
    # with its most obvious season missing, which is exactly what got noticed.
    # A typical continental winter, tapering into and out of the shoulders.
    lying = {12: 0.18, 1: 0.30, 2: 0.22, 3: 0.05, 11: 0.02}
    frames = []
    for month in range(1, 13):
        clearish = sky_weather.WeatherState(cloud_frac=0.2, temp_c=14.0,
                                         wind_kmh=7.0,
                                         snow_depth_m=lying.get(month, 0.0))
        when = datetime(2026, month, 15, 15, 0, tzinfo=sky_settings.TZ)
        for i in range(3):
            frames.append(sky_render_scene.render_scene(when, clearish, seed=5,
                                                phase=i / 3, scene="grove"))
    save_gif("skystrip-seasons.gif", frames, 260)

    # And one hour under weather it is not currently having. Midday rather
    # than dusk so the precipitation is actually visible against the sky.
    noon = datetime(2026, 11, 3, 13, 30, tzinfo=sky_settings.TZ)
    weathers = [
        sky_weather.WeatherState(cloud_frac=0.0, temp_c=8.0, wind_kmh=5.0),
        sky_weather.WeatherState(cloud_frac=1.0, temp_c=6.0, wind_kmh=10.0),
        sky_weather.WeatherState(cloud_frac=0.8, rain=True, rain_tier=0,
                              temp_c=7.0, wind_kmh=12.0),          # drizzle
        sky_weather.WeatherState(cloud_frac=0.95, rain=True, rain_tier=2,
                              temp_c=5.0, wind_kmh=25.0),          # downpour
        sky_weather.WeatherState(cloud_frac=1.0, rain=True, thunder=True,
                              temp_c=12.0, wind_kmh=40.0),         # storm
        sky_weather.WeatherState(cloud_frac=1.0, rain=True, thunder=True,
                              severe=True, severe_event="Severe Thunderstorm",
                              temp_c=14.0, wind_kmh=60.0),         # warned
        sky_weather.WeatherState(cloud_frac=0.9, snow=True, temp_c=-3.0,
                              wind_kmh=15.0),                      # snow
        # Falling snow with nothing on the ground is only the first hour of a
        # storm. These two are the rest of it: snow landing, and then the
        # clear cold day afterwards where the sky is done but the ground is
        # not. That second one is the whole reason settled depth is a
        # separate thing from `snow`.
        sky_weather.WeatherState(cloud_frac=0.95, snow=True, temp_c=-4.0,
                              wind_kmh=20.0, snow_depth_m=0.12),   # settling
        sky_weather.WeatherState(cloud_frac=0.1, temp_c=-8.0, wind_kmh=6.0,
                              snow_depth_m=0.35),                  # the morning after
    ]
    frames = []
    for wx in weathers:
        for i in range(8):
            frames.append(sky_render_scene.render_scene(noon, wx, seed=7,
                                                phase=i / 8, scene="house"))
    save_gif("skystrip-weather.gif", frames, 130)

    # The Christmas treatment, off then on, for the two scenes that read on
    # hardware: the roofline string at the house and the recoloured windows
    # in the skyline. The subject here is the DIFFERENCE, so weather, seed
    # and clock stay fixed within each pair and only the decoration toggles.
    # December, after dark -- the skyline recolour only touches windows that
    # are already lit, so there is nothing to see before dusk. 17:40 local
    # is where this was checked on the device.
    dusk = datetime(2026, 12, 24, 17, 40, tzinfo=sky_settings.TZ)
    cold_clear = sky_weather.WeatherState(cloud_frac=0.15, temp_c=-4.0,
                                       wind_kmh=6.0)
    frames = []
    for scene in ("house", "skyline"):
        for on in (False, True):
            sky_settings.CHRISTMAS_FORCED = on
            for i in range(8):
                frames.append(sky_render_scene.render_scene(dusk, cold_clear, seed=7,
                                                    phase=i / 8, scene=scene))
    sky_settings.CHRISTMAS_FORCED = None  # don't leave this set for the rest
    save_gif("skystrip-christmas.gif", frames, 130)


def barkeep_gif(host: str) -> None:
    """Barkeep's own UI, captured live over ~30 seconds.

    Headless Chrome rather than the browser-automation tools, which need the
    Claude extension connected. The interesting part is that the page mirrors
    both displays live, so the frames genuinely differ as the bar rotates.
    """
    import shutil
    import subprocess
    import tempfile

    # PATH first: a headless browser on PATH is the portable case. The bundle
    # path below is a last resort for one platform that does not put it there.
    chrome = (shutil.which("chromium") or shutil.which("chromium-browser")
              or shutil.which("google-chrome") or shutil.which("chrome") or "")
    if not chrome:
        bundled = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        chrome = bundled if Path(bundled).exists() else ""
    if not chrome:
        print("  barkeep skipped: no Chrome found")
        return
    with tempfile.TemporaryDirectory() as tmp:
        shots = []
        for i in range(12):
            out = Path(tmp) / f"b{i:02d}.png"
            subprocess.run(
                [chrome, "--headless", "--disable-gpu", "--hide-scrollbars",
                 "--window-size=1280,620", "--virtual-time-budget=2500",
                 f"--screenshot={out}", f"http://{host}/"],
                capture_output=True, check=False)
            if out.exists():
                shots.append(Image.open(out).convert("RGB")
                             .crop((140, 60, 1140, 600)))
        if not shots:
            print(f"  barkeep skipped: {host} unreachable")
            return
        frames = [s.resize((s.width // 2, s.height // 2), Image.LANCZOS)
                  for s in shots]
        OUT.mkdir(parents=True, exist_ok=True)
        frames[0].save(OUT / "barkeep.gif", save_all=True,
                       append_images=frames[1:], duration=700, loop=0,
                       optimize=True)
        shots[0].save(OUT / "barkeep.png")
        print(f"  {OUT / 'barkeep.gif'}  {len(frames)} frames  "
              f"{(OUT / 'barkeep.gif').stat().st_size / 1024:.0f} KB")


def render_app_gifs(output_dir: Path) -> None:
    global OUT

    previous = OUT
    OUT = output_dir
    try:
        dsn_gifs()
        skystrip_gifs()
    finally:
        OUT = previous


def verify_app_gifs(expected_dir: Path) -> bool:
    with tempfile.TemporaryDirectory(prefix="busybar-demo-gifs-") as tmp:
        actual_dir = Path(tmp)
        render_app_gifs(actual_dir)
        changed = [
            name for name in APP_GIFS
            if not (expected_dir / name).is_file()
            or (expected_dir / name).read_bytes() != (actual_dir / name).read_bytes()
        ]
    if changed:
        print("demo GIF check failed: " + ", ".join(changed), file=sys.stderr)
        return False
    print(f"demo GIF check passed: {len(APP_GIFS)} deterministic app clips")
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output-dir", type=Path, default=ROOT / "docs" / "media",
        help="write or verify media here (default: docs/media)",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="render into a temporary directory and compare checked-in bytes",
    )
    parser.add_argument(
        "--barkeep-host",
        help="also capture the live Barkeep UI from this explicit host:port",
    )
    args = parser.parse_args(argv)

    print("rendering demo GIFs:")
    if args.check:
        if args.barkeep_host:
            parser.error("--barkeep-host cannot be combined with --check")
        return 0 if verify_app_gifs(args.output_dir) else 1

    global OUT
    OUT = args.output_dir
    render_app_gifs(args.output_dir)
    if args.barkeep_host:
        try:
            barkeep_gif(args.barkeep_host)
        except Exception as exc:  # noqa: BLE001 - needs a running barkeep
            print(f"  barkeep skipped: {exc}")
    else:
        print("  barkeep skipped: pass --barkeep-host HOST:PORT for a live capture")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

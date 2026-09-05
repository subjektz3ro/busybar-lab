"""Live sky strip — the actual sky outside your window, on the bar.

Calculated solar elevation drives the base gradient (astral math, no API).
Weather observations mute it with cloud cover and shift it storm green-grey
under observed thunder; nearby reports from an optional, operator-authorized
lightning feed briefly illuminate the rendered sky backdrop and pulse the top
LEDs.
Over that sky sits the original house art (apps/assets/house.png), a local
lunar-phase cue or the sun with a soft glow, cloud-aware stars, falling rain or
snow when the weather feeds report it, and a tiny clock.

    uv run apps/skystrip.py --enable-network-providers
                                       # run the watcher (Ctrl+C clears the bar)
    uv run apps/skystrip.py --once     # local snapshot; no provider polling
    uv run apps/skystrip.py --report --enable-network-providers
                                       # fetch and speak one weather report
    uv run apps/skystrip.py --preview scratch/sky.png [--at 03:30] [--cloud 0.5]
                                       # render a frame to PNG only, no device

Elements carry a timeout so the bar self-clears if the watcher dies. Draws
yield politely (HTTP 409) while a BUSY/CUSTOM session owns the display.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime, timezone

from astral import moon
from PIL import Image

from apps.skystrip_app import config as _config
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import runtime as _runtime
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from apps.skystrip_app.audio import output as _audio_output
from apps.skystrip_app.audio import report as _audio_report
from apps.skystrip_app.audio import report_policy as _audio_report_policy
from apps.skystrip_app.providers import weather as _providers_weather
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import scene as _render_scene
from apps.skystrip_app.render import status as _render_status
from busybar_dev import aconnect
from busybar_dev.config import CoordinateRedactingFilter
from busybar_dev.device import is_refusal as _is_refusal


def _report_inputs_ready(state: _model.SkyState) -> bool:
    """Whether CLI narration has every forecast source available here."""
    return bool(state.hourly and (state.forecast or state.nws_point_covered is False))


async def report_once() -> None:
    """Prepare and speak the live report synchronously for CLI auditioning."""
    state = _model.SkyState()
    bb = await aconnect()
    poller = asyncio.create_task(_providers_weather.poll_nws(state))
    try:
        for _ in range(80):  # up to ~20s for the sources available here
            if _report_inputs_ready(state):
                break
            await asyncio.sleep(0.25)
        await asyncio.sleep(1.0)  # let the obs land too
        text = _audio_report_policy._current_report_text(state)
        fname = await _audio_report._prepare_report_take(bb, state, text)
        try:
            await _audio_output._play_audio(bb, state, fname, "report", lambda: True)
        except Exception as exc:
            # _play_audio re-raises a refusal on purpose — it must not answer a
            # 409 with the device-global STOP, which would silence another app.
            # Catching it belongs here, in the one-shot CLI path the workflow
            # tells you to run on hardware.
            if not _is_refusal(exc):
                raise
            _limits.logger.info(
                "a BUSY/CUSTOM session owns audio; the report will keep"
            )
    finally:
        poller.cancel()
        try:
            await poller
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass
        await bb.aclose()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--report",
        action="store_true",
        help=(
            "fetch and speak one live weather report; standalone use also "
            "requires --enable-network-providers"
        ),
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="push one local snapshot; no provider polling",
    )
    parser.add_argument(
        "--enable-network-providers",
        action="store_true",
        help=(
            "standalone live/report modes only: allow Open-Meteo and, for "
            "the watcher, RainViewer requests after reviewing their terms; "
            "this flag does not grant data rights"
        ),
    )
    parser.add_argument(
        "--preview",
        metavar="PNG",
        help="render current scene to a PNG and exit (no device or provider I/O)",
    )
    parser.add_argument(
        "--at", metavar="HH:MM", help="preview only: pretend it's this local time"
    )
    parser.add_argument(
        "--cloud", type=float, default=None, help="preview only: cloud fraction 0..1"
    )
    parser.add_argument(
        "--storm", action="store_true", help="preview only: storm palette"
    )
    parser.add_argument("--rain", action="store_true", help="preview only")
    parser.add_argument(
        "--raintier",
        type=int,
        default=1,
        choices=(0, 1, 2),
        help="preview only: 0 drizzle / 1 rain / 2 downpour",
    )
    parser.add_argument("--snow", action="store_true", help="preview only")
    parser.add_argument(
        "--fog", action="store_true", help="force reported fog (preview only)"
    )
    parser.add_argument(
        "--obscuration",
        choices=("haze", "smoke", "dust", "ash"),
        help="force an obscuration (preview only)",
    )
    parser.add_argument(
        "--snowdepth",
        type=float,
        default=0.0,
        metavar="METRES",
        help="settled snow on the ground, preview only "
        "(0.01=dusting, 0.08=covered, 0.25=deep)",
    )
    parser.add_argument(
        "--wind", type=float, default=0.0, help="preview only: wind speed km/h"
    )
    parser.add_argument(
        "--winddir",
        type=float,
        default=None,
        help="preview only: wind FROM direction, degrees",
    )
    parser.add_argument(
        "--temp", type=float, default=20.0, help="preview only: temperature C"
    )
    parser.add_argument(
        "--month", type=int, default=None, help="preview only: pretend month 1-12"
    )
    parser.add_argument(
        "--humidity",
        type=float,
        default=50.0,
        help="preview only: relative humidity %%",
    )
    parser.add_argument(
        "--vis", type=float, default=16000.0, help="preview only: visibility in meters"
    )
    parser.add_argument(
        "--scene",
        choices=_config.SCENES,
        default="house",
        help="preview only: which scene to render",
    )
    parser.add_argument(
        "--moonday",
        type=float,
        default=None,
        help="preview only: force moon phase day 0-29.5",
    )
    parser.add_argument(
        "--christmas",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="force the Christmas treatment on or off, "
        "preview only (default: follow the date)",
    )
    return parser


OPEN_METEO_NOTICE = (
    "Weather data by Open-Meteo.com (CC BY 4.0; free API non-commercial). "
    "Terms: https://open-meteo.com/en/terms"
)

RAINVIEWER_NOTICE = (
    "Weather radar data by RainViewer (public API for personal, educational, "
    "and small-scale community use). Terms: "
    "https://www.rainviewer.com/api.html"
)


def _provider_network_required(args: argparse.Namespace) -> bool:
    """Whether the selected CLI path starts a built-in provider poller.

    Preview is renderer-only. ``--once`` talks to the bar but renders the
    local default snapshot; it never starts Open-Meteo or RainViewer polling.
    ``--report`` takes precedence over ``--once`` in :func:`main`, so their
    combined spelling still requires the provider boundary.
    """
    return args.preview is None and (args.report or not args.once)


def _provider_network_enabled(args: argparse.Namespace) -> bool:
    """Whether this process crossed a supported provider-use boundary."""
    return (
        not _provider_network_required(args)
        or args.enable_network_providers
        or os.environ.get("BARKEEP_MANAGED") == "1"
    )


def _provider_notice(args: argparse.Namespace) -> str:
    """Return attribution for the providers this exact CLI mode will call."""
    if args.report:
        return f"Skystrip data: {OPEN_METEO_NOTICE}"
    return f"Skystrip data: {OPEN_METEO_NOTICE} {RAINVIEWER_NOTICE}"


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # Timestamps: dsn has always had them and skystrip has not, so 800 kB of
    # the primary debugging surface on a headless Pi could not be correlated
    # with anything. The redaction filter is a backstop behind
    # describe_exception, so the next code path that formats a URL is covered
    # by construction rather than by review.
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(CoordinateRedactingFilter())
    logging.getLogger("busylib").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        _settings.configure_runtime()
    except ValueError as exc:
        # Under systemd a raw ValueError is a silent restart loop with a dark
        # panel; the journal should carry one named sentence instead.
        raise SystemExit(f"skystrip configuration error: {exc}") from None
    # Before app work, and on stderr, because an unconfigured install otherwise
    # looks like a working one.
    if unlocated := _settings.warn_if_unlocated():
        print(f"warning: {unlocated}", file=sys.stderr)

    if args.preview:
        now = datetime.now(timezone.utc)
        if args.at:
            hh, mm = map(int, args.at.split(":"))
            now = (
                datetime.now(_settings.TZ)
                .replace(hour=hh, minute=mm)
                .astimezone(timezone.utc)
            )
        if args.month:
            now = (
                now.astimezone(_settings.TZ)
                .replace(month=args.month, day=15)
                .astimezone(timezone.utc)
            )
        wx = _weather.WeatherState(
            cloud_frac=args.cloud if args.cloud is not None else 0.0,
            rain=args.rain,
            rain_tier=args.raintier,
            snow=args.snow,
            thunder=args.storm,
            wind_kmh=args.wind,
            wind_dir=args.winddir,
            temp_c=args.temp,
            humidity=args.humidity,
            visibility_m=args.vis,
            snow_depth_m=args.snowdepth,
            fog=args.fog,
            obscuration=args.obscuration or "",
        )
        if args.moonday is not None:
            _render_primitives.MOON_DAY_OVERRIDE = args.moonday
        if args.christmas is not None:
            _settings.CHRISTMAS_FORCED = args.christmas
        frames = _render_scene.render_loop_frames(now, wx, seed=0, scene=args.scene)
        big = [
            f.resize((_limits.W * 8, _limits.H * 8), Image.Resampling.NEAREST)
            for f in frames
        ]
        if args.preview.endswith(".gif"):
            big[0].save(
                args.preview,
                save_all=True,
                append_images=big[1:],
                duration=1000 // _limits.ANIM_FPS,
                loop=0,
            )
        else:
            big[0].save(args.preview)
        print(
            f"saved {args.preview} (clock {_render_status.clock_str(now)}; "
            f"moon phase day {moon.phase(now.astimezone(_settings.TZ).date()):.1f}/29.5)"
        )
        return
    if not _provider_network_enabled(args):
        providers = "Open-Meteo" if args.report else "Open-Meteo/RainViewer"
        parser.error(
            f"standalone {providers} polling is off. Review "
            "apps/skystrip.md#provider-terms-and-commercial-use, then rerun "
            "with --enable-network-providers. The flag enables requests; it "
            "does not grant rights or assert that your use meets provider "
            "terms. --once by itself and --preview do not poll these providers."
        )
    if _provider_network_required(args):
        # Flush before creating the coroutine: the attribution and use limits
        # must be visible before the first provider request, not merely after
        # a fast HTTP response has already arrived.
        print(_provider_notice(args), file=sys.stderr, flush=True)
    if args.report:
        asyncio.run(report_once())
        return
    asyncio.run(_runtime.run(args.once))

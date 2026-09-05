"""dsn — NASA's Deep Space Network, live, on the 72x16 front strip.

    uv run apps/dsn.py --dry-run
    uv run apps/dsn.py --once
    uv run apps/dsn.py

The default Network view is a literal three-site ground-dish roster: site link
total, active dish suffixes, and an attached count when one physical antenna
carries several spacecraft links. A rested wheel choice opens a bounded
selected-dish Focus with one source-reported local alt-az aim and the links
that share it. Hold the wheel to move through that global view, a selected-link
antenna/RF instrument, and the original Earth-to-spacecraft distance journey.
The prior Three Skies triptych and legacy paged contact rows remain available
with DSN_NETWORK_STYLE=skies and rows. The distance renderer remains:
a represented carrier slice crosses at the published one-way light-time
estimate, normally compressed 600x and at that estimated cadence when locked.

Controls: the wheel moves through live signals behind an instant pop-up (the
scene commits when it rests); a tap starts the original real-time light-time
watch; a hold cycles Network, Instrument and Distance; START narrates
the pass. Narration is cache-only on the button path: a missing line is
prioritised for background preparation and acknowledged immediately in pixels.

Drawn mostly-OFF on purpose. The panel's LEDs are physically spaced about a
pixel apart, so a filled background reads as a haze of separated dots and
drowns whatever sits on it; black is truly off, the gaps disappear into it,
and only lit pixels carry shape. Nothing here is a gradient.

Everything is gated on the live feed: no link, no scene.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from datetime import datetime, timezone

import httpx

from apps.dsn_app import feed as _feed
from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import runtime as _runtime
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.audio import words as _audio_words
from apps.dsn_app.device import assets as _device_assets
from apps.dsn_app.render import distance as _render_distance
from apps.dsn_app.render import instrument as _render_instrument
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_dishes as _render_network_dishes
from apps.dsn_app.render import network_rows as _render_network_rows
from apps.dsn_app.render import network_skies as _render_network_skies
from apps.dsn_app.render import timing as _render_timing


def describe_links(
    links: list[_source.Link],
    names: dict[str, str],
    dish_types: dict[str, str] | None = None,
    view: str | None = None,
) -> list[str]:
    """What --dry-run prints: every live link, rendered but not drawn.

    A function rather than inline in main() so a test can run it — this path
    referenced an undefined name for two commits and crashed on the one
    command the README tells you to run first.
    """
    view = view or _settings.DEFAULT_VIEW
    out = [f"{len(links)} active link(s)"]
    network_blob: bytes | None = None
    if view == "network":
        if _settings.DSN_NETWORK_STYLE == "dishes":
            selected_key = links[0].key if links else None
            network_frames, fps, hold = (
                _render_network_dishes.render_dish_network_frames(
                    links, selected_key=selected_key
                )
            )
        elif _settings.DSN_NETWORK_STYLE == "skies":
            selected_key = links[0].key if links else None
            network_frames, fps, hold = _render_network_skies.render_three_skies_frames(
                links, names=names, selected_key=selected_key
            )
        else:
            network_frames, fps, hold = _render_network_rows.render_network_page_frames(
                links, 0, names=names
            )
        network_blob = _device_assets.encode_native_frames(network_frames, fps, hold)
    for link in links:
        if view == "instrument":
            frames, fps, hold = _render_instrument.render_instrument_frames(
                link, names=names
            )
            blob = _device_assets.encode_native_frames(frames, fps, hold)
        elif view == "network":
            blob = network_blob or b""
        else:
            frames, fps, hold = _render_distance.render_frames(
                link, datetime.now(timezone.utc), names, dish_types=dish_types
            )
            blob = _device_assets.encode_native_frames(frames, fps, hold)
        lt = f"{link.light_s / 60:6.1f} min" if link.light_s else "    ?  "
        rate = _render_labels.rate_label(link.down_bps)
        crossing = (
            f"{_render_timing.crossing_seconds(link.light_s):.0f}s"
            if link.light_s
            else "unknown"
        )
        out.append(
            f"  {link.complex_name:10s} {link.dish:6s} -> {link.craft:5s} "
            f"{link.band} {rate:>10s}  "
            f"az {link.azimuth:3.0f} el {link.elevation:2.0f}  "
            f"{len(_telemetry.link_streams(link))} receive record(s)  "
            f"light {lt} -> crossing {crossing}  "
            f"({len(blob) / 1024:.0f} kB)"
        )
        out.append(f"      says: {_audio_words.spoken(link, names, dish_types)}")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="render and report without touching the device",
    )
    parser.add_argument(
        "--once", action="store_true", help="push a single loop and exit"
    )
    args = parser.parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(message)s",
    )
    _settings.configure_runtime()

    if args.dry_run:
        state = _model.State()
        # Names too: without them the narration preview reads 'MRO' where the
        # real thing says 'Mars Reconnaissance Orbiter', which makes a dry run
        # a poor rehearsal of the one output it exists to rehearse.
        asyncio.run(_feed.fetch_names(state))
        with httpx.Client(headers=_limits.UA, timeout=20) as client:
            links = _source.parse_feed(client.get(_limits.DSN_XML).content)
        print("\n".join(describe_links(links, state.names, state.dish_types)))
        return

    asyncio.run(_runtime.run(args.once))

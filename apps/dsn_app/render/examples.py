"""DSN render / examples."""

from __future__ import annotations

from datetime import datetime, timezone

from PIL import Image

from apps.dsn_app import source as _source
from apps.dsn_app.render import distance as _render_distance
from apps.dsn_app.render import instrument as _render_instrument
from apps.dsn_app.render import network_dishes as _render_network_dishes


def _visual_fixture_links() -> list[_source.Link]:
    """A fixed, representative Network snapshot for deterministic rendering.

    Values mirror the shapes the contract tests exercise: three Goldstone
    contacts, one Canberra uplink, one Madrid contact. No feed, no clock.
    """

    def link(
        *,
        site: str,
        dish: str,
        craft: str,
        azimuth: float,
        elevation: float,
        up: bool = False,
    ) -> _source.Link:
        down_streams = (_source.DownStream("X", 2_000_000.0, -130.0),)
        up_streams = (_source.UpStream("X", 18.0, "command"),) if up else ()
        return _source.Link(
            complex_name=site,
            dish=dish,
            craft=craft,
            elevation=elevation,
            band="X",
            down_bps=2_000_000.0,
            up_active=up,
            range_km=100_000.0,
            down_dbm=-130.0,
            up_kw=18.0 if up else 0.0,
            streams=len(down_streams),
            azimuth=azimuth,
            pointing_valid=True,
            down_streams=down_streams,
            up_band="X" if up else "",
            up_streams=up_streams,
        )

    return [
        link(
            site="Goldstone", dish="DSS23", craft="IMAP", azimuth=210.0, elevation=45.0
        ),
        link(
            site="Goldstone", dish="DSS24", craft="CHDR", azimuth=120.0, elevation=30.0
        ),
        link(
            site="Goldstone", dish="DSS26", craft="SOHO", azimuth=70.0, elevation=50.0
        ),
        link(
            site="Canberra",
            dish="DSS34",
            craft="M01O",
            azimuth=48.0,
            elevation=22.0,
            up=True,
        ),
        link(site="Madrid", dish="DSS54", craft="JWST", azimuth=150.0, elevation=60.0),
    ]


def render_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """The pure zero-argument seam declared by `[dsn.viz]` in apps.toml.

    Renders the default dish/link Network board over the fixed snapshot
    above, through the same code path the live app draws with.
    """
    frames, fps, _hold = _render_network_dishes.render_dish_network_frames(
        _visual_fixture_links()
    )
    return {"front": (frames, fps)}


def _visual_fixture_contact() -> _source.Link:
    """One richer fixed contact for the single-link Instrument/Distance views."""
    return _source.Link(
        complex_name="Madrid",
        dish="DSS54",
        craft="JWST",
        elevation=60.0,
        band="X",
        down_bps=2_000_000.0,
        up_active=True,
        range_km=1_500_000.0,
        naif=-170,
        down_dbm=-130.0,
        up_kw=18.0,
        streams=2,
        azimuth=150.0,
        pointing_valid=True,
        down_streams=(
            _source.DownStream("X", 2_000_000.0, -130.0),
            _source.DownStream("K", 8_000_000.0, -128.0),
        ),
        up_band="X",
        up_streams=(_source.UpStream("X", 18.0, "command"),),
    )


def render_instrument_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """`[dsn.viz.scenarios.instrument]`: one antenna and contact, in detail."""
    frames, fps, _hold = _render_instrument.render_instrument_frames(
        _visual_fixture_contact()
    )
    return {"front": (frames, fps)}


def render_distance_visual() -> dict[str, tuple[list[Image.Image], int]]:
    """`[dsn.viz.scenarios.distance]`: the light-time journey, at a fixed instant."""
    fixed_now = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)
    frames, fps, _hold = _render_distance.render_frames(
        _visual_fixture_contact(), fixed_now
    )
    return {"front": (frames, fps)}

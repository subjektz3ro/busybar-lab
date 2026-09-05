"""DSN selection."""

from __future__ import annotations

import asyncio
import time
from dataclasses import replace

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry


def network_focus_active(state: _model.State, now: float | None = None) -> bool:
    """Whether a deliberate wheel-rest Focus Lens still owns Network.

    ``inf`` is the pre-accept state: a refused display draw must not consume
    the user's selection.  The first accepted Focus scene replaces it with a
    monotonic deadline equal to one complete native name cycle.
    """
    if state.network_focus_until <= 0:
        return False
    current = now
    if current is None:
        try:
            current = asyncio.get_running_loop().time()
        except RuntimeError:  # pure host-side callers need no running loop
            current = time.monotonic()
    return state.network_focus_until > current


def clear_network_focus(state: _model.State) -> None:
    """Return Network to its ambient overview and discard frozen Focus."""
    state.network_focus_key = None
    state.network_focus_until = 0.0
    state.network_focus_links = ()
    state.network_focus_names.clear()
    state.network_focus_trails.clear()


def commit_picker_selection(state: _model.State, now: float | None = None) -> None:
    """Commit a rested wheel selection without making asset upload interactive."""
    state.picking = False
    source_fresh = not state.feed_seeded or _telemetry.feed_freshness(state) == "fresh"
    if (
        _settings.DSN_NETWORK_STYLE in _limits.NETWORK_FOCUS_STYLES
        and state.view == "network"
        and source_fresh
    ):
        selected = state.current()
        state.network_focus_key = selected.key if selected is not None else None
        if state.network_focus_key:
            state.network_focus_until = float("inf")
            state.network_focus_links = tuple(replace(link) for link in state.links)
            state.network_focus_names = dict(state.names)
            state.network_focus_trails = {
                key: list(points) for key, points in state.aim_trails.items()
            }
        else:
            clear_network_focus(state)
    else:
        clear_network_focus(state)


def network_focus_inputs(
    state: _model.State,
) -> tuple[list[_source.Link], dict[str, str], dict[str, list[tuple[int, int]]]]:
    """The accepted snapshot a deliberate Focus Lens is allowed to claim."""
    if state.network_focus_links:
        return (
            list(state.network_focus_links),
            dict(state.network_focus_names),
            {key: list(points) for key, points in state.network_focus_trails.items()},
        )
    # Direct pure-test/state restoration callers may arm Focus without going
    # through the wheel helper. Falling back stays honest and deterministic.
    return (
        list(state.links),
        dict(state.names),
        {key: list(points) for key, points in state.aim_trails.items()},
    )


def watch_live_link(state: _model.State) -> _source.Link | None:
    """The newest source Link associated with a locally frozen watch."""
    if state.watch is None or not state.watch.on_air or not state.watch.live_key:
        return None
    return next(
        (link for link in state.links if link.key == state.watch.live_key), None
    )


def watch_contact_state(state: _model.State) -> str:
    if state.watch is None:
        return "live"
    if watch_live_link(state) is None:
        return "off_air"
    if state.watch.live_key != state.watch.link.key:
        return "handoff"
    return "live"


def narration_target_link(state: _model.State) -> _source.Link | None:
    """Use current RF/dish identity during a frozen light-time watch."""
    if state.watch is not None and state.realtime_since is not None:
        return watch_live_link(state)
    return state.current()


def distance_display_link(state: _model.State, fallback: _source.Link) -> _source.Link:
    """Current RF/pointing with the click-time journey distance frozen."""
    live = watch_live_link(state)
    if live is None or state.watch is None:
        return fallback
    frozen = state.watch.link
    return replace(
        live,
        range_km=frozen.range_km,
        up_range_km=frozen.up_range_km,
        down_range_km=frozen.down_range_km,
    )


def scene_intent_token(state: _model.State) -> tuple:
    """Mutable selection/render state that must not cross an upload await."""
    current = state.current()
    return (
        id(current),
        current.key if current else None,
        state.view,
        state.focus,
        state.narration_focus,
        state.realtime_since,
        state.rt_generation,
        state.picking,
        state.network_page,
        state.network_focus_key,
        network_focus_active(state),
        (
            (state.watch.on_air, state.watch.live_key)
            if state.watch is not None
            else None
        ),
        _telemetry.feed_freshness(state),
    )


def note_manual_selection(state: _model.State, now: float | None = None) -> None:
    """Keep a deliberately chosen contact visible long enough to observe it."""
    current = now if now is not None else asyncio.get_running_loop().time()
    state.manual_until = current + _limits.MANUAL_DWELL_S


async def rotate(state: _model.State) -> None:
    while True:
        await asyncio.sleep(_settings.ROTATE_S)
        if (
            state.view != "network"
            and not state.focus
            and not state.narration_focus
            and state.narration_request is None
            and state.narration_notice is None
            and not state.completion_pending
            and not state.picking
            and asyncio.get_running_loop().time() >= state.manual_until
            and len(state.links) > 1
        ):
            state.cursor = (state.cursor + 1) % len(state.links)
            state.dirty.set()


from apps.dsn_app.render import scope as _render_scope


def note_pointing(state: _model.State, links: list[_source.Link]) -> None:
    """Retain only real, pixel-visible antenna motion for the tracking tail."""
    live_keys = {link.key for link in links}
    for key in list(state.aim_trails):
        if key not in live_keys:
            state.aim_trails.pop(key, None)
    for link in links:
        point = _render_scope.link_pointing_pixel(link)
        if point is None:
            state.aim_trails.pop(link.key, None)
            continue
        trail = state.aim_trails.setdefault(link.key, [])
        if not trail or trail[-1] != point:
            trail.append(point)
            del trail[:-7]

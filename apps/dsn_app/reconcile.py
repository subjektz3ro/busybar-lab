"""DSN reconcile."""

from __future__ import annotations

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import selection as _selection
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.audio import policy as _audio_policy


def _stream_signature(link: _source.Link) -> tuple:
    return tuple(
        (
            _source.band_key(stream.band),
            stream.bps is not None,
            _telemetry.rate_bucket(stream.bps),
            _telemetry.receive_power_bucket(stream.dbm),
        )
        for stream in _telemetry.link_streams(link)
    )


def visual_events(
    before: list[_source.Link], after: list[_source.Link], now: float
) -> list[dict]:
    """Glanceable semantic transitions, deliberately blind to raw jitter."""
    old_by_key = {link.key: link for link in before}
    new_by_key = {link.key: link for link in after}
    removed = [link for key, link in old_by_key.items() if key not in new_by_key]
    added = [link for key, link in new_by_key.items() if key not in old_by_key]
    events: list[dict] = []

    # A same-craft disappear/appear in one feed update is a handoff, not a
    # loss immediately followed by an acquisition.
    handed_old: set[str] = set()
    handed_new: set[str] = set()
    crafts = {link.craft for link in removed} | {link.craft for link in added}
    for craft in crafts:
        old_matches = [link for link in removed if link.craft == craft]
        new_matches = [link for link in added if link.craft == craft]
        if len(old_matches) == len(new_matches) == 1:
            old, new = old_matches[0], new_matches[0]
            handed_old.add(old.key)
            handed_new.add(new.key)
            events.append(
                {
                    "t": round(now, 1),
                    "event": "handoff",
                    "craft": new.craft,
                    "dish": new.dish,
                    "from_dish": old.dish,
                    "complex": new.complex_name,
                    "azimuth": new.azimuth,
                    "elevation": new.elevation,
                    "pointing_valid": new.pointing_valid,
                    "from_complex": old.complex_name,
                    "from_azimuth": old.azimuth,
                    "from_elevation": old.elevation,
                    "from_pointing_valid": old.pointing_valid,
                }
            )

    for link in added:
        if link.key not in handed_new:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "acquire",
                    "craft": link.craft,
                    "dish": link.dish,
                    "complex": link.complex_name,
                }
            )
    for link in removed:
        if link.key not in handed_old:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "loss",
                    "craft": link.craft,
                    "dish": link.dish,
                    "complex": link.complex_name,
                }
            )

    for key in old_by_key.keys() & new_by_key.keys():
        old, new = old_by_key[key], new_by_key[key]
        old_flags = (old.arrayed, old.mspa, old.ddor)
        new_flags = (new.arrayed, new.mspa, new.ddor)
        if old_flags != new_flags:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "modes",
                    "craft": new.craft,
                    "dish": new.dish,
                    "before_flags": old_flags,
                    "flags": new_flags,
                }
            )
        old_streams, new_streams = _stream_signature(old), _stream_signature(new)
        old_bands = tuple(item[0] for item in old_streams)
        new_bands = tuple(item[0] for item in new_streams)
        if old_bands != new_bands or len(old_streams) != len(new_streams):
            events.append(
                {
                    "t": round(now, 1),
                    "event": "streams",
                    "craft": new.craft,
                    "dish": new.dish,
                    "before_streams": len(old_streams),
                    "streams": len(new_streams),
                    "bands": new_bands,
                }
            )
        old_direction = (old.up_active, bool(old_streams))
        new_direction = (new.up_active, bool(new_streams))
        if old_direction != new_direction:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "direction",
                    "craft": new.craft,
                    "dish": new.dish,
                    "up": new_direction[0],
                    "down": new_direction[1],
                }
            )
    return events


def queue_events(state: _model.State, events: list[dict]) -> None:
    for event in events:
        # Freshness is state, not history. A recovery makes an unseen stale
        # card false (and vice versa), so only the newest state may remain.
        if event.get("event") in {"stale", "recovered"}:
            state.event_queue[:] = [
                queued
                for queued in state.event_queue
                if queued.get("event") not in {"stale", "recovered"}
            ]
        state.event_queue.append(event)
    if len(state.event_queue) > _limits.EVENT_QUEUE_MAX:
        del state.event_queue[: -_limits.EVENT_QUEUE_MAX]


def reconciled_selection_key(
    after: list[_source.Link],
    selected_key: str | None,
    selected_craft: str | None,
) -> str | None:
    """Choose the same semantic selection independent of feed record order.

    Exact identity wins.  A dish handoff keeps the craft only when that
    continuation is unique. The final key sort is deliberately independent
    of both snapshots' XML ordering, which has no selection semantics.
    """
    if not after:
        return None
    live_by_key = {link.key: link for link in after}
    if selected_key in live_by_key:
        return selected_key
    same_craft = (
        [link for link in after if link.craft == selected_craft]
        if selected_craft is not None
        else []
    )
    if len(same_craft) == 1:
        return same_craft[0].key
    return min(live_by_key)


def reconcile_links(
    state: _model.State, links: list[_source.Link], now: float
) -> list[dict]:
    """Update a snapshot without moving the user's selection to another craft."""
    before = state.links
    selected = state.current()
    selected_key = selected.key if selected is not None else None
    selected_craft = selected.craft if selected is not None else None
    narrated = next(
        (link for link in before if link.key == state.narration_focus), None
    )
    completed = next(
        (link for link in before if link.key == state.completion_pending), None
    )
    events = visual_events(before, links, now) if state.feed_seeded else []

    if state.watch is not None:
        # The represented crossing belongs to the click, not to continued DSN
        # coverage. Reconcile its separate live-contact annotation on *every*
        # snapshot: a one-poll gap must not leave an hours-long watch claiming
        # OFF AIR after the same dish (or its handoff) comes back.
        watch = state.watch
        craft_links = [link for link in links if link.craft == watch.link.craft]
        live = next((link for link in craft_links if link.key == state.focus), None)
        if live is None:
            live = next(
                (link for link in craft_links if link.key == watch.link.key), None
            )
        if live is None and watch.live_key:
            live = next(
                (link for link in craft_links if link.key == watch.live_key), None
            )
        if live is None and len(craft_links) == 1:
            live = craft_links[0]

        previous_live = watch.live_key if watch.on_air else None
        watch.on_air = live is not None
        watch.live_key = live.key if live is not None else None
        if live is not None:
            state.focus = live.key
            selected_key = live.key
            if previous_live != live.key:
                _limits.logger.info("watch contact live: %s", live.key)
        elif previous_live is not None:
            _limits.logger.info("watch continues off-air: %s", state.focus)
    elif state.focus and not any(link.key == state.focus for link in links):
        handoff = [link for link in links if link.craft == selected_craft]
        if len(handoff) == 1:
            state.focus = handoff[0].key
            selected_key = handoff[0].key
            _limits.logger.info("focus handoff: %s", state.focus)
        else:
            _limits.logger.info("focused link left the network: %s", state.focus)
            if state.view_before_lock is not None:
                state.view = state.view_before_lock
            state.focus = None
            state.realtime_since = None
            state.rt_generation = None
            state.view_before_lock = None

    if state.narration_focus and not any(
        link.key == state.narration_focus for link in links
    ):
        handoff = (
            [link for link in links if link.craft == narrated.craft]
            if narrated is not None
            else []
        )
        old_narration_focus = state.narration_focus
        state.narration_focus = handoff[0].key if len(handoff) == 1 else None
        _model.note_narration_change(state)
        if selected_key == old_narration_focus and state.narration_focus:
            selected_key = state.narration_focus

    if state.completion_pending and state.completion_link is None:
        state.completion_link = completed

    state.links = links
    live_keys = {link.key for link in links}
    for mapping in (
        state.narration_texts,
        state.narration_frozen_at,
        state.narration_candidates,
    ):
        for key in list(mapping):
            if key not in live_keys:
                mapping.pop(key, None)
    if state.narration_priority not in live_keys:
        state.narration_priority = None
    requested_key = (
        state.narration_request.key
        if state.narration_request is not None
        else state.narration_notice.key
        if state.narration_notice is not None
        else None
    )
    if requested_key is not None and requested_key not in live_keys:
        _audio_policy.clear_narration_request(state)
    if state.network_focus_key is not None and state.network_focus_key not in live_keys:
        # A Network semantic zoom is exact-link context. A handoff may
        # preserve the craft selection, but silently reusing another dish's
        # aim would change the physical owner. Return to the ambient Network.
        _selection.clear_network_focus(state)
    selected_key = reconciled_selection_key(links, selected_key, selected_craft)
    if selected_key:
        for index, link in enumerate(links):
            if link.key == selected_key:
                state.cursor = index
                break
        else:
            state.cursor = min(state.cursor, max(0, len(links) - 1))
    else:
        state.cursor = min(state.cursor, max(0, len(links) - 1))
    _selection.note_pointing(state, links)
    state.feed_seeded = True
    return events

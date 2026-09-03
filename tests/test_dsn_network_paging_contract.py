"""Contracts for bounded, host-paged DSN Network assets.

The Network board can have many simultaneous contacts, but no one native
``.anim`` should contain the whole contact rotation.  These tests keep that
policy at the source-to-device boundary without requiring NASA or a BUSY Bar.
"""

from __future__ import annotations

import asyncio
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
dsn = pytest.importorskip("dsn")


@pytest.fixture(autouse=True)
def legacy_rows_style(monkeypatch):
    """This module pins the rollback renderer's paging contract explicitly."""
    monkeypatch.setattr(dsn, "DSN_NETWORK_STYLE", "rows")


def contact(site: str, dish: str, craft: str) -> dsn.Link:
    return dsn.Link(
        complex_name=site,
        dish=dish,
        craft=craft,
        elevation=30.0,
        band="X",
        down_bps=20_000.0,
        up_active=True,
        range_km=2.1e10,
        down_dbm=-140.0,
        up_kw=18.0,
        streams=1,
        azimuth=120.0,
        down_streams=(dsn.DownStream("X", 20_000.0, -140.0),),
        up_band="X",
    )


def paged_contacts() -> list[dsn.Link]:
    return [
        contact("Goldstone", "DSS14", "G1"),
        contact("Goldstone", "DSS24", "G2"),
        contact("Goldstone", "DSS25", "G3"),
        contact("Madrid", "DSS54", "M1"),
        contact("Madrid", "DSS55", "M2"),
        contact("Canberra", "DSS43", "C1"),
    ]


def network_state(links: list[dsn.Link] | None = None) -> dsn.State:
    state = dsn.State(links=list(links or paged_contacts()))
    state.view = "network"
    state.feed_seeded = True
    state.feed_timestamp_ms = int(time.time() * 1000)
    state.feed_advanced_at = time.time()
    state.freshness = "fresh"
    return state


class RecordingBar:
    def __init__(self, *, refuse_draw: bool = False):
        self.refuse_draw = refuse_draw
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.removed: list[str] = []

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draws.append(payload)
        if self.refuse_draw:
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    async def storage_remove(self, path: str):
        self.removed.append(path)


def visible_contact_keys(signature: tuple) -> set[str]:
    """Read dish/craft identities from a page-aware Network signature."""
    contacts_by_site = signature[1]
    return {
        f"{row[0]}/{row[1]}"
        for _, rows in contacts_by_site
        for row in rows
    }


def test_each_runtime_asset_contains_exactly_one_contact_page(monkeypatch):
    links = paged_contacts()
    calls: list[tuple[int, int]] = []
    real_render = dsn.render_network_page_frames

    def recording_render(links, page, freshness="fresh", names=None):
        frames, fps, hold = real_render(links, page, freshness, names)
        calls.append((page, len(frames)))
        return frames, fps, hold

    monkeypatch.setattr(dsn, "render_network_page_frames", recording_render)
    # Encoding is orthogonal here; recording the frame sequence passed to it
    # makes the runtime boundary explicit and keeps this test quick.
    monkeypatch.setattr(dsn.anim, "encode_anim", lambda *args, **kwargs: b"anim")

    state = network_state(links)
    bb = RecordingBar()
    assert asyncio.run(dsn.push_scene(
        bb, state, state.current(), dsn.scene_signature(state, state.current())))

    # Page zero is the only asset drawn.  The implementation may use its
    # eight-second dwell to warm page one, but every asset it builds must still
    # be one bounded page rather than the former whole-network rotation.
    assert calls[0] == (0, dsn.NETWORK_CONTACT_FRAMES)
    assert all(frame_count == dsn.NETWORK_CONTACT_FRAMES
               for _, frame_count in calls)
    assert len(bb.uploads) == len(calls)
    assert len(bb.draws) == 1


def test_page_dwell_is_eight_seconds_or_sixteen_for_a_long_name():
    short = [contact("Goldstone", "DSS14", "VGR2")]
    short_frames, short_fps, short_hold = dsn.render_network_page_frames(short, 0)

    long_name = "Advanced Composition Explorer Extended Mission"
    long = [replace(short[0], craft="ACE")]
    long_frames, long_fps, long_hold = dsn.render_network_page_frames(
        long, 0, names={"ace": long_name})

    assert len(short_frames) * short_hold / short_fps == 8
    assert len(long_frames) * long_hold / long_fps == 16
    assert len(short_frames) == dsn.NETWORK_CONTACT_FRAMES
    assert len(long_frames) == 2 * dsn.NETWORK_CONTACT_FRAMES


def test_network_refresh_deadline_is_one_page_dwell_not_a_renewal_batch():
    """``next_draw`` is both the accepted-page dwell and advance boundary."""
    short = network_state([
        contact("Goldstone", "DSS14", "VGR2"),
        contact("Goldstone", "DSS24", "MRO"),
    ])
    assert dsn.scene_refresh_s(short, short.current()) == 8

    long_name = "Advanced Composition Explorer Extended Mission"
    long = network_state([
        contact("Goldstone", "DSS14", "ACE"),
        contact("Goldstone", "DSS24", "MRO"),
    ])
    long.names = {"ace": long_name}
    assert dsn.network_page_duration_s(long.links, 0, long.names) == 16
    assert dsn.scene_refresh_s(long, long.current()) == 16


def test_long_name_completes_before_its_page_turns_over(monkeypatch):
    full_name = "ADVANCED COMPOSITION EXPLORER EXTENDED MISSION"
    link = contact("Goldstone", "DSS14", "ACE")
    positions: list[int] = []
    original = dsn._text

    def recording_text(px, x, y, text, colour, clip=None):
        if text == full_name:
            positions.append(x)
        return original(px, x, y, text, colour, clip)

    monkeypatch.setattr(dsn, "_text", recording_text)
    frames, fps, hold = dsn.render_network_page_frames(
        [link], 0, names={"ace": full_name.title()})

    assert len(frames) * hold / fps == 16
    assert positions
    assert max(positions) >= 17, "the full name never begins at the craft box"
    assert min(positions) <= 37 - dsn.text_width(full_name), (
        "the page turned before the final glyphs entered the craft box"
    )


def test_due_page_advances_once_and_only_after_an_accepted_scene():
    state = network_state()

    # Startup is page zero. A generic due=True before any accepted Network
    # scene must not skip it.
    assert not dsn.advance_network_page_if_due(state, True)
    assert (state.network_page, state.network_page_pending) == (0, False)

    accepted = RecordingBar()
    assert asyncio.run(dsn.push_scene(
        accepted, state, state.current(),
        dsn.scene_signature(state, state.current())))
    assert (state.network_page, state.network_page_pending) == (0, False)

    # The host's next_draw deadline is the dwell clock. Before it expires the
    # page is stable; once expired, only one next-page intent may be minted.
    assert not dsn.advance_network_page_if_due(state, False)
    assert dsn.advance_network_page_if_due(state, True)
    assert (state.network_page, state.network_page_pending) == (1, True)
    assert not dsn.advance_network_page_if_due(state, True)
    assert (state.network_page, state.network_page_pending) == (1, True)

    refused = RecordingBar(refuse_draw=True)
    with pytest.raises(exceptions.BusyBarAPIError):
        asyncio.run(dsn.push_scene(
            refused, state, state.current(),
            dsn.scene_signature(state, state.current())))
    assert (state.network_page, state.network_page_pending) == (1, True)
    assert not dsn.advance_network_page_if_due(state, True)
    assert state.network_page == 1, "a retry skipped an unseen contact page"

    retry = RecordingBar()
    assert asyncio.run(dsn.push_scene(
        retry, state, state.current(),
        dsn.scene_signature(state, state.current())))
    assert (state.network_page, state.network_page_pending) == (1, False)
    assert not dsn.advance_network_page_if_due(state, False)
    assert state.network_page == 1


def test_revisiting_a_page_reuses_its_immutable_uploaded_asset(monkeypatch):
    state = network_state()
    bb = RecordingBar()
    monkeypatch.setattr(dsn.anim, "encode_anim", lambda *args, **kwargs: b"anim")

    async def scenario():
        paths = []
        for page in (0, 1, 0):
            state.network_page = page
            signature = dsn.scene_signature(state, state.current())
            assert await dsn.push_scene(bb, state, state.current(), signature)
            paths.append(bb.draws[-1].elements[0].path)
        return paths

    paths = asyncio.run(scenario())

    # The current dwell may also finish prewarming page 2 while these pushes
    # yield. That is useful bounded work, not a duplicate of page 0; pin the
    # foreground identity rather than relying on task-scheduling starvation.
    assert 2 <= len(bb.uploads) <= dsn.network_page_count(state.links)
    assert len({name for _app, name, _blob in bb.uploads}) == len(bb.uploads)
    assert len(bb.draws) == 3
    assert paths[0] == paths[2]
    assert paths[0] != paths[1]
    assert len(state.scene_cache) <= dsn.SCENE_CACHE_MAX


def test_global_pages_make_every_uneven_site_contact_reachable():
    links = [
        *(contact("Goldstone", f"DSS{14 + index}", f"G{index}")
          for index in range(5)),
        contact("Madrid", "DSS54", "M0"),
        contact("Madrid", "DSS55", "M1"),
        contact("Canberra", "DSS43", "C0"),
    ]

    assert dsn.network_page_count(links) == 5
    seen: set[str] = set()
    for page in range(dsn.network_page_count(links)):
        signature = dsn.network_signature(links, "fresh", page=page)
        page_keys = visible_contact_keys(signature)
        assert len(page_keys) == 3, "each non-empty site gets one row per page"
        seen.update(page_keys)

    assert seen == {link.key for link in links}


def test_out_of_range_page_maps_safely_when_live_topology_shrinks():
    state = network_state()
    state.network_page = dsn.network_page_count(state.links) - 1
    state.last_scene_signature = dsn.scene_signature(state, state.current())
    state.last_scene_filename = "accepted.anim"

    state.links = [contact("Canberra", "DSS43", "ONLY")]
    # There is no second page to advance to.  The stale numeric index may be
    # retained, but page selection must modulo it into the live topology.
    assert not dsn.advance_network_page_if_due(state, True)
    assert state.network_page_pending is False
    signature = dsn.scene_signature(state, state.current())
    assert visible_contact_keys(signature) == {"DSS43/ONLY"}

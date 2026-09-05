"""Pixel- and truth-level contracts for the DSN Three Skies network view.

These tests intentionally stay on the host side.  They describe what the
72x16 framebuffer may claim from one accepted NASA source snapshot; no BUSY
Bar and no network connection are required.
"""

from __future__ import annotations

import asyncio
import math
import sys
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from busylib import exceptions

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import reconcile as dsn_reconcile
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import settings as dsn_settings
from apps.dsn_app import source as dsn_source
from apps.dsn_app import telemetry as dsn_telemetry
from apps.dsn_app.device import assets as dsn_device_assets
from apps.dsn_app.device import display as dsn_device_display
from apps.dsn_app.device import events as dsn_device_events
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.device import scenes as dsn_device_scenes
from apps.dsn_app.render import dish as dsn_render_dish
from apps.dsn_app.render import events as dsn_render_events
from apps.dsn_app.render import labels as dsn_render_labels
from apps.dsn_app.render import network_data as dsn_render_network_data
from apps.dsn_app.render import network_rows as dsn_render_network_rows
from apps.dsn_app.render import network_skies as dsn_render_network_skies
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import scope as dsn_render_scope
from apps.dsn_app.render import text as dsn_render_text
from busybar_dev import anim
PANEL_STEP = 77  # ceil(30% of an 8-bit channel), measured on the real panel


def link(
        *, site: str = "Goldstone", dish: str = "DSS26",
        craft: str = "MMS2", azimuth: float = 132.0,
        elevation: float = 38.0, band: str = "S",
        pointing_valid: bool = True, down: bool = True,
        up: bool = False,
        ) -> dsn_source.Link:
    streams = ((dsn_source.DownStream(band, 2_000_000.0, -130.0),)
               if down else ())
    return dsn_source.Link(
        complex_name=site,
        dish=dish,
        craft=craft,
        elevation=elevation,
        band=band if down else "",
        down_bps=2_000_000.0 if down else 0.0,
        up_active=up,
        range_km=100_000.0,
        down_dbm=-130.0 if down else None,
        up_kw=18.0 if up else 0.0,
        streams=len(streams),
        azimuth=azimuth,
        pointing_valid=pointing_valid,
        down_streams=streams,
        up_band="X" if up else "",
    )


def render(
        links: list[dsn_source.Link], *, freshness: str = "fresh",
        names: dict[str, str] | None = None,
        selected_key: str | None = None,
        trails: dict[str, list[tuple[int, int]]] | None = None,
        focus: bool = False,
        ):
    return dsn_render_network_skies.render_three_skies_frames(
        links, freshness=freshness, names=names,
        selected_key=selected_key, trails=trails, focus=focus)


def shifted_point(value: dsn_source.Link, centre: tuple[int, int]) -> tuple[int, int]:
    point = dsn_render_scope.link_pointing_pixel(value)
    assert point is not None
    return (centre[0] + point[0] - dsn_render_palette.SCOPE_CX,
            centre[1] + point[1] - dsn_render_palette.SCOPE_CY)


def ink(text: str) -> set[tuple[int, int]]:
    """Independent proportional-font ink, including no implementation clips."""
    points: set[tuple[int, int]] = set()
    cursor = 0
    for char in text.upper():
        glyph = dsn_render_text.FONT[char]
        for y, row in enumerate(glyph):
            for x, bit in enumerate(row):
                if bit == "1":
                    points.add((cursor + x, y))
        cursor += len(glyph[0]) + dsn_render_text.GLYPH_GAP
    return points


def assert_complete_monochrome_text(
        frame, text: str, x: int, y: int,
        ) -> tuple[int, int, int]:
    expected = {(x + dx, y + dy) for dx, dy in ink(text)}
    assert expected
    colours = {frame.getpixel(point) for point in expected}
    assert len(colours) == 1
    colour = colours.pop()
    assert colour != dsn_render_palette.OFF
    return colour


def test_three_skies_owns_three_exact_scopes_and_protects_both_rails():
    assert dsn_render_palette.THREE_SKIES_SCOPE_CENTERS == ((15, 7), (39, 7), (63, 7))
    frames, fps, hold = render([])

    assert frames and fps == dsn_limits.INSTRUMENT_FPS and hold == 1
    assert {frame.size for frame in frames} == {(72, 16)}
    for frame in frames:
        for centre_x, centre_y in dsn_render_palette.THREE_SKIES_SCOPE_CENTERS:
            # Zenith, the four literal cardinal points on the horizon, and no
            # decorative ellipse squeezed into a site's 24-pixel cell.
            for point in (
                    (centre_x, centre_y),
                    (centre_x, centre_y - 6),
                    (centre_x + 6, centre_y),
                    (centre_x, centre_y + 6),
                    (centre_x - 6, centre_y),
                    ):
                assert frame.getpixel(point) != dsn_render_palette.OFF
        # Site-to-site optical moats, then the dedicated freshness moat.  A
        # fresh claim is a native lease and therefore never baked at x=71.
        for x in (23, 47, dsn_render_palette.FRESH_GUTTER_X, dsn_render_palette.FRESH_X):
            assert all(frame.getpixel((x, y)) == dsn_render_palette.OFF
                       for y in range(dsn_limits.H))


@pytest.mark.parametrize(
    ("azimuth", "delta"),
    [(0.0, (0, -6)), (90.0, (6, 0)),
     (180.0, (0, 6)), (270.0, (-6, 0))],
)
def test_scope_heads_use_literal_local_alt_az_cardinals(azimuth, delta):
    contact = link(azimuth=azimuth, elevation=0.0)
    frame = render([contact])[0][0]
    centre = dsn_render_palette.THREE_SKIES_SCOPE_CENTERS[0]
    expected = (centre[0] + delta[0], centre[1] + delta[1])

    assert shifted_point(contact, centre) == expected
    assert frame.getpixel(expected) == dsn_render_palette.BAND_PULSE["S"]


def test_site_header_numbers_contacts_not_streams_or_dishes():
    first = link(dish="DSS24", craft="A")
    # One contact can carry three receive streams; it is still one contact in
    # the Network-level count.  Two craft on one MSPA dish remain two
    # displayable contacts even though their pointing node is shared.
    first = replace(
        first, streams=3,
        down_streams=(
            dsn_source.DownStream("S", 1_000.0, -140.0),
            dsn_source.DownStream("X", 2_000.0, -135.0),
            dsn_source.DownStream("K", 3_000.0, -130.0),
        ),
    )
    second = link(dish="DSS25", craft="B")
    third = link(dish="DSS25", craft="C")
    frame = render([first, second, third])[0][0]

    assert_complete_monochrome_text(frame, "G3", 0, 0)
    assert_complete_monochrome_text(frame, "M0", 24, 0)
    assert_complete_monochrome_text(frame, "C0", 48, 0)


def test_colliding_contacts_share_one_stable_spatial_node():
    first = link(dish="DSS24", craft="A", azimuth=91.0, elevation=41.0,
                 band="S")
    second = link(dish="DSS25", craft="B", azimuth=92.0, elevation=41.0,
                  band="X")
    centre = dsn_render_palette.THREE_SKIES_SCOPE_CENTERS[0]
    assert shifted_point(first, centre) == shifted_point(second, centre)

    frames_a, _, _ = render([first, second])
    frames_b, _, _ = render([second, first])
    assert [frame.tobytes() for frame in frames_a] == [
        frame.tobytes() for frame in frames_b]
    assert len({frame.crop((9, 1, 22, 14)).tobytes()
                for frame in frames_a}) == 1

    # A collision is a non-spatial ledger fact, never two invented nearby
    # dots.  All semantic RF node hues combined still occupy one scope cell.
    node_colours = set(dsn_render_palette.BAND_PULSE.values()) | {
        dsn_render_palette.UNKNOWN_PULSE, dsn_render_palette.UPLINK,
    }
    frame = frames_a[0]
    spatial_nodes = {(x, y) for x in range(9, 22) for y in range(1, 14)
                     if frame.getpixel((x, y)) in node_colours}
    assert spatial_nodes == {shifted_point(first, centre)}

    assert dsn_render_network_skies.three_skies_signature([first, second]) == \
        dsn_render_network_skies.three_skies_signature([second, first])


def test_missing_pointing_gets_a_complete_tally_and_never_a_fake_head():
    missing = link(pointing_valid=False)
    frame_one = render([missing])[0][0]
    frame_two = render([missing, replace(missing, craft="MMS3")])[0][0]

    # The local sky remains geometry-only; a missing az/el must not silently
    # become horizon north, zenith, or any other plausible-looking aim.
    node_colours = set(dsn_render_palette.BAND_PULSE.values()) | {
        dsn_render_palette.UNKNOWN_PULSE, dsn_render_palette.UPLINK, dsn_render_palette.SCOPE_HEAD,
    }
    assert not {(x, y) for x in range(9, 22) for y in range(1, 14)
                if frame_one.getpixel((x, y)) in node_colours}

    # ?1 / ?2 are promised as complete static labels in the site's ledger;
    # prove the final composed pixels carry every glyph stroke.
    assert_complete_monochrome_text(frame_one, "?1", 0, 10)
    assert_complete_monochrome_text(frame_two, "?2", 0, 10)
    assert frame_one.crop((0, 10, 10, 15)).tobytes() != \
        frame_two.crop((0, 10, 10, 15)).tobytes()


def test_selection_recolours_the_exact_cell_without_moving_the_aim():
    contact = link(azimuth=132.0, elevation=38.0)
    centre = dsn_render_palette.THREE_SKIES_SCOPE_CENTERS[0]
    expected = shifted_point(contact, centre)
    plain = render([contact])[0][0]
    selected = render([contact], selected_key=contact.key)[0][0]

    assert plain.getpixel(expected) == dsn_render_palette.BAND_PULSE["S"]
    selected_colour = selected.getpixel(expected)
    assert selected_colour != plain.getpixel(expected)
    assert min(selected_colour) >= 220
    assert max(selected_colour) - min(selected_colour) <= 35

    # Selection may add its dish ledger outside the scope, but it cannot
    # nudge the physical pointing head to manufacture visual separation.
    changed_in_scope = {
        (x, y) for x in range(9, 22) for y in range(1, 14)
        if plain.getpixel((x, y)) != selected.getpixel((x, y))
    }
    assert changed_in_scope == {expected}


def test_only_selected_dish_gets_four_history_cells_plus_its_current_head():
    selected = link(azimuth=0.0, elevation=90.0)
    other = link(dish="DSS24", craft="OTHER", azimuth=180.0,
                 elevation=45.0, band="X")
    # Consecutive repeats are accepted source samples, but they are not new
    # physical LED positions.  Retain the last five *distinct* observations:
    # local 3,4,5,6 as trail and local 7 as the current zenith head.
    observations = [
        (1, 7), (2, 7), (3, 7), (4, 7),
        (5, 7), (5, 7), (5, 7), (6, 7), (7, 7),
    ]
    trails = {selected.key: observations, other.key: observations}
    frame = render(
        [selected, other], selected_key=selected.key, trails=trails)[0][0]
    shift = dsn_render_palette.THREE_SKIES_SCOPE_CENTERS[0][0] - dsn_render_palette.SCOPE_CX

    assert {point for point in ((shift + x, 7) for x in range(1, 7))
            if frame.getpixel(point) == dsn_render_network_skies.THREE_SKIES_TRAIL} == {
                (shift + 3, 7), (shift + 4, 7),
                (shift + 5, 7), (shift + 6, 7),
            }
    assert frame.getpixel((shift + 7, 7)) != dsn_render_palette.SCOPE_TRAIL

    without_selection = render(
        [selected, other], selected_key=None, trails=trails)[0][0]
    assert all(without_selection.getpixel((x, y)) != dsn_render_network_skies.THREE_SKIES_TRAIL
               for x in range(dsn_limits.W) for y in range(dsn_limits.H))


def test_source_freshness_never_turns_the_local_skies_into_fake_motion():
    contacts = [
        link(site="Goldstone", dish="DSS26", craft="MMS2"),
        link(site="Madrid", dish="DSS63", craft="JWST", band="K"),
        link(site="Canberra", dish="DSS43", craft="VGR1", band="X"),
    ]
    for freshness in ("fresh", "delayed", "stale", "offline"):
        frames, _, _ = render(contacts, freshness=freshness)
        assert len({frame.crop((0, 0, 71, 16)).tobytes()
                    for frame in frames}) == 1
        assert all(frame.getpixel((dsn_render_palette.FRESH_GUTTER_X, y)) == dsn_render_palette.OFF
                   for frame in frames for y in range(dsn_limits.H))

    fresh = render(contacts, freshness="fresh")[0]
    delayed = render(contacts, freshness="delayed")[0]
    stale = render(contacts, freshness="stale")[0]
    assert all(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.OFF
               for frame in fresh for y in range(dsn_limits.H))
    assert len({tuple(frame.getpixel((dsn_render_palette.FRESH_X, y))
                      for y in range(dsn_limits.H)) for frame in delayed}) > 1
    assert all(sum(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.DELAYED
                   for y in range(dsn_limits.H)) in (0, 3)
               for frame in delayed)
    assert all(sum(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.STALE
                   for y in range(dsn_limits.H)) == 3
               for frame in stale)


def test_static_triptych_uses_native_duration_folding_not_forty_full_frames():
    frames, fps, hold = render([link()])
    folded = dsn_device_assets.encode_native_frames(frames, fps, hold)
    forced = anim.encode_anim(
        frames, fps=fps, durations=[hold] * len(frames))

    assert len(frames) == dsn_limits.INSTRUMENT_FRAMES
    assert len(folded) * 10 < len(forced)


def test_focus_lens_marquees_every_pixel_of_the_complete_friendly_name():
    contact = link()
    names = {"mms2": "Magnetospheric Multiscale 2"}
    label = names["mms2"].upper()
    frames, fps, _ = render(
        [contact], names=names, selected_key=contact.key, focus=True)

    assert len(frames) >= dsn_render_text.scroll_frame_count(label, 21)
    assert len(frames) % dsn_limits.INSTRUMENT_FRAMES == 0
    assert fps == dsn_limits.INSTRUMENT_FPS
    source_ink = ink(label)
    cycle = dsn_render_text.text_width(label) + dsn_render_text.SCROLL_GAP_PX
    exposed: set[tuple[int, int]] = set()
    box = (0, 20)

    for index, frame in enumerate(frames):
        offset = dsn_render_text.scroll_offset(label, index / len(frames), 21)
        expected: set[tuple[int, int]] = set()
        for copy in (0, cycle):
            for source_x, source_y in source_ink:
                target_x = box[0] - offset + copy + source_x
                if box[0] <= target_x <= box[1]:
                    expected.add((target_x, 6 + source_y))
                    if copy == 0:
                        exposed.add((source_x, source_y))
        actual = {(x, y) for x in range(box[0], box[1] + 1)
                  for y in range(6, 11)
                  if frame.getpixel((x, y)) == dsn_render_palette.NAME}
        assert actual == expected

    assert exposed == source_ink


def marquee_exposed_ink(frames, text: str, y: int,
                        colour: tuple[int, int, int]) -> set[tuple[int, int]]:
    """Which independently rasterized source pixels entered the Focus box."""
    source = ink(text)
    cycle = dsn_render_text.text_width(text) + dsn_render_text.SCROLL_GAP_PX
    exposed: set[tuple[int, int]] = set()
    for index, frame in enumerate(frames):
        offset = dsn_render_text.scroll_offset(text, index / len(frames), 21)
        for copy in (0, cycle):
            for source_x, source_y in source:
                target_x = -offset + copy + source_x
                if 0 <= target_x <= 20 and frame.getpixel(
                        (target_x, y + source_y)) == colour:
                    if copy == 0:
                        exposed.add((source_x, source_y))
    return exposed


def test_focus_scrolls_complete_two_digit_index_instead_of_clipping_it():
    contacts = [link(dish=f"DSS{10 + index}", craft=f"C{index}")
                for index in range(13)]
    selected = contacts[-1]
    frames, _, _ = render(
        contacts, names={selected.craft.lower(): "A"},
        selected_key=selected.key, focus=True)
    index_label = "13/13"

    assert dsn_render_text.text_width(index_label) > 21
    assert marquee_exposed_ink(
        frames, index_label, 11, dsn_render_dish.ANTENNA) == ink(index_label)


def test_focus_does_not_draw_a_ghost_wrap_copy_for_a_short_name():
    contact = link(craft="A")
    frame = render([contact], selected_key=contact.key, focus=True)[0][0]
    actual = {(x, y) for x in range(21) for y in range(6, 11)
              if frame.getpixel((x, y)) == dsn_render_palette.NAME}
    expected = {(x, 6 + y) for x, y in ink("A")}
    assert actual == expected


def test_focus_font_covers_every_character_in_vendored_friendly_names():
    import xml.etree.ElementTree as ET

    root = ET.parse(Path(__file__).parent / "fixtures" / "dsn_config.xml")
    friendly = [element.get("friendlyName", "").upper()
                for element in root.iter()
                if element.tag.lower().endswith("spacecraft")]
    source_chars = set("".join(friendly))
    assert source_chars <= set(dsn_render_text.FONT)
    assert "(" in source_chars and ")" in source_chars

    contact = link(craft="M20")
    label = "MARS 2020 (PERSEVERANCE)"
    frames, _, _ = render(
        [contact], names={"m20": label},
        selected_key=contact.key, focus=True)
    assert marquee_exposed_ink(frames, label, 6, dsn_render_palette.NAME) == ink(label)


def test_focus_replaces_unknown_live_name_glyphs_instead_of_drawing_blanks():
    contact = link(craft="FUTURE")
    source_name = "D’Arcy & Próbe_+—"
    label = dsn_render_labels.craft_label(
        contact.craft, {"future": source_name})

    assert label == "D?ARCY ? PR?BE?+?"
    assert set(label) <= set(dsn_render_text.FONT)
    frames, _, _ = render(
        [contact], names={"future": source_name},
        selected_key=contact.key, focus=True)
    assert marquee_exposed_ink(frames, label, 6, dsn_render_palette.NAME) == ink(label)


def test_focus_keeps_missing_pointing_explicit_in_main_and_context_scopes():
    selected = link(pointing_valid=False)
    madrid = link(site="Madrid", dish="DSS63", craft="MISSING",
                  pointing_valid=False)
    frames, _, _ = render(
        [selected, madrid], selected_key=selected.key, focus=True)

    assert marquee_exposed_ink(frames, "1/2 ?1", 11, dsn_render_dish.ANTENNA) == \
        ink("1/2 ?1")
    assert_complete_monochrome_text(frames[0], "?", 58, 0)


@pytest.mark.parametrize("collision", range(2, 10))
def test_focus_context_status_exposes_complete_missing_and_collision_tokens(
        collision):
    selected = link()
    madrid = [
        link(site="Madrid", dish="DSS63", craft="MISSING",
             pointing_valid=False),
        *[
            link(site="Madrid", dish=f"DSS{64 + index}", craft=f"M{index}",
                 azimuth=90.0, elevation=45.0)
            for index in range(collision)
        ],
    ]
    frames, _, _ = render(
        [selected, *madrid], selected_key=selected.key, focus=True)

    assert len(frames) == dsn_limits.INSTRUMENT_FRAMES
    for index, frame in enumerate(frames):
        token = "?" if index < 20 else str(collision)
        expected = {(58 + x, y) for x, y in ink(token)}
        actual = {(x, y) for x in range(58, 62) for y in range(5)
                  if frame.getpixel((x, y)) == dsn_render_network_skies.THREE_SKIES_LEDGER}
        assert actual == expected
        assert_complete_monochrome_text(
            frame, dsn_render_network_skies._site_count_label("M", collision + 1), 48, 0)
        assert all(frame.getpixel((62, y)) == dsn_render_palette.OFF
                   for y in range(dsn_limits.H))


def test_ambient_ledger_preserves_missing_and_collision_facts_together():
    missing = link(dish="DSS23", craft="MISSING", pointing_valid=False)
    first = link(dish="DSS24", craft="A", azimuth=91.0, elevation=41.0)
    second = link(dish="DSS25", craft="B", azimuth=92.0, elevation=41.0)
    frame = render([missing, first, second])[0][0]

    assert_complete_monochrome_text(frame, "?1", 0, 5)
    assert_complete_monochrome_text(frame, "2", 0, 10)
    for point in ((6, 10), (6, 12), (6, 14), (7, 10), (7, 14)):
        assert frame.getpixel(point) == dsn_render_network_skies.THREE_SKIES_LEDGER


@pytest.mark.parametrize("collision", range(2, 10))
def test_focus_collision_tally_is_complete_and_cannot_overwrite_the_sky(
        collision):
    contacts = [
        link(dish=f"DSS{10 + index}", craft=f"C{index}",
             azimuth=248.3, elevation=0.0)
        for index in range(collision)
    ]
    selected = contacts[0]
    frame = render(
        contacts, selected_key=selected.key, focus=True)[0][0]
    digit = {(21 + x, 11 + y) for x, y in ink(str(collision))}
    bracket = {(26, 11), (26, 13), (26, 15),
               (27, 11), (27, 15)}
    ledger = {(x, y) for x in range(dsn_limits.W) for y in range(dsn_limits.H)
              if frame.getpixel((x, y)) == dsn_render_network_skies.THREE_SKIES_LEDGER}

    assert ledger == digit | bracket
    assert all(frame.getpixel((25, y)) == dsn_render_palette.OFF for y in range(dsn_limits.H))
    assert max(x for x, _ in ledger) < (
        dsn_render_network_skies.THREE_SKIES_MAIN_CENTER[0] - dsn_render_network_skies.THREE_SKIES_MAIN_R)
    projected = dsn_render_network_skies._project_link(
        selected, *dsn_render_network_skies.THREE_SKIES_MAIN_CENTER, dsn_render_network_skies.THREE_SKIES_MAIN_R)
    assert projected == (28, 11), "fixture no longer exercises the old overlap"
    assert frame.getpixel(projected) == dsn_render_network_skies.THREE_SKIES_SELECTED


def test_scope_semantic_colours_clear_the_measured_physical_panel_step():
    node_colours = tuple(dsn_render_palette.BAND_PULSE.values()) + (
        dsn_render_palette.UNKNOWN_PULSE, dsn_render_palette.UPLINK, dsn_render_network_skies.THREE_SKIES_SELECTED)
    for colour in node_colours:
        assert max(abs(a - b) for a, b in zip(
            dsn_render_palette.THREE_SKIES_NORTH, colour)) >= PANEL_STEP
        assert max(abs(a - b) for a, b in zip(
            dsn_render_network_skies.THREE_SKIES_TRAIL, colour)) >= PANEL_STEP
    assert max(abs(a - b) for a, b in zip(
        dsn_render_network_skies.THREE_SKIES_TRAIL, dsn_render_palette.SCOPE_RING)) >= PANEL_STEP
    for colour in node_colours + (
            dsn_render_palette.SCOPE_RING, dsn_render_palette.THREE_SKIES_NORTH,
            dsn_render_network_skies.THREE_SKIES_TRAIL):
        assert max(abs(a - b) for a, b in zip(
            dsn_render_network_skies.THREE_SKIES_LEDGER, colour)) >= PANEL_STEP


def test_rows_remains_an_explicit_runtime_fallback(monkeypatch):
    contact = link()
    state = dsn_model.State(links=[contact], view="network")
    source_now = 1_800_000_000.0
    state.feed_timestamp_ms = int(source_now * 1000)
    state.feed_advanced_at = source_now
    now = datetime.fromtimestamp(source_now + 1.0, tz=timezone.utc)

    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "rows")
    assert dsn_device_scene_policy.scene_signature(state, contact, now) == dsn_render_network_rows.network_signature(
        [contact], "fresh", state.names, page=state.network_page)
    rows, _, _ = dsn_render_network_rows.render_network_page_frames([contact], 0)
    skies, _, _ = render([contact])
    assert rows[0].tobytes() != skies[0].tobytes()


def test_network_focus_is_bounded_and_never_an_ambient_auto_rotation():
    state = dsn_model.State(view="network")
    assert state.network_focus_until == 0.0
    assert dsn_selection.network_focus_active(state, now=100.0) is False

    state.network_focus_until = 108.0
    assert dsn_selection.network_focus_active(state, now=100.0) is True
    assert dsn_selection.network_focus_active(state, now=108.0) is False


def test_only_a_rested_skies_network_pick_arms_the_focus_lens(monkeypatch):
    contact = link()
    state = dsn_model.State(links=[contact], view="network", picking=True)
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    # A live detent owns the instant picker; it must not race an animation
    # upload or reveal a Focus asset underneath the user's moving hand.
    dsn_selection.note_manual_selection(state, now=10.0)
    assert state.picking is True
    assert state.network_focus_key is None
    assert dsn_selection.network_focus_active(state, now=10.0) is False

    # The main loop calls this only after PICK_REST_S.  `inf` means an upload
    # refusal cannot consume the user's dwell before the first accepted draw.
    dsn_selection.commit_picker_selection(state, now=11.0)
    assert state.picking is False
    assert state.network_focus_key == contact.key
    assert state.network_focus_until == float("inf")
    assert dsn_selection.network_focus_active(state, now=1_000_000.0) is True


@pytest.mark.parametrize(("style", "view"), [
    ("rows", "network"),
    ("skies", "instrument"),
    ("skies", "distance"),
])
def test_picker_rest_never_opens_focus_outside_skies_network(
        monkeypatch, style, view):
    contact = link()
    state = dsn_model.State(
        links=[contact], view=view, picking=True,
        network_focus_key=contact.key,
        network_focus_until=float("inf"),
    )
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", style)

    dsn_selection.commit_picker_selection(state, now=20.0)

    assert state.picking is False
    assert state.network_focus_key is None
    assert state.network_focus_until == 0.0


@pytest.mark.parametrize(("age", "freshness"), [
    ((dsn_settings.FEED_DELAYED_S + dsn_settings.FEED_STALE_S) / 2.0, "delayed"),
    (dsn_settings.FEED_STALE_S + 1.0, "stale"),
])
def test_picker_rest_never_arms_focus_from_an_aged_seeded_snapshot(
        monkeypatch, age, freshness):
    wall_now = 1_800_000_000.0
    contact = link()
    state = dsn_model.State(
        links=[contact], view="network", picking=True, feed_seeded=True,
        feed_timestamp_ms=int((wall_now - age) * 1000),
        feed_advanced_at=wall_now - age,
        network_focus_key=contact.key,
        network_focus_until=float("inf"),
        network_focus_links=(replace(contact),),
        network_focus_names={"mms2": "Magnetospheric Multiscale 2"},
        network_focus_trails={contact.key: [(1, 1), (2, 2)]},
    )
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(time, "time", lambda: wall_now)
    assert dsn_telemetry.feed_freshness(state) == freshness

    dsn_selection.commit_picker_selection(state, now=123.0)

    assert state.picking is False
    assert state.network_focus_key is None
    assert state.network_focus_until == 0.0
    assert state.network_focus_links == ()
    assert state.network_focus_names == {}
    assert state.network_focus_trails == {}


@pytest.mark.parametrize("replacement_kind", ["departure", "handoff"])
def test_reconcile_clears_exact_link_focus_without_stranding_ambient_selection(
        replacement_kind):
    steady = link(
        site="Madrid", dish="DSS63", craft="VGR2",
        azimuth=80.0, elevation=30.0)
    focused = link()
    handoff = link(
        site="Canberra", dish="DSS43", craft=focused.craft,
        azimuth=210.0, elevation=25.0)
    state = dsn_model.State(
        links=[steady, focused], cursor=1, view="network", feed_seeded=True,
        network_focus_key=focused.key,
        network_focus_until=float("inf"),
        network_focus_links=(replace(steady), replace(focused)),
        network_focus_names={"mms2": "Magnetospheric Multiscale 2"},
        network_focus_trails={focused.key: [(1, 1), (2, 2)]},
    )
    incoming = ([steady] if replacement_kind == "departure"
                else [steady, handoff])

    events = dsn_reconcile.reconcile_links(state, incoming, now=200.0)

    assert [event["event"] for event in events] == [
        "loss" if replacement_kind == "departure" else "handoff"
    ]
    assert state.network_focus_key is None
    assert state.network_focus_until == 0.0
    assert state.network_focus_links == ()
    assert state.network_focus_names == {}
    assert state.network_focus_trails == {}
    assert state.view == "network"
    assert state.focus is None
    assert 0 <= state.cursor < len(state.links)
    assert state.current() is (
        steady if replacement_kind == "departure" else handoff)


# --- runtime boundary -----------------------------------------------------


def runtime_state(*contacts: dsn_source.Link) -> dsn_model.State:
    now = time.time()
    state = dsn_model.State(links=list(contacts), view="network")
    state.feed_seeded = True
    state.feed_timestamp_ms = int(now * 1000)
    state.feed_advanced_at = now
    state.freshness = "fresh"
    return state


class RuntimeBar:
    def __init__(self, *, refuse_draw: bool = False) -> None:
        self.refuse_draw = refuse_draw
        self.uploads: list[tuple[str, str, bytes]] = []
        self.draws = []
        self.draw_times: list[float] = []
        self.removed: list[str] = []

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draws.append(payload)
        self.draw_times.append(asyncio.get_running_loop().time())
        if self.refuse_draw:
            raise exceptions.BusyBarAPIError(
                "Not drawn due to low priority", status_code=409)

    async def storage_remove(self, path: str):
        self.removed.append(path)


def test_push_scene_uses_three_skies_and_never_touches_rows(monkeypatch):
    contact = link()
    state = runtime_state(contact)
    state.network_page = 7  # row-only state must be inert in Three Skies
    bb = RuntimeBar()
    calls = []
    real_render = dsn_render_network_skies.render_three_skies_frames

    def recording_skies(
            links, freshness="fresh", names=None, selected_key=None,
            trails=None, focus=False):
        calls.append((list(links), freshness, dict(names or {}), selected_key,
                      dict(trails or {}), focus))
        return real_render(
            links, freshness, names, selected_key, trails, focus)

    def row_renderer_must_not_run(*args, **kwargs):
        raise AssertionError("Three Skies runtime called the row renderer")

    def row_prewarm_must_not_run(*args, **kwargs):
        raise AssertionError("Three Skies runtime started row prewarming")

    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_render_network_skies, "render_three_skies_frames", recording_skies)
    monkeypatch.setattr(dsn_render_network_rows, "render_network_page_frames",
                        row_renderer_must_not_run)
    monkeypatch.setattr(dsn_device_scenes, "start_network_page_warm",
                        row_prewarm_must_not_run)
    monkeypatch.setattr(anim, "encode_anim",
                        lambda *args, **kwargs: b"three-skies")

    accepted = asyncio.run(dsn_device_scenes.push_scene(
        bb, state, contact, ("three-skies-runtime",)))

    assert accepted is True
    assert len(calls) == 1
    rendered_links, freshness, names, selected_key, trails, focus = calls[0]
    assert rendered_links == [contact]
    assert freshness == "fresh"
    assert names == {}
    assert selected_key == contact.key
    assert trails == {}
    assert focus is False
    assert len(bb.uploads) == len(bb.draws) == 1
    assert bb.uploads[0][2] == b"three-skies"
    assert state.network_page == 7


def test_accepted_focus_starts_exactly_one_native_loop_after_acceptance(
        monkeypatch):
    contact = link()
    state = runtime_state(contact)
    state.names = {"mms2": "Magnetospheric Multiscale 2"}
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(anim, "encode_anim",
                        lambda *args, **kwargs: b"focus")
    dsn_selection.commit_picker_selection(state, now=10.0)
    assert math.isinf(state.network_focus_until)
    expected_loop = dsn_render_network_skies.three_skies_loop_s(
        state.links, state.names, contact.key, True)

    assert asyncio.run(dsn_device_scenes.push_scene(
        bb, state, contact, ("three-skies-focus-runtime",))) is True

    assert len(bb.draw_times) == 1
    assert math.isfinite(state.network_focus_until)
    assert state.network_focus_until - bb.draw_times[0] == pytest.approx(
        expected_loop, abs=0.05)
    assert dsn_selection.network_focus_active(
        state, now=state.network_focus_until - 0.001)
    assert not dsn_selection.network_focus_active(
        state, now=state.network_focus_until)


def test_focus_dwell_starts_after_the_retiring_picker_is_no_longer_visible(
        monkeypatch):
    contact = link()
    state = runtime_state(contact)
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(anim, "encode_anim",
                        lambda *args, **kwargs: b"focus")
    dsn_selection.commit_picker_selection(state, now=10.0)

    async def scenario():
        state.interactive_visible_until = asyncio.get_running_loop().time() + 1.0
        expected_visible = state.interactive_visible_until
        accepted = await dsn_device_scenes.push_scene(
            bb, state, contact, ("three-skies-masked-focus",))
        return accepted, expected_visible

    accepted, expected_visible = asyncio.run(scenario())
    loop_s = dsn_render_network_skies.three_skies_loop_s(
        state.network_focus_links, state.network_focus_names,
        contact.key, True)
    assert accepted is True
    assert state.network_focus_until == pytest.approx(
        expected_visible + loop_s, abs=0.05)


def test_focus_snapshot_does_not_restart_when_new_source_geometry_arrives(
        monkeypatch):
    contact = link(azimuth=10.0, elevation=30.0)
    state = runtime_state(contact)
    state.names = {"mms2": "Magnetospheric Multiscale 2"}
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    dsn_selection.commit_picker_selection(state, now=10.0)
    before = dsn_device_scene_policy.scene_signature(state, contact)

    state.links = [replace(contact, azimuth=210.0, elevation=5.0)]
    state.names["mms2"] = "A changed label must wait for ambient"
    state.aim_trails[contact.key] = [(1, 1), (7, 7)]
    after = dsn_device_scene_policy.scene_signature(state, state.links[0])

    assert before == after


def test_event_cards_wait_until_the_deliberate_focus_lens_finishes(
        monkeypatch):
    contact = link()
    state = runtime_state(contact)
    state.event_queue = [{"event": "acquire", "craft": "MMS2",
                          "dish": "DSS26", "t": time.time()}]
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    async def scenario():
        state.network_focus_key = contact.key
        state.network_focus_until = asyncio.get_running_loop().time() + 8.0
        return await dsn_device_events.show_next_event(bb, state)

    assert asyncio.run(scenario()) is False
    assert bb.draws == []
    assert len(state.event_queue) == 1


def test_refused_focus_draw_preserves_the_unstarted_infinite_dwell(monkeypatch):
    contact = link()
    state = runtime_state(contact)
    state.names = {"mms2": "Magnetospheric Multiscale 2"}
    bb = RuntimeBar(refuse_draw=True)
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(anim, "encode_anim",
                        lambda *args, **kwargs: b"focus")
    dsn_selection.commit_picker_selection(state, now=10.0)
    intended_key = state.network_focus_key

    with pytest.raises(exceptions.BusyBarAPIError) as caught:
        asyncio.run(dsn_device_scenes.push_scene(
            bb, state, contact, ("three-skies-focus-refused",)))

    assert caught.value.status_code == 409
    assert state.network_focus_key == intended_key == contact.key
    assert math.isinf(state.network_focus_until)
    assert state.last_scene_signature is None
    assert state.last_scene_filename is None


def test_expired_focus_signature_is_the_ambient_triptych_signature(
        monkeypatch):
    first = link(dish="DSS24", craft="FIRST")
    second = link(dish="DSS26", craft="MMS2")
    state = runtime_state(first, second)
    state.names = {"mms2": "Magnetospheric Multiscale 2"}
    state.network_focus_key = second.key
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    async def scenario():
        loop_now = asyncio.get_running_loop().time()
        state.network_focus_until = loop_now + 60.0
        focus_signature = dsn_device_scene_policy.scene_signature(state, state.current())

        # This is the state the main loop observes at its expiry boundary:
        # the finite lease is already inactive before its cleanup assignment.
        state.network_focus_until = loop_now
        assert not dsn_selection.network_focus_active(state, now=loop_now)
        expired_signature = dsn_device_scene_policy.scene_signature(state, state.current())

        state.network_focus_until = 0.0
        state.network_focus_key = None
        ambient_signature = dsn_device_scene_policy.scene_signature(state, state.current())
        return focus_signature, expired_signature, ambient_signature

    focus_signature, expired_signature, ambient_signature = asyncio.run(
        scenario())
    assert focus_signature[0] == "three-skies-focus"
    assert expired_signature[0] == ambient_signature[0] == "three-skies"
    assert focus_signature != ambient_signature
    assert expired_signature == ambient_signature


def test_skies_never_advances_or_prewarms_the_row_page_machine(monkeypatch):
    contacts = [
        link(dish="DSS24", craft="A"),
        link(dish="DSS25", craft="B"),
        link(dish="DSS26", craft="C"),
    ]
    state = runtime_state(*contacts)
    state.network_page = 2
    state.last_scene_signature = ("network-page", "old-row")
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    assert dsn_device_scene_policy.advance_network_page_if_due(state, due=True) is False
    assert state.network_page == 2
    assert state.network_page_pending is False
    assert dsn_device_scenes.start_network_page_warm(object(), state) is None
    assert state.network_warm_task is None
    assert state.network_warm_signature is None


def test_refused_changing_scenes_keep_the_uploaded_cache_bounded(monkeypatch):
    contact = link()
    state = runtime_state(contact)
    bb = RuntimeBar(refuse_draw=True)
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(anim, "encode_anim",
                        lambda *args, **kwargs: b"small")

    async def scenario():
        for index in range(dsn_limits.SCENE_CACHE_MAX + 3):
            with pytest.raises(exceptions.BusyBarAPIError):
                await dsn_device_scenes.push_scene(
                    bb, state, contact, ("changing-sky", index))

    asyncio.run(scenario())
    assert len(state.scene_cache) <= dsn_limits.SCENE_CACHE_MAX
    assert len(bb.removed) >= 3


# --- data-specific Handoff Echo ------------------------------------------


def handoff_event(
        *, craft: str = "ARTEMIS1",
        old_site: str = "Canberra", old_dish: str = "DSS43",
        old_azimuth: float = 0.0, old_elevation: float = 45.0,
        old_pointing: bool = True,
        new_site: str = "Goldstone", new_dish: str = "DSS14",
        new_azimuth: float = 180.0, new_elevation: float = 45.0,
        new_pointing: bool = True,
        ) -> tuple[dict, dsn_source.Link, dsn_source.Link]:
    """One unambiguous observed same-craft association change."""
    old = link(
        site=old_site, dish=old_dish, craft=craft,
        azimuth=old_azimuth, elevation=old_elevation,
        pointing_valid=old_pointing,
    )
    new = link(
        site=new_site, dish=new_dish, craft=craft,
        azimuth=new_azimuth, elevation=new_elevation,
        pointing_valid=new_pointing,
    )
    events = dsn_reconcile.visual_events([old], [new], now=time.time())
    assert len(events) == 1 and events[0]["event"] == "handoff"
    return events[0], old, new


def event_scope_point(
        site: str, azimuth: float, elevation: float,
        ) -> tuple[int, int]:
    """Independent local alt-az projection for the fixed triptych."""
    centre = {
        name: scope_centre
        for (name, _initial, _y), scope_centre in zip(
            dsn_render_network_data.NETWORK_SITES, dsn_render_palette.THREE_SKIES_SCOPE_CENTERS)
    }[site]
    angle = math.radians(azimuth % 360.0)
    radius = ((90.0 - min(90.0, max(0.0, elevation))) / 90.0
              * dsn_render_palette.THREE_SKIES_SCOPE_R)
    return (
        int(round(centre[0] + math.sin(angle) * radius)),
        int(round(centre[1] - math.cos(angle) * radius)),
    )


def event_animation(payload):
    animations = [element for element in payload.elements
                  if element.type == "animation"]
    return animations[0] if len(animations) == 1 else None


def event_text(payload):
    texts = [element for element in payload.elements
             if element.type == "text"]
    return texts[0] if len(texts) == 1 else None


def release_event_card(state: dsn_model.State) -> None:
    """Advance a host-side fake past an accepted four-second card."""
    state.active_event_label = None
    state.active_event_asset = None
    state.active_event_embedded_label = False
    state.active_event_until = 0.0
    state.interactive_visible_until = 0.0


def test_handoff_echo_phases_exact_cells_then_complete_embedded_label():
    target, old, new = handoff_event()
    frames, fps, hold = dsn_render_events.render_handoff_echo_frames(target)

    assert len(frames) == dsn_limits.EVENT_FRAMES == 20
    assert fps == dsn_limits.EVENT_FPS == 5
    assert hold == 1
    assert len(frames) / fps == pytest.approx(4.0)

    old_point = event_scope_point(
        old.complex_name, old.azimuth, old.elevation)
    new_point = event_scope_point(
        new.complex_name, new.azimuth, new.elevation)

    old_frames = frames[:6]
    label_frames = frames[6:14]
    new_frames = frames[14:]
    assert tuple(map(len, (old_frames, label_frames, new_frames))) == (6, 8, 6)

    def varying_pixels(phase) -> set[tuple[int, int]]:
        return {
            (x, y)
            for y in range(dsn_limits.H)
            for x in range(dsn_limits.W)
            if len({frame.getpixel((x, y)) for frame in phase}) > 1
        }

    def maximum_channel_delta(phase, point) -> int:
        colours = {frame.getpixel(point) for frame in phase}
        return max(
            abs(first[channel] - second[channel])
            for first in colours
            for second in colours
            for channel in range(3)
        )

    # The physical-panel pulse changes brightness at the exact measured cell,
    # never by growing a decorative halo or moving the observation.  Each
    # change clears the panel's measured 30% / 77-channel visibility floor.
    assert varying_pixels(old_frames) == {old_point}
    assert varying_pixels(new_frames) == {new_point}
    assert maximum_channel_delta(old_frames, old_point) >= PANEL_STEP
    assert maximum_channel_delta(new_frames, new_point) >= PANEL_STEP
    assert all(frame.getpixel(new_point) == dsn_render_palette.OFF for frame in old_frames)
    assert all(frame.getpixel(old_point) == dsn_render_palette.OFF for frame in new_frames)

    for point, phase in ((old_point, old_frames), (new_point, new_frames)):
        neighbours = {
            (point[0] + dx, point[1] + dy)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            if (dx, dy) != (0, 0)
        }
        assert all(
            frame.getpixel(neighbour) == dsn_render_palette.OFF
            for frame in phase
            for neighbour in neighbours
        )

    # The middle beat is independently reconstructable full ink on black.
    # That makes it impossible for a later-composited native label to erase
    # either endpoint, and catches clipped suffixes in the final framebuffer.
    label = dsn_render_labels.event_label(target)
    full_ink = ink(label)
    label_width = max(x for x, _y in full_ink) + 1
    x0 = (dsn_limits.W - label_width) // 2
    expected_label = {(x0 + x, 5 + y) for x, y in full_ink}
    for frame in label_frames:
        actual = {
            (x, y)
            for y in range(dsn_limits.H)
            for x in range(dsn_limits.W)
            if frame.getpixel((x, y)) != dsn_render_palette.OFF
        }
        assert actual == expected_label
        assert len({frame.getpixel(point) for point in actual}) == 1

    # These are independent local skies. Any drawn association line between
    # Canberra and Goldstone must cross both optical moats, so keeping them
    # black during the two geometry beats proves the effect does not imply one
    # shared coordinate plane. The intervening text-only beat may legitimately
    # use the full width for its complete centered label.
    for frame in (*old_frames, *new_frames):
        for x in (23, 47, dsn_render_palette.FRESH_GUTTER_X, dsn_render_palette.FRESH_X):
            assert all(frame.getpixel((x, y)) == dsn_render_palette.OFF
                       for y in range(dsn_limits.H))


def test_handoff_echo_signature_includes_geometry_and_embedded_label():
    target, _old, _new = handoff_event()
    renamed = dict(target, craft="VGR2")
    moved, _moved_old, _moved_new = handoff_event(new_azimuth=90.0)

    signature = dsn_render_events.handoff_echo_signature(target)
    assert signature is not None
    assert dsn_render_events.handoff_echo_signature(renamed) is not None
    assert dsn_render_events.handoff_echo_signature(renamed) != signature
    assert dsn_render_events.handoff_echo_signature(moved) != signature


def test_skies_handoff_uploads_one_shot_echo_with_no_native_text_overlay(
        monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"specific-handoff")

    state.event_queue[:] = [target]
    assert asyncio.run(dsn_device_events.show_next_event(bb, state)) is True
    payload = bb.draws[-1]

    assert len(bb.uploads) == 1
    assert bb.uploads[0][2] == b"specific-handoff"
    animation = event_animation(payload)
    assert animation is not None
    assert animation.path == bb.uploads[0][1]
    assert animation.loop is False
    assert animation.timeout == dsn_limits.EVENT_TIMEOUT_S == 4
    assert event_text(payload) is None
    assert state.active_event_label == dsn_render_labels.event_label(target)
    assert state.active_event_embedded_label is True


def test_refused_skies_handoff_preserves_event_and_reuses_exact_echo_path(
        monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    state.event_queue = [target]
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"retryable-echo")

    class RefuseOnceBar(RuntimeBar):
        def __init__(self):
            super().__init__()
            self.refused = False

        async def display_draw(self, payload):
            self.draws.append(payload)
            if not self.refused:
                self.refused = True
                raise exceptions.BusyBarAPIError(
                    "Not drawn due to low priority", status_code=409)

    async def scenario():
        bb = RefuseOnceBar()
        assert await dsn_device_events.show_next_event(bb, state) is False
        first_path = event_animation(bb.draws[-1]).path
        assert state.event_queue == [target]
        assert len(bb.uploads) == len(state.scene_cache) == 1

        assert await dsn_device_events.show_next_event(bb, state) is True
        return bb, first_path

    bb, first_path = asyncio.run(scenario())
    assert state.event_queue == []
    assert len(bb.uploads) == 1
    assert [event_animation(payload).path for payload in bb.draws] == [
        first_path, first_path,
    ]
    assert all(event_text(payload) is None for payload in bb.draws)
    assert state.active_event_embedded_label is True


@pytest.mark.parametrize("craft", [
    "MARSRECONNAISSANCEORBITER",
    "VGR_1",
])
def test_handoff_echo_ineligible_label_uses_complete_native_text_only(
        monkeypatch, craft):
    target, _old, new = handoff_event(craft=craft)
    state = runtime_state(new)
    state.event_queue = [target]
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    def must_not_encode(*args, **kwargs):
        raise AssertionError("an ineligible embedded label reached encoding")

    monkeypatch.setattr(dsn_device_assets, "encode_native_frames", must_not_encode)

    assert asyncio.run(dsn_device_events.show_next_event(bb, state)) is True

    assert bb.uploads == []
    assert event_animation(bb.draws[-1]) is None
    native = event_text(bb.draws[-1])
    assert native is not None
    assert native.text == dsn_render_labels.event_label(target)
    assert native.scroll_rate == 1400
    assert state.active_event_embedded_label is False
    assert state.scene_cache == {}
    assert state.event_queue == []


@pytest.mark.parametrize(("old_valid", "new_valid"), [
    (False, True), (True, False), (False, False),
])
def test_skies_handoff_with_invalid_pointing_is_text_only_and_never_uploads(
        monkeypatch, old_valid, new_valid):
    target, _old, new = handoff_event(
        old_pointing=old_valid, new_pointing=new_valid)
    state = runtime_state(new)
    state.event_queue = [target]
    # Even a resident generic handoff must not substitute invented geometry.
    state.event_assets["handoff"] = "dsnevt_generic_handoff.anim"
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    assert asyncio.run(dsn_device_events.show_next_event(bb, state)) is True

    assert bb.uploads == []
    assert event_animation(bb.draws[-1]) is None
    assert event_text(bb.draws[-1]).text == dsn_render_labels.event_label(target)
    assert state.active_event_embedded_label is False
    assert state.scene_cache == {}
    assert state.event_queue == []


def test_picker_retires_embedded_handoff_without_creating_event_text(
        monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    state.event_queue = [target]
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"embedded-retirement")

    async def scenario():
        assert await dsn_device_events.show_next_event(bb, state) is True
        assert state.active_event_embedded_label is True
        state.picking = True
        await dsn_device_display.draw_picker(bb, state)

    asyncio.run(scenario())

    event_payload, picker_payload = bb.draws
    assert "eventtx" not in {element.id for element in event_payload.elements}
    assert "eventtx" not in {element.id for element in picker_payload.elements}
    retired = [element for element in picker_payload.elements
               if element.id in {"eventbg", "eventanim", "eventtx"}]
    assert {element.id for element in retired} == {"eventbg", "eventanim"}
    assert {element.timeout for element in retired} == {1}
    assert state.active_event_label is None
    assert state.active_event_asset is None
    assert state.active_event_embedded_label is False


def test_rows_handoff_keeps_the_prebaked_generic_asset_without_dynamic_upload(
        monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    state.event_queue = [target]
    state.event_assets["handoff"] = "dsnevt_generic_handoff.anim"
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "rows")

    assert asyncio.run(dsn_device_events.show_next_event(bb, state)) is True

    assert bb.uploads == []
    animation = event_animation(bb.draws[-1])
    assert animation is not None
    assert animation.path == "dsnevt_generic_handoff.anim"
    assert animation.loop is False


def test_handoff_echo_scene_cache_is_bounded_across_changing_geometry(
        monkeypatch):
    state = runtime_state()
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"small-echo")
    targets = [
        handoff_event(
            craft=f"C{index}",
            old_azimuth=(index * 29.0) % 360.0,
            old_elevation=10.0 + index % 7 * 10.0,
            new_azimuth=(index * 47.0 + 90.0) % 360.0,
            new_elevation=15.0 + index % 6 * 11.0,
        )[0]
        for index in range(dsn_limits.SCENE_CACHE_MAX + 3)
    ]
    signatures = {dsn_render_events.handoff_echo_signature(target) for target in targets}
    assert len(signatures) > dsn_limits.SCENE_CACHE_MAX, \
        "fixture did not produce enough distinct observed cell pairs"

    async def scenario():
        for target in targets:
            state.event_queue[:] = [target]
            assert await dsn_device_events.show_next_event(bb, state) is True
            release_event_card(state)

    asyncio.run(scenario())

    assert len(state.scene_cache) <= dsn_limits.SCENE_CACHE_MAX
    assert len(bb.removed) >= len(signatures) - dsn_limits.SCENE_CACHE_MAX


def test_handoff_echo_never_renders_or_uploads_while_wheel_picker_owns_input(
        monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    state.event_queue = [target]
    state.picking = True
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")

    assert asyncio.run(dsn_device_events.show_next_event(bb, state)) is False
    assert bb.uploads == []
    assert bb.draws == []
    assert state.scene_cache == {}
    assert state.event_queue == [target]


def test_picker_wins_if_it_arrives_during_a_handoff_echo_upload(monkeypatch):
    target, _old, new = handoff_event()
    state = runtime_state(new)
    state.event_queue = [target]
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "skies")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"gated-echo")

    class GatedUploadBar(RuntimeBar):
        def __init__(self):
            super().__init__()
            self.upload_started = asyncio.Event()
            self.release_upload = asyncio.Event()

        async def assets_upload(self, app: str, name: str, blob: bytes):
            self.upload_started.set()
            await self.release_upload.wait()
            await super().assets_upload(app, name, blob)

    async def scenario():
        bb = GatedUploadBar()
        showing = asyncio.create_task(dsn_device_events.show_next_event(bb, state))
        await asyncio.wait_for(bb.upload_started.wait(), 0.1)
        state.picking = True
        picking = asyncio.create_task(dsn_device_display.draw_picker(bb, state))
        await asyncio.sleep(0)
        bb.release_upload.set()
        assert await showing is False
        await picking
        return bb

    bb = asyncio.run(scenario())
    labels = [element.text for payload in bb.draws for element in payload.elements
              if getattr(element, "text", None) is not None]
    assert labels == [dsn_device_display.picker_label(state)]
    assert state.event_queue == [target]
    assert len(bb.uploads) == 1
    assert len(state.scene_cache) == 1

"""Pixel and runtime contracts for the DSN dish-roster Network redesign.

The ambient view is an inventory, not a coordinate plot: each site reports
its number of live link associations and each active dish appears exactly
once, with any per-dish multiplicity attached to that dish (``34(2)``).  A
rested wheel choice is the only operation that opens the selected-dish Focus,
where one source-reported aim and all links sharing that dish can be explained.

These tests are deliberately host-only.  They inspect final 72x16 pixels and
runtime intent without a BUSY Bar or network connection.
"""

from __future__ import annotations

import asyncio
import math
import os
import subprocess
import sys
import tomllib
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import config as dsn_config
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import settings as dsn_settings
from apps.dsn_app import source as dsn_source
from apps.dsn_app.device import assets as dsn_device_assets
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.device import scenes as dsn_device_scenes
from apps.dsn_app.render import dish as dsn_render_dish
from apps.dsn_app.render import labels as dsn_render_labels
from apps.dsn_app.render import network_dishes as dsn_render_network_dishes
from apps.dsn_app.render import network_skies as dsn_render_network_skies
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import scope as dsn_render_scope
from apps.dsn_app.render import text as dsn_render_text


ROSTER_BOX = (11, 69)
FOCUS_NAME_BOX = (16, 37)
FOCUS_HEADER_BOX = (0, 69)
FOCUS_ROWS = (6, 11)
QUIET = (85, 105, 130)
PANEL_STEP = 77


def link(
        *, site: str = "Canberra", dish: str = "DSS34",
        craft: str = "M01O", azimuth: float = 48.0,
        elevation: float = 22.0, band: str = "S",
        pointing_valid: bool = True, down: bool = True,
        up: bool = False,
        ) -> dsn_source.Link:
    down_streams = (
        (dsn_source.DownStream(band, 2_000_000.0, -130.0),) if down else ()
    )
    up_streams = (
        (dsn_source.UpStream("X", 18.0, "command"),) if up else ()
    )
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
        streams=len(down_streams),
        azimuth=azimuth,
        pointing_valid=pointing_valid,
        down_streams=down_streams,
        up_band="X" if up else "",
        up_streams=up_streams,
    )


def accepted_snapshot() -> list[dsn_source.Link]:
    """The concrete G3 / M0 / C5 case that exposed the old mystery tally."""
    return [
        link(site="Goldstone", dish="DSS23", craft="IMAP",
             azimuth=210.0, elevation=45.0),
        link(site="Goldstone", dish="DSS24", craft="CHDR",
             azimuth=120.0, elevation=30.0),
        link(site="Goldstone", dish="DSS26", craft="SOHO",
             azimuth=70.0, elevation=50.0),
        link(dish="DSS34", craft="M01O", up=True),
        link(dish="DSS34", craft="MRO", band="X"),
        link(dish="DSS35", craft="SPP", azimuth=80.0, elevation=35.0),
        link(dish="DSS36", craft="LRO", azimuth=130.0, elevation=40.0),
        link(dish="DSS43", craft="JNO", azimuth=200.0, elevation=55.0),
    ]


def ink(text: str) -> set[tuple[int, int]]:
    """Independent full glyph ink, with no implementation clip involved."""
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


def assert_complete_text(
        frame, text: str, x: int, y: int,
        colour: tuple[int, int, int],
        ) -> None:
    """Every promised source stroke must survive final composition."""
    expected = {(x + dx, y + dy) for dx, dy in ink(text)}
    assert expected
    assert all(0 <= xx < dsn_limits.W and 0 <= yy < dsn_limits.H
               for xx, yy in expected)
    wrong = {point: frame.getpixel(point) for point in expected
             if frame.getpixel(point) != colour}
    assert not wrong, f"{text!r} has wrong final pixels: {wrong}"


def assert_exact_text_region(
        frame, text: str, x: int, y: int, box: tuple[int, int],
        colour: tuple[int, int, int],
        ) -> None:
    """A static semantic region contains the complete label and nothing else."""
    expected = {(x + dx, y + dy) for dx, dy in ink(text)}
    actual = {
        (xx, yy)
        for xx in range(box[0], box[1] + 1)
        for yy in range(y, y + 5)
        if frame.getpixel((xx, yy)) != dsn_render_palette.OFF
    }
    assert actual == expected
    assert {frame.getpixel(point) for point in actual} == {colour}


def contains_complete_text(frame, text: str, y: int,
                           box: tuple[int, int]) -> bool:
    """Whether one frame exposes a whole token, including its parentheses."""
    source = ink(text)
    width = dsn_render_text.text_width(text)
    for x0 in range(box[0], box[1] - width + 2):
        actual = {
            (x - x0, yy - y)
            for x in range(x0, x0 + width)
            for yy in range(y, y + 5)
            if frame.getpixel((x, yy)) != dsn_render_palette.OFF
        }
        if actual == source:
            return True
    return False


def assert_complete_marquee(
        frames, text: str, y: int, box: tuple[int, int],
        colour: tuple[int, int, int] | None,
        ) -> None:
    """Compare every viewport to full source ink and expose every column."""
    source = ink(text)
    width = box[1] - box[0] + 1
    cycle = dsn_render_text.text_width(text) + dsn_render_text.SCROLL_GAP_PX
    exposed: set[tuple[int, int]] = set()
    for index, frame in enumerate(frames):
        offset = dsn_render_text.independent_scroll_offset(
            text, width, index, len(frames))
        expected: set[tuple[int, int]] = set()
        for copy in (0, cycle):
            for source_x, source_y in source:
                target_x = box[0] - offset + copy + source_x
                if box[0] <= target_x <= box[1]:
                    expected.add((target_x, y + source_y))
                    if copy == 0:
                        exposed.add((source_x, source_y))
        actual = {
            (x, yy)
            for x in range(box[0], box[1] + 1)
            for yy in range(y, y + 5)
            if (frame.getpixel((x, yy)) != dsn_render_palette.OFF if colour is None
                else frame.getpixel((x, yy)) == colour)
        }
        assert actual == expected
    assert exposed == source


def test_network_style_parser_accepts_dishes_and_preserves_both_rollbacks():
    """The environment boundary must accept the new value, not just tests."""
    app_dir = Path(__file__).resolve().parent.parent / "apps"
    probe = (
        "import sys; "
        f"sys.path.insert(0, {str(app_dir)!r}); "
        "from apps.dsn_app import settings as dsn_settings; "
        "dsn_settings.configure_runtime(); print(dsn_settings.DSN_NETWORK_STYLE)"
    )
    for supplied, expected in (
            ("dishes", "dishes"),
            ("skies", "skies"),
            ("rows", "rows"),
            ("not-a-style", "dishes"),
            ):
        env = dict(os.environ, DSN_NETWORK_STYLE=supplied)
        result = subprocess.run(
            [sys.executable, "-c", probe], env=env,
            check=True, capture_output=True, text=True,
        )
        assert result.stdout.strip() == expected


def test_registry_environment_and_runtime_share_one_network_style_default():
    root = Path(__file__).resolve().parent.parent
    registry = tomllib.loads((root / "apps.toml").read_text())
    declared = registry["dsn"]["config"]["DSN_NETWORK_STYLE"]
    env_values = [
        line.split("=", 1)[1].strip()
        for line in (root / ".env.example").read_text().splitlines()
        if line.startswith("DSN_NETWORK_STYLE=")
    ]

    assert declared["default"] == "dishes"
    assert declared["choices"] == ["dishes", "skies", "rows"]
    assert set(declared["choices"]) == set(dsn_config.NETWORK_STYLES)
    assert env_values == [declared["default"]]


def test_grouping_is_site_scoped_sorted_and_keeps_every_link_on_its_dish():
    links = accepted_snapshot()
    # A deliberately impossible cross-site duplicate proves grouping is not
    # merely a global dict keyed by the two dish-number digits.
    foreign = link(site="Madrid", dish="DSS34", craft="FOREIGN")
    groups = dsn_render_network_dishes.group_links_by_dish([foreign, *reversed(links)], "Canberra")

    assert [dish for dish, _ in groups] == ["DSS34", "DSS35", "DSS36", "DSS43"]
    assert [[item.craft for item in contacts] for _, contacts in groups] == [
        ["M01O", "MRO"], ["SPP"], ["LRO"], ["JNO"],
    ]
    assert all(item is not foreign
               for _, contacts in groups for item in contacts)


def test_ambient_roster_reconciles_site_totals_with_attached_multiplicity():
    frames, fps, hold = dsn_render_network_dishes.render_dish_network_frames(accepted_snapshot())

    assert frames and fps == dsn_limits.INSTRUMENT_FPS and hold == 1
    assert {frame.size for frame in frames} == {(72, 16)}
    for frame in frames:
        assert_complete_text(frame, "G3", 0, 0, dsn_render_dish.ANTENNA)
        assert_complete_text(frame, "M0", 0, 5, dsn_render_dish.ANTENNA)
        assert_complete_text(frame, "NO LINKS", 13, 5, QUIET)
        assert_complete_text(frame, "C5", 0, 10, dsn_render_dish.ANTENNA)
        assert_complete_text(frame, "34", 11, 10, dsn_render_palette.DISH_NO)
        assert_complete_text(frame, "(2)", 21, 10,
                             dsn_render_network_dishes.DISH_NETWORK_COUNT)
        assert contains_complete_text(frame, "34(2)", 10, ROSTER_BOX)
        for dish in ("23", "24", "26", "35", "36", "43"):
            row = 0 if dish in {"23", "24", "26"} else 10
            assert contains_complete_text(frame, dish, row, ROSTER_BOX)

    # A fitting inventory is a steady fact. It does not wiggle merely to look
    # live, and the source heartbeat remains the separate native x=71 lease.
    assert len({frame.crop((0, 0, 71, 16)).tobytes()
                for frame in frames}) == 1


def test_ambient_roster_is_invariant_to_pointing_and_has_no_sky_plot():
    links = accepted_snapshot()
    changed = [
        replace(item, azimuth=(item.azimuth + 173.0) % 360.0,
                elevation=89.0 - item.elevation,
                pointing_valid=not item.pointing_valid)
        for item in links
    ]
    selected_key = links[3].key
    before = dsn_render_network_dishes.render_dish_network_frames(
        links, selected_key=selected_key)[0]
    after = dsn_render_network_dishes.render_dish_network_frames(
        changed, selected_key=selected_key)[0]

    assert [frame.tobytes() for frame in before] == [
        frame.tobytes() for frame in after]
    assert dsn_render_network_dishes.dish_network_signature(
        links, selected_key=selected_key) == dsn_render_network_dishes.dish_network_signature(
            changed, selected_key=selected_key)
    positional_colours = {
        dsn_render_palette.SCOPE_RING, dsn_render_palette.SCOPE_HEAD, dsn_render_palette.SCOPE_TRAIL,
        dsn_render_palette.THREE_SKIES_NORTH, dsn_render_network_skies.THREE_SKIES_TRAIL,
    }
    # Selected-dish identity legitimately reuses the old selected white, so
    # exclude that one hue; literal pointing invariance above is the stronger
    # proof that the white token is not a coordinate head.
    assert not {
        frame.getpixel((x, y))
        for frame in before for x in range(70) for y in range(dsn_limits.H)
    } & positional_colours


def test_an_overfull_roster_exposes_every_complete_token_instead_of_slicing():
    links = [
        link(dish="DSS34", craft="A"),
        link(dish="DSS34", craft="B"),
        link(dish="DSS35", craft="C"),
        link(dish="DSS36", craft="D"),
        link(dish="DSS37", craft="E"),
        link(dish="DSS38", craft="F"),
        link(dish="DSS39", craft="G"),
    ]
    frames, _, _ = dsn_render_network_dishes.render_dish_network_frames(links)

    assert len({frame.crop((ROSTER_BOX[0], 10, ROSTER_BOX[1] + 1, 15)).tobytes()
                for frame in frames}) > 1
    for frame in frames:
        assert_complete_text(frame, "C7", 0, 10, dsn_render_dish.ANTENNA)
    for token in ("34(2)", "35", "36", "37", "38", "39"):
        assert any(contains_complete_text(frame, token, 10, ROSTER_BOX)
                   for frame in frames), token


def test_site_and_dish_link_totals_are_not_silently_capped_at_one_digit():
    contacts = [link(craft=f"C{index}") for index in range(12)]
    frames, _, _ = dsn_render_network_dishes.render_dish_network_frames(contacts)

    for frame in frames:
        assert_complete_text(frame, "C12", 0, 10, dsn_render_dish.ANTENNA)
        assert contains_complete_text(frame, "34(12)", 10, ROSTER_BOX)


def test_capacity_roster_exposes_twelve_distinct_dishes_and_one_shared_link():
    contacts = [
        link(dish=f"DSS{30 + index}", craft=f"C{index}")
        for index in range(12)
    ]
    contacts.append(link(dish="DSS34", craft="SHARED"))
    frames, fps, _ = dsn_render_network_dishes.render_dish_network_frames(contacts)

    assert len(frames) == dsn_render_network_dishes.dish_network_frame_count(contacts)
    assert len({frame.crop((11, 10, 70, 15)).tobytes()
                for frame in frames}) > 1
    assert fps == dsn_limits.INSTRUMENT_FPS
    for frame in frames:
        assert_complete_text(frame, "C13", 0, 10, dsn_render_dish.ANTENNA)
    expected = [str(number) for number in range(30, 42)]
    expected[4] = "34(2)"
    for token in expected:
        assert any(contains_complete_text(frame, token, 10, ROSTER_BOX)
                   for frame in frames), token


@pytest.mark.parametrize("freshness", ["fresh", "delayed", "stale", "offline"])
def test_roster_reserves_x70_and_uses_only_x71_for_freshness(freshness):
    frames, _, _ = dsn_render_network_dishes.render_dish_network_frames(
        accepted_snapshot(), freshness=freshness)

    assert all(frame.getpixel((70, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))
    columns = [tuple(frame.getpixel((71, y)) for y in range(dsn_limits.H))
               for frame in frames]
    if freshness == "fresh":
        assert set(columns) == {(dsn_render_palette.OFF,) * dsn_limits.H}
    elif freshness == "delayed":
        assert len(set(columns)) == 2
        assert all(set(column) <= {dsn_render_palette.OFF, dsn_render_palette.DELAYED}
                   for column in columns)
    else:
        expected = tuple(dsn_render_palette.STALE if y in (0, dsn_limits.H // 2, dsn_limits.H - 1)
                         else dsn_render_palette.OFF for y in range(dsn_limits.H))
        assert set(columns) == {expected}


@pytest.mark.parametrize("freshness", ["fresh", "delayed", "stale", "offline"])
def test_focus_preserves_the_same_black_gutter_and_freshness_rail(freshness):
    contacts = [link(craft="M01O", up=True), link(craft="MRO", band="X")]
    frames, _, _ = dsn_render_network_dishes.render_dish_focus_frames(
        contacts, freshness=freshness, selected_key=contacts[0].key)

    assert all(frame.getpixel((70, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))
    allowed = ({dsn_render_palette.OFF} if freshness == "fresh" else
               {dsn_render_palette.OFF, dsn_render_palette.DELAYED} if freshness == "delayed" else
               {dsn_render_palette.OFF, dsn_render_palette.STALE})
    assert all(set(frame.getpixel((71, y)) for y in range(dsn_limits.H)) <= allowed
               for frame in frames)


def test_selected_dish_focus_has_one_aim_and_two_explicit_link_rows():
    links = accepted_snapshot()
    selected = next(item for item in links if item.craft == "M01O")
    frames, fps, hold = dsn_render_network_dishes.render_dish_focus_frames(
        links, selected_key=selected.key)

    assert frames and fps == dsn_limits.INSTRUMENT_FPS and hold == 1
    assert {frame.size for frame in frames} == {(72, 16)}
    expected_head = dsn_render_scope._project_angles(
        selected.azimuth, selected.elevation, 7, 10, 5)
    for frame in frames:
        # The full-width grammar is self-explanatory without learned A/E
        # initials, and Focus does not repeat ambient's multiplicity syntax.
        assert_exact_text_region(
            frame, "C34 AZ048 EL22", 0, 0, FOCUS_HEADER_BOX,
            dsn_render_network_dishes.DISH_NETWORK_SELECTED)
        assert_complete_text(frame, "M01O", 16, 6,
                             dsn_render_network_dishes.DISH_NETWORK_SELECTED)
        assert_complete_text(frame, "TX", 39, 6, dsn_render_palette.UPLINK)
        assert_complete_text(frame, "RX", 54, 6, dsn_render_palette.BAND_PULSE["S"])
        assert_complete_text(frame, "MRO", 16, 11, dsn_render_palette.NAME)
        assert_complete_text(frame, "RX", 54, 11, dsn_render_palette.BAND_PULSE["X"])
        assert frame.getpixel(expected_head) in {
            dsn_render_network_skies.THREE_SKIES_SELECTED, dsn_render_palette.SCOPE_HEAD,
        }

        scope_box = {(x, y) for x in range(2, 13) for y in range(5, 16)}
        heads = {point for point in scope_box
                 if frame.getpixel(point) in {
                     dsn_render_network_skies.THREE_SKIES_SELECTED, dsn_render_palette.SCOPE_HEAD,
                 }}
        assert heads == {expected_head}
        assert all(frame.getpixel((x, y)) != dsn_render_palette.SCOPE_RING
                   for x in range(13, 70) for y in range(dsn_limits.H))
        assert all(frame.getpixel((70, y)) == dsn_render_palette.OFF for y in range(dsn_limits.H))


def test_focus_marquees_both_complete_friendly_names_in_their_own_rows():
    first = link(craft="M01O", up=True)
    second = link(craft="MRO", band="X")
    names = {
        "m01o": "Mars Odyssey",
        "mro": "Mars Reconnaissance Orbiter",
    }
    frames, _, _ = dsn_render_network_dishes.render_dish_focus_frames(
        [first, second], names=names, selected_key=first.key)

    assert len(frames) >= max(
        dsn_render_text.scroll_frame_count(dsn_render_labels.craft_label(first.craft, names), 22),
        dsn_render_text.scroll_frame_count(dsn_render_labels.craft_label(second.craft, names), 22),
    )
    assert_complete_marquee(
        frames, dsn_render_labels.craft_label(first.craft, names),
        FOCUS_ROWS[0], FOCUS_NAME_BOX, dsn_render_network_dishes.DISH_NETWORK_SELECTED)
    assert_complete_marquee(
        frames, dsn_render_labels.craft_label(second.craft, names),
        FOCUS_ROWS[1], FOCUS_NAME_BOX, dsn_render_palette.NAME)


def test_long_selected_name_restarts_a_complete_marquee_on_every_focus_page():
    selected = link(craft="MRO", up=True)
    contacts = [selected, link(craft="ONE"), link(craft="TWO"),
                link(craft="TRE")]
    names = {"mro": "Mars Reconnaissance Orbiter"}
    label = dsn_render_labels.craft_label(selected.craft, names)
    page_frames = dsn_render_text.scroll_frame_count(
        label, FOCUS_NAME_BOX[1] - FOCUS_NAME_BOX[0] + 1)
    frames, fps, _ = dsn_render_network_dishes.render_dish_focus_frames(
        contacts, names=names, selected_key=selected.key)

    assert fps == dsn_limits.INSTRUMENT_FPS
    assert len(frames) == 3 * page_frames
    for page in range(3):
        start = page * page_frames
        assert_complete_marquee(
            frames[start:start + page_frames], label, FOCUS_ROWS[0],
            FOCUS_NAME_BOX, dsn_render_network_dishes.DISH_NETWORK_SELECTED)
        assert all(frames[start].getpixel((14, y))
                   == dsn_render_network_dishes.DISH_NETWORK_COUNT for y in range(6, 11))


def test_new_semantic_colours_clear_the_measured_physical_panel_step():
    marker_neighbours = {
        dsn_render_palette.OFF, dsn_render_palette.DISH_NO, dsn_render_network_dishes.DISH_NETWORK_SELECTED, dsn_render_palette.NAME,
        dsn_render_dish.ANTENNA, dsn_render_palette.UPLINK, dsn_render_palette.SCOPE_RING,
        *dsn_render_palette.BAND_PULSE.values(),
    }
    for neighbour in marker_neighbours:
        assert max(abs(a - b) for a, b in zip(
            dsn_render_network_dishes.DISH_NETWORK_COUNT, neighbour)) >= PANEL_STEP
    assert max(abs(a - b) for a, b in zip(
        dsn_render_network_dishes.DISH_NETWORK_SELECTED, dsn_render_palette.DISH_NO)) >= PANEL_STEP
    assert max(abs(a - b) for a, b in zip(QUIET, dsn_render_palette.OFF)) >= PANEL_STEP


@pytest.mark.parametrize("bad_geometry", ["missing", "inconsistent"])
def test_focus_says_no_aim_instead_of_inventing_one(bad_geometry):
    first = link(craft="M01O", up=True)
    second = link(craft="MRO", band="X")
    if bad_geometry == "missing":
        second = replace(second, pointing_valid=False)
    else:
        second = replace(second, azimuth=210.0, elevation=70.0)
    frames, _, _ = dsn_render_network_dishes.render_dish_focus_frames(
        [first, second], selected_key=first.key)

    for frame in frames:
        assert_exact_text_region(
            frame, "C34 NO AIM", 0, 0, FOCUS_HEADER_BOX,
            dsn_render_network_dishes.DISH_NETWORK_COUNT)
        assert_complete_text(frame, "M01O", 16, 6,
                             dsn_render_network_dishes.DISH_NETWORK_SELECTED)
        assert_complete_text(frame, "MRO", 16, 11, dsn_render_palette.NAME)
        # Missing or contradictory coordinates invalidate the dish-scoped
        # claim. No plausible ring/head survives underneath the warning.
        assert not {
            frame.getpixel((x, y))
            for x in range(2, 13) for y in range(5, 16)
        } & {
            dsn_render_palette.SCOPE_RING, dsn_render_palette.SCOPE_HEAD, dsn_render_network_skies.THREE_SKIES_SELECTED,
        }


def test_future_dish_suffix_is_marqueed_in_full_in_ambient_and_focus():
    """A future DSS identifier must never regress to the old ``[-2:]`` slice."""
    future = link(dish="DSS123456789012345", craft="FUTURE")
    suffix = "123456789012345"
    header = f"C{suffix} AZ048 EL22"

    ambient, _, _ = dsn_render_network_dishes.render_dish_network_frames([future])
    assert dsn_render_text.text_width(suffix) > ROSTER_BOX[1] - ROSTER_BOX[0] + 1
    assert_complete_marquee(
        ambient, suffix, 10, ROSTER_BOX, dsn_render_palette.DISH_NO)

    focus, _, _ = dsn_render_network_dishes.render_dish_focus_frames(
        [future], selected_key=future.key)
    assert dsn_render_text.text_width(header) > FOCUS_HEADER_BOX[1] + 1
    assert_complete_marquee(
        focus, header, 0, FOCUS_HEADER_BOX,
        dsn_render_network_dishes.DISH_NETWORK_SELECTED)
    assert len(focus) == pytest.approx(
        dsn_render_network_dishes.dish_focus_loop_s([future], {}, future.key)
        * dsn_limits.INSTRUMENT_FPS)

    missing = replace(future, pointing_valid=False)
    missing_header = f"C{suffix} NO AIM"
    missing_focus, _, _ = dsn_render_network_dishes.render_dish_focus_frames(
        [missing], selected_key=missing.key)
    assert_complete_marquee(
        missing_focus, missing_header, 0, FOCUS_HEADER_BOX,
        dsn_render_network_dishes.DISH_NETWORK_COUNT)
    assert len(missing_focus) == pytest.approx(
        dsn_render_network_dishes.dish_focus_loop_s([missing], {}, missing.key)
        * dsn_limits.INSTRUMENT_FPS)


def test_focus_pages_more_than_two_co_dish_links_without_losing_one():
    selected = link(craft="ONE", up=True)
    contacts = [
        selected,
        link(craft="TWO", band="X"),
        link(craft="TRE", band="K"),
        link(craft="FOUR", band="KA"),
        link(craft="FIVE", band="S"),
    ]
    frames, fps, _ = dsn_render_network_dishes.render_dish_focus_frames(
        contacts, selected_key=selected.key)

    assert len(frames) == pytest.approx(
        dsn_render_network_dishes.dish_focus_loop_s(contacts, {}, selected.key) * fps)
    assert len(frames) >= 3 * dsn_limits.INSTRUMENT_FRAMES
    # The selected START/narration target leads the first page and has its own
    # hue; the other four contacts still each receive a complete visible row.
    assert_complete_text(frames[0], "ONE", 16, 6,
                         dsn_render_network_dishes.DISH_NETWORK_SELECTED)
    assert all(frames[0].getpixel((16 + x, 6 + y))
               == dsn_render_network_dishes.DISH_NETWORK_SELECTED for x, y in ink("ONE"))
    assert all(frames[0].getpixel((14, y)) == dsn_render_network_dishes.DISH_NETWORK_COUNT
               for y in range(6, 11))
    for name in ("ONE", "TWO", "TRE", "FOUR", "FIVE"):
        assert any(contains_complete_text(frame, name, row, FOCUS_NAME_BOX)
                   for frame in frames for row in FOCUS_ROWS), name
    for frame in frames:
        assert_exact_text_region(
            frame, "C34 AZ048 EL22", 0, 0, FOCUS_HEADER_BOX,
            dsn_render_network_dishes.DISH_NETWORK_SELECTED)


def _fresh_state(*links: dsn_source.Link) -> dsn_model.State:
    state = dsn_model.State(links=list(links), view="network", picking=True)
    state.feed_seeded = False
    return state


def test_only_picker_rest_opens_dish_focus_and_freezes_the_whole_dish(monkeypatch):
    links = accepted_snapshot()
    selected_index = next(index for index, item in enumerate(links)
                          if item.craft == "M01O")
    state = _fresh_state(*links)
    state.cursor = selected_index
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "dishes")

    # Detent-time state continues to be the instant native picker. No Focus
    # asset or dwell exists until the main loop observes PICK_REST_S.
    dsn_selection.note_manual_selection(state, now=10.0)
    before = dsn_device_scene_policy.scene_signature(
        state, state.current(), datetime.now(timezone.utc))
    assert state.network_focus_key is None
    assert before[0] == "dish-network"

    dsn_selection.commit_picker_selection(state, now=10.0 + dsn_limits.PICK_REST_S)
    after = dsn_device_scene_policy.scene_signature(
        state, state.current(), datetime.now(timezone.utc))

    assert state.picking is False
    assert state.network_focus_key == links[selected_index].key
    assert math.isinf(state.network_focus_until)
    assert after[0] == "dish-focus"
    assert {item.craft for item in state.network_focus_links
            if item.dish == "DSS34"} == {"M01O", "MRO"}


@pytest.mark.parametrize(
    ("style", "ambient_prefix", "focus_prefix"),
    [
        ("dishes", "dish-network", "dish-focus"),
        ("skies", "three-skies", "three-skies-focus"),
        ("rows", "network-page", "network-page"),
    ],
)
def test_new_style_does_not_remove_either_rollback(
        monkeypatch, style, ambient_prefix, focus_prefix):
    selected = link()
    state = _fresh_state(selected)
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", style)

    ambient = dsn_device_scene_policy.scene_signature(state, selected)
    assert ambient[0] == ambient_prefix
    dsn_selection.commit_picker_selection(state, now=20.0)
    focused = dsn_device_scene_policy.scene_signature(state, selected)
    assert focused[0] == focus_prefix
    if style == "rows":
        assert state.network_focus_key is None
        assert state.network_focus_until == 0.0
    else:
        assert state.network_focus_key == selected.key
        assert math.isinf(state.network_focus_until)


class RuntimeBar:
    def __init__(self) -> None:
        self.draw_times: list[float] = []
        self.uploads: list[tuple[str, str, bytes]] = []

    async def assets_upload(self, app: str, name: str, blob: bytes):
        self.uploads.append((app, name, blob))

    async def display_draw(self, payload):
        self.draw_times.append(asyncio.get_running_loop().time())

    async def storage_remove(self, path: str):
        return None


def test_accepted_dish_focus_starts_one_native_loop_after_the_picker_mask(
        monkeypatch):
    first = link(craft="M01O", up=True)
    second = link(craft="MRO", band="X")
    state = _fresh_state(first, second)
    state.names = {"m01o": "Mars Odyssey", "mro": "Mars Reconnaissance Orbiter"}
    bb = RuntimeBar()
    monkeypatch.setattr(dsn_settings, "DSN_NETWORK_STYLE", "dishes")
    monkeypatch.setattr(dsn_device_assets, "encode_native_frames",
                        lambda *args, **kwargs: b"dish-focus")
    dsn_selection.commit_picker_selection(state, now=10.0)
    expected_loop = dsn_render_network_dishes.dish_focus_loop_s(
        state.network_focus_links, state.network_focus_names,
        state.network_focus_key)

    async def scenario():
        # Far enough ahead that push_scene's `max(loop.time(), ...)` picks the
        # mask deadline no matter how slowly this runs. The original used +0.5s
        # with a 0.05s tolerance, which made the assertion a race against the
        # function under test: under coverage instrumentation push_scene took
        # 0.68s, the other branch of the max() won, and the test failed by
        # 0.18s. A wall-clock budget is not a contract.
        state.interactive_visible_until = (
            asyncio.get_running_loop().time() + 60.0)
        visible_at = state.interactive_visible_until
        accepted = await dsn_device_scenes.push_scene(bb, state, first)
        return accepted, visible_at

    accepted, visible_at = asyncio.run(scenario())

    assert accepted is True
    assert len(bb.uploads) == len(bb.draw_times) == 1
    assert math.isfinite(state.network_focus_until)
    # Exact, not approximate: the deadline IS the mask deadline plus one loop.
    assert state.network_focus_until == visible_at + expected_loop

"""Regression contracts for adversarial DSN rendering boundaries."""

from __future__ import annotations

import math
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import source as dsn_source
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.render import carriers as dsn_render_carriers
from apps.dsn_app.render import dish as dsn_render_dish
from apps.dsn_app.render import distance as dsn_render_distance
from apps.dsn_app.render import instrument as dsn_render_instrument
from apps.dsn_app.render import labels as dsn_render_labels
from apps.dsn_app.render import network_data as dsn_render_network_data
from apps.dsn_app.render import network_dishes as dsn_render_network_dishes
from apps.dsn_app.render import network_rows as dsn_render_network_rows
from apps.dsn_app.render import network_skies as dsn_render_network_skies
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import text as dsn_render_text
from apps.dsn_app.render import timing as dsn_render_timing


NOW = datetime(2026, 8, 8, 12, tzinfo=timezone.utc)


def link(*, site: str = "Goldstone", dish: str = "DSS14",
         craft: str = "FUTURE", azimuth: float = 0.0,
         elevation: float = 45.0, down: bool = True,
         up: bool = True) -> dsn_source.Link:
    streams = ((dsn_source.DownStream("X", 1_000.0, -140.0),) if down else ())
    return dsn_source.Link(
        complex_name=site, dish=dish, craft=craft,
        elevation=elevation, azimuth=azimuth, pointing_valid=True,
        band="X" if down else "", down_bps=1_000.0 if down else None,
        up_active=up, range_km=100_000.0, streams=len(streams),
        down_streams=streams,
        up_streams=((dsn_source.UpStream("X", 18.0),) if up else ()),
    )


def source_ink(text: str) -> set[tuple[int, int]]:
    points: set[tuple[int, int]] = set()
    cursor = 0
    for char in text:
        glyph = dsn_render_text.FONT[char]
        for y, row in enumerate(glyph):
            for x, bit in enumerate(row):
                if bit == "1":
                    points.add((cursor + x, y))
        cursor += len(glyph[0]) + dsn_render_text.GLYPH_GAP
    return points


def exposed_ink(frames, text: str, box: tuple[int, int], y: int,
                colour: tuple[int, int, int]) -> set[tuple[int, int]]:
    source = source_ink(text)
    cycle = dsn_render_text.text_width(text) + dsn_render_text.SCROLL_GAP_PX
    exposed: set[tuple[int, int]] = set()
    for index, frame in enumerate(frames):
        offset = dsn_render_text.independent_scroll_offset(
            text, box[1] - box[0] + 1, index, len(frames))
        for copy in (0, cycle):
            for source_x, source_y in source:
                target_x = box[0] - offset + copy + source_x
                if (box[0] <= target_x <= box[1]
                        and frame.getpixel((target_x, y + source_y)) == colour):
                    exposed.add((source_x, source_y))
    return exposed


def test_distance_signature_includes_visible_receive_presence():
    uplink = link(down=False)
    unknown_receive = replace(
        uplink, streams=1, down_streams=(dsn_source.DownStream("", None, None),))
    state = dsn_model.State(links=[uplink], view="distance", names={})

    assert dsn_device_scene_policy.scene_signature(state, uplink, NOW) != \
        dsn_device_scene_policy.scene_signature(state, unknown_receive, NOW)
    a = dsn_render_distance.render_frames(uplink, NOW)[0]
    b = dsn_render_distance.render_frames(unknown_receive, NOW)[0]
    assert [frame.tobytes() for frame in a] != [frame.tobytes() for frame in b]


def test_distance_signature_keeps_distinct_spacing_inside_one_text_label():
    first = link()
    first = replace(first, range_km=dsn_source.C_KM_S)
    second = replace(first, range_km=dsn_source.C_KM_S * 1.2)
    state = dsn_model.State(links=[first], view="distance")

    assert dsn_render_labels.light_label(first.light_s) == dsn_render_labels.light_label(second.light_s)
    assert dsn_render_carriers.represented_packet_spacing(first.light_s, False) != \
        dsn_render_carriers.represented_packet_spacing(second.light_s, False)
    assert dsn_device_scene_policy.scene_signature(state, first, NOW) != \
        dsn_device_scene_policy.scene_signature(state, second, NOW)
    first_frames = dsn_render_distance.render_frames(first, NOW)[0]
    second_frames = dsn_render_distance.render_frames(second, NOW)[0]
    assert [frame.tobytes() for frame in first_frames] != \
        [frame.tobytes() for frame in second_frames]


def test_future_dish_identity_is_complete_in_distance_and_instrument():
    contact = link(dish="DSS1234567890")

    dish, sep, box, header, combined, frame_count = \
        dsn_render_distance.distance_header_layout(contact, {})
    assert dish == "1234567890"
    assert sep is None and combined
    assert header == "1234567890/FUTURE"
    distance = dsn_render_distance.render_frames(contact, NOW)[0]
    assert len(distance) == frame_count <= dsn_limits.MAX_ANIMATION_FRAMES
    # Both semantic colour segments must enter the final composed box.
    assert any(frame.getpixel((x, y)) == dsn_render_palette.DISH_NO
               for frame in distance for x in range(box[0], box[1] + 1)
               for y in range(5))
    assert any(frame.getpixel((x, y)) == dsn_render_palette.NAME
               for frame in distance for x in range(box[0], box[1] + 1)
               for y in range(5))

    inst_dish, inst_sep, inst_box, inst_header, inst_frames = \
        dsn_render_instrument.instrument_header_layout(contact, {})
    assert inst_dish == "" and inst_sep is None
    assert inst_header == header
    instrument = dsn_render_instrument.render_instrument_frames(contact)[0]
    assert len(instrument) >= inst_frames
    assert exposed_ink(
        instrument[:inst_frames], inst_header, inst_box, 0, dsn_render_palette.NAME
    ) == source_ink(inst_header)


def test_future_dish_identity_is_complete_in_rows_and_three_skies():
    contact = link(dish="DSS1234567890")
    suffix = "1234567890"

    rows = dsn_render_network_rows.render_network_page_frames([contact], 0)[0]
    assert exposed_ink(rows, suffix, (6, 15), 0, dsn_render_palette.DISH_NO) == \
        source_ink(suffix)

    skies = dsn_render_network_skies.render_three_skies_frames(
        [contact], selected_key=contact.key)[0]
    assert exposed_ink(skies, suffix, (0, 8), 10, dsn_render_palette.DISH_NO) == \
        source_ink(suffix)

    identity = f"G{suffix}"
    focus = dsn_render_network_skies.render_three_skies_frames(
        [contact], selected_key=contact.key, focus=True)[0]
    assert exposed_ink(focus, identity, (0, 20), 0, dsn_render_palette.DISH_NO) == \
        source_ink(identity)


def test_three_skies_validates_same_dish_aim_before_pixel_quantization():
    first = link(craft="A", azimuth=0.0)
    second = link(craft="B", azimuth=9.0)
    assert dsn_render_network_skies._project_link(first, 63, 7, 6) == \
        dsn_render_network_skies._project_link(second, 63, 7, 6)

    groups, missing = dsn_render_network_skies._group_scope_links(
        [first, second], "Goldstone", 63, 7, 6)
    assert groups == []
    assert missing == 2


def test_three_skies_uses_visible_overflow_tokens_not_false_nines():
    contacts = [link(dish=f"DSS{20 + index}", craft=f"C{index}")
                for index in range(12)]
    frame = dsn_render_network_skies.render_three_skies_frames(contacts)[0][0]

    assert dsn_render_network_skies._site_count_label("G", 9) == "G9"
    assert dsn_render_network_skies._site_count_label("G", 12) == "G>"
    expected = {(x, y) for x, y in source_ink("G>")}
    assert all(frame.getpixel(point) == dsn_render_dish.ANTENNA for point in expected)


def test_dish_roster_uses_a_bounded_independent_clock_not_row_lcm():
    contacts = []
    for site, count in (("Goldstone", 9), ("Madrid", 17), ("Canberra", 35)):
        contacts.extend(link(site=site, dish=f"DSS{1000 + index}",
                             craft=f"{site[0]}{index}")
                        for index in range(count))
    frames = dsn_render_network_dishes.render_dish_network_frames(contacts)[0]

    assert len(frames) == dsn_render_network_dishes.dish_network_frame_count(contacts)
    assert dsn_limits.INSTRUMENT_FRAMES <= len(frames) <= dsn_limits.MAX_ANIMATION_FRAMES
    assert len(frames) < 1_200
    for site, _, _ in dsn_render_network_data.NETWORK_SITES:
        groups = dsn_render_network_dishes.group_links_by_dish(contacts, site)
        width = dsn_render_network_dishes._dish_roster_width(groups)
        if width <= 59:
            continue
        cycle = width + dsn_render_text.SCROLL_GAP_PX
        duration = len(frames) / dsn_limits.INSTRUMENT_FPS
        turns = max(1, math.floor(
            dsn_limits.SCROLL_SPEED_PX_S * duration / cycle))
        assert turns * cycle / duration <= dsn_limits.SCROLL_SPEED_PX_S


def test_distance_long_names_extend_the_native_loop_without_speeding_rf():
    contact = link()
    names = {"future": "X RAY MULTI MIRROR OBSERVATORY WITH LONG NAME"}
    frames = dsn_render_distance.render_frames(contact, NOW, names)[0]
    _, _, box, label, _, planned = dsn_render_distance.distance_header_layout(contact, names)

    assert len(frames) == planned > dsn_limits.ANIM_FRAMES
    assert len(frames) <= dsn_limits.MAX_ANIMATION_FRAMES
    cycle = dsn_render_text.text_width(label) + dsn_render_text.SCROLL_GAP_PX
    duration = len(frames) / dsn_limits.ANIM_FPS
    turns = max(1, math.floor(dsn_limits.SCROLL_SPEED_PX_S * duration / cycle))
    assert turns * cycle / duration <= dsn_limits.SCROLL_SPEED_PX_S
    # RF still repeats its exact eight-second source clock.
    rf_box = (dsn_limits.TRACK0, dsn_render_palette.UP_Y, dsn_limits.TRACK1 + 1, dsn_render_timing.DOWN_Y + 1)
    assert frames[0].crop(rf_box).tobytes() == \
        frames[dsn_limits.ANIM_FRAMES].crop(rf_box).tobytes()


def test_distance_refresh_is_aligned_to_the_extended_native_loop():
    contact = link()
    state = dsn_model.State(
        links=[contact], view="distance",
        names={"future": "X RAY MULTI MIRROR OBSERVATORY WITH LONG NAME"})
    loop_s = dsn_render_distance.distance_loop_s(contact, state.names)
    refresh_s = dsn_device_scene_policy.scene_refresh_s(state, contact)
    assert refresh_s / loop_s == pytest.approx(round(refresh_s / loop_s))


def test_equal_distance_clock_keys_use_one_exact_render_timestamp():
    contact = link()
    state = dsn_model.State(links=[contact], view="distance")
    cadence = dsn_device_scene_policy.scene_refresh_s(state, contact)
    bucket_start = math.floor(NOW.timestamp() / cadence) * cadence
    first = datetime.fromtimestamp(bucket_start + 1, timezone.utc)
    second = datetime.fromtimestamp(bucket_start + cadence - 1, timezone.utc)

    first_bucket, first_anchor = dsn_device_scene_policy.distance_render_clock(
        state, contact, first)
    second_bucket, second_anchor = dsn_device_scene_policy.distance_render_clock(
        state, contact, second)

    assert first_bucket == second_bucket
    assert first_anchor == second_anchor
    assert dsn_device_scene_policy.scene_signature(state, contact, first) == \
        dsn_device_scene_policy.scene_signature(state, contact, second)
    first_frames = dsn_render_distance.render_frames(contact, first_anchor)[0]
    second_frames = dsn_render_distance.render_frames(contact, second_anchor)[0]
    assert [frame.tobytes() for frame in first_frames] == \
        [frame.tobytes() for frame in second_frames]


def test_meaning_bearing_distance_tether_clears_physical_panel_step():
    assert max(dsn_render_palette.TETHER) >= 77
    assert max(dsn_render_palette.UP_TETHER) >= 77


def test_instrument_overflow_lane_does_not_sum_receive_rates(monkeypatch):
    contact = replace(
        link(), streams=4,
        down_streams=tuple(
            dsn_source.DownStream("X", 1_000_000.0, -140.0)
            for _ in range(4)),
        down_bps=None)
    counts: list[int] = []
    original = dsn_render_instrument._carrier_marks

    def record(px, y, phase, count, colour, outward, span=1):
        if not outward:
            counts.append(count)
        return original(px, y, phase, count, colour, outward, span)

    monkeypatch.setattr(dsn_render_instrument, "_carrier_marks", record)
    dsn_render_instrument.render_instrument_frames(contact)
    assert 0 in counts


def test_distance_signature_uses_the_same_canonical_site_as_the_renderer():
    contact = link(site="gdscc")
    state = dsn_model.State(links=[contact], view="distance")
    unknown = dsn_device_scene_policy.scene_signature(state, contact, NOW)
    unknown_frames = dsn_render_distance.render_frames(contact, NOW, site_lons={})[0]

    state.site_lons = {"Goldstone": -116.8895382}
    known = dsn_device_scene_policy.scene_signature(state, contact, NOW)
    known_frames = dsn_render_distance.render_frames(
        contact, NOW, site_lons=state.site_lons)[0]

    assert dsn_render_network_data._site_name("gdscc") == "Goldstone"
    assert unknown != known
    assert [frame.tobytes() for frame in unknown_frames] != \
        [frame.tobytes() for frame in known_frames]


def test_documented_network_total_names_every_admitted_target_truthfully():
    docs = Path("apps/dsn.md").read_text()
    prose = " ".join(docs.split())

    assert "dish-to-tracked-target associations" in docs
    assert "dish-to-spacecraft associations" not in docs
    assert "tracked flight hardware such as an Artemis upper stage" in prose


def test_dish_focus_bounds_cross_product_of_long_names_and_many_contacts():
    contacts = [link(craft=f"C{index}")
                for index in range(dsn_source.FEED_LINKS_PER_DISH_MAX)]
    names = {contact.craft.lower(): "W" * dsn_source.SOURCE_NAME_MAX
             for contact in contacts}

    frames = dsn_render_network_dishes.render_dish_focus_frames(
        contacts, names=names, selected_key=contacts[0].key)[0]
    summary = f"+{len(contacts) - 1} TARGETS"

    assert len(frames) <= dsn_limits.MAX_ANIMATION_FRAMES
    assert exposed_ink(
        frames, summary, dsn_render_network_dishes.DISH_FOCUS_CRAFT_BOX, 11,
        dsn_render_network_dishes.DISH_NETWORK_COUNT) == source_ink(summary)

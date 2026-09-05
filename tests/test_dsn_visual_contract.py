"""Adversarial visual/UX contracts for the DSN live instrument.

These are deliberately source-to-pixel tests.  They encode what must survive
the physical panel's LED gaps and measured ~30% contrast floor without asking
a BUSY Bar, NASA, or the Pi to participate.
"""

from __future__ import annotations

import asyncio
import itertools
import sys
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.dsn_app import input as dsn_input
from apps.dsn_app import limits as dsn_limits
from apps.dsn_app import model as dsn_model
from apps.dsn_app import selection as dsn_selection
from apps.dsn_app import source as dsn_source
from apps.dsn_app.device import scene_policy as dsn_device_scene_policy
from apps.dsn_app.render import distance as dsn_render_distance
from apps.dsn_app.render import instrument as dsn_render_instrument
from apps.dsn_app.render import labels as dsn_render_labels
from apps.dsn_app.render import network_data as dsn_render_network_data
from apps.dsn_app.render import network_rows as dsn_render_network_rows
from apps.dsn_app.render import palette as dsn_render_palette
from apps.dsn_app.render import text as dsn_render_text


PANEL_STEP = 77  # ceil(30% of an 8-bit channel), measured on the real panel


def independent_source_ink(text: str) -> set[tuple[int, int]]:
    """Rasterise declared glyph data without calling production `_text`."""
    result: set[tuple[int, int]] = set()
    cursor = 0
    for ch in text.upper():
        glyph = dsn_render_text.FONT[ch]
        for y, row in enumerate(glyph):
            for x, bit in enumerate(row):
                if bit == "1":
                    result.add((cursor + x, y))
        cursor += len(glyph[0]) + dsn_render_text.GLYPH_GAP
    return result


def independent_text_width(text: str) -> int:
    if not text:
        return 0
    return (sum(len(dsn_render_text.FONT[ch][0]) + dsn_render_text.GLYPH_GAP
                for ch in text.upper()) - dsn_render_text.GLYPH_GAP)


def instrument_link(**changes) -> dsn_source.Link:
    base = dsn_source.Link(
        complex_name="Canberra",
        dish="DSS43",
        craft="LRO",
        elevation=21.0,
        band="K",
        down_bps=28_736_400.0,
        up_active=True,
        range_km=366_000.0,
        down_dbm=-88.0,
        up_kw=18.0,
        streams=2,
        azimuth=331.0,
        down_streams=(
            dsn_source.DownStream("K", 28_590_000.0, -88.0),
            dsn_source.DownStream("S", 146_400.0, -120.0),
        ),
        up_band="X",
    )
    return replace(base, **changes)


def one_stream(band: str = "X", *, rate: float = 100.0,
               dbm: float | None = None, **changes) -> dsn_source.Link:
    return instrument_link(
        band=band,
        down_bps=rate,
        down_dbm=dbm,
        up_active=False,
        up_kw=0.0,
        streams=1,
        down_streams=(dsn_source.DownStream(band, rate, dbm),),
        up_band="",
        **changes,
    )


def metric_labels(link: dsn_source.Link) -> list[str]:
    return [label for phase in dsn_render_instrument._instrument_metrics(link)
            for label in phase if label]


def colour_step(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
    return max(abs(x - y) for x, y in zip(a, b))


def test_wheel_selection_starts_and_resets_a_manual_dwell(monkeypatch):
    """A deliberate selection must not be stolen by the 20-second rotator.

    The second detent resets the whole dwell rather than inheriting whatever
    few seconds remained from the first selection.
    """
    monkeypatch.setattr(dsn_limits, "MANUAL_DWELL_S", 120.0, raising=False)
    state = dsn_model.State(links=[instrument_link(craft="LRO"),
                             instrument_link(dish="DSS24", craft="CGO")])

    class InputBar:
        def __init__(self):
            self.first_draw = asyncio.Event()
            self.second_draw = asyncio.Event()
            self.next_turn = asyncio.Event()
            self.draws = 0

        async def stream_status_ws(self):
            yield {"updates": [{"input": {"encoder_event": {"delta": 1}}}]}
            await self.next_turn.wait()
            yield {"updates": [{"input": {"encoder_event": {"delta": -1}}}]}
            await asyncio.Event().wait()

        async def display_draw(self, payload):
            self.draws += 1
            (self.first_draw if self.draws == 1 else self.second_draw).set()

    async def scenario():
        bb = InputBar()
        listener = asyncio.create_task(dsn_input.listen_input(bb, state))
        await asyncio.wait_for(bb.first_draw.wait(), 0.2)
        first = state.manual_until
        assert first >= asyncio.get_running_loop().time() + 119.0

        await asyncio.sleep(0.01)
        bb.next_turn.set()
        await asyncio.wait_for(bb.second_draw.wait(), 0.2)
        assert state.manual_until > first

        listener.cancel()
        with pytest.raises(asyncio.CancelledError):
            await listener

    asyncio.run(scenario())


def test_auto_rotation_yields_while_manual_dwell_is_active(monkeypatch):
    """The rotator itself owns the final guard, not only the input path."""
    state = dsn_model.State(links=[instrument_link(craft="LRO"),
                             instrument_link(dish="DSS24", craft="CGO")])
    state.manual_until = float("inf")
    real_sleep = asyncio.sleep
    blocked = asyncio.Event()
    calls = 0

    async def one_rotation_then_block(delay):
        nonlocal calls
        calls += 1
        if calls > 1:
            await blocked.wait()

    monkeypatch.setattr(asyncio, "sleep", one_rotation_then_block)

    async def scenario():
        rotating = asyncio.create_task(dsn_selection.rotate(state))
        await real_sleep(0)  # first sleep/check, then blocked in the second
        assert state.cursor == 0
        rotating.cancel()
        with pytest.raises(asyncio.CancelledError):
            await rotating

    asyncio.run(scenario())


def test_transmit_power_never_mutates_the_top_label_pixels():
    """A strong source flare used to grow upward into DSS43's first digit."""
    quiet = instrument_link(up_active=False, up_kw=0.0)
    loud = instrument_link(up_active=True, up_kw=18.0)
    a = dsn_render_instrument.render_instrument_frames(quiet)[0][0]
    b = dsn_render_instrument.render_instrument_frames(loud)[0][0]

    assert a.crop((0, 0, dsn_render_palette.FRESH_GUTTER_X, 5)).tobytes() == \
        b.crop((0, 0, dsn_render_palette.FRESH_GUTTER_X, 5)).tobytes()


def test_selected_instrument_scrolls_the_full_nasa_spacecraft_name(monkeypatch):
    link = instrument_link(craft="LRO")
    names = {"lro": "Lunar Reconnaissance Orbiter"}
    seen: list[str] = []
    original = dsn_render_text._text

    def recording_text(px, x, y, text, colour, clip=None):
        seen.append(text)
        return original(px, x, y, text, colour, clip)

    monkeypatch.setattr(dsn_render_text, "_text", recording_text)
    frames, _, _ = dsn_render_instrument.render_instrument_frames(link, names=names)

    assert "LUNAR RECONNAISSANCE ORBITER" in seen
    assert any(frame.tobytes() != frames[0].tobytes() for frame in frames[1:])
    assert (dsn_render_instrument.instrument_signature(link, [], "fresh", names)
            != dsn_render_instrument.instrument_signature(link, [], "fresh", {}))


def test_every_band_colour_clears_the_measured_panel_step():
    colours = {band: dsn_render_palette.BAND_PULSE[band] for band in ("S", "X", "K", "KA")}
    for (a_name, a), (b_name, b) in itertools.combinations(colours.items(), 2):
        assert colour_step(a, b) >= PANEL_STEP, \
            f"{a_name}/{b_name} differ by only {colour_step(a, b)}/255"


def test_k_and_ka_are_not_the_same_visual_band():
    assert dsn_render_palette.BAND_PULSE["K"] != dsn_render_palette.BAND_PULSE["KA"]


def test_unknown_band_has_an_honest_distinct_colour():
    frame = dsn_render_instrument.render_instrument_frames(one_stream(""))[0][0]
    # One 100-bps inbound mark at phase zero is x=49.  Sampling the rendered
    # pixel tests the fallback path rather than merely a palette declaration.
    unknown = frame.getpixel((49, 9))
    assert unknown not in dsn_render_palette.BAND_PULSE.values()
    for band, colour in dsn_render_palette.BAND_PULSE.items():
        assert colour_step(unknown, colour) >= PANEL_STEP, \
            f"unknown/{band} differ by only {colour_step(unknown, colour)}/255"


def overflow_links() -> tuple[dsn_source.Link, dsn_source.Link]:
    literal = (
        dsn_source.DownStream("S", 100.0, -155.0),
        dsn_source.DownStream("S", 5_000.0, -145.0),
    )
    overflow = (
        dsn_source.DownStream("S", 50_000.0, -140.0),
        dsn_source.DownStream("S", 450_000.0, -130.0),
        dsn_source.DownStream("S", 5_000_000.0, -120.0),
        dsn_source.DownStream("S", 10_000_000.0, -110.0),
    )
    total = sum(stream.bps for stream in literal + overflow)
    six = instrument_link(
        craft="WIND", band="S", down_bps=total, down_dbm=-110.0,
        streams=6, down_streams=literal + overflow,
    )
    collapsed = instrument_link(
        craft="WIND", band="S", down_bps=total, down_dbm=-110.0,
        streams=3,
        down_streams=literal + (
            dsn_source.DownStream("S", sum(stream.bps for stream in overflow), -110.0),),
    )
    return six, collapsed


def test_overflow_lane_visibly_identifies_the_real_stream_count():
    six, collapsed = overflow_links()
    labels = metric_labels(six)
    assert any("6" in label or "+4" in label for label in labels), labels

    six_frames = dsn_render_instrument.render_instrument_frames(six)[0]
    collapsed_frames = dsn_render_instrument.render_instrument_frames(collapsed)[0]
    assert any(a.tobytes() != b.tobytes()
               for a, b in zip(six_frames, collapsed_frames)), \
        "six real streams are visually indistinguishable from three"


@pytest.mark.parametrize(
    ("changes", "explanation"),
    [({"arrayed": True}, ("ARRAY", "ARR")),
     ({"mspa": True}, ("MSPA",)),
     ({"ddor": True}, ("DDOR",))],
)
def test_each_mode_glyph_gets_an_explanatory_text_phase(
        monkeypatch, changes, explanation):
    seen: list[str] = []
    original = dsn_render_text._text

    def recording_text(px, x, y, text, colour, clip=None):
        seen.append(text)
        return original(px, x, y, text, colour, clip)

    monkeypatch.setattr(dsn_render_text, "_text", recording_text)
    dsn_render_instrument.render_instrument_frames(instrument_link(**changes))
    assert any(word in label for label in seen for word in explanation), seen


def test_active_power_metrics_name_rx_and_tx_compactly():
    labels = metric_labels(instrument_link())
    rx = [label for label in labels if label.startswith("RX")]
    tx = [label for label in labels if label.startswith("TX")]
    assert rx and tx, labels
    assert max(dsn_render_text.text_width(rx[0]), dsn_render_text.text_width(tx[0])) <= 27


def test_normal_and_hostile_content_never_claim_the_freshness_gutter():
    hostile = instrument_link(
        dish="DSS123456789",
        craft="MARS-RECONNAISSANCE-ORBITER-WITH-A-LONG-CODE",
        band="X-BAND-WITH-A-LONG-NAME",
        down_bps=999_999_999_999.0,
        down_dbm=-12_345.0,
        up_kw=999_999.0,
        streams=1,
        down_streams=(dsn_source.DownStream(
            "X-BAND-WITH-A-LONG-NAME", 999_999_999_999.0, -12_345.0),),
    )
    for link in (instrument_link(), hostile):
        frames = dsn_render_instrument.render_instrument_frames(link, freshness="fresh")[0]
        assert all(frame.getpixel((dsn_render_palette.FRESH_GUTTER_X, y)) == dsn_render_palette.OFF
                   for frame in frames for y in range(dsn_limits.H))
        # The baked fresh scene must also leave the native lease column alone.
        assert all(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.OFF
                   for frame in frames for y in range(dsn_limits.H))

        room = dsn_render_palette.INSTRUMENT_CONTENT_X1 - dsn_render_palette.INSTRUMENT_X0 + 1
        for left, right in dsn_render_instrument._instrument_metrics(link):
            assert dsn_render_text.text_width(left) <= room
            assert dsn_render_text.text_width(right) <= room
            assert dsn_render_text.text_width(left) + 3 + dsn_render_text.text_width(right) <= room

    first = dsn_render_instrument.render_instrument_frames(hostile)[0][0]
    assert any(first.getpixel((x, y)) == dsn_render_palette.NAME
               for x in range(dsn_render_palette.INSTRUMENT_X0, dsn_render_palette.INSTRUMENT_CONTENT_X1 + 1)
               for y in range(5)), "hostile dish text erased the craft identity"


def test_subsecond_light_time_is_never_rendered_as_zero():
    assert dsn_render_labels.light_label(0.01) == "SUBSEC"
    assert dsn_render_labels.light_label(0.99) == "SUBSEC"


def test_uplink_only_distance_scrolls_the_complete_status_word():
    """MMS2's SUBSEC + UPLINK row once amputated the status to ``UPLI``."""
    link = instrument_link(
        craft="MMS2", range_km=100_000.0, down_bps=0.0,
        streams=0, down_streams=(), up_active=True,
    )
    frames, _, _ = dsn_render_distance.render_frames(
        link, datetime(2026, 8, 8, tzinfo=timezone.utc))

    tx = dsn_render_palette.GLOBE_CX + dsn_render_palette.GLOBE_R + 3
    far = dsn_render_labels.light_label(link.light_s)
    assert far == "SUBSEC"
    box = (tx + dsn_render_text.text_width(far) + 3, dsn_limits.W - 1)
    assert dsn_render_text.text_width("UPLINK") > box[1] - box[0] + 1

    # Compare the final composed pixels to an independently unclipped full
    # word moving through its semantic box. A call spy would miss clipping
    # inside _text, exactly as the old NO LINK regression did.
    visible_source_columns: set[int] = set()
    offsets: list[int] = []
    cycle = dsn_render_text.text_width("UPLINK") + dsn_render_text.SCROLL_GAP_PX
    for index, frame in enumerate(frames):
        off = dsn_render_text.independent_scroll_offset(
            "UPLINK", box[1] - box[0] + 1, index, len(frames))
        offsets.append(off)
        expected = Image.new("RGB", (dsn_limits.W, dsn_limits.H), dsn_render_palette.OFF)
        px = expected.load()
        dsn_render_text._text(px, box[0] - off, dsn_limits.H - 5, "UPLINK", dsn_render_palette.RATE,
                  clip=box)
        dsn_render_text._text(px, box[0] - off + dsn_render_text.text_width("UPLINK")
                  + dsn_render_text.SCROLL_GAP_PX, dsn_limits.H - 5, "UPLINK", dsn_render_palette.RATE,
                  clip=box)
        crop = (box[0], dsn_limits.H - 5, box[1] + 1, dsn_limits.H)
        assert frame.crop(crop).tobytes() == expected.crop(crop).tobytes()

        for source_x in range(dsn_render_text.text_width("UPLINK")):
            placements = (box[0] - off + source_x,
                          box[0] - off + cycle + source_x)
            if any(box[0] <= target_x <= box[1] for target_x in placements):
                visible_source_columns.add(source_x)

    assert visible_source_columns == set(range(dsn_render_text.text_width("UPLINK")))
    # The last native frame hands back by the same ordinary per-frame step,
    # rather than teleporting the label at the .anim seam.
    assert offsets[0] == 0
    steps = [(later - earlier) % cycle
             for earlier, later in zip(offsets, offsets[1:])]
    seam_step = (-offsets[-1]) % cycle
    assert max([*steps, seam_step]) - min([*steps, seam_step]) <= 1


def carrier_positions(outward: bool) -> list[int]:
    positions = []
    colour = (251, 17, 193)
    for index in range(dsn_limits.INSTRUMENT_FRAMES):
        image = Image.new("RGB", (dsn_limits.W, dsn_limits.H), dsn_render_palette.OFF)
        dsn_render_instrument._carrier_marks(image.load(), 9, index / dsn_limits.INSTRUMENT_FRAMES,
                           1, colour, outward)
        lit = [x for x in range(dsn_render_palette.INSTRUMENT_X0, dsn_render_palette.INSTRUMENT_X1 + 1)
               if image.getpixel((x, 9)) == colour]
        assert len(lit) == 1
        positions.append(lit[0])
    return positions


@pytest.mark.parametrize("outward", [False, True])
def test_one_carrier_reaches_both_endpoints_and_has_a_clean_loop_seam(outward):
    positions = carrier_positions(outward)
    assert min(positions) <= dsn_render_palette.INSTRUMENT_X0 + 1
    assert max(positions) >= dsn_render_palette.INSTRUMENT_X1 - 1

    signed = 1 if outward else -1
    steps = [b - a for a, b in zip(positions, positions[1:] + positions[:1])]
    wraps = [i for i, step in enumerate(steps) if step * signed < 0]
    assert len(wraps) == 1
    ordinary = [step * signed for i, step in enumerate(steps) if i not in wraps]
    assert ordinary and all(1 <= step <= 3 for step in ordinary)

    wrap = wraps[0]
    before, after = positions[wrap], positions[(wrap + 1) % len(positions)]
    if outward:
        assert before >= dsn_render_palette.INSTRUMENT_X1 - 1
        assert after <= dsn_render_palette.INSTRUMENT_X0 + 1
    else:
        assert before <= dsn_render_palette.INSTRUMENT_X0 + 1
        assert after >= dsn_render_palette.INSTRUMENT_X1 - 1

    # The .anim's actual last->first boundary is ordinary motion, never the
    # large carrier re-emission wrap.
    assert 1 <= (positions[0] - positions[-1]) * signed <= 3


# --- all-network view -----------------------------------------------------


def network_links() -> list[dsn_source.Link]:
    """One genuinely different live topology at each DSN complex."""
    canberra = replace(
        one_stream("X", rate=12_000.0, dbm=-132.0),
        complex_name="Canberra", dish="DSS43", craft="LRO",
        up_active=True, up_kw=18.0, up_band="X",
    )
    goldstone = replace(
        instrument_link(),
        complex_name="Goldstone", dish="DSS14", craft="MRO",
        up_active=False, up_kw=0.0, up_band="",
        streams=2,
        down_streams=(
            dsn_source.DownStream("X", 1_000.0, -140.0),
            dsn_source.DownStream("S", 10_000.0, -130.0),
        ),
    )
    madrid = replace(
        instrument_link(),
        complex_name="Madrid", dish="DSS63", craft="JWST",
        band="", down_bps=0.0, down_dbm=None,
        up_active=True, up_kw=20.0, up_band="KA",
        streams=0, down_streams=(),
    )
    return [canberra, goldstone, madrid]


def network_frame_bytes(links: list[dsn_source.Link]) -> tuple[bytes, ...]:
    frames, _, _ = dsn_render_network_rows.render_network_frames(links, freshness="fresh")
    return tuple(frame.tobytes() for frame in frames)


def test_network_view_is_a_three_site_live_72_by_16_instrument():
    frames, fps, hold = dsn_render_network_rows.render_network_frames(
        network_links(), freshness="fresh")
    assert frames and fps > 0 and hold > 0
    assert all(frame.size == (72, 16) for frame in frames)

    # Each complex owns a readable five-ish-pixel row.  The middle row gets
    # the spare scanline; testing bands rather than exact baselines leaves the
    # artist room to tune separators without making a site disappear.
    row_bands = (range(0, 5), range(5, 11), range(11, 16))
    for rows in row_bands:
        assert any(frame.getpixel((x, y)) != dsn_render_palette.OFF
                   for frame in frames for y in rows for x in range(70))

    # The freshness rail has its own physical column, with x=70 kept as a
    # black optical gutter.  A fresh lease is native, not baked into the anim.
    assert all(frame.getpixel((dsn_render_palette.FRESH_GUTTER_X, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))
    assert all(frame.getpixel((dsn_render_palette.FRESH_X, y)) == dsn_render_palette.OFF
               for frame in frames for y in range(dsn_limits.H))


def test_empty_network_sites_show_every_pixel_of_no_link():
    """The 33px label once lived in a 31px clip, amputating the K on LEDs."""
    frames, _, _ = dsn_render_network_rows.render_network_page_frames([], 0, freshness="fresh")
    expected = Image.new("RGB", (dsn_limits.W, dsn_limits.H), dsn_render_palette.OFF)
    for x, y in independent_source_ink("NO LINK"):
        expected.putpixel((7 + x, y), (85, 105, 130))

    # Compare the complete label rather than merely recording that _text was
    # called: the bug was inside its clip window and looked fine to call spies.
    expected_row = expected.crop((7, 0, 40, 5)).tobytes()
    assert expected.getpixel((39, 0)) != dsn_render_palette.OFF  # K's missing outer stroke
    for frame in frames:
        for y0 in (0, 5, 10):
            assert frame.crop((7, y0, 40, y0 + 5)).tobytes() == expected_row
            assert all(frame.getpixel((x, y)) == dsn_render_palette.OFF
                       for x in range(40, 71) for y in range(y0, y0 + 5))


def test_network_view_visibly_reports_direction_and_stream_count():
    base = network_links()
    canberra = base[0]

    down_one = replace(canberra, up_active=False, up_kw=0.0, up_band="")
    both_one = canberra
    both_two = replace(
        canberra, streams=2,
        down_bps=canberra.down_bps + 500.0,
        down_streams=canberra.down_streams
        + (dsn_source.DownStream("S", 500.0, -150.0),),
    )

    down_frames = network_frame_bytes([down_one, *base[1:]])
    both_frames = network_frame_bytes([both_one, *base[1:]])
    two_frames = network_frame_bytes([both_two, *base[1:]])
    assert down_frames != both_frames, "an uplink has no visible direction cue"
    assert both_frames != two_frames, "one and two downlink streams look identical"


def test_network_signature_changes_exactly_with_visible_topology():
    links = network_links()
    signature = dsn_render_network_rows.network_signature(links, freshness="fresh")

    # Range, sky pointing, and RF power belong to the selected-link
    # instrument.  If they do not alter network pixels, they must not churn an
    # 80 KB animation upload either.
    hidden = [replace(link, range_km=(link.range_km or 1.0) + 123_456.0,
                      elevation=(link.elevation + 37.0) % 90.0,
                      azimuth=(link.azimuth + 123.0) % 360.0,
                      down_dbm=-17.0, up_kw=999.0)
              for link in links]
    assert network_frame_bytes(hidden) == network_frame_bytes(links)
    assert dsn_render_network_rows.network_signature(hidden, freshness="fresh") == signature

    direction = [replace(links[0], up_active=False), *links[1:]]
    count = [replace(
        links[0], streams=2,
        down_streams=links[0].down_streams
        + (dsn_source.DownStream("S", 500.0, -150.0),)), *links[1:]]
    for changed in (direction, count):
        assert network_frame_bytes(changed) != network_frame_bytes(links)
        assert dsn_render_network_rows.network_signature(changed, freshness="fresh") != signature


def test_hold_cycles_network_instrument_distance_and_back():
    state = dsn_model.State(view="network")
    assert [dsn_input.toggle_view(state) for _ in range(3)] == [
        "instrument", "distance", "network",
    ]


def test_every_paged_network_contact_gets_a_complete_friendly_name_marquee():
    base = network_links()[1]
    contacts = [
        replace(base, dish=f"DSS{14 + index}", craft=craft)
        for index, craft in enumerate(("ACE", "MRO", "ORX"))
    ]
    names = {
        "ace": "Advanced Composition Explorer",
        "mro": "Mars Reconnaissance Orbiter",
        "orx": "OSIRIS APEX Extended Mission",
    }
    box = (17, 37)
    for page, contact in enumerate(contacts):
        frames, fps, _ = dsn_render_network_rows.render_network_frames(
            contacts, names=names, page=page)
        label = names[contact.craft.lower()].upper()
        source = independent_source_ink(label)
        width = independent_text_width(label)
        cycle = width + dsn_render_text.SCROLL_GAP_PX
        exposed_source_columns: set[int] = set()
        offsets: list[int] = []

        assert len(frames) / fps >= 8
        for local, frame in enumerate(frames):
            offset = int(local / len(frames) * cycle)
            offsets.append(offset)
            expected: set[tuple[int, int]] = set()
            for copy in ((0,) if offset == 0 else (0, cycle)):
                for source_x, source_y in source:
                    target_x = box[0] - offset + copy + source_x
                    if box[0] <= target_x <= box[1]:
                        expected.add((target_x, source_y))
                        exposed_source_columns.add(source_x)
            actual = {
                (x, y)
                for y in range(5) for x in range(box[0], box[1] + 1)
                if frame.getpixel((x, y)) == dsn_render_palette.NAME
            }
            assert actual == expected

        assert exposed_source_columns == {x for x, _ in source}
        # The native last->first wrap advances no farther than an ordinary
        # step; a clipped-but-contained label or broken seam cannot hide here.
        ordinary_steps = [b - a for a, b in zip(offsets, offsets[1:])]
        seam_step = cycle - offsets[-1]
        assert seam_step <= max(ordinary_steps)


def test_multiple_uplink_records_and_wind_are_visible_without_power_summing():
    base = network_links()[0]
    one = replace(base, up_streams=(dsn_source.UpStream("X", 18.0, "data"),),
                  wind_kmh=8.0)
    two = replace(base, up_streams=(
        dsn_source.UpStream("X", 18.0, "data"),
        dsn_source.UpStream("S", 5.0, "ranging"),
    ), wind_kmh=11.0)

    assert tuple(dsn_render_instrument._instrument_metrics(two)) != tuple(dsn_render_instrument._instrument_metrics(one))
    assert any("2SIG" in page for page in dsn_render_instrument._instrument_metrics(two))
    assert ("WIND", "10KMH") in dsn_render_instrument._instrument_metrics(two)
    assert (dsn_render_instrument.render_instrument_frames(one)[0][0].tobytes()
            != dsn_render_instrument.render_instrument_frames(two)[0][0].tobytes())


def test_unknown_activity_uses_a_complete_generic_label_not_a_clipped_prefix():
    base = network_links()[0]
    unknown = replace(base, activity="Calibration and Maintenance")
    missing = replace(base, activity="")

    assert ("ACT", "OTHER") in dsn_render_instrument._instrument_metrics(unknown)
    assert ("ACT", "UNKNOWN") in dsn_render_instrument._instrument_metrics(missing)
    assert not any("CALIBRAT" in value
                   for page in dsn_render_instrument._instrument_metrics(unknown)
                   for value in page)


def test_multi_band_metric_splits_pages_instead_of_amputating_a_band():
    base = network_links()[0]
    mixed = replace(
        base,
        band="",
        down_bps=999_000.0,
        streams=2,
        down_streams=(
            dsn_source.DownStream("Ka", 500_000.0, -140.0),
            dsn_source.DownStream("S", 499_000.0, -141.0),
        ),
    )

    pages = dsn_render_instrument._instrument_metrics(mixed)
    assert ("KA/S", "RATE?") in pages
    assert ("999KBPS", "") not in pages
    assert not any(value.endswith("/") or value == "S/K"
                   for page in pages for value in page)


def test_hostile_wind_and_unknown_up_band_stay_bounded_and_honest():
    base = network_links()[0]
    hostile = replace(
        base,
        band="",
        down_bps=0.0,
        streams=0,
        down_streams=(),
        up_active=True,
        up_streams=(dsn_source.UpStream("FutureExperimentalBandName", 18.0, "data"),),
        wind_kmh=1e100,
    )

    pages = dsn_render_instrument._instrument_metrics(hostile)
    assert ("?", "TX") in pages
    assert ("WIND", "") in pages
    assert ("UNKNOWN", "") in pages
    frames, _, _ = dsn_render_instrument.render_instrument_frames(hostile)
    assert frames and all(frame.size == (dsn_limits.W, dsn_limits.H) for frame in frames)


def test_dense_instrument_metrics_extend_by_whole_rf_loops_for_readable_dwell():
    base = network_links()[0]
    dense = replace(
        base,
        arrayed=True,
        mspa=True,
        ddor=True,
        activity="Engineering Upgrades",
        wind_kmh=35.0,
        up_streams=(
            dsn_source.UpStream("X", 18.0, "data"),
            dsn_source.UpStream("Ka", 10.0, "data"),
        ),
        down_streams=(
            dsn_source.DownStream("Ka", 500_000.0, -140.0),
            dsn_source.DownStream("S", 499_000.0, -141.0),
        ),
        streams=2,
        down_bps=999_000.0,
    )
    metrics = dsn_render_instrument._instrument_metrics(dense, "handoff")
    frames, _, _ = dsn_render_instrument.render_instrument_frames(
        dense, contact_state="handoff")

    assert len(frames) % dsn_limits.INSTRUMENT_FRAMES == 0
    assert len(frames) >= len(metrics) * dsn_render_palette.INSTRUMENT_METRIC_MIN_FRAMES
    page_counts = [0] * len(metrics)
    for index in range(len(frames)):
        page_counts[min(len(metrics) - 1,
                        index * len(metrics) // len(frames))] += 1
    assert min(page_counts) >= dsn_render_palette.INSTRUMENT_METRIC_MIN_FRAMES
    prior = replace(dense, dish="DSS34")
    state = dsn_model.State(
        links=[dense], view="instrument",
        watch=dsn_model.Watch(
            link=prior, started_at=0.0, light_s=1.0, deadline=1.0,
            generation=1, return_view="instrument", live_key=dense.key),
    )
    loop_s = len(frames) / dsn_limits.INSTRUMENT_FPS
    refresh_s = dsn_device_scene_policy.scene_refresh_s(state, dense)
    assert refresh_s / loop_s == pytest.approx(round(refresh_s / loop_s))


def test_dense_metrics_do_not_slow_the_spacecraft_name_marquee():
    sparse = replace(network_links()[0], craft="LRO")
    dense = replace(
        sparse,
        arrayed=True,
        mspa=True,
        ddor=True,
        activity="Engineering Upgrades",
        wind_kmh=35.0,
        up_streams=(
            dsn_source.UpStream("X", 18.0, "data"),
            dsn_source.UpStream("Ka", 10.0, "data"),
        ),
        down_streams=(
            dsn_source.DownStream("Ka", 500_000.0, -140.0),
            dsn_source.DownStream("S", 499_000.0, -141.0),
        ),
        streams=2,
        down_bps=999_000.0,
    )
    names = {"lro": "Lunar Reconnaissance Orbiter"}
    _, _, box, _, header_frames = dsn_render_instrument.instrument_header_layout(sparse, names)
    sparse_frames = dsn_render_instrument.render_instrument_frames(sparse, names=names)[0]
    dense_frames = dsn_render_instrument.render_instrument_frames(
        dense, names=names, contact_state="handoff")[0]

    # Compare the final composed header pixels, not merely scroll offsets.
    crop = (box[0], 0, box[1] + 1, 5)
    reference = [frame.crop(crop).tobytes()
                 for frame in sparse_frames[:header_frames]]
    assert len(dense_frames) % header_frames == 0
    for start in range(0, len(dense_frames), header_frames):
        assert [frame.crop(crop).tobytes()
                for frame in dense_frames[start:start + header_frames]] == reference


def test_network_signature_uses_the_same_explicit_count_overflow_as_pixels():
    base = network_links()[0]
    ten = tuple(dsn_source.DownStream("X", 1000.0, -140.0) for _ in range(10))
    eleven = ten + (dsn_source.DownStream("X", 1000.0, -140.0),)
    a = [replace(base, streams=10, down_streams=ten)]
    b = [replace(base, streams=11, down_streams=eleven)]
    assert network_frame_bytes(a) == network_frame_bytes(b)
    assert dsn_render_network_rows.network_signature(a, "fresh") == dsn_render_network_rows.network_signature(b, "fresh")
    frame = dsn_render_network_rows.render_network_page_frames(a, 0)[0][0]
    site_y = next(y for site, _, y in dsn_render_network_data.NETWORK_SITES
                  if site == dsn_render_network_data._site_name(base.complex_name))
    assert any(frame.getpixel((x, y)) == dsn_render_palette.RATE
               for x in range(66, 70) for y in range(site_y, site_y + 5))


def test_distance_watch_names_off_air_and_handoff_instead_of_stale_live_rate(
        monkeypatch):
    link = replace(network_links()[0], range_km=dsn_source.C_KM_S * 120)
    labels: list[str] = []
    original = dsn_render_text._text

    def recording_text(px, x, y, text, colour, clip=None):
        labels.append(text)
        return original(px, x, y, text, colour, clip)

    monkeypatch.setattr(dsn_render_text, "_text", recording_text)
    now = datetime(2026, 8, 8, tzinfo=timezone.utc)
    dsn_render_distance.render_frames(link, now, realtime_since=now.timestamp() - 1,
                      on_air=False)
    assert "OFF AIR" in labels
    labels.clear()
    dsn_render_distance.render_frames(link, now, realtime_since=now.timestamp() - 1,
                      on_air=True, handoff=True)
    assert "HANDOFF" in labels

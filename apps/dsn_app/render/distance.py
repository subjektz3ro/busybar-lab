"""DSN render / distance."""

from __future__ import annotations

import math
from datetime import datetime

from PIL import Image

from apps.dsn_app import formatting as _formatting
from apps.dsn_app import limits as _limits
from apps.dsn_app import source as _source
from apps.dsn_app.render import carriers as _render_carriers
from apps.dsn_app.render import craft as _render_craft
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import globe as _render_globe
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import text as _render_text
from apps.dsn_app.render import timing as _render_timing


def distance_header_layout(
    link: _source.Link,
    names: dict[str, str] | None = None,
) -> tuple[str, int | None, tuple[int, int], str, bool, int]:
    """Complete Distance identity, with a combined future-width fallback.

    Current two-digit dishes keep the established coloured dish/name layout.
    If a future complete dish suffix would consume the craft box, both tokens
    become one segmented marquee rather than clipping, renaming, or indexing
    outside the framebuffer.
    """
    tx = _render_palette.GLOBE_CX + _render_palette.GLOBE_R + 3
    num_x = tx + _render_dish.DISH_ICON_W + 2
    dish = _render_network_data._dish_suffix(link.dish)
    craft = _render_labels.craft_label(link.craft, names or {})
    sep_candidate = num_x + _render_text.text_width(dish) + 2
    if sep_candidate + 3 <= _limits.W - max(_render_text.glyph_width("?"), 1):
        sep: int | None = sep_candidate
        box = (sep_candidate + 3, _limits.W - 1)
        moving = craft
        combined = False
    else:
        sep = None
        box = (num_x, _limits.W - 1)
        moving = f"{dish}/{craft}"
        combined = True
    frames = _render_text.scroll_frame_count(
        moving, box[1] - box[0] + 1, _limits.ANIM_FRAMES
    )
    return dish, sep, box, moving, combined, frames


def distance_frame_count(
    link: _source.Link,
    names: dict[str, str] | None = None,
) -> int:
    """Native asset length required by the complete Distance identity."""
    return distance_header_layout(link, names)[-1]


def distance_loop_s(
    link: _source.Link,
    names: dict[str, str] | None = None,
) -> float:
    return distance_frame_count(link, names) / _limits.ANIM_FPS


def render_frames(
    link: _source.Link,
    now: datetime,
    names: dict[str, str] | None = None,
    site_lons: dict[str, float] | None = None,
    realtime_since: float | None = None,
    dish_types: dict[str, str] | None = None,
    on_air: bool | None = None,
    handoff: bool = False,
    freshness: str = "fresh",
) -> tuple[list[Image.Image], int, int]:
    """One message's journey home, looped. Returns (frames, fps, frame_hold).

    Mostly-off by design: the panel's LEDs are physically spaced, so only
    lit pixels carry shape. Everything not part of Earth, the spacecraft,
    the pulse or the labels stays black.
    """
    # Where the sun actually is: the subsolar longitude is noon's meridian,
    # so the terminator on the globe is the real one.
    sun_lat, sun_lon = _render_globe.subsolar(now)
    # The globe is centred on the complex doing the listening, so its lit or
    # dark face is the real day or night AT THAT ANTENNA. The camera used to
    # drift instead — the centre advanced 15 deg/hr while the subsolar point
    # retreated 15 deg/hr, so they lapped twice a day and the disc went from
    # fully lit to fully dark and back every twelve hours, which no vantage
    # point in the universe does.
    centre_lon = (site_lons or {}).get(
        _render_network_data._site_name(link.complex_name)
    )
    if (
        centre_lon is not None
        and math.isfinite(centre_lon)
        and -180.0 <= centre_lon <= 180.0
    ):
        site_geometry_known = True
        true_spin = (centre_lon % 360.0) / 360.0
    else:
        site_geometry_known = False
        true_spin = 0.0
    # Locked: the crossing is the real one, not the 1/600 browsing speed.
    pulse = _render_palette.BAND_PULSE.get(
        _source.band_key(link.band), _render_palette.UNKNOWN_PULSE
    )
    big_dish = _formatting.dish_metres(link.dish, dish_types) == "70"
    light_s = link.light_s
    live = realtime_since is not None and light_s is not None
    range_known = light_s is not None
    # Past a couple of minutes an 8-second loop cannot animate the truth, so
    # the chain is placed from the wall clock instead and simply sits there
    # between redraws. It is not frozen; it is moving at 0.0006 px/s.
    listening = link.down_active
    if (
        realtime_since is not None
        and light_s is not None
        and light_s > _limits.RT_SEAMLESS_MAX_S
    ):
        creeping = True
        progress = _render_carriers.represented_progress(
            light_s, now.timestamp(), realtime_since
        )[1]
    else:
        creeping = False
        progress = 0.0

    dish_no, header_sep_x, header_box, header_text, combined_header, frame_count = (
        distance_header_layout(link, names)
    )

    track0, track1 = _limits.TRACK0, _limits.TRACK1
    frames: list[Image.Image] = []
    for i in range(frame_count):
        rf_index = i % _limits.ANIM_FRAMES
        phase = rf_index / _limits.ANIM_FRAMES
        # A browsing carrier is source-driven and freezes when the source
        # lease ages out. A locked journey is a disclosed local stopwatch, so
        # its represented head may continue under an explicit stale label.
        rf_phase = phase if freshness == "fresh" or live else 0.0
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)

        # One turn per loop: seamless, and identical for every spacecraft
        # because the loop is a fixed 8 seconds. (It was once tied to a loop
        # whose length was the crossing time, so Earth span faster whenever a
        # nearer craft came up. That must not come back.)
        #
        # The shadow turns WITH the planet, because night belongs to the
        # geography and not to the screen. Every frame is a true picture of
        # which longitudes are in daylight at this instant; the loop simply
        # carries you around the planet faster than anyone could travel.
        # Turning past the terminator is also what guarantees you see one at
        # all — pinning it to the screen left the disc entirely lit or
        # entirely dark for the whole loop at some hours.
        # Anchored to the wall clock, not to the start of this animation:
        # every push previously restarted the turn wherever the loop began,
        # so a new scene snapped the planet to a different rotation. Deriving
        # it from the clock means a fresh loop picks up exactly where the last
        # one left off.
        # MINUS, not plus. `spin` is the longitude at the centre of the disc,
        # and for an observer fixed in space that longitude DECREASES as Earth
        # turns east - which is exactly why the subsolar longitude above
        # decreases too. Adding spun the planet backwards: Greenwich drifted
        # left across the disc where the real thing drifts right, and since
        # the shadow rides the geography the terminator swept the wrong way
        # with it.
        spin = (true_spin - phase - now.timestamp() / _limits.LOOP_S) % 1.0
        if site_geometry_known:
            _render_globe._globe(px, spin, sun_lat, sun_lon)
        else:
            _render_globe._unknown_globe(px)
        _render_craft._craft(
            px, _render_craft.CRAFT_X, _render_craft.CRAFT_Y, link.craft, phase
        )

        # The chain, travelling spacecraft -> Earth. Every packet advances
        # exactly one spacing across the loop, so the pattern at the last
        # frame is the pattern at the first: seamless at any speed.
        # TWO links, stacked, because a pass is a conversation. Earth's
        # half runs on the upper row and the spacecraft's on the lower, each
        # with its own tether. Both on one line would be ambiguous no matter
        # how they were coloured; the renderer used to dodge that by drawing
        # only one direction, so on a two-way pass half the conversation was
        # simply invisible.
        spacing = _render_carriers.represented_packet_spacing(link.light_s, live) or 2.0
        positions = []  # the downlink chain, reused by the arrival flare

        if link.up_active and creeping:
            # Locked, Earth's half is one represented carrier slice too. The distance is the
            # same in both directions, so a full chain outbound beside a
            # single creeping pulse inbound would claim they travel at
            # different speeds.
            for tx_ in range(track0, track1 + 1):
                px[tx_, _render_palette.UP_Y] = _render_palette.UP_TETHER
            uphead = track0 + progress * (track1 - track0)
            back = int(math.floor(uphead)) - 2
            while back >= track0:
                px[back, _render_palette.UP_Y] = _render_timing._scale_rgb(
                    _render_palette.UPLINK, 0.28
                )
                back -= 2
            for dx in (0, -1, -2):
                _render_timing._mark(
                    px,
                    uphead + dx,
                    _render_palette.UP_Y,
                    _render_palette.UPLINK
                    if dx == 0
                    else _render_timing._scale_rgb(
                        _render_palette.UPLINK, 0.5 if dx == -1 else 0.25
                    ),
                )
        elif link.up_active and range_known:
            # Earth SHOUTS: tens of kilowatts. Drawn heavy and bright.
            _render_carriers._link_row(
                px,
                _render_palette.UP_Y,
                track0,
                track1,
                rf_phase,
                spacing,
                outward=True,
                span=3,
                colour=_render_palette.UPLINK,
                tether=_render_palette.UP_TETHER,
            )
        elif link.up_active:
            _render_carriers._static_link_row(
                px,
                _render_palette.UP_Y,
                track0,
                track1,
                span=3,
                colour=_render_palette.UPLINK,
                tether=_render_palette.UP_TETHER,
            )

        if listening:
            if creeping:
                head = track1 - progress * (track1 - track0)
                positions = [head]
                for tx_ in range(track0, track1 + 1):
                    px[tx_, _render_timing.DOWN_Y] = _render_palette.TETHER
                trail = int(math.ceil(head)) + 2
                while trail <= track1:
                    px[trail, _render_timing.DOWN_Y] = _render_timing._scale_rgb(
                        pulse, 0.28
                    )
                    trail += 2
                _render_timing._mark(px, head, _render_timing.DOWN_Y, pulse)
                for dx in (1, 2):  # a short bright wake
                    _render_timing._mark(
                        px,
                        head + dx,
                        _render_timing.DOWN_Y,
                        _render_timing._scale_rgb(pulse, 0.5 if dx == 1 else 0.25),
                    )
            elif range_known:
                # ...and what comes back is attowatts. Thin, and never as
                # bright as the uplink: 18 kW out against eight attowatts back is
                # a ratio near 10^21, and
                # the contrast IS the story. Bounded by the panel's gamma
                # floor so "faint" still survives on real LEDs.
                positions = _render_carriers._link_row(
                    px,
                    _render_timing.DOWN_Y,
                    track0,
                    track1,
                    rf_phase,
                    spacing,
                    outward=False,
                    span=_render_carriers.pulse_span(link.down_bps),
                    colour=pulse,
                    tether=_render_palette.TETHER,
                )
            else:
                _render_carriers._static_link_row(
                    px,
                    _render_timing.DOWN_Y,
                    track0,
                    track1,
                    span=_render_carriers.pulse_span(link.down_bps),
                    colour=pulse,
                    tether=_render_palette.TETHER,
                )
        else:
            # Nothing coming down, but the link is still there. Drawing the
            # quiet direction as a bare tether says "open, and silent", which
            # is the truth on an uplink-only pass.
            for tx_ in range(track0, track1 + 1):
                px[tx_, _render_timing.DOWN_Y] = _render_palette.TETHER

        # Flare the limb when a packet actually lands, whatever the spacing.
        if any(track0 <= int(round(p)) <= track0 + 1 for p in positions):
            for dy in (-1, 0, 1):
                y = _render_timing.DOWN_Y + dy
                gx = _render_palette.GLOBE_CX + _render_palette.GLOBE_R
                if 0 <= gx < _limits.W and 0 <= y < _limits.H:
                    px[gx, y] = (255, 240, 200)

        # Two text rows, both clear of the globe's column.
        # Top:    who it is                        | which dish
        # Bottom: how far away, how fast
        if (
            live
            and freshness == "fresh"
            and on_air is not False
            and not handoff
            and (rf_index // 10) % 2 == 0
        ):
            # Two pixels in the free corner, blinking once every four seconds:
            # the chain itself may not visibly move for half an hour, so
            # something has to say the strip is alive and showing real time.
            for mx in (0, 1):
                px[mx, 0] = (255, 120, 60)

        # Each label sits at the end it describes: the antenna beside the
        # globe it is standing on, the spacecraft's name beside the
        # spacecraft. They used to be the other way round, which made the
        # eye cross the strip to connect either one to its subject.
        # Arraying, MSPA and DDOR were narrated and never drawn, so a
        # four-dish array looked exactly like one dish. One column at x14 —
        # the only empty space on this row — carries them: three marks for
        # several dishes on one spacecraft, two for several spacecraft in one
        # beam, one for a navigation fix. They can co-occur and never collide.
        if link.arrayed:
            for yy in (0, 2, 4):
                px[_render_dish.MODE_X, yy] = _render_dish.ANTENNA
        if link.mspa:
            for yy in (1, 3):
                px[_render_dish.MODE_X, yy] = _render_palette.NAME
        if link.ddor:
            px[_render_dish.MODE_X, 2] = _render_dish.DDOR_MARK

        tx = _render_palette.GLOBE_CX + _render_palette.GLOBE_R + 3
        _render_dish._dish_icon(
            px, tx, 0, link.elevation if link.pointing_valid else None, big_dish
        )
        num_x = tx + _render_dish.DISH_ICON_W + 2
        header_box_px = header_box[1] - header_box[0] + 1
        header_off = _render_text.independent_scroll_offset(
            header_text, header_box_px, i, frame_count
        )
        if combined_header:
            segments = (
                (dish_no, _render_palette.DISH_NO),
                ("/", (70, 85, 105)),
                (
                    _render_labels.craft_label(link.craft, names or {}),
                    _render_palette.NAME,
                ),
            )
            _render_labels._draw_text_segments(
                px, header_box[0] - header_off, 0, segments, header_box
            )
            if header_off:
                _render_labels._draw_text_segments(
                    px,
                    header_box[0]
                    - header_off
                    + _render_text.text_width(header_text)
                    + _render_text.SCROLL_GAP_PX,
                    0,
                    segments,
                    header_box,
                )
        else:
            _render_text._text(px, num_x, 0, dish_no, _render_palette.DISH_NO)
            # A dim rule, not a '|' glyph: it costs 1px instead of 5, and
            # prevents "43" and "VOYAGER 2" reading as one token.
            assert header_sep_x is not None
            for yy in range(0, 5):
                px[header_sep_x, yy] = (40, 55, 75)
            _render_text._text(
                px,
                header_box[0] - header_off,
                0,
                header_text,
                _render_palette.NAME,
                clip=header_box,
            )
            if header_off:
                _render_text._text(
                    px,
                    header_box[0]
                    - header_off
                    + _render_text.text_width(header_text)
                    + _render_text.SCROLL_GAP_PX,
                    0,
                    header_text,
                    _render_palette.NAME,
                    clip=header_box,
                )
        # Left/right rather than one string keeps the stable distance beside
        # a right-hand status. When both complete tokens do not fit, only the
        # right token moves; it is never shortened into a misleading prefix.
        # Locked, the countdown is a DEVICE element that ticks by itself once
        # a second (see _countdown_payload). Baked into these frames it would
        # only change when the scene is re-pushed, which on Voyager is once
        # every five minutes — a clock that jumps five minutes at a time.
        # Its text is still measured here so the rate label on the right is
        # laid out against the space it occupies, then simply not drawn.
        if live:
            assert realtime_since is not None
            far = _render_timing.countdown_label(
                light_s, realtime_since, now.timestamp()
            )
        else:
            far = _render_labels.light_label(light_s)
        fast_full = (
            "DELAY"
            if freshness == "delayed"
            else "STALE"
            if freshness in {"stale", "offline"}
            else "OFF AIR"
            if live and on_air is False
            else "HANDOFF"
            if live and handoff
            else _render_labels.rate_label(link.down_bps)
            if listening
            else "UPLINK"
            if link.up_active
            else "QUIET"
        )
        far, fast = _render_labels.fit_row(far, fast_full, _limits.W - 1 - tx)
        if not live:  # the device draws it when locked
            _render_text._text(px, tx, _limits.H - 5, far, _render_palette.DIST)
        if fast == fast_full:
            fast_x = _limits.W - 1 - _render_text.text_width(fast)
            _render_text._text(px, fast_x, _limits.H - 5, fast, _render_palette.RATE)
        else:
            # A semantic status or unit must never be made to "fit" by
            # amputating its suffix. MMS2's SUBSEC + UPLINK used to become
            # UPLI here. Keep the distance fixed and marquee the complete
            # right-hand label through the remaining semantic box.
            fast = _render_labels.fit_label(fast_full, _limits.W)
            fast_x = tx + _render_text.text_width(far) + 3
            fast_box = (fast_x, _limits.W - 1)
            off = _render_text.independent_scroll_offset(
                fast, fast_box[1] - fast_box[0] + 1, i, frame_count
            )
            _render_text._text(
                px,
                fast_box[0] - off,
                _limits.H - 5,
                fast,
                _render_palette.RATE,
                clip=fast_box,
            )
            _render_text._text(
                px,
                fast_box[0]
                - off
                + _render_text.text_width(fast)
                + _render_text.SCROLL_GAP_PX,
                _limits.H - 5,
                fast,
                _render_palette.RATE,
                clip=fast_box,
            )
        # Same divider as the top row. On Voyager these two close to within a
        # couple of pixels and '19.8H' '160BPS' reads as one long number.
        gap0, gap1 = tx + _render_text.text_width(far), fast_x
        if gap1 - gap0 >= 3:
            for yy in range(_limits.H - 5, _limits.H):
                px[(gap0 + gap1) // 2, yy] = (40, 55, 75)
        frames.append(img)
    return frames, _limits.ANIM_FPS, 1

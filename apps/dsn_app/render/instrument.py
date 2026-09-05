"""DSN render / instrument."""

from __future__ import annotations

import math

from PIL import Image

from apps.dsn_app import formatting as _formatting
from apps.dsn_app import limits as _limits
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import scope as _render_scope
from apps.dsn_app.render import text as _render_text


def _carrier_marks(
    px,
    y: int,
    phase: float,
    count: int,
    colour: tuple[int, int, int],
    outward: bool,
    span: int = 1,
) -> None:
    """Move every carrier at one fixed symbolic speed; density carries bps."""
    if count <= 0:
        return
    width = _render_palette.INSTRUMENT_X1 - _render_palette.INSTRUMENT_X0
    for index in range(count):
        # Start away from an endpoint so a one-carrier stream is visible in
        # the first frame instead of spending its opening beat under a flare.
        fraction = (index / count + phase + 0.20) % 1.0
        # Sample width+1 positions so a 40-frame clock reaches both physical
        # endpoints without dwelling twice on either one. That keeps motion
        # to one or two LEDs per 200 ms and leaves a single intentional wrap.
        offset = max(0, min(width, int(round(fraction * (width + 1)))))
        x = (
            _render_palette.INSTRUMENT_X0 + offset
            if outward
            else _render_palette.INSTRUMENT_X1 - offset
        )
        direction = -1 if outward else 1
        for offset in range(span):
            xx = x + direction * offset
            if _render_palette.INSTRUMENT_X0 <= xx <= _render_palette.INSTRUMENT_X1:
                px[xx, y] = colour


def _draw_mode_glyph(
    px, x: int, rows: tuple[str, ...], colour: tuple[int, int, int]
) -> None:
    for dy, row in enumerate(rows):
        for dx, bit in enumerate(row):
            if bit == "1":
                px[x + dx, 6 + dy] = colour


def _metric_pages(left: str, right: str) -> tuple[tuple[str, str], ...]:
    """Lay out complete metric tokens, splitting instead of amputating them."""
    room = _render_palette.INSTRUMENT_CONTENT_X1 - _render_palette.INSTRUMENT_X0 + 1
    left = "".join(ch for ch in left.upper() if ch in _render_text.FONT)
    right = "".join(ch for ch in right.upper() if ch in _render_text.FONT)
    # All callers cap/normalise their generated vocabulary. A future token
    # wider than the entire metric rail is a programmer contract violation,
    # not permission to ship a misleading prefix.
    if _render_text.text_width(left) > room or _render_text.text_width(right) > room:
        raise ValueError(
            f"instrument metric token exceeds {room}px: {left!r}, {right!r}"
        )
    if _render_text.text_width(left) + 3 + _render_text.text_width(right) <= room:
        return ((left, right),)
    return ((left, ""), (right, ""))


def receive_power_label(dbm: float | None) -> str:
    """Compact plausible spacecraft receive power without a false exact cap."""
    if dbm is None:
        return "NONE"
    if not math.isfinite(dbm):
        return "POWER?"
    if not _source.RECEIVE_POWER_MIN_DBM <= dbm < 0.0:
        return "RANGE?"
    return f"{dbm:.0f}DBM"


def transmit_power_label(kw: float | None) -> str:
    """Compact source power with explicit upper-bound and invalid states."""
    if kw is None or not math.isfinite(kw) or kw < 0:
        return "POWER?"
    if kw == 0:
        return "NONE"
    if kw >= 1:
        return (
            f">{_formatting.POWER_LABEL_MAX:.0f}KW"
            if kw > _formatting.POWER_LABEL_MAX
            else f"{kw:.0f}KW"
        )
    watts = kw * 1000
    return (
        f">{_formatting.POWER_LABEL_MAX:.0f}W"
        if watts > _formatting.POWER_LABEL_MAX
        else f"{watts:.0f}W"
    )


def signal_count_label(count: int, suffix: str) -> str:
    """A compact exact record count, or an explicit two-digit overflow."""
    bounded = max(0, int(count))
    return f">99{suffix}" if bounded > 99 else f"{bounded}{suffix}"


def _instrument_metrics(
    link: _source.Link, contact_state: str = "live"
) -> tuple[tuple[str, str], ...]:
    streams = _telemetry.link_streams(link)
    up_streams = _telemetry.link_upstreams(link)
    bands = []
    for down_stream in streams:
        key = _source.band_key(down_stream.band)
        label = key if key in _render_palette.BAND_PULSE else "?"
        if label not in bands:
            bands.append(label)
    if streams:
        band = (
            "/".join(bands)
            if 0 < len(bands) <= 3
            else f"{len(bands)}BAND"
            if bands
            else "?"
        )
        # Record multiplicity does not prove independent, summable throughput.
        # Use the record itself only when exactly one is present; never trust a
        # legacy aggregate scalar beside several published receiver records.
        rate = _render_labels.rate_label(streams[0].bps if len(streams) == 1 else None)
    else:
        up_bands = []
        for up_stream in up_streams:
            key = _source.band_key(up_stream.band)
            label = key if key in _render_palette.BAND_PULSE else "?"
            if label not in up_bands:
                up_bands.append(label)
        band = "/".join(up_bands) or ("UP" if link.up_active else "?")
        rate = "TX" if link.up_active else "QUIET"
    pages = list(_metric_pages(band, rate))
    rx = receive_power_label(link.down_dbm)
    known_up_powers = tuple(
        stream.kw
        for stream in up_streams
        if stream.kw is not None and math.isfinite(stream.kw) and stream.kw > 0
    )
    # An active uplink with no positive published power is unknown, not a
    # claim that a zero-power transmitter is operating. Inactive links retain
    # the useful NONE state.
    tx_power = (
        max(known_up_powers)
        if known_up_powers
        else None
        if up_streams or link.up_active
        else 0.0
    )
    tx = transmit_power_label(tx_power)
    pages.extend(_metric_pages("RX", rx))
    pages.extend(_metric_pages("TX", tx))
    count = (
        signal_count_label(max(link.streams, len(streams)), "RX")
        if streams
        else "NO RX"
    )
    direction = (
        "DUPLEX"
        if link.up_active and streams
        else "RX"
        if streams
        else "TX"
        if link.up_active
        else "QUIET"
    )
    pages.extend(_metric_pages(direction, count))
    badge = _formatting.activity_badge(link.activity)
    badge_label = {"ENGINEER": "ENG", "UPGRADE": "UPGRD"}.get(badge, badge or "UNKNOWN")
    pages.extend(_metric_pages("ACT", badge_label))
    if contact_state == "off_air":
        pages.insert(0, ("OFF AIR", ""))
    elif contact_state == "handoff":
        pages.insert(0, ("HANDOFF", ""))
    for mode, active in (
        ("ARRAY", link.arrayed),
        ("MSPA", link.mspa),
        ("DDOR", link.ddor),
    ):
        if active:
            pages.extend(_metric_pages("MODE", mode))
    wind = _telemetry.wind_bucket(link.wind_kmh)
    if wind is not None:
        value = wind if isinstance(wind, str) else f"{wind}KMH"
        pages.extend(_metric_pages("WIND", value))
    if len(up_streams) > 1:
        pages.extend(_metric_pages("UP", signal_count_label(len(up_streams), "SIG")))
    return tuple(pages)


def instrument_signature(
    link: _source.Link,
    trail: list[tuple[int, int]],
    freshness: str,
    names: dict[str, str] | None = None,
    contact_state: str = "live",
) -> tuple:
    """Only fields that can change a physical LED belong in this signature."""
    streams = tuple(
        (
            _source.band_key(s.band),
            _telemetry.rate_bucket(s.bps),
            _telemetry.receive_power_bucket(s.dbm),
        )
        for s in _telemetry.link_streams(link)
    )
    upstreams = tuple(
        _telemetry.transmit_power_bucket(s.kw or 0.0)
        for s in _telemetry.link_upstreams(link)[:3]
    )
    return (
        "instrument",
        link.key,
        _render_labels.craft_label(link.craft, names or {}),
        tuple(trail),
        _render_scope.link_pointing_pixel(link),
        streams,
        upstreams,
        link.arrayed,
        link.mspa,
        link.ddor,
        _instrument_metrics(link, contact_state),
        freshness,
        contact_state,
    )


def instrument_header_layout(
    link: _source.Link,
    names: dict[str, str] | None = None,
) -> tuple[str, int | None, tuple[int, int], str, int]:
    """Stable header geometry plus a marquee-safe native frame count."""
    dish = _render_network_data._dish_suffix(link.dish)
    sep_candidate = _render_palette.INSTRUMENT_X0 + _render_text.text_width(dish) + 2
    sep: int | None = sep_candidate
    box = (sep_candidate + 2, _render_palette.INSTRUMENT_CONTENT_X1)
    craft = _render_labels.craft_label(link.craft, names or {})
    label = craft
    if box[1] - box[0] + 1 < max(_render_text.glyph_width("?"), 1):
        # A future identifier may consume the old craft box.  One complete
        # combined marquee is truthful; taking the last three digits silently
        # renames the antenna.
        dish = ""
        sep = None
        box = (_render_palette.INSTRUMENT_X0, _render_palette.INSTRUMENT_CONTENT_X1)
        label = f"{_render_network_data._dish_suffix(link.dish)}/{craft}"
    frame_count = _render_text.scroll_frame_count(label, box[1] - box[0] + 1)
    return dish, sep, box, label, frame_count


def instrument_frame_count(
    link: _source.Link,
    names: dict[str, str] | None = None,
    contact_state: str = "live",
) -> int:
    """Whole header/RF cycles needed to give every metric a readable dwell."""
    header_frame_count = instrument_header_layout(link, names)[-1]
    metric_frames = (
        len(_instrument_metrics(link, contact_state))
        * _render_palette.INSTRUMENT_METRIC_MIN_FRAMES
    )
    return max(
        header_frame_count,
        math.ceil(metric_frames / header_frame_count) * header_frame_count,
    )


def render_instrument_frames(
    link: _source.Link,
    trail: list[tuple[int, int]] | None = None,
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    contact_state: str = "live",
) -> tuple[list[Image.Image], int, int]:
    """A literal live antenna/RF instrument for one selected DSN link."""
    streams = list(_telemetry.link_streams(link))
    upstreams = list(_telemetry.link_upstreams(link))
    if len(streams) > 3:
        # Three physical rows fit. Keep two records literal, then use one
        # explicit overflow reference lane. Record multiplicity does not prove
        # independent, summable throughput or power, so the overflow lane must
        # not manufacture either scalar.
        rest = streams[2:]
        rest_bands = {
            _source.band_key(s.band) for s in rest if _source.band_key(s.band)
        }
        streams = streams[:2] + [
            _source.DownStream(
                next(iter(rest_bands)) if len(rest_bands) == 1 else "",
                None,
                None,
                f"{len(rest)} RECORDS",
            )
        ]
    ys = {1: [9], 2: [8, 10], 3: [8, 9, 10]}.get(len(streams), [])
    metrics = _instrument_metrics(link, contact_state)
    frozen = freshness != "fresh" or contact_state == "off_air"
    dish, sep, box, label, header_frame_count = instrument_header_layout(link, names)
    # Metrics may need several RF loops, but they must not slow the unrelated
    # identity marquee.  The header owns its own fixed-speed native cycle;
    # extend the asset by whole header cycles so every repeated seam remains
    # exact while the metric pages get a readable dwell.
    frame_count = instrument_frame_count(link, names, contact_state)
    frames: list[Image.Image] = []
    for index in range(frame_count):
        text_phase = (index % header_frame_count) / header_frame_count
        phase = (
            0.0
            if frozen
            else (index % _limits.INSTRUMENT_FRAMES) / _limits.INSTRUMENT_FRAMES
        )
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)

        for point in _render_scope.SCOPE_POINTS:
            px[point] = _render_palette.SCOPE_RING
        px[_render_palette.SCOPE_CX, _render_palette.SCOPE_CY] = (
            _render_palette.SCOPE_RING
        )
        for point in (trail or [])[:-1]:
            px[point] = _render_palette.SCOPE_TRAIL
        current_point = _render_scope.link_pointing_pixel(link)
        if current_point is not None:
            px[current_point] = _render_palette.SCOPE_HEAD
        else:
            _render_text._text(px, 5, 5, "?", _render_palette.DISH_NO)
        px[_render_palette.SCOPE_CX, 0] = _render_palette.FRESH  # north fiducial

        _render_text._text(
            px, _render_palette.INSTRUMENT_X0, 0, dish, _render_palette.DISH_NO
        )
        for yy in range(5):
            if sep is not None and sep < _render_palette.FRESH_X:
                px[sep, yy] = (50, 70, 90)
        off = _render_text.scroll_offset(label, text_phase, box[1] - box[0] + 1)
        _render_text._text(px, box[0] - off, 0, label, _render_palette.NAME, clip=box)
        if off:
            _render_text._text(
                px,
                box[0]
                - off
                + _render_text.text_width(label)
                + _render_text.SCROLL_GAP_PX,
                0,
                label,
                _render_palette.NAME,
                clip=box,
            )

        # Every published active up-signal record gets a spatially separate
        # row (up to the three physical rows available). Several records are
        # not called several transmitters: the source does not promise that.
        up_rows = {1: [6], 2: [5, 7], 3: [5, 6, 7]}.get(min(3, len(upstreams)), [])
        if upstreams:
            for up_index, (y, up_stream) in enumerate(zip(up_rows, upstreams[:3])):
                for x in range(
                    _render_palette.INSTRUMENT_X0, _render_palette.INSTRUMENT_X1 + 1
                ):
                    px[x, y] = _render_palette.UP_TETHER
                _carrier_marks(
                    px,
                    y,
                    phase + up_index / len(up_rows),
                    3,
                    _render_palette.UPLINK,
                    True,
                    span=2,
                )
                strength = _telemetry.transmit_power_bucket(up_stream.kw or 0.0)
                # Power grows sideways within the RF band, never upward into
                # the dish number. The old strong flare changed DSS43's digit.
                for dx in range(strength):
                    xx = _render_palette.INSTRUMENT_X0 - 1 + dx
                    if (
                        _render_palette.INSTRUMENT_X0 - 1
                        <= xx
                        <= _render_palette.INSTRUMENT_X1
                    ):
                        px[xx, y] = _render_palette.UPLINK
        else:
            # A silent outbound direction remains a bare reference tether.
            for x in range(
                _render_palette.INSTRUMENT_X0, _render_palette.INSTRUMENT_X1 + 1
            ):
                px[x, 6] = _render_palette.UP_TETHER

        if streams:
            for y, down_stream in zip(ys, streams):
                colour = _render_palette.BAND_PULSE.get(
                    _source.band_key(down_stream.band), _render_palette.UNKNOWN_PULSE
                )
                for x in range(
                    _render_palette.INSTRUMENT_X0, _render_palette.INSTRUMENT_X1 + 1
                ):
                    px[x, y] = _render_palette.INSTRUMENT_TETHER
                _carrier_marks(
                    px,
                    y,
                    phase,
                    (0, 1, 2, 3, 5, 7)[_telemetry.rate_bucket(down_stream.bps)],
                    colour,
                    False,
                )
                strength = _telemetry.receive_power_bucket(down_stream.dbm)
                if strength:
                    flare = ((175, 105, 35), (225, 145, 55), colour)[strength - 1]
                    for dx in range(strength):
                        px[_render_palette.INSTRUMENT_X0 + dx, y] = flare
        else:
            for x in range(
                _render_palette.INSTRUMENT_X0, _render_palette.INSTRUMENT_X1 + 1
            ):
                px[x, 9] = _render_palette.INSTRUMENT_TETHER

        if link.arrayed:
            _draw_mode_glyph(
                px, 61, ("101", "111", "010", "010", "010"), _render_dish.ANTENNA
            )
        if link.mspa:
            _draw_mode_glyph(
                px, 64, ("010", "010", "111", "101", "101"), _render_palette.NAME
            )
        if link.ddor:
            _draw_mode_glyph(
                px, 67, ("010", "101", "010", "101", "010"), _render_dish.DDOR_MARK
            )

        metric_page = min(len(metrics) - 1, index * len(metrics) // frame_count)
        left, right = metrics[metric_page]
        # X-band carrier amber runs immediately above this row. DIST amber was
        # only 8 luminance points and 12% channel separation away, so a moving
        # carrier could merge into the label's top stroke on the physical
        # panel. Keep the first band/rate page green; later semantic labels use
        # the neutral identity ink, which remains distinct from every carrier.
        _render_text._text(
            px,
            _render_palette.INSTRUMENT_X0,
            11,
            left,
            _render_palette.RATE if metric_page == 0 else _render_palette.NAME,
        )
        _render_text._text(
            px,
            _render_palette.INSTRUMENT_CONTENT_X1 - _render_text.text_width(right),
            11,
            right,
            _render_palette.RATE if metric_page == 0 else _render_palette.UPLINK,
        )

        # Fresh is intentionally absent here: one two-LED native rectangle
        # forms a short lease renewed only when NASA's timestamp advances. That
        # proves host/source life without redrawing and restarting this loop.
        if freshness == "delayed":
            if (index // 5) % 2 == 0:
                for y in (0, _limits.H // 2, _limits.H - 1):
                    px[_render_palette.FRESH_X, y] = _render_palette.DELAYED
        elif freshness in {"stale", "offline"}:
            for y in (0, _limits.H // 2, _limits.H - 1):
                px[_render_palette.FRESH_X, y] = _render_palette.STALE
        frames.append(img)
    return frames, _limits.INSTRUMENT_FPS, 1

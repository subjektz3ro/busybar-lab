"""DSN render / network dishes."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from PIL import Image

from apps.dsn_app import limits as _limits
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import scope as _render_scope
from apps.dsn_app.render import text as _render_text

# --- Dish/link network ----------------------------------------------------
# Network answers ground topology first: site link total, physical dish, and
# an attached count when one dish carries several tracked-target associations.
# Pointing geometry belongs to the deliberate selected-dish Focus below.
DISH_NETWORK_ROSTER_X0, DISH_NETWORK_ROSTER_X1 = 11, 69

DISH_NETWORK_TOKEN_GAP = 1

DISH_NETWORK_SELECTED = (238, 242, 250)

DISH_NETWORK_COUNT = (40, 255, 180)

DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R = 7, 10, 5

DISH_FOCUS_CRAFT_BOX = (16, 37)

DISH_FOCUS_TX_X = 39

DISH_FOCUS_RX_X = 54


@dataclass(frozen=True)
class DishRosterRow:
    """Named render plan so timing cannot be confused with geometry."""

    site_total: str
    y: int
    groups: tuple[tuple[str, tuple[_source.Link, ...]], ...]
    width: int
    row_frames: int
    roster_x0: int


@dataclass(frozen=True)
class DishFocusPage:
    """One bounded semantic page; ``omitted`` is an explicit summary."""

    contacts: tuple[_source.Link, ...]
    duration: int
    omitted: int = 0


def group_links_by_dish(
    links: list[_source.Link],
    site: str | None = None,
) -> list[tuple[str, tuple[_source.Link, ...]]]:
    """Canonical physical-dish groups, optionally within one complex."""
    selected = [
        link
        for link in links
        if site is None or _render_network_data._site_name(link.complex_name) == site
    ]
    by_dish: dict[str, list[_source.Link]] = {}
    for link in sorted(selected, key=lambda item: (item.dish, item.craft, item.key)):
        by_dish.setdefault(link.dish, []).append(link)
    return [
        (dish, tuple(by_dish[dish]))
        for dish in sorted(
            by_dish, key=lambda item: (_render_network_data._dish_suffix(item), item)
        )
    ]


def _dish_group_token_width(dish: str, links: tuple[_source.Link, ...]) -> int:
    width = _render_text.text_width(_render_network_data._dish_suffix(dish))
    if len(links) > 1:
        width += _render_text.GLYPH_GAP + _render_text.text_width(f"({len(links)})")
    return width


def _dish_roster_width(groups: list[tuple[str, tuple[_source.Link, ...]]]) -> int:
    if not groups:
        return 0
    return sum(
        _dish_group_token_width(dish, links) for dish, links in groups
    ) + DISH_NETWORK_TOKEN_GAP * (len(groups) - 1)


def _pixel_scroll_frame_count(
    width: int,
    box_px: int,
    minimum: int = _limits.INSTRUMENT_FRAMES,
) -> int:
    """Whole RF clocks for an already-measured coloured token strip."""
    if width <= box_px:
        return minimum
    needed = math.ceil(
        (width + _render_text.SCROLL_GAP_PX)
        / _limits.SCROLL_SPEED_PX_S
        * _limits.INSTRUMENT_FPS
    )
    cycles = max(1, math.ceil(needed / _limits.INSTRUMENT_FRAMES))
    return min(
        _limits.MAX_ANIMATION_FRAMES, max(minimum, cycles * _limits.INSTRUMENT_FRAMES)
    )


def _draw_dish_roster_strip(
    px,
    x: int,
    y: int,
    groups: (
        list[tuple[str, tuple[_source.Link, ...]]]
        | tuple[tuple[str, tuple[_source.Link, ...]], ...]
    ),
    selected_key: str | None,
    clip: tuple[int, int],
) -> int:
    """One complete coloured site roster; returns its measured width."""
    cursor = x
    for group_index, (dish, group_links) in enumerate(groups):
        suffix = _render_network_data._dish_suffix(dish)
        selected = bool(
            selected_key and any(link.key == selected_key for link in group_links)
        )
        _render_text._text(
            px,
            cursor,
            y,
            suffix,
            DISH_NETWORK_SELECTED if selected else _render_palette.DISH_NO,
            clip=clip,
        )
        cursor += _render_text.text_width(suffix)
        if len(group_links) > 1:
            cursor += _render_text.GLYPH_GAP
            count = f"({len(group_links)})"
            _render_text._text(px, cursor, y, count, DISH_NETWORK_COUNT, clip=clip)
            cursor += _render_text.text_width(count)
        if group_index + 1 < len(groups):
            cursor += DISH_NETWORK_TOKEN_GAP
    return cursor - x


def dish_network_frame_count(links: list[_source.Link]) -> int:
    """One bounded asset clock; each row keeps its own seam-safe phase."""
    row_clocks = []
    for site, initial, _ in _render_network_data.NETWORK_SITES:
        site_total = f"{initial}{len(_render_network_data._network_links(links, site))}"
        roster_x0 = max(
            DISH_NETWORK_ROSTER_X0,
            _render_text.text_width(site_total) + _render_text.GLYPH_GAP,
        )
        box_px = DISH_NETWORK_ROSTER_X1 - roster_x0 + 1
        groups = group_links_by_dish(links, site)
        row_clocks.append(_pixel_scroll_frame_count(_dish_roster_width(groups), box_px))
    return max(row_clocks, default=_limits.INSTRUMENT_FRAMES)


def render_dish_network_frames(
    links: list[_source.Link],
    freshness: str = "fresh",
    selected_key: str | None = None,
) -> tuple[list[Image.Image], int, int]:
    """Literal site → physical dish → live-link-count Network board.

    Ordinary rows are static. A future dense source state that cannot fit its
    remaining roster box scrolls the complete coloured token strip through a
    whole native cycle; no dish or attached multiplicity is clipped or dropped.
    """
    models = []
    for site, initial, y in _render_network_data.NETWORK_SITES:
        site_links = _render_network_data._network_links(links, site)
        site_total = f"{initial}{len(site_links)}"
        roster_x0 = max(
            DISH_NETWORK_ROSTER_X0,
            _render_text.text_width(site_total) + _render_text.GLYPH_GAP,
        )
        box_px = DISH_NETWORK_ROSTER_X1 - roster_x0 + 1
        groups = group_links_by_dish(links, site)
        width = _dish_roster_width(groups)
        row_frames = _pixel_scroll_frame_count(width, box_px)
        models.append(
            DishRosterRow(site_total, y, tuple(groups), width, row_frames, roster_x0)
        )
    # LCM made three ordinary independent rows multiply into multi-minute,
    # multi-megabyte assets.  One bounded clock is enough: each strip below
    # completes an integer number of its own cycles at its own readable rate.
    frame_count = max(
        (model.row_frames for model in models), default=_limits.INSTRUMENT_FRAMES
    )

    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        for model in models:
            box_px = DISH_NETWORK_ROSTER_X1 - model.roster_x0 + 1
            _render_text._text(
                px,
                0,
                model.y,
                model.site_total,
                _render_dish.ANTENNA,
                clip=(0, model.roster_x0 - _render_text.GLYPH_GAP - 1),
            )
            if not model.groups:
                _render_text._text(
                    px, 13, model.y, "NO LINKS", (85, 105, 130), clip=(13, 50)
                )
                continue
            offset = _render_text.independent_pixel_scroll_offset(
                model.width, box_px, index, frame_count
            )
            start = model.roster_x0 - offset
            _draw_dish_roster_strip(
                px,
                start,
                model.y,
                model.groups,
                selected_key,
                (model.roster_x0, DISH_NETWORK_ROSTER_X1),
            )
            if offset:
                _draw_dish_roster_strip(
                    px,
                    start + model.width + _render_text.SCROLL_GAP_PX,
                    model.y,
                    model.groups,
                    selected_key,
                    (model.roster_x0, DISH_NETWORK_ROSTER_X1),
                )
        _render_scope._draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames, _limits.INSTRUMENT_FPS, 1


def dish_network_signature(
    links: list[_source.Link],
    freshness: str = "fresh",
    selected_key: str | None = None,
) -> tuple:
    """Pixel-exact immutable-cache key for the dish roster."""
    frames, fps, hold = render_dish_network_frames(links, freshness, selected_key)
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return ("dish-network", fps, hold, len(frames), digest.hexdigest())


def dish_network_loop_s(links: list[_source.Link]) -> float:
    return dish_network_frame_count(links) / _limits.INSTRUMENT_FPS


def _dish_focus_group(
    links: list[_source.Link],
    selected_key: str | None,
) -> tuple[_source.Link | None, list[_source.Link]]:
    selected = next((link for link in links if link.key == selected_key), None)
    if selected is None:
        return None, []
    same_dish = [
        link
        for link in links
        if (
            _render_network_data._site_name(link.complex_name)
            == _render_network_data._site_name(selected.complex_name)
            and link.dish == selected.dish
        )
    ]
    ordered = [selected]
    ordered.extend(
        sorted(
            (link for link in same_dish if link.key != selected.key),
            key=lambda item: (item.craft, item.key),
        )
    )
    return selected, ordered


def _dish_focus_pages(
    group: list[_source.Link],
    names: dict[str, str] | None = None,
    header: str = "",
    header_box_px: int = 70,
) -> list[DishFocusPage]:
    pages: list[DishFocusPage] = []
    # Keep the exact wheel/START target visible on every page.  The second
    # row walks the other links sharing this physical dish, so semantic zoom
    # never turns into an unexplained page where the action target vanished.
    page_groups = (
        [[group[0]]] if len(group) <= 1 else [[group[0], other] for other in group[1:]]
    )
    for page_links in page_groups or [[]]:
        duration = _render_text.scroll_frame_count(
            header, header_box_px, _limits.INSTRUMENT_FRAMES
        )
        for link in page_links:
            duration = max(
                duration,
                _render_text.scroll_frame_count(
                    _render_labels.craft_label(link.craft, names or {}),
                    DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
                    _limits.INSTRUMENT_FRAMES,
                ),
            )
        pages.append(DishFocusPage(tuple(page_links), duration))
    if not pages:
        return [DishFocusPage((), _limits.INSTRUMENT_FRAMES)]
    if sum(page.duration for page in pages) <= _limits.MAX_ANIMATION_FRAMES:
        return pages

    # Friendly names and co-dish multiplicity are independent source axes. A
    # valid but hostile combination must not multiply into thousands of eager
    # PIL frames. Keep as many complete identity pages as fit while reserving
    # one exact ``+N TARGETS`` page for everything deliberately omitted.
    selected = (group[0],) if group else ()

    def overflow_page(count: int) -> DishFocusPage:
        duration = _render_text.scroll_frame_count(
            header, header_box_px, _limits.INSTRUMENT_FRAMES
        )
        if selected:
            duration = max(
                duration,
                _render_text.scroll_frame_count(
                    _render_labels.craft_label(selected[0].craft, names or {}),
                    DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
                    _limits.INSTRUMENT_FRAMES,
                ),
            )
        duration = max(
            duration,
            _render_text.scroll_frame_count(
                f"+{count} TARGETS",
                DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1,
                _limits.INSTRUMENT_FRAMES,
            ),
        )
        return DishFocusPage(selected, duration, count)

    bounded: list[DishFocusPage] = []
    used = 0
    for index, focus_page in enumerate(pages):
        omitted = len(pages) - index
        summary = overflow_page(omitted)
        if used + focus_page.duration + summary.duration > _limits.MAX_ANIMATION_FRAMES:
            bounded.append(summary)
            break
        bounded.append(focus_page)
        used += focus_page.duration
    return bounded


def _dish_focus_aim(
    group: list[_source.Link],
) -> tuple[int, int] | None:
    """One source aim per physical dish, or no invented geometry."""
    if not group or any(not link.pointing_valid for link in group):
        return None
    source_aims = {
        (round(link.azimuth % 360.0, 3), round(link.elevation, 3)) for link in group
    }
    if len(source_aims) != 1:
        return None
    selected = group[0]
    return _render_scope._project_angles(
        selected.azimuth, selected.elevation, DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R
    )


def _dish_focus_rx_colour(link: _source.Link) -> tuple[int, int, int] | None:
    streams = _telemetry.link_streams(link)
    if not streams:
        return None
    keys = [_source.band_key(stream.band) for stream in streams]
    if keys and len(set(keys)) == 1 and keys[0] in _render_palette.BAND_PULSE:
        return _render_palette.BAND_PULSE[keys[0]]
    return _render_palette.UNKNOWN_PULSE


def _draw_focus_contact(
    px,
    link: _source.Link,
    y: int,
    selected_key: str,
    local: int,
    duration: int,
    names: dict[str, str] | None = None,
) -> None:
    label = _render_labels.craft_label(link.craft, names or {})
    box_px = DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1
    offset = _render_text.independent_scroll_offset(label, box_px, local, duration)
    colour = DISH_NETWORK_SELECTED if link.key == selected_key else _render_palette.NAME
    _render_text._text(
        px,
        DISH_FOCUS_CRAFT_BOX[0] - offset,
        y,
        label,
        colour,
        clip=DISH_FOCUS_CRAFT_BOX,
    )
    if offset:
        _render_text._text(
            px,
            DISH_FOCUS_CRAFT_BOX[0]
            - offset
            + _render_text.text_width(label)
            + _render_text.SCROLL_GAP_PX,
            y,
            label,
            colour,
            clip=DISH_FOCUS_CRAFT_BOX,
        )
    if link.key == selected_key:
        # White vs ordinary name ink is below the panel's measured contrast
        # floor. A five-pixel mint owner bar makes the exact START target
        # unmistakable without taking a character from the complete name.
        for yy in range(y, y + 5):
            px[14, yy] = DISH_NETWORK_COUNT
    if _telemetry.link_upstreams(link):
        _render_text._text(
            px,
            DISH_FOCUS_TX_X,
            y,
            "TX",
            _render_palette.UPLINK,
            clip=(DISH_FOCUS_TX_X, DISH_FOCUS_TX_X + 8),
        )
    rx_colour = _dish_focus_rx_colour(link)
    if rx_colour is not None:
        _render_text._text(
            px,
            DISH_FOCUS_RX_X,
            y,
            "RX",
            rx_colour,
            clip=(DISH_FOCUS_RX_X, DISH_FOCUS_RX_X + 8),
        )


def _draw_focus_overflow(
    px,
    count: int,
    y: int,
    local: int,
    duration: int,
) -> None:
    """Name the omitted associations instead of silently dropping them."""
    label = f"+{count} TARGETS"
    box_px = DISH_FOCUS_CRAFT_BOX[1] - DISH_FOCUS_CRAFT_BOX[0] + 1
    offset = _render_text.independent_scroll_offset(label, box_px, local, duration)
    _render_text._text(
        px,
        DISH_FOCUS_CRAFT_BOX[0] - offset,
        y,
        label,
        DISH_NETWORK_COUNT,
        clip=DISH_FOCUS_CRAFT_BOX,
    )
    if offset:
        _render_text._text(
            px,
            DISH_FOCUS_CRAFT_BOX[0]
            - offset
            + _render_text.text_width(label)
            + _render_text.SCROLL_GAP_PX,
            y,
            label,
            DISH_NETWORK_COUNT,
            clip=DISH_FOCUS_CRAFT_BOX,
        )


def _draw_dish_focus_header(
    px,
    header: str,
    colour: tuple[int, int, int],
    local: int,
    duration: int,
) -> None:
    box = (0, 69)
    box_px = box[1] - box[0] + 1
    offset = _render_text.independent_scroll_offset(header, box_px, local, duration)
    _render_text._text(px, box[0] - offset, 0, header, colour, clip=box)
    if offset:
        _render_text._text(
            px,
            box[0]
            - offset
            + _render_text.text_width(header)
            + _render_text.SCROLL_GAP_PX,
            0,
            header,
            colour,
            clip=box,
        )


def _dish_focus_header(
    selected: _source.Link,
    group: list[_source.Link],
) -> tuple[str, tuple[int, int, int], tuple[int, int] | None]:
    """Complete plain-language aim header shared by render and timing."""
    initial = next(
        (
            initial
            for site, initial, _ in _render_network_data.NETWORK_SITES
            if site == _render_network_data._site_name(selected.complex_name)
        ),
        "?",
    )
    identity = f"{initial}{_render_network_data._dish_suffix(selected.dish)}"
    aim = _dish_focus_aim(group)
    if aim is None:
        return f"{identity} NO AIM", DISH_NETWORK_COUNT, None
    azimuth = int(round(group[0].azimuth)) % 360
    elevation = max(0, min(90, int(round(group[0].elevation))))
    return (f"{identity} AZ{azimuth:03d} EL{elevation:02d}", DISH_NETWORK_SELECTED, aim)


def render_dish_focus_frames(
    links: list[_source.Link],
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    selected_key: str | None = None,
) -> tuple[list[Image.Image], int, int]:
    """One selected physical dish aim and every link that shares it."""
    selected, group = _dish_focus_group(list(links), selected_key)
    if selected is None:
        return render_dish_network_frames(list(links), freshness, selected_key)
    header, header_colour, aim = _dish_focus_header(selected, group)
    pages = _dish_focus_pages(group, names, header, 70)
    schedule = [(page, local) for page in pages for local in range(page.duration)]

    frames = []
    for index, (page, local) in enumerate(schedule):
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        _draw_dish_focus_header(px, header, header_colour, local, page.duration)
        if aim is None:
            _render_text._text(px, 5, 8, "?", DISH_NETWORK_COUNT, clip=(5, 8))
        else:
            _render_scope._draw_scope(px, DISH_FOCUS_CX, DISH_FOCUS_CY, DISH_FOCUS_R)
            px[aim] = DISH_NETWORK_SELECTED
        for row, link in zip((6, 11), page.contacts):
            _draw_focus_contact(
                px, link, row, selected.key, local, page.duration, names
            )
        if page.omitted:
            _draw_focus_overflow(px, page.omitted, 11, local, page.duration)
        _render_scope._draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames, _limits.INSTRUMENT_FPS, 1


def dish_focus_signature(
    links: list[_source.Link],
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    selected_key: str | None = None,
) -> tuple:
    frames, fps, hold = render_dish_focus_frames(links, freshness, names, selected_key)
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return ("dish-focus", fps, hold, len(frames), digest.hexdigest())


def dish_focus_loop_s(
    links: list[_source.Link],
    names: dict[str, str] | None,
    selected_key: str | None,
) -> float:
    selected, group = _dish_focus_group(list(links), selected_key)
    if selected is None:
        return dish_network_loop_s(list(links))
    header = _dish_focus_header(selected, group)[0]
    return (
        sum(page.duration for page in _dish_focus_pages(group, names, header, 70))
        / _limits.INSTRUMENT_FPS
    )

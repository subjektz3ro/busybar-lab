"""DSN render / network rows."""

from __future__ import annotations

from PIL import Image

from apps.dsn_app import limits as _limits
from apps.dsn_app import source as _source
from apps.dsn_app import telemetry as _telemetry
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import text as _render_text

NETWORK_X0, NETWORK_X1 = 40, 62

NETWORK_CONTACT_FRAMES = _limits.INSTRUMENT_FRAMES  # at least eight seconds/contact


def network_signature(
    links: list[_source.Link],
    freshness: str,
    names: dict[str, str] | None = None,
    page: int | None = None,
) -> tuple:
    """Only topology that the three-site board actually turns into pixels."""
    contacts = []
    for site, _, _ in _render_network_data.NETWORK_SITES:
        rows = []
        site_links = _render_network_data._network_links(links, site)
        visible = (
            site_links
            if page is None or not site_links
            else [site_links[page % len(site_links)]]
        )
        for link in visible:
            bands = tuple(
                _source.band_key(stream.band)
                if _source.band_key(stream.band) in _render_palette.BAND_PULSE
                else ""
                for stream in _telemetry.link_streams(link)[:3]
            )
            up_count = min(3, len(_telemetry.link_upstreams(link)))
            rows.append(
                (
                    link.dish,
                    _render_labels.contact_label(link, names or {}),
                    up_count,
                    bands,
                    _render_network_data._compact_count(
                        len(_telemetry.link_streams(link))
                    ),
                    link.arrayed,
                    link.mspa,
                    link.ddor,
                )
            )
        contacts.append((site, tuple(rows)))
    return ("network" if page is None else "network-page", tuple(contacts), freshness)


def _network_mark(
    px,
    y: int,
    phase: float,
    colour: tuple[int, int, int],
    outward: bool,
    offset: float = 0.0,
) -> None:
    width = NETWORK_X1 - NETWORK_X0
    fraction = (phase + offset + 0.2) % 1.0
    step = max(0, min(width, int(round(fraction * (width + 2))) - 1))
    x = NETWORK_X0 + step if outward else NETWORK_X1 - step
    px[x, y] = colour


def network_page_durations(
    links: list[_source.Link],
    names: dict[str, str] | None = None,
) -> tuple[dict[str, list[_source.Link]], list[int]]:
    """Contact grouping and whole-RF-cycle dwell for every global page."""
    grouped = {
        site: _render_network_data._network_links(links, site)
        for site, _, _ in _render_network_data.NETWORK_SITES
    }
    pages = max(
        1, *(len(grouped[site]) for site, _, _ in _render_network_data.NETWORK_SITES)
    )
    durations: list[int] = []
    for page in range(pages):
        duration = NETWORK_CONTACT_FRAMES
        for site, _, _ in _render_network_data.NETWORK_SITES:
            contacts = grouped[site]
            if contacts:
                contact = contacts[page % len(contacts)]
                label = _render_labels.contact_label(contact, names or {})
                duration = max(
                    duration,
                    _render_text.scroll_frame_count(label, 21, NETWORK_CONTACT_FRAMES),
                    _render_text.scroll_frame_count(
                        _render_network_data._dish_suffix(contact.dish),
                        10,
                        NETWORK_CONTACT_FRAMES,
                    ),
                )
        durations.append(duration)
    return grouped, durations


def network_page_count(links: list[_source.Link]) -> int:
    return max(
        1,
        *(
            len(_render_network_data._network_links(links, site))
            for site, _, _ in _render_network_data.NETWORK_SITES
        ),
    )


def network_page_duration_s(
    links: list[_source.Link], page: int, names: dict[str, str] | None = None
) -> float:
    _, durations = network_page_durations(links, names)
    return durations[page % len(durations)] / _limits.INSTRUMENT_FPS


def render_network_frames(
    links: list[_source.Link],
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    page: int | None = None,
) -> tuple[list[Image.Image], int, int]:
    """All three DSN complexes, each paging through its actual live contacts.

    A runtime page is one bounded native asset; the host advances only at its
    loop boundary and resident page assets are reused. With ``page=None`` the
    full deterministic sequence remains available to previews and dry-run.
    Every friendly name completes a full marquee before the next contact.
    """
    grouped, page_durations = network_page_durations(links, names)
    page_indices = (
        range(len(page_durations)) if page is None else (page % len(page_durations),)
    )
    schedule = [
        (page_index, local, page_durations[page_index])
        for page_index in page_indices
        for local in range(page_durations[page_index])
    ]
    frozen = freshness != "fresh"
    frames: list[Image.Image] = []
    for index, (page, local, duration) in enumerate(schedule):
        phase = (
            0.0
            if frozen
            else (local % _limits.INSTRUMENT_FRAMES) / _limits.INSTRUMENT_FRAMES
        )
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        for site, initial, y0 in _render_network_data.NETWORK_SITES:
            contacts = grouped[site]
            _render_text._text(px, 0, y0, initial, _render_dish.ANTENNA)
            if not contacts:
                # NO LINK is 33px in the proportional font: x=7..39.  The
                # old craft-label box ended at 37 and amputated the K's outer
                # stroke on every empty complex. There is no RF lane in this
                # branch, so use the full space up to its x=40 boundary.
                _render_text._text(
                    px, 7, y0, "NO LINK", (85, 105, 130), clip=(7, NETWORK_X0 - 1)
                )
                continue
            link = contacts[page % len(contacts)]
            dish = _render_network_data._dish_suffix(link.dish)
            dish_box = (6, 15)
            dish_off = _render_text.independent_scroll_offset(
                dish, dish_box[1] - dish_box[0] + 1, local, duration
            )
            _render_text._text(
                px,
                dish_box[0] - dish_off,
                y0,
                dish,
                _render_palette.DISH_NO,
                clip=dish_box,
            )
            if dish_off:
                _render_text._text(
                    px,
                    dish_box[0]
                    - dish_off
                    + _render_text.text_width(dish)
                    + _render_text.SCROLL_GAP_PX,
                    y0,
                    dish,
                    _render_palette.DISH_NO,
                    clip=dish_box,
                )
            craft = _render_labels.contact_label(link, names or {})
            craft_box = (17, 37)
            craft_off = _render_text.independent_scroll_offset(
                craft, craft_box[1] - craft_box[0] + 1, local, duration
            )
            _render_text._text(
                px,
                craft_box[0] - craft_off,
                y0,
                craft,
                _render_palette.NAME,
                clip=craft_box,
            )
            if craft_off:
                _render_text._text(
                    px,
                    craft_box[0]
                    - craft_off
                    + _render_text.text_width(craft)
                    + _render_text.SCROLL_GAP_PX,
                    y0,
                    craft,
                    _render_palette.NAME,
                    clip=craft_box,
                )

            # Two rows make direction readable in a still glance as well as
            # through motion. A silent direction has no lit tether.
            upstreams = _telemetry.link_upstreams(link)
            if upstreams:
                for x in range(NETWORK_X0, NETWORK_X1 + 1):
                    px[x, y0 + 1] = _render_palette.UP_TETHER
                for up_index, _ in enumerate(upstreams[:3]):
                    _network_mark(
                        px,
                        y0 + 1,
                        phase,
                        _render_palette.UPLINK,
                        True,
                        up_index / max(1, min(3, len(upstreams))),
                    )
            streams = _telemetry.link_streams(link)
            if streams:
                for x in range(NETWORK_X0, NETWORK_X1 + 1):
                    px[x, y0 + 3] = _render_palette.INSTRUMENT_TETHER
                # Several real streams remain several coloured carriers even
                # though this overview has only one receive row per complex.
                for stream_index, stream in enumerate(streams[:3]):
                    colour = _render_palette.BAND_PULSE.get(
                        _source.band_key(stream.band), _render_palette.UNKNOWN_PULSE
                    )
                    _network_mark(
                        px,
                        y0 + 3,
                        phase,
                        colour,
                        False,
                        stream_index / max(1, min(3, len(streams))),
                    )
            count = _render_network_data._compact_count(len(streams))
            _render_text._text(
                px,
                66,
                y0,
                count,
                _render_palette.RATE if streams else (85, 105, 130),
                clip=(66, 69),
            )

            # Dish-scoped modes change the node geometry instead of hiding in
            # prose: array converges, MSPA forks, DDOR adds a reference point.
            if link.arrayed:
                px[NETWORK_X0 - 2, y0] = _render_dish.ANTENNA
                px[NETWORK_X0 - 2, y0 + 2] = _render_dish.ANTENNA
            if link.mspa:
                px[NETWORK_X1 + 1, y0 + 2] = _render_palette.NAME
                px[NETWORK_X1 + 1, y0 + 4] = _render_palette.NAME
            if link.ddor:
                px[NETWORK_X1 + 2, y0 + 2] = _render_dish.DDOR_MARK

        if freshness == "delayed" and (index // 5) % 2 == 0:
            for y in (0, _limits.H // 2, _limits.H - 1):
                px[_render_palette.FRESH_X, y] = _render_palette.DELAYED
        elif freshness in {"stale", "offline"}:
            for y in (0, _limits.H // 2, _limits.H - 1):
                px[_render_palette.FRESH_X, y] = _render_palette.STALE
        frames.append(img)
    return frames, _limits.INSTRUMENT_FPS, 1


def render_network_page_frames(
    links: list[_source.Link],
    page: int,
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
) -> tuple[list[Image.Image], int, int]:
    """The bounded runtime form of the three-site Network board."""
    return render_network_frames(links, freshness, names, page=page)

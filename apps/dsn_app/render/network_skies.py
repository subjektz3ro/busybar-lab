"""DSN render / network skies."""

from __future__ import annotations

import hashlib

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

THREE_SKIES_MAIN_CENTER = (35, 8)

THREE_SKIES_MAIN_R = 7

THREE_SKIES_CONTEXT_CENTERS = ((66, 4), (66, 12))

THREE_SKIES_CONTEXT_R = 3

THREE_SKIES_SELECTED = (238, 242, 250)

# Mint is a non-RF ledger ink. It clears the measured 30%/77-channel panel
# step from every semantic node colour, including the selected white head.
THREE_SKIES_LEDGER = (40, 255, 180)

THREE_SKIES_TRAIL = (30, 160, 30)


def _project_link(
    link: _source.Link, cx: int, cy: int, radius: int
) -> tuple[int, int] | None:
    """Literal local az/el projection at an arbitrary scope radius."""
    if not link.pointing_valid:
        return None
    return _render_scope._project_angles(link.azimuth, link.elevation, cx, cy, radius)


def _group_scope_links(
    links: list[_source.Link], site: str, cx: int, cy: int, radius: int
) -> tuple[list[dict], int]:
    """Group dish-scoped aims, then honest pixel collisions.

    MSPA can publish several spacecraft contacts for one dish.  They share one
    physical aim and therefore one spatial node.  Independently aimed dishes
    can quantize to the same LED too; those contacts remain one cell with a
    nonspatial tally rather than being jittered into invented positions.
    """
    site_links = _render_network_data._network_links(links, site)
    by_dish: dict[str, list[_source.Link]] = {}
    for link in site_links:
        by_dish.setdefault(link.dish, []).append(link)

    cells: dict[tuple[int, int], dict] = {}
    missing = 0
    for dish in sorted(by_dish):
        dish_links = sorted(by_dish[dish], key=lambda item: item.craft)
        # A dish has one source aim.  Missing or internally inconsistent
        # coordinates cannot be resolved by first quantizing distinct angles
        # onto the same LED.  Validate source geometry before projection.
        if any(not link.pointing_valid for link in dish_links):
            missing += len(dish_links)
            continue
        source_aims = {
            (round(link.azimuth % 360.0, 3), round(link.elevation, 3))
            for link in dish_links
        }
        if len(source_aims) != 1:
            missing += len(dish_links)
            continue
        point = _project_link(dish_links[0], cx, cy, radius)
        assert point is not None
        cell = cells.setdefault(point, {"point": point, "links": [], "dishes": []})
        cell["links"].extend(dish_links)
        cell["dishes"].append(dish)

    groups = []
    for point in sorted(cells):
        cell = cells[point]
        cell["links"] = sorted(cell["links"], key=lambda item: (item.dish, item.craft))
        cell["dishes"] = tuple(sorted(cell["dishes"]))
        groups.append(cell)
    return groups, missing


def _scope_group_colour(group: dict, selected_key: str | None) -> tuple[int, int, int]:
    links = group["links"]
    if selected_key and any(link.key == selected_key for link in links):
        return THREE_SKIES_SELECTED
    downstreams = [stream for link in links for stream in _telemetry.link_streams(link)]
    if downstreams:
        keys = [_source.band_key(stream.band) for stream in downstreams]
        if (
            keys
            and all(key in _render_palette.BAND_PULSE for key in keys)
            and len(set(keys)) == 1
        ):
            return _render_palette.BAND_PULSE[keys[0]]
        return _render_palette.UNKNOWN_PULSE
    if any(_telemetry.link_upstreams(link) for link in links):
        return _render_palette.UPLINK
    return _render_palette.UNKNOWN_PULSE


def _distinct_trail(points: list[tuple[int, int]] | None) -> list[tuple[int, int]]:
    """At most five distinct observed cells, including the current head."""
    newest_first: list[tuple[int, int]] = []
    seen: set[tuple[int, int]] = set()
    for point in reversed(points or []):
        if point not in seen:
            newest_first.append(point)
            seen.add(point)
    return list(reversed(newest_first[:5]))


def _map_trail_point(
    point: tuple[int, int], cx: int, cy: int, radius: int
) -> tuple[int, int]:
    """Map the retained R6 pixel observation into this scope's resolution."""
    return (
        int(
            round(
                cx
                + (point[0] - _render_palette.SCOPE_CX)
                * radius
                / _render_palette.SCOPE_R
            )
        ),
        int(
            round(
                cy
                + (point[1] - _render_palette.SCOPE_CY)
                * radius
                / _render_palette.SCOPE_R
            )
        ),
    )


def _site_count_label(initial: str, count: int) -> str:
    # Ten or more cannot fit as an exact numeral in this rollback cell.  G>
    # is visibly an overflow state; G9 remains exactly nine, never a silent
    # numeric cap pretending that twelve associations are nine.
    return f"{initial}{count}" if count <= 9 else f"{initial}>"


def _missing_count(count: int) -> str:
    return f"?{_render_network_data._compact_count(count)}"


def _selected_link(
    links: list[_source.Link], selected_key: str | None
) -> _source.Link | None:
    return next((link for link in links if link.key == selected_key), None)


def _scope_collision(groups: list[dict]) -> int:
    return max((len(group["links"]) for group in groups), default=1)


def _draw_collision_ledger(px, base: int, y: int, collision: int) -> None:
    _render_text._text(
        px,
        base,
        y,
        _render_network_data._compact_count(collision),
        THREE_SKIES_LEDGER,
        clip=(base, base + 4),
    )
    # A bracket beside the count says "one spatial cell contains this many
    # contacts". It is a ledger mark, never a second plotted position.
    for x, yy in (
        (base + 6, y),
        (base + 6, y + 2),
        (base + 6, y + 4),
        (base + 7, y),
        (base + 7, y + 4),
    ):
        px[x, yy] = THREE_SKIES_LEDGER


def _draw_focus_collision_ledger(px, collision: int) -> None:
    """A complete tally between the identity rail and the R7 main sky."""
    # The left text box ends at x20; exhaustive rounded R7 projection starts
    # at x28. A full four-column digit, one OFF moat and a compact bracket fit
    # in that honest nonspatial gap. Do not move this onto a reachable cell.
    _render_text._text(
        px,
        21,
        11,
        _render_network_data._compact_count(collision),
        THREE_SKIES_LEDGER,
        clip=(21, 24),
    )
    for x, y in ((26, 11), (26, 13), (26, 15), (27, 11), (27, 15)):
        px[x, y] = THREE_SKIES_LEDGER


def _draw_site_ledger(
    px,
    base: int,
    site_links: list[_source.Link],
    groups: list[dict],
    missing: int,
    selected_key: str | None,
    index: int,
    frame_count: int,
) -> None:
    """Nonspatial facts that must not masquerade as another sky position."""
    collision = _scope_collision(groups)
    if missing and collision > 1:
        # Two complete five-pixel rows fit before the ring. Never make one
        # truthful warning erase the other merely because both happened.
        _render_text._text(
            px,
            base,
            5,
            _missing_count(missing),
            THREE_SKIES_LEDGER,
            clip=(base, base + 8),
        )
        _draw_collision_ledger(px, base, 10, collision)
        return
    if missing:
        _render_text._text(
            px,
            base,
            10,
            _missing_count(missing),
            THREE_SKIES_LEDGER,
            clip=(base, base + 8),
        )
        return
    if collision > 1:
        _draw_collision_ledger(px, base, 10, collision)
        return
    selected = _selected_link(site_links, selected_key)
    if selected is not None:
        dish = _render_network_data._dish_suffix(selected.dish)
        box = (base, base + 8)
        offset = _render_text.independent_scroll_offset(
            dish, box[1] - box[0] + 1, index, frame_count
        )
        _render_text._text(
            px, box[0] - offset, 10, dish, _render_palette.DISH_NO, clip=box
        )
        if offset:
            _render_text._text(
                px,
                box[0]
                - offset
                + _render_text.text_width(dish)
                + _render_text.SCROLL_GAP_PX,
                10,
                dish,
                _render_palette.DISH_NO,
                clip=box,
            )


def _render_three_skies_ambient(
    links: list[_source.Link],
    freshness: str,
    selected_key: str | None,
    trails: dict[str, list[tuple[int, int]]],
) -> list[Image.Image]:
    models = []
    for (site, initial, _), centre in zip(
        _render_network_data.NETWORK_SITES, _render_palette.THREE_SKIES_SCOPE_CENTERS
    ):
        site_links = _render_network_data._network_links(links, site)
        groups, missing = _group_scope_links(
            links, site, centre[0], centre[1], _render_palette.THREE_SKIES_SCOPE_R
        )
        models.append((site, initial, centre, site_links, groups, missing))

    selected = _selected_link(links, selected_key)
    frame_count = (
        _render_text.scroll_frame_count(
            _render_network_data._dish_suffix(selected.dish),
            9,
            _limits.INSTRUMENT_FRAMES,
        )
        if selected is not None
        else _limits.INSTRUMENT_FRAMES
    )
    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        for base, model in zip((0, 24, 48), models):
            _, initial, centre, site_links, groups, missing = model
            _render_text._text(
                px,
                base,
                0,
                _site_count_label(initial, len(site_links)),
                _render_dish.ANTENNA,
                clip=(base, base + 9),
            )
            _render_scope._draw_scope(
                px, centre[0], centre[1], _render_palette.THREE_SKIES_SCOPE_R
            )
            selected = _selected_link(site_links, selected_key)
            if selected is not None:
                trail = _distinct_trail(trails.get(selected.key))
                for point in trail[:-1]:
                    px[
                        _map_trail_point(
                            point,
                            centre[0],
                            centre[1],
                            _render_palette.THREE_SKIES_SCOPE_R,
                        )
                    ] = THREE_SKIES_TRAIL
            for group in groups:
                px[group["point"]] = _scope_group_colour(group, selected_key)
            _draw_site_ledger(
                px, base, site_links, groups, missing, selected_key, index, frame_count
            )
        _render_scope._draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames


def _focus_index_label(links: list[_source.Link], selected_key: str) -> str:
    if len(links) > 99:
        return "MANY"
    index = next((i for i, link in enumerate(links, 1) if link.key == selected_key), 1)
    return f"{index}/{len(links)}"


def _draw_focus_marquee(
    px, text: str, y: int, colour: tuple[int, int, int], index: int, frame_count: int
) -> None:
    """Draw one complete 21-pixel Focus token, scrolling only if required."""
    box = (0, 20)
    width = _render_text.text_width(text)
    off = _render_text.independent_scroll_offset(text, 21, index, frame_count)
    _render_text._text(px, box[0] - off, y, text, colour, clip=box)
    if width > 21:
        _render_text._text(
            px,
            box[0] - off + width + _render_text.SCROLL_GAP_PX,
            y,
            text,
            colour,
            clip=box,
        )


def _render_three_skies_focus(
    links: list[_source.Link],
    freshness: str,
    names: dict[str, str],
    selected: _source.Link,
    trails: dict[str, list[tuple[int, int]]],
) -> list[Image.Image]:
    label = _render_labels.craft_label(selected.craft, names)
    selected_site = _render_network_data._site_name(selected.complex_name)
    main_groups, main_missing = _group_scope_links(
        links,
        selected_site,
        THREE_SKIES_MAIN_CENTER[0],
        THREE_SKIES_MAIN_CENTER[1],
        THREE_SKIES_MAIN_R,
    )
    context_sites = [
        item for item in _render_network_data.NETWORK_SITES if item[0] != selected_site
    ]
    dish = _render_network_data._dish_suffix(selected.dish)
    site_initial = next(
        (
            initial
            for site, initial, _ in _render_network_data.NETWORK_SITES
            if site == selected_site
        ),
        "?",
    )
    identity = f"{site_initial}{dish}"
    index_label = _focus_index_label(links, selected.key)
    if main_missing:
        index_label += f" {_missing_count(main_missing)}"
    frame_count = max(
        _render_text.scroll_frame_count(identity, 21, _limits.INSTRUMENT_FRAMES),
        _render_text.scroll_frame_count(label, 21, _limits.INSTRUMENT_FRAMES),
        _render_text.scroll_frame_count(index_label, 21, _limits.INSTRUMENT_FRAMES),
    )
    trail = _distinct_trail(trails.get(selected.key))
    main_collision = _scope_collision(main_groups)

    frames = []
    for index in range(frame_count):
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        _draw_focus_marquee(
            px, identity, 0, _render_palette.DISH_NO, index, frame_count
        )
        _draw_focus_marquee(px, label, 6, _render_palette.NAME, index, frame_count)
        _draw_focus_marquee(
            px, index_label, 11, _render_dish.ANTENNA, index, frame_count
        )

        _render_scope._draw_scope(px, *THREE_SKIES_MAIN_CENTER, THREE_SKIES_MAIN_R)
        for point in trail[:-1]:
            px[
                _map_trail_point(point, *THREE_SKIES_MAIN_CENTER, THREE_SKIES_MAIN_R)
            ] = THREE_SKIES_TRAIL
        for group in main_groups:
            px[group["point"]] = _scope_group_colour(group, selected.key)
        if main_collision > 1:
            _draw_focus_collision_ledger(px, main_collision)

        for (site, initial, _), centre, y0 in zip(
            context_sites, THREE_SKIES_CONTEXT_CENTERS, (0, 8)
        ):
            site_links = _render_network_data._network_links(links, site)
            _render_text._text(
                px,
                48,
                y0,
                _site_count_label(initial, len(site_links)),
                _render_dish.ANTENNA,
                clip=(48, 57),
            )
            _render_scope._draw_scope(px, centre[0], centre[1], THREE_SKIES_CONTEXT_R)
            groups, missing = _group_scope_links(
                links, site, centre[0], centre[1], THREE_SKIES_CONTEXT_R
            )
            collision = _scope_collision(groups)
            context_status = ""
            if missing and collision > 1:
                # Focus lasts a whole 40-frame clock, so both nonspatial facts
                # receive one complete four-second block instead of one hiding
                # the other in this compact context rail.
                context_status = (
                    "?"
                    if (index // 20) % 2 == 0
                    else _render_network_data._compact_count(collision)
                )
            elif missing:
                context_status = "?"
            elif collision > 1:
                context_status = _render_network_data._compact_count(collision)
            if context_status:
                _render_text._text(
                    px, 58, y0, context_status, THREE_SKIES_LEDGER, clip=(58, 61)
                )
            for group in groups:
                px[group["point"]] = _scope_group_colour(group, None)

        _render_scope._draw_freshness_frame(px, freshness, index)
        frames.append(img)
    return frames


def render_three_skies_frames(
    links: list[_source.Link],
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    selected_key: str | None = None,
    trails: dict[str, list[tuple[int, int]]] | None = None,
    focus: bool = False,
) -> tuple[list[Image.Image], int, int]:
    """Three literal local skies, or the user-invoked selected Focus Lens."""
    links = list(links)
    selected = _selected_link(links, selected_key)
    if focus and selected is not None:
        frames = _render_three_skies_focus(
            links, freshness, names or {}, selected, trails or {}
        )
    else:
        frames = _render_three_skies_ambient(
            links, freshness, selected_key, trails or {}
        )
    return frames, _limits.INSTRUMENT_FPS, 1


def three_skies_signature(
    links: list[_source.Link],
    freshness: str = "fresh",
    names: dict[str, str] | None = None,
    selected_key: str | None = None,
    trails: dict[str, list[tuple[int, int]]] | None = None,
    focus: bool = False,
) -> tuple:
    """A pixel-exact cache signature without raw telemetry jitter."""
    frames, fps, hold = render_three_skies_frames(
        links, freshness, names, selected_key, trails, focus
    )
    digest = hashlib.blake2s(digest_size=16)
    for frame in frames:
        digest.update(frame.tobytes())
    return (
        "three-skies-focus"
        if focus and _selected_link(links, selected_key)
        else "three-skies",
        fps,
        hold,
        len(frames),
        digest.hexdigest(),
    )


def three_skies_loop_s(
    links: list[_source.Link],
    names: dict[str, str] | None,
    selected_key: str | None,
    focus: bool,
) -> float:
    """Native duration of the exact ambient or Focus asset."""
    selected = _selected_link(links, selected_key)
    if focus and selected is not None:
        selected_site = _render_network_data._site_name(selected.complex_name)
        main_groups, main_missing = _group_scope_links(
            links,
            selected_site,
            THREE_SKIES_MAIN_CENTER[0],
            THREE_SKIES_MAIN_CENTER[1],
            THREE_SKIES_MAIN_R,
        )
        del main_groups
        initial = next(
            (
                initial
                for site, initial, _ in _render_network_data.NETWORK_SITES
                if site == selected_site
            ),
            "?",
        )
        identity = f"{initial}{_render_network_data._dish_suffix(selected.dish)}"
        index_label = _focus_index_label(links, selected.key)
        if main_missing:
            index_label += f" {_missing_count(main_missing)}"
        frames = max(
            _render_text.scroll_frame_count(identity, 21, _limits.INSTRUMENT_FRAMES),
            _render_text.scroll_frame_count(
                _render_labels.craft_label(selected.craft, names or {}),
                21,
                _limits.INSTRUMENT_FRAMES,
            ),
            _render_text.scroll_frame_count(index_label, 21, _limits.INSTRUMENT_FRAMES),
        )
        return frames / _limits.INSTRUMENT_FPS
    if selected is not None:
        frames = _render_text.scroll_frame_count(
            _render_network_data._dish_suffix(selected.dish),
            9,
            _limits.INSTRUMENT_FRAMES,
        )
        return frames / _limits.INSTRUMENT_FPS
    return _limits.INSTRUMENT_LOOP_S

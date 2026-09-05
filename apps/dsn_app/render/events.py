"""DSN render / events."""

from __future__ import annotations

import math

from PIL import Image

from apps.dsn_app import limits as _limits
from apps.dsn_app.render import dish as _render_dish
from apps.dsn_app.render import labels as _render_labels
from apps.dsn_app.render import network_data as _render_network_data
from apps.dsn_app.render import palette as _render_palette
from apps.dsn_app.render import scope as _render_scope
from apps.dsn_app.render import text as _render_text

# --- source-triggered transition grammar ---------------------------------
EVENT_DISH = (210, 175, 80)

EVENT_CRAFT = (225, 235, 250)

EVENT_DISH_DIM = (55, 45, 10)

EVENT_CRAFT_DIM = (60, 70, 85)

EVENT_TEXT = (159, 232, 255)

# Neutral topology, deliberately >= the measured 30% physical-panel step from
# every semantic RF hue. Cyan was mathematically different from UPLINK but
# looked identical on the actual LEDs and therefore still implied direction.
EVENT_LINK = (160, 160, 160)

EVENT_DIM = (48, 72, 96)


def event_effect(event: dict) -> str | None:
    """Map a real feed transition to one of a finite set of native assets."""
    kind = event.get("event")
    if kind in {"acquire", "loss", "handoff"}:
        return kind
    if kind == "streams":
        before_streams = int(event.get("before_streams") or 0)
        after_streams = int(event.get("streams") or 0)
        if after_streams > before_streams:
            return "split"
        if after_streams < before_streams:
            return "merge"
        return None
    if kind == "modes":
        before_flags = tuple(event.get("before_flags") or (False, False, False))
        after_flags = tuple(event.get("flags") or (False, False, False))
        if bool(before_flags[0]) != bool(after_flags[0]):
            return "array" if after_flags[0] else "unarray"
        # MSPA and DDOR have different physical geometry. Until they have
        # dedicated truthful art, their exact native text card is the effect.
        return None
    if kind == "direction":
        # A finite asset cannot infer which lane appeared/disappeared from the
        # after-state alone. The exact TX/RX/DUPLEX/QUIET label stays truthful.
        return None
    return None


def _event_line(
    px,
    start: tuple[int, int],
    end: tuple[int, int],
    colour: tuple[int, int, int],
    fraction: float = 1.0,
    reverse: bool = False,
) -> None:
    """A clipped integer line whose lit extent makes assembly legible."""
    fraction = max(0.0, min(1.0, fraction))
    x0, y0 = start
    x1, y1 = end
    steps = max(abs(x1 - x0), abs(y1 - y0), 1)
    count = int(round(steps * fraction))
    indices = range(steps - count, steps + 1) if reverse else range(count + 1)
    for step in indices:
        x = int(round(x0 + (x1 - x0) * step / steps))
        y = int(round(y0 + (y1 - y0) * step / steps))
        if 0 <= x < _limits.W and 0 <= y < _limits.H:
            px[x, y] = colour


def _event_node(px, x: int, y: int, colour: tuple[int, int, int]) -> None:
    for dx, dy in ((0, 0), (-1, 0), (1, 0), (0, -1), (0, 1)):
        xx, yy = x + dx, y + dy
        if 0 <= xx < _limits.W and 0 <= yy < _limits.H:
            px[xx, yy] = colour


def _handoff_echo_endpoint(
    event: dict,
    prefix: str,
) -> tuple[int, tuple[int, int]] | None:
    """One observed endpoint in its own local-sky coordinate frame."""
    valid_key = f"{prefix}pointing_valid"
    site_key = f"{prefix}complex"
    azimuth_key = f"{prefix}azimuth"
    elevation_key = f"{prefix}elevation"
    if not event.get(valid_key):
        return None
    site = _render_network_data._site_name(str(event.get(site_key) or ""))
    site_index = next(
        (
            index
            for index, (name, _initial, _y) in enumerate(
                _render_network_data.NETWORK_SITES
            )
            if name == site
        ),
        None,
    )
    if site_index is None:
        return None
    try:
        azimuth = float(event[azimuth_key])
        elevation = float(event[elevation_key])
    except (KeyError, TypeError, ValueError):
        return None
    if not (math.isfinite(azimuth) and math.isfinite(elevation)):
        return None
    centre = _render_palette.THREE_SKIES_SCOPE_CENTERS[site_index]
    return (
        site_index,
        _render_scope._project_angles(
            azimuth,
            elevation,
            centre[0],
            centre[1],
            _render_palette.THREE_SKIES_SCOPE_R,
        ),
    )


def handoff_echo_signature(event: dict) -> tuple | None:
    """Content identity for one truthful, fully composed handoff card."""
    if event.get("event") != "handoff":
        return None
    old = _handoff_echo_endpoint(event, "from_")
    new = _handoff_echo_endpoint(event, "")
    label = _render_labels.event_label(event)
    if (
        old is None
        or new is None
        or not label.isascii()
        or any(ch.upper() not in _render_text.FONT for ch in label)
        or _render_text.text_width(label) > _limits.W
    ):
        return None
    return ("handoff-echo", old, new, label)


def render_handoff_echo_frames(
    event: dict,
) -> tuple[list[Image.Image], int, int]:
    """Pulse exact old/new cells around a complete, non-overlapping label.

    Geometry and text occupy separate time phases in one immutable asset, so
    firmware element composition can never paint the label over an observed
    endpoint. The pulses alter only the exact measured cells; no halo or
    connector invents another point in either local coordinate frame.
    """
    signature = handoff_echo_signature(event)
    if signature is None:
        raise ValueError("handoff echo requires valid aims and a fitting label")
    _kind, old, new, label = signature
    _old_site, old_point = old
    _new_site, new_point = new
    frames: list[Image.Image] = []
    for index in range(_limits.EVENT_FRAMES):
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)
        if index < 6 or index >= 14:
            for base, ((_site, initial, _y), centre) in zip(
                (0, 24, 48),
                zip(
                    _render_network_data.NETWORK_SITES,
                    _render_palette.THREE_SKIES_SCOPE_CENTERS,
                ),
            ):
                _render_text._text(
                    px, base, 0, initial, _render_dish.ANTENNA, clip=(base, base + 4)
                )
                _render_scope._draw_scope(
                    px, centre[0], centre[1], _render_palette.THREE_SKIES_SCOPE_R
                )
            if index < 6:
                px[old_point] = EVENT_DISH if index % 2 == 0 else EVENT_DISH_DIM
            else:
                px[new_point] = EVENT_CRAFT if index % 2 == 0 else EVENT_CRAFT_DIM
        else:
            x = (_limits.W - _render_text.text_width(label)) // 2
            _render_text._text(px, x, 5, label, EVENT_TEXT, clip=(0, _limits.W - 1))
        frames.append(img)
    return frames, _limits.EVENT_FPS, 1


def render_event_frames(effect: str) -> tuple[list[Image.Image], int, int]:
    """Prebaked transition art. Only a source event chooses an asset.

    Motion inside the four-second asset is representational; the event that
    starts it is observed. Keeping the vocabulary finite lets every asset be
    resident before an event happens, so a live transition never waits on an
    encode or upload.
    """
    if effect not in _limits.EVENT_EFFECTS:
        raise ValueError(f"unknown DSN event effect: {effect}")
    frames: list[Image.Image] = []
    for index in range(_limits.EVENT_FRAMES):
        progress = index / max(1, _limits.EVENT_FRAMES - 1)
        img = Image.new("RGB", (_limits.W, _limits.H), _render_palette.OFF)
        px = _render_text.image_pixels(img)

        # The label occupies rows 5..10 as a native element. The visual
        # grammar lives above and below it so neither one destroys the other.
        if effect in {"acquire", "loss"}:
            amount = progress if effect == "acquire" else 1.0 - progress
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), (66, 2), EVENT_DIM)
            # Assemble from both endpoints. A one-way sweep would imply RF
            # direction that the generic acquire/loss event does not supply.
            _event_line(px, (5, 2), (36, 2), EVENT_LINK, amount)
            _event_line(px, (36, 2), (66, 2), EVENT_LINK, amount, reverse=True)
        elif effect == "handoff":
            old_amount = max(0.0, 1.0 - progress * 2.0)
            new_amount = max(0.0, min(1.0, progress * 2.0 - 1.0))
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 3, 13, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), (66, 2), EVENT_DIM)
            _event_line(px, (5, 13), (66, 13), EVENT_DIM)
            _event_line(px, (5, 2), (66, 2), EVENT_LINK, old_amount)
            _event_line(px, (5, 13), (66, 13), EVENT_LINK, new_amount)
            # Both old and new dishes meet the same spacecraft endpoint.
            _event_line(px, (66, 13), (66, 2), EVENT_LINK, new_amount)
            # The actual site/dish transition is the source event; the
            # crossing dot merely helps the eye connect old to new. Route it
            # at the Earth end, outside the native label's safety mask, so it
            # cannot disappear for half the animation on the physical LEDs.
            _event_line(px, (5, 2), (5, 13), EVENT_DIM)
            # Keep the moving cross one pixel clear of both five-pixel dish
            # glyphs.  At y=2/13 its left arm recoloured 20% of the endpoint.
            hand_y = int(round(3 + 9 * progress))
            _event_node(px, 5, hand_y, EVENT_LINK)
        elif effect in {"split", "merge"}:
            amount = progress if effect == "split" else 1.0 - progress
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_node(px, 3, 2, EVENT_DISH)
            _event_line(px, (5, 2), (66, 2), EVENT_LINK, 1.0)
            # A stream-count change is several records on this same
            # dish/craft contact—not a second antenna. Assemble a parallel
            # lane symmetrically, joined to the one endpoint at each side.
            _event_line(px, (5, 2), (5, 13), EVENT_LINK, amount)
            _event_line(px, (66, 2), (66, 13), EVENT_LINK, amount)
            _event_line(px, (5, 13), (36, 13), EVENT_LINK, amount)
            _event_line(px, (36, 13), (66, 13), EVENT_LINK, amount, reverse=True)
        else:  # array / unarray
            amount = progress if effect == "array" else 1.0 - progress
            join = (35, 2)
            _event_node(px, 3, 2, EVENT_DISH)
            _event_node(px, 3, 7, EVENT_DISH)
            _event_node(px, 3, 13, EVENT_DISH)
            _event_node(px, 68, 2, EVENT_CRAFT)
            _event_line(px, (5, 2), join, EVENT_LINK, amount)
            _event_line(px, (5, 7), join, EVENT_LINK, amount)
            _event_line(px, (5, 13), join, EVENT_LINK, amount)
            _event_line(px, join, (66, 2), EVENT_LINK, amount)

        # Firmware text is centred at (36, 8). Keep its entire likely glyph
        # box black even when a branch or handoff crosses the middle; the
        # source event label is information and the effect is supporting art.
        for yy in range(5, 11):
            for xx in range(7, 65):
                px[xx, yy] = _render_palette.OFF

        frames.append(img)
    return frames, _limits.EVENT_FPS, 1

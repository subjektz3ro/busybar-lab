"""DSN render / labels."""

from __future__ import annotations

import math

from apps.dsn_app import formatting as _formatting
from apps.dsn_app import source as _source
from apps.dsn_app.render import text as _render_text


def craft_label(code: str, names: dict[str, str]) -> str:
    """NASA's friendlyName when known, otherwise code; never sliced.

    Anything wider than the box scrolls (see scroll_offset), so 'Advanced
    Composition Explorer' is shown rather than clipped to a stub. Unsupported
    custom-font characters keep their position as an explicit question mark.
    """
    full = names.get(code.lower(), "")
    full = "".join(ch for ch in full if ch.isprintable()).strip()
    # The config feed is live vocabulary, not a frozen allowlist. Silently
    # skipping a future apostrophe/ampersand leaves a mysterious blank in the
    # promised complete marquee. Preserve its position with an explicit
    # drawable unknown glyph instead.
    label = (full or code).upper()
    return "".join(ch if ch in _render_text.FONT else "?" for ch in label)


def contact_label(link: _source.Link, names: dict[str, str]) -> str:
    """Full craft identity followed by a meaningful live activity badge."""
    craft = craft_label(link.craft, names)
    badge = _formatting.activity_badge(link.activity)
    # TTC/telemetry is the ordinary meaning of a visible RF contact. Keep the
    # network row focused on identity; surface exceptional engineering state.
    return f"{craft} / {badge}" if badge in {"DEMO", "UPGRADE", "ENGINEER"} else craft


def rate_label(bps: float | None) -> str:
    """Always ...BPS: '160B' next to '19.8H' reads as bytes, and the units
    are the whole point of putting the number there. Values beyond the compact
    display's range get an inequality, never a false capped exact value."""
    if bps is None or not math.isfinite(bps) or bps < 0:
        return "RATE?"
    if bps >= 1e9:
        gbps = bps / 1e9
        return (
            f">{_formatting.RATE_LABEL_MAX_GBPS:.0f}GBPS"
            if gbps > _formatting.RATE_LABEL_MAX_GBPS
            else f"{gbps:.0f}GBPS"
        )
    if bps >= 1e6:
        return f"{bps / 1e6:.0f}MBPS"
    if bps >= 1e3:
        return f"{bps / 1e3:.0f}KBPS"
    if bps > 0:
        return f"{int(bps)}BPS"
    return "0BPS"


def fit_row(left: str, right: str, room: int, gap: int = 3) -> tuple[str, str]:
    """Fit two complete labels, or tell the caller the right one must move.

    Anchoring to opposite ends is not enough on its own: '18:43' beside
    '246KBPS' wants 61 px of a 56 px row and the two overlapped outright on
    the panel. Returning an empty right label is an explicit layout signal;
    the Distance renderer responds by marqueeing the complete semantic token.
    This helper must never invent a prefix such as UPLI or strip a unit.
    """
    left = fit_label(left, room)
    if _render_text.text_width(left) + gap + _render_text.text_width(right) <= room:
        return left, right
    return left, ""


def fit_label(text: str, room: int) -> str:
    """Clip a hostile/source token to actual pixel width, never into a gutter."""
    text = "".join(ch for ch in text.upper() if ch in _render_text.FONT)
    while text and _render_text.text_width(text) > room:
        text = text[:-1]
    return text


def light_label(light_s: float | None) -> str:
    """Distance as time — the thing actually worth reading."""
    if not light_s:
        return "?"
    if light_s < 1:
        return "SUBSEC"
    if light_s < 90:
        return f"{light_s:.0f}SEC"
    if light_s < 5400:
        return f"{light_s / 60:.0f}M"
    return f"{light_s / 3600:.1f}H"


def _draw_text_segments(
    px,
    x: int,
    y: int,
    segments: tuple[tuple[str, tuple[int, int, int]], ...],
    clip: tuple[int, int],
) -> None:
    """Draw adjacent semantic text segments without losing glyph columns."""
    cursor = x
    for text, colour in segments:
        for ch in text:
            _render_text._text(px, cursor, y, ch, colour, clip=clip)
            cursor += _render_text.glyph_width(ch) + _render_text.GLYPH_GAP


def event_label(event: dict) -> str:
    craft = str(event.get("craft") or "DSN").upper()
    dish = str(event.get("dish") or "").upper().replace("DSS", "")
    kind = event.get("event")
    if kind == "acquire":
        return _render_text.device_text(f"+{craft} {dish}".strip())
    if kind == "loss":
        return _render_text.device_text(f"-{craft} {dish}".strip())
    if kind == "handoff":
        old = str(event.get("from_dish") or "").upper().replace("DSS", "")
        return _render_text.device_text(f"{craft} {old}>{dish}")
    if kind == "streams":
        count = int(event.get("streams") or 0)
        raw_bands = tuple(event.get("bands") or ())
        bands = "/".join(_source.band_key(band) or "?" for band in raw_bands)
        detailed = f"{craft} {count} {bands}".strip()
        return _render_text.device_text(
            detailed if bands else f"{craft} {count} SIGNALS"
        )
    if kind == "direction":
        suffix = (
            "DUPLEX"
            if event.get("up") and event.get("down")
            else "TX"
            if event.get("up")
            else "RX"
            if event.get("down")
            else "QUIET"
        )
        return _render_text.device_text(f"{craft} {suffix}")
    if kind == "modes":
        active = [
            name
            for name, on in zip(("ARRAY", "MSPA", "DDOR"), event.get("flags") or ())
            if on
        ]
        suffix = active[0] if len(active) == 1 else "MODES" if active else "NORMAL"
        return _render_text.device_text(f"{craft} {suffix}")
    if kind == "stale":
        return "FEED STALE"
    if kind == "recovered":
        return "FEED LIVE"
    return _render_text.device_text(craft)

"""DSN render / network data."""

from __future__ import annotations

from apps.dsn_app import source as _source
from apps.dsn_app.render import text as _render_text

# --- all-network live contact board ---------------------------------------
NETWORK_SITES = (("Goldstone", "G", 0), ("Madrid", "M", 5), ("Canberra", "C", 10))


def _site_name(name: str) -> str:
    folded = (name or "").strip().lower()
    if folded in _source.SITE_NAMES:
        return _source.SITE_NAMES[folded]
    if "gold" in folded:
        return "Goldstone"
    if "madrid" in folded or "roble" in folded:
        return "Madrid"
    if "canberra" in folded or "tidbin" in folded:
        return "Canberra"
    return name


def _network_links(links: list[_source.Link], site: str) -> list[_source.Link]:
    return sorted(
        (link for link in links if _site_name(link.complex_name) == site),
        key=lambda link: (link.dish, link.craft),
    )


def _dish_suffix(dish: str) -> str:
    """Complete source dish identity after the conventional DSS prefix."""
    raw = (dish or "").upper().removeprefix("DSS")
    return "".join(ch if ch in _render_text.FONT else "?" for ch in (raw or "?"))


def _compact_count(count: int) -> str:
    """One-glyph exact-or-overflow tally for the rollback ledgers."""
    return str(count) if count <= 9 else "+"

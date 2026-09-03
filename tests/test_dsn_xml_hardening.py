"""The remote DSN XML feed is parsed with explicit safety limits.

Every other bound in dsn's ingestion layer does what its name says —
_bounded_source_text, FEED_DISH_ELEMENTS_MAX, FEED_SIGNAL_RECORDS_PER_DISH_MAX.
FEED_XML_MAX_BYTES did not: stdlib ElementTree expands internal entities, so a
370-byte document expanded to 5,200,000 characters through
`ET.fromstring().findtext()`. One more nesting level is ~52M, and the 1 MB
byte budget permits far deeper.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from defusedxml.common import EntitiesForbidden

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "apps"))

import dsn  # noqa: E402

BILLION_LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE dsn [
<!ENTITY a "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA">
<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">
<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">
<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">
<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">
<!ENTITY f "&e;&e;&e;&e;&e;&e;&e;&e;&e;&e;">
]>
<dsn><timestamp>&f;</timestamp></dsn>"""

EXTERNAL_ENTITY = b"""<?xml version="1.0"?>
<!DOCTYPE dsn [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<dsn><timestamp>&xxe;</timestamp></dsn>"""


def test_the_bomb_is_well_under_the_byte_budget():
    """Which is the whole point: the size check could never have caught it."""
    assert len(BILLION_LAUGHS) < dsn.FEED_XML_MAX_BYTES / 1000


def test_entity_expansion_is_refused_by_parse_feed():
    with pytest.raises(EntitiesForbidden):
        dsn.parse_feed(BILLION_LAUGHS)


def test_entity_expansion_is_refused_by_parse_config():
    with pytest.raises(EntitiesForbidden):
        dsn.parse_config(BILLION_LAUGHS)


def test_the_timestamp_reader_does_not_expand_either():
    """feed_timestamp_ms bounds the string AFTER parsing, so the expansion
    had already happened by the time it looked."""
    assert dsn.feed_timestamp_ms(BILLION_LAUGHS) is None


def test_an_external_entity_is_refused():
    """Refused at the entity-declaration stage, before the SYSTEM reference is
    ever resolved — so file:///etc/passwd is never opened."""
    assert dsn.feed_timestamp_ms(EXTERNAL_ENTITY) is None
    with pytest.raises(EntitiesForbidden):
        dsn.parse_feed(EXTERNAL_ENTITY)


def test_ordinary_feeds_still_parse():
    """The hardening must not break the thing it protects."""
    good = (b'<dsn><timestamp>1780000000000</timestamp>'
            b'<station name="gdscc" friendlyName="Goldstone"/></dsn>')
    assert dsn.feed_timestamp_ms(good) == 1780000000000
    assert dsn.parse_feed(good) == []

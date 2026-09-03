"""Bounded, deterministic policy for NWS CAP alert features.

The NWS ``/alerts`` API returns GeoJSON whose ``properties`` object carries
Common Alerting Protocol (CAP) fields.  This module deliberately separates
parsing and policy from the display/audio code: a product name is presentation
text, not an alarm classification.
"""

from __future__ import annotations

import re

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any


MAX_FEATURES = 256
MAX_REFERENCES = 64
MAX_IDENTIFIER_CHARS = 512
MAX_REFERENCE_CHARS = 512
MAX_EVENT_CHARS = 256
MAX_HEADLINE_CHARS = 1024
MAX_ENUM_CHARS = 32

_REFERENCE_LIST_CHARS = MAX_REFERENCES * (MAX_REFERENCE_CHARS + 96)
_TEST_WORDS = re.compile(r"\b(?:test|drill|exercise)\b", re.IGNORECASE)
_SEVERITY_RANK = {
    "unknown": 0,
    "minor": 1,
    "moderate": 2,
    "severe": 3,
    "extreme": 4,
}
_URGENCY_RANK = {
    "unknown": 0,
    "past": 1,
    "future": 2,
    "expected": 3,
    "immediate": 4,
}
_CERTAINTY_RANK = {
    "unknown": 0,
    "unlikely": 1,
    "possible": 2,
    "likely": 3,
    "observed": 4,
}


class AlertPayloadError(ValueError):
    """The GeoJSON envelope cannot be processed within the safety bounds."""


class _InvalidFeature(ValueError):
    """Internal sentinel for an individual unusable feature."""


@dataclass(frozen=True, slots=True)
class Alert:
    """The CAP fields Skystrip needs to identify and rank an active alert.

    All datetimes are timezone-aware and normalized to UTC. ``references``
    contains CAP identifiers rather than display text, which makes it usable
    for episode continuity across ``Update`` messages.
    """

    identifier: str
    references: tuple[str, ...]
    event: str
    headline: str
    status: str
    message_type: str
    severity: str
    urgency: str
    certainty: str
    effective: datetime
    onset: datetime | None
    expires: datetime
    ends: datetime | None


def _bounded_text(
    value: object,
    *,
    name: str,
    limit: int,
    required: bool = True,
) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise _InvalidFeature(f"{name} is not a string")
    text = value.strip()
    if required and not text:
        raise _InvalidFeature(f"{name} is empty")
    if len(text) > limit:
        raise _InvalidFeature(f"{name} exceeds {limit} characters")
    # CAP display fields should not carry terminal controls or embedded NULs.
    # Reject rather than silently rewriting identity or policy-bearing text.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in text):
        raise _InvalidFeature(f"{name} contains control characters")
    return text


def _datetime_field(
    value: object,
    *,
    name: str,
    required: bool,
) -> datetime | None:
    if value in (None, ""):
        if required:
            raise _InvalidFeature(f"{name} is missing")
        return None
    text = _bounded_text(value, name=name, limit=64)
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
        )
    except ValueError as exc:
        raise _InvalidFeature(f"{name} is not an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise _InvalidFeature(f"{name} must include a timezone")
    return parsed.astimezone(UTC)


def _identifier_from_reference(value: object, *, index: int) -> str:
    if isinstance(value, str):
        return _bounded_text(
            value,
            name=f"references[{index}]",
            limit=MAX_REFERENCE_CHARS,
        )
    if not isinstance(value, Mapping):
        raise _InvalidFeature(f"references[{index}] is malformed")
    identifier = value.get("identifier", value.get("id", value.get("@id")))
    return _bounded_text(
        identifier,
        name=f"references[{index}].identifier",
        limit=MAX_REFERENCE_CHARS,
    )


def _references(value: object) -> tuple[str, ...]:
    if value in (None, ""):
        return ()
    identifiers: list[str] = []
    if isinstance(value, str):
        raw = _bounded_text(
            value,
            name="references",
            limit=_REFERENCE_LIST_CHARS,
        )
        tokens = raw.split()
        if len(tokens) > MAX_REFERENCES:
            raise _InvalidFeature("too many references")
        for index, token in enumerate(tokens):
            # CAP XML serializes each reference as sender,identifier,sent.
            # GeoJSON implementations also commonly expose bare identifiers.
            fields = token.split(",")
            candidate = fields[1] if len(fields) == 3 else token
            identifiers.append(
                _bounded_text(
                    candidate,
                    name=f"references[{index}]",
                    limit=MAX_REFERENCE_CHARS,
                )
            )
    elif isinstance(value, list):
        if len(value) > MAX_REFERENCES:
            raise _InvalidFeature("too many references")
        identifiers.extend(
            _identifier_from_reference(item, index=index)
            for index, item in enumerate(value)
        )
    else:
        raise _InvalidFeature("references is malformed")
    # Reference order is not policy-bearing. Canonicalizing it makes duplicate
    # feed entries and continuity comparisons independent of response order.
    return tuple(sorted(set(identifiers), key=str.casefold))


def _is_test_or_drill(event: str, headline: str) -> bool:
    return bool(_TEST_WORDS.search(event) or _TEST_WORDS.search(headline))


def _utc_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(UTC)


def parse_alert_feature(
    feature: object,
    *,
    now: datetime | None = None,
) -> Alert | None:
    """Parse one active NWS GeoJSON feature, or return ``None`` if unusable.

    Inactive, test, cancelled, malformed and out-of-bounds features all return
    ``None``. Envelope-level failures are instead reported by
    :func:`parse_active_alerts`, so callers can distinguish a bad response from
    a valid response containing no active warnings.
    """

    current = _utc_now(now)
    try:
        if not isinstance(feature, Mapping):
            raise _InvalidFeature("feature is not an object")
        if "type" in feature and feature["type"] != "Feature":
            raise _InvalidFeature("object is not a GeoJSON Feature")
        properties = feature.get("properties")
        if not isinstance(properties, Mapping):
            raise _InvalidFeature("feature properties are missing")

        identifier = _bounded_text(
            feature.get(
                "id",
                properties.get("identifier", properties.get("id", properties.get("@id"))),
            ),
            name="identifier",
            limit=MAX_IDENTIFIER_CHARS,
        )
        event = _bounded_text(
            properties.get("event"), name="event", limit=MAX_EVENT_CHARS
        )
        headline = _bounded_text(
            properties.get("headline"),
            name="headline",
            limit=MAX_HEADLINE_CHARS,
            required=False,
        )
        status = _bounded_text(
            properties.get("status"), name="status", limit=MAX_ENUM_CHARS
        )
        message_type = _bounded_text(
            properties.get("messageType"),
            name="messageType",
            limit=MAX_ENUM_CHARS,
        )
        severity = _bounded_text(
            properties.get("severity"), name="severity", limit=MAX_ENUM_CHARS
        )
        urgency = _bounded_text(
            properties.get("urgency"), name="urgency", limit=MAX_ENUM_CHARS
        )
        certainty = _bounded_text(
            properties.get("certainty"), name="certainty", limit=MAX_ENUM_CHARS
        )
        effective = _datetime_field(
            properties.get("effective"), name="effective", required=True
        )
        onset = _datetime_field(
            properties.get("onset"), name="onset", required=False
        )
        expires = _datetime_field(
            properties.get("expires"), name="expires", required=True
        )
        ends = _datetime_field(properties.get("ends"), name="ends", required=False)

        assert effective is not None and expires is not None
        if status.casefold() != "actual":
            raise _InvalidFeature("alert is not Actual")
        if message_type.casefold() not in {"alert", "update"}:
            raise _InvalidFeature("alert is cancelled or not actionable")
        if _is_test_or_drill(event, headline):
            raise _InvalidFeature("alert is a test or drill")
        if effective > current:
            raise _InvalidFeature("alert is not yet effective")
        if expires <= current or (ends is not None and ends <= current):
            raise _InvalidFeature("alert has expired")

        return Alert(
            identifier=identifier,
            references=_references(properties.get("references")),
            event=event,
            headline=headline,
            status=status,
            message_type=message_type,
            severity=severity,
            urgency=urgency,
            certainty=certainty,
            effective=effective,
            onset=onset,
            expires=expires,
            ends=ends,
        )
    except _InvalidFeature:
        return None


def _product_rank(alert: Alert) -> int:
    words = alert.event.casefold().split()
    if not words:
        return 0
    if words[-1] == "emergency":
        return 2
    if words[-1] == "warning":
        return 1
    return 0


def visual_eligible(alert: Alert) -> bool:
    """Whether an active alert warrants Skystrip's warning presentation."""

    return (
        alert.severity.casefold() in {"severe", "extreme"}
        and _product_rank(alert) > 0
    )


def siren_eligible(alert: Alert) -> bool:
    """Whether an active warning may sound the siren.

    This intentionally uses the CAP severity field, never hazard-name
    substrings.  ``Severe Thunderstorm Warning`` is not enough: only the exact
    CAP severity ``Extreme`` passes the user-selected alarm policy.
    """

    return visual_eligible(alert) and alert.severity.casefold() == "extreme"


def _selection_key(alert: Alert) -> tuple[Any, ...]:
    """A total ordering with safety fields first and stable text last."""

    return (
        int(siren_eligible(alert)),
        _SEVERITY_RANK.get(alert.severity.casefold(), -1),
        _product_rank(alert),
        _URGENCY_RANK.get(alert.urgency.casefold(), -1),
        _CERTAINTY_RANK.get(alert.certainty.casefold(), -1),
        alert.effective.timestamp(),
        alert.expires.timestamp(),
        alert.identifier.casefold(),
        alert.event.casefold(),
        alert.headline.casefold(),
        alert.references,
    )


def parse_active_alerts(
    document: object,
    *,
    now: datetime | None = None,
) -> tuple[Alert, ...]:
    """Parse and deterministically rank the active features in GeoJSON.

    Individual malformed features are ignored. A malformed or unbounded
    envelope raises :class:`AlertPayloadError`, allowing a polling caller to
    keep its last valid state rather than misreading a bad response as an
    authoritative all-clear.
    """

    current = _utc_now(now)
    if not isinstance(document, Mapping):
        raise AlertPayloadError("alert response is not an object")
    features = document.get("features")
    if not isinstance(features, list):
        raise AlertPayloadError("alert response has no features array")
    if len(features) > MAX_FEATURES:
        raise AlertPayloadError(f"alert response exceeds {MAX_FEATURES} features")

    by_identifier: dict[str, Alert] = {}
    for feature in features:
        alert = parse_alert_feature(feature, now=current)
        if alert is None:
            continue
        previous = by_identifier.get(alert.identifier)
        if previous is None or _selection_key(alert) > _selection_key(previous):
            by_identifier[alert.identifier] = alert
    return tuple(sorted(by_identifier.values(), key=_selection_key, reverse=True))


def select_visual_alert(alerts: Iterable[Alert]) -> Alert | None:
    """Return the highest-ranked Severe/Extreme warning or emergency."""

    eligible = (alert for alert in alerts if visual_eligible(alert))
    return max(eligible, key=_selection_key, default=None)


def select_siren_alert(alerts: Iterable[Alert]) -> Alert | None:
    """Return the highest-ranked exact-Extreme siren candidate."""

    eligible = (alert for alert in alerts if siren_eligible(alert))
    return max(eligible, key=_selection_key, default=None)


def reference_identifiers(alert: Alert) -> frozenset[str]:
    """Return this CAP identifier plus the episode identifiers it references."""

    return frozenset((alert.identifier, *alert.references))


def same_episode(previous: Alert, current: Alert) -> bool:
    """Whether two alert messages share an identifier/reference lineage."""

    return bool(reference_identifiers(previous) & reference_identifiers(current))


def is_escalation(previous: Alert, current: Alert) -> bool:
    """Whether a continuing CAP episode became materially more urgent.

    Headline-only changes are routine updates. Severity, product class,
    urgency, certainty, or the named hazard becoming stronger/different is an
    escalation and must not inherit an old acknowledgement.
    """

    if not same_episode(previous, current):
        return False
    return (
        _SEVERITY_RANK.get(current.severity.casefold(), -1)
        > _SEVERITY_RANK.get(previous.severity.casefold(), -1)
        or _product_rank(current) > _product_rank(previous)
        or _URGENCY_RANK.get(current.urgency.casefold(), -1)
        > _URGENCY_RANK.get(previous.urgency.casefold(), -1)
        or _CERTAINTY_RANK.get(current.certainty.casefold(), -1)
        > _CERTAINTY_RANK.get(previous.certainty.casefold(), -1)
        or current.event.casefold() != previous.event.casefold()
    )


def preserve_acknowledgement(previous: Alert | None, current: Alert) -> bool:
    """True only for a routine update to the already-acknowledged episode."""

    return (
        previous is not None
        and same_episode(previous, current)
        and not is_escalation(previous, current)
    )


def should_rearm(previous: Alert | None, current: Alert) -> bool:
    """Whether a selected current episode needs a fresh acknowledgement."""

    return not preserve_acknowledgement(previous, current)


__all__ = [
    "Alert",
    "AlertPayloadError",
    "MAX_FEATURES",
    "MAX_REFERENCES",
    "parse_active_alerts",
    "parse_alert_feature",
    "preserve_acknowledgement",
    "reference_identifiers",
    "same_episode",
    "select_siren_alert",
    "select_visual_alert",
    "should_rearm",
    "siren_eligible",
    "is_escalation",
    "visual_eligible",
]

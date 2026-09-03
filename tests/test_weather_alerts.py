from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

import pytest

from busybar_dev.weather_alerts import (
    AlertPayloadError,
    MAX_FEATURES,
    MAX_REFERENCES,
    is_escalation,
    parse_active_alerts,
    parse_alert_feature,
    preserve_acknowledgement,
    same_episode,
    select_siren_alert,
    select_visual_alert,
    should_rearm,
    siren_eligible,
    visual_eligible,
)


NOW = datetime(2026, 8, 9, 18, 0, tzinfo=UTC)


def feature(
    identifier: str = "urn:alert:a",
    *,
    event: str = "Tornado Warning",
    headline: str = "Tornado Warning issued August 9",
    status: str = "Actual",
    message_type: str = "Alert",
    severity: str = "Extreme",
    urgency: str = "Immediate",
    certainty: str = "Observed",
    effective: datetime = NOW - timedelta(minutes=5),
    onset: datetime | None = NOW - timedelta(minutes=3),
    expires: datetime = NOW + timedelta(hours=1),
    ends: datetime | None = NOW + timedelta(minutes=45),
    references: object = None,
) -> dict:
    def stamp(value: datetime | None) -> str | None:
        return value.isoformat() if value is not None else None

    return {
        "id": identifier,
        "type": "Feature",
        "properties": {
            "event": event,
            "headline": headline,
            "status": status,
            "messageType": message_type,
            "severity": severity,
            "urgency": urgency,
            "certainty": certainty,
            "effective": stamp(effective),
            "onset": stamp(onset),
            "expires": stamp(expires),
            "ends": stamp(ends),
            "references": [] if references is None else references,
        },
    }


def parsed(**kwargs):
    result = parse_alert_feature(feature(**kwargs), now=NOW)
    assert result is not None
    return result


def test_retains_cap_fields_and_normalizes_datetimes_to_utc():
    eastern = timezone(timedelta(hours=-4))
    alert = parse_alert_feature(
        feature(
            references=[
                {"identifier": "urn:alert:root"},
                {"@id": "urn:alert:prior"},
            ],
            effective=NOW.astimezone(eastern) - timedelta(minutes=5),
            onset=NOW.astimezone(eastern) - timedelta(minutes=3),
            expires=NOW.astimezone(eastern) + timedelta(hours=1),
            ends=NOW.astimezone(eastern) + timedelta(minutes=45),
        ),
        now=NOW,
    )
    assert alert is not None
    assert alert.identifier == "urn:alert:a"
    assert alert.references == ("urn:alert:prior", "urn:alert:root")
    assert alert.event == "Tornado Warning"
    assert alert.headline == "Tornado Warning issued August 9"
    assert alert.status == "Actual"
    assert alert.message_type == "Alert"
    assert alert.severity == "Extreme"
    assert alert.urgency == "Immediate"
    assert alert.certainty == "Observed"
    assert alert.effective == NOW - timedelta(minutes=5)
    assert alert.onset == NOW - timedelta(minutes=3)
    assert alert.expires == NOW + timedelta(hours=1)
    assert alert.ends == NOW + timedelta(minutes=45)
    assert all(
        value is None or value.tzinfo is UTC
        for value in (alert.effective, alert.onset, alert.expires, alert.ends)
    )


@pytest.mark.parametrize("status", ["Test", "Exercise", "System", "Draft"])
def test_rejects_every_non_actual_status(status):
    assert parse_alert_feature(feature(status=status), now=NOW) is None


@pytest.mark.parametrize("message_type", ["Cancel", "Ack", "Error", "Test"])
def test_rejects_cancelled_and_non_actionable_message_types(message_type):
    assert parse_alert_feature(feature(message_type=message_type), now=NOW) is None


@pytest.mark.parametrize(
    ("event", "headline"),
    [
        ("Test Tornado Warning", "Tornado Warning"),
        ("Tornado Drill", "Tornado Warning"),
        ("Tornado Warning", "THIS IS AN EXERCISE"),
    ],
)
def test_rejects_tests_and_drills_even_if_marked_actual(event, headline):
    assert parse_alert_feature(feature(event=event, headline=headline), now=NOW) is None


def test_rejects_not_yet_effective_expired_and_ended_alerts():
    assert (
        parse_alert_feature(
            feature(effective=NOW + timedelta(seconds=1)), now=NOW
        )
        is None
    )
    assert parse_alert_feature(feature(expires=NOW), now=NOW) is None
    assert parse_alert_feature(feature(ends=NOW), now=NOW) is None


@pytest.mark.parametrize("field", ["effective", "expires"])
def test_rejects_missing_or_naive_required_datetimes(field):
    item = feature()
    item["properties"][field] = None
    assert parse_alert_feature(item, now=NOW) is None
    item["properties"][field] = "2026-08-09T18:00:00"
    assert parse_alert_feature(item, now=NOW) is None


def test_rejects_naive_now_argument():
    with pytest.raises(ValueError, match="timezone-aware"):
        parse_alert_feature(feature(), now=NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("event", "X" * 257),
        ("headline", "X" * 1025),
        ("severity", "X" * 33),
        ("headline", "bad\x00headline"),
        ("urgency", 123),
    ],
)
def test_rejects_malformed_or_unbounded_strings(field, value):
    item = feature()
    item["properties"][field] = value
    assert parse_alert_feature(item, now=NOW) is None


def test_references_accept_cap_string_and_remain_bounded():
    alert = parsed(
        references=(
            "sender@example,urn:alert:prior,2026-08-09T17:00:00Z "
            "urn:alert:root"
        )
    )
    assert alert.references == ("urn:alert:prior", "urn:alert:root")

    item = feature(references=[f"urn:alert:{i}" for i in range(MAX_REFERENCES + 1)])
    assert parse_alert_feature(item, now=NOW) is None


@pytest.mark.parametrize("payload", [None, [], {}, {"features": None}])
def test_rejects_malformed_geojson_envelopes(payload):
    with pytest.raises(AlertPayloadError):
        parse_active_alerts(payload, now=NOW)


def test_rejects_unbounded_geojson_envelope():
    payload = {"features": [feature(str(i)) for i in range(MAX_FEATURES + 1)]}
    with pytest.raises(AlertPayloadError, match="exceeds"):
        parse_active_alerts(payload, now=NOW)


def test_skips_bad_individual_features_without_losing_valid_feature():
    payload = {"features": [None, {"type": "Thing"}, feature()]}
    assert [alert.identifier for alert in parse_active_alerts(payload, now=NOW)] == [
        "urn:alert:a"
    ]


@pytest.mark.parametrize(
    ("event", "severity", "visual", "siren"),
    [
        ("Severe Thunderstorm Warning", "Severe", True, False),
        ("Tornado Warning", "Extreme", True, True),
        ("Extreme Wind Warning", "Extreme", True, True),
        ("Flash Flood Emergency", "Extreme", True, True),
        ("Tornado Watch", "Extreme", False, False),
        ("Tornado Warning", "Moderate", False, False),
        ("Heat Advisory", "Extreme", False, False),
    ],
)
def test_visual_and_siren_policy_use_cap_fields_not_hazard_substrings(
    event, severity, visual, siren
):
    alert = parsed(event=event, headline=event, severity=severity)
    assert visual_eligible(alert) is visual
    assert siren_eligible(alert) is siren


def test_simultaneous_alert_selection_is_ranked_and_feed_order_independent():
    severe = feature(
        "urn:alert:severe",
        event="Severe Thunderstorm Warning",
        headline="Severe Thunderstorm Warning",
        severity="Severe",
    )
    likely = feature(
        "urn:alert:likely",
        event="Extreme Wind Warning",
        headline="Extreme Wind Warning",
        severity="Extreme",
        certainty="Likely",
    )
    observed = feature(
        "urn:alert:observed",
        event="Tornado Warning",
        headline="Tornado Warning",
        severity="Extreme",
        certainty="Observed",
    )
    first = parse_active_alerts({"features": [severe, likely, observed]}, now=NOW)
    second = parse_active_alerts(
        {"features": [observed, likely, severe]}, now=NOW
    )
    assert [alert.identifier for alert in first] == [
        alert.identifier for alert in second
    ]
    assert select_visual_alert(first).identifier == "urn:alert:observed"
    assert select_siren_alert(first).identifier == "urn:alert:observed"


def test_selection_tie_is_stable_even_for_duplicate_identifiers():
    older = feature(
        "urn:alert:a",
        headline="Older",
        effective=NOW - timedelta(minutes=10),
    )
    newer = feature(
        "urn:alert:a",
        headline="Newer",
        effective=NOW - timedelta(minutes=1),
    )
    forward = parse_active_alerts({"features": [older, newer]}, now=NOW)
    reverse = parse_active_alerts({"features": [newer, older]}, now=NOW)
    assert forward == reverse
    assert forward[0].headline == "Newer"


def test_continuity_uses_same_identifier_direct_reference_and_shared_lineage():
    root = parsed(identifier="urn:alert:root")
    same = parsed(identifier="urn:alert:root", headline="Updated wording")
    update = parsed(
        identifier="urn:alert:update",
        message_type="Update",
        references=[{"identifier": "urn:alert:root"}],
    )
    sibling = parsed(
        identifier="urn:alert:sibling",
        message_type="Update",
        references=["urn:alert:root"],
    )
    unrelated = parsed(identifier="urn:alert:other")
    assert same_episode(root, same)
    assert same_episode(root, update)
    assert same_episode(update, sibling)
    assert not same_episode(root, unrelated)


def test_routine_update_preserves_acknowledgement():
    previous = parsed(identifier="urn:alert:root")
    update = parsed(
        identifier="urn:alert:update",
        message_type="Update",
        headline="Routine wording update",
        references=["urn:alert:root"],
    )
    assert not is_escalation(previous, update)
    assert preserve_acknowledgement(previous, update)
    assert not should_rearm(previous, update)


def test_new_or_escalated_episode_rearms_acknowledgement():
    previous = parsed(
        identifier="urn:alert:root",
        event="Severe Thunderstorm Warning",
        headline="Severe Thunderstorm Warning",
        severity="Severe",
        urgency="Expected",
        certainty="Likely",
    )
    escalated = parsed(
        identifier="urn:alert:update",
        message_type="Update",
        event="Tornado Warning",
        headline="Tornado Warning",
        references=["urn:alert:root"],
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
    )
    new_episode = parsed(identifier="urn:alert:new")
    assert is_escalation(previous, escalated)
    assert should_rearm(previous, escalated)
    assert not preserve_acknowledgement(previous, escalated)
    assert should_rearm(escalated, new_episode)


def test_no_eligible_alert_selects_none():
    alerts = parse_active_alerts(
        {
            "features": [
                feature(
                    event="Severe Thunderstorm Watch",
                    headline="Severe Thunderstorm Watch",
                    severity="Severe",
                )
            ]
        },
        now=NOW,
    )
    assert select_visual_alert(alerts) is None
    assert select_siren_alert(alerts) is None

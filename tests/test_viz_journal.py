import json
import sqlite3

import pytest

from busybar_viz.journal import IdempotencyConflict, JournalError, RevisionConflict, SessionJournal


def test_session_creation_event_id_recovers_a_lost_response(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    event_id = "evt_0000000000000000000000000000000a"

    created_session, created = journal.create_session(
        "Retry-safe draft", event_id=event_id,
    )
    retried_session, retried = journal.create_session(
        "Retry-safe draft", event_id=event_id,
    )

    assert retried_session.id == created_session.id
    assert retried.id == created.id
    assert retried_session.revision == 1
    with pytest.raises(IdempotencyConflict, match="event id was reused"):
        journal.create_session("Different draft", event_id=event_id)


def test_journal_is_wal_append_only_and_optimistically_revisioned(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, created = journal.create_session("General visual draft")
    assert session.revision == created.revision == 1

    with sqlite3.connect(journal.path) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE events SET kind = 'forged' WHERE id = ?", (created.id,)
            )

    session, feedback = journal.append_event(
        session.id,
        expected_revision=1,
        kind="review.feedback",
        actor="user",
        body={"message": "Keep the status readable"},
        event_id="evt_00000000000000000000000000000001",
    )
    assert session.revision == feedback.revision == 2

    # Retrying an identical client event is idempotent even though its original
    # expected revision is now stale.
    retried_session, retried = journal.append_event(
        session.id,
        expected_revision=1,
        kind="review.feedback",
        actor="user",
        body={"message": "Keep the status readable"},
        event_id="evt_00000000000000000000000000000001",
    )
    assert retried.id == feedback.id
    assert retried_session.revision == 2

    with pytest.raises(RevisionConflict) as conflict:
        journal.append_event(
            session.id,
            expected_revision=1,
            kind="review.feedback",
            actor="user",
            body={"message": "stale writer"},
        )
    assert conflict.value.current == 2


def test_only_latest_requested_job_promotes_its_artifact(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Race proof")
    job_one = "job_00000000000000000000000000000001"
    job_two = "job_00000000000000000000000000000002"
    artifact_one = "a" * 64
    artifact_two = "b" * 64

    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=job_one,
        request={"scenario_id": "example/one"},
    )
    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=job_two,
        request={"scenario_id": "example/two"},
    )

    session, old_completion = journal.finish_render(
        session.id,
        job_id=job_one,
        status="succeeded",
        artifact_id=artifact_one,
        passed=True,
    )
    assert old_completion.body["promoted"] is False
    assert session.current_artifact_id is None

    session, new_completion = journal.finish_render(
        session.id,
        job_id=job_two,
        status="succeeded",
        artifact_id=artifact_two,
        passed=False,
    )
    assert new_completion.body["promoted"] is True
    assert session.current_artifact_id == artifact_two

    exported = [json.loads(line) for line in journal.export_jsonl(session.id).splitlines()]
    assert [event["revision"] for event in exported] == list(
        range(1, session.revision + 1)
    )
    assert exported[-1]["artifact_id"] == artifact_two


def test_agent_presentation_invalidates_an_older_pending_render_promotion(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Agent beats pending worker")
    job_id = "job_00000000000000000000000000000001"
    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=job_id,
        request={"scenario_id": "example/slow"},
    )
    assert session.latest_render_job_id == job_id

    session, _ = journal.present_artifact(
        session.id,
        expected_revision=session.revision,
        artifact_id="c" * 64,
    )
    assert session.current_artifact_id == "c" * 64
    assert session.latest_render_job_id is None

    session, completion = journal.finish_render(
        session.id,
        job_id=job_id,
        status="succeeded",
        artifact_id="a" * 64,
        passed=True,
    )
    assert completion.body["promoted"] is False
    assert session.current_artifact_id == "c" * 64


def test_current_artifact_feedback_retry_keeps_its_original_reference(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Idempotent review")
    first_job = "job_00000000000000000000000000000001"
    second_job = "job_00000000000000000000000000000002"
    event_id = "evt_00000000000000000000000000000001"

    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=first_job,
        request={"scenario_id": "example/one"},
    )
    session, _ = journal.finish_render(
        session.id,
        job_id=first_job,
        status="succeeded",
        artifact_id="a" * 64,
    )
    original_revision = session.revision
    session, feedback = journal.append_event(
        session.id,
        expected_revision=original_revision,
        kind="review.feedback",
        actor="user",
        body={"message": "first artifact"},
        use_current_artifact=True,
        event_id=event_id,
    )
    assert feedback.artifact_id == "a" * 64

    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=second_job,
        request={"scenario_id": "example/two"},
    )
    session, _ = journal.finish_render(
        session.id,
        job_id=second_job,
        status="succeeded",
        artifact_id="b" * 64,
    )
    retried_session, retried = journal.append_event(
        session.id,
        expected_revision=original_revision,
        kind="review.feedback",
        actor="user",
        body={"message": "first artifact"},
        use_current_artifact=True,
        event_id=event_id,
    )
    assert retried.id == feedback.id
    assert retried.artifact_id == "a" * 64
    assert retried_session.current_artifact_id == "b" * 64


def test_render_completion_requires_ownership_and_is_idempotent(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Terminal integrity")
    job_id = "job_00000000000000000000000000000001"
    with pytest.raises(JournalError, match="was not requested"):
        journal.finish_render(session.id, job_id=job_id, status="failed")

    session, _ = journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=job_id,
        request={"scenario_id": "example"},
    )
    session, completion = journal.finish_render(
        session.id,
        job_id=job_id,
        status="succeeded",
        artifact_id="a" * 64,
        passed=True,
    )
    revision = session.revision
    retried_session, retried = journal.finish_render(
        session.id,
        job_id=job_id,
        status="succeeded",
        artifact_id="a" * 64,
        passed=True,
    )
    assert retried.id == completion.id
    assert retried_session.revision == revision
    with pytest.raises(IdempotencyConflict, match="contradictory"):
        journal.finish_render(session.id, job_id=job_id, status="failed")


def test_interrupted_render_reconciliation_is_durable_and_idempotent(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Restart recovery")
    job_id = "job_00000000000000000000000000000001"
    journal.request_render(
        session.id,
        expected_revision=session.revision,
        job_id=job_id,
        request={"scenario_id": "example"},
    )
    recovered = journal.reconcile_interrupted_renders()
    assert len(recovered) == 1
    assert recovered[0].body["status"] == "failed"
    assert "process ended" in recovered[0].body["detail"]
    assert journal.reconcile_interrupted_renders() == ()


def test_jsonl_export_does_not_truncate_after_one_thousand_events(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Long collaboration")
    with sqlite3.connect(journal.path) as connection:
        for revision in range(2, 1003):
            connection.execute(
                """
                INSERT INTO events(
                    id, session_id, revision, kind, actor, body_json,
                    artifact_id, created_at
                ) VALUES (?, ?, ?, 'review.feedback', 'user', '{}', NULL, ?)
                """,
                (
                    f"evt_{revision:032x}",
                    session.id,
                    revision,
                    "2026-08-09T00:00:00.000Z",
                ),
            )
        connection.execute(
            "UPDATE sessions SET revision = 1002 WHERE id = ?", (session.id,)
        )
    exported = journal.export_jsonl(session.id).splitlines()
    assert len(exported) == 1002
    assert json.loads(exported[-1])["revision"] == 1002
    assert json.loads(exported[-1])["schema"] == "busybar.session-event/v1"


def test_agent_can_optimistically_present_an_artifact(tmp_path):
    journal = SessionJournal(tmp_path / "sessions.sqlite3")
    session, _ = journal.create_session("Agent handoff")
    event_id = "evt_00000000000000000000000000000007"
    session, presented = journal.present_artifact(
        session.id,
        expected_revision=session.revision,
        artifact_id="c" * 64,
        message="Candidate from the deterministic renderer",
        event_id=event_id,
    )
    assert session.current_artifact_id == "c" * 64
    assert presented.kind == "artifact.presented"
    assert presented.actor == "agent"

    retried_session, retried = journal.present_artifact(
        session.id,
        expected_revision=1,
        artifact_id="c" * 64,
        message="Candidate from the deterministic renderer",
        event_id=event_id,
    )
    assert retried.id == presented.id
    assert retried_session.revision == session.revision

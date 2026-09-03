"""Durable, append-only collaboration sessions for :mod:`busybar_viz`.

The journal is deliberately independent of HTTP and render execution.  Every
mutation is an optimistic append against a session revision, while internal
render completion events append at the then-current revision.  A completion
only promotes its artifact when it still belongs to the newest requested job.
"""

from __future__ import annotations

import json
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping

from .limits import MAX_JSON_BYTES
from .models import SESSION_EVENT_SCHEMA, SESSION_SCHEMA

_ID_RE = re.compile(r"^(?:ses|evt|job)_[0-9a-f]{32}$")
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_RE = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_MAX_TITLE_LENGTH = 160


class JournalError(ValueError):
    """Base class for stable journal failures."""


class SessionNotFound(JournalError):
    def __init__(self, session_id: str) -> None:
        super().__init__(f"unknown session: {session_id}")
        self.session_id = session_id


class RevisionConflict(JournalError):
    def __init__(self, session_id: str, expected: int, current: int) -> None:
        super().__init__(
            f"session {session_id} is at revision {current}, not {expected}"
        )
        self.session_id = session_id
        self.expected = expected
        self.current = current


class IdempotencyConflict(JournalError):
    """An event id was reused for different event content."""


@dataclass(frozen=True, slots=True)
class SessionRecord:
    id: str
    title: str
    revision: int
    current_artifact_id: str | None
    latest_render_job_id: str | None
    created_at: str
    updated_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SESSION_SCHEMA,
            "id": self.id,
            "title": self.title,
            "revision": self.revision,
            "current_artifact_id": self.current_artifact_id,
            "latest_render_job_id": self.latest_render_job_id,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True, slots=True)
class EventRecord:
    id: str
    session_id: str
    revision: int
    kind: str
    actor: str
    body: Mapping[str, Any]
    artifact_id: str | None
    created_at: str

    def as_dict(self) -> dict[str, object]:
        return {
            "schema": SESSION_EVENT_SCHEMA,
            "id": self.id,
            "session_id": self.session_id,
            "revision": self.revision,
            "kind": self.kind,
            "actor": self.actor,
            "body": dict(self.body),
            "artifact_id": self.artifact_id,
            "created_at": self.created_at,
        }


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


def _json(value: object, *, label: str) -> str:
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, RecursionError) as exc:
        raise JournalError(f"{label} must contain finite JSON values") from exc
    if len(encoded) > MAX_JSON_BYTES:
        raise JournalError(f"{label} exceeds the JSON event budget")
    return encoded.decode("ascii")


def _validate_id(value: str, prefix: str) -> str:
    if not isinstance(value, str) or not _ID_RE.fullmatch(value):
        raise JournalError(f"invalid {prefix} id")
    if not value.startswith(f"{prefix}_"):
        raise JournalError(f"invalid {prefix} id")
    return value


def _validate_artifact(value: str | None) -> str | None:
    if value is not None and (
        not isinstance(value, str) or not _ARTIFACT_RE.fullmatch(value)
    ):
        raise JournalError("artifact ids must be lowercase SHA-256 digests")
    return value


def _validate_token(value: str, label: str) -> str:
    if not isinstance(value, str) or not _TOKEN_RE.fullmatch(value):
        raise JournalError(f"invalid {label}")
    return value


class SessionJournal:
    """SQLite-backed event journal with one short-lived connection per call."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self.path,
            timeout=5,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        connection.execute("PRAGMA synchronous = FULL")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = FULL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    revision INTEGER NOT NULL CHECK (revision >= 0),
                    current_artifact_id TEXT,
                    latest_render_job_id TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id TEXT NOT NULL UNIQUE,
                    session_id TEXT NOT NULL REFERENCES sessions(id),
                    revision INTEGER NOT NULL CHECK (revision > 0),
                    kind TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    body_json TEXT NOT NULL,
                    artifact_id TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (session_id, revision)
                );

                CREATE INDEX IF NOT EXISTS events_session_created
                    ON events(session_id, created_at);

                CREATE TRIGGER IF NOT EXISTS events_are_immutable_update
                BEFORE UPDATE ON events BEGIN
                    SELECT RAISE(ABORT, 'session events are append-only');
                END;

                CREATE TRIGGER IF NOT EXISTS events_are_immutable_delete
                BEFORE DELETE ON events BEGIN
                    SELECT RAISE(ABORT, 'session events are append-only');
                END;
                """
            )

    @staticmethod
    def _session(row: sqlite3.Row) -> SessionRecord:
        return SessionRecord(
            id=row["id"],
            title=row["title"],
            revision=row["revision"],
            current_artifact_id=row["current_artifact_id"],
            latest_render_job_id=row["latest_render_job_id"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> EventRecord:
        return EventRecord(
            id=row["id"],
            session_id=row["session_id"],
            revision=row["revision"],
            kind=row["kind"],
            actor=row["actor"],
            body=json.loads(row["body_json"]),
            artifact_id=row["artifact_id"],
            created_at=row["created_at"],
        )

    def create_session(
        self,
        title: str,
        *,
        session_id: str | None = None,
        event_id: str | None = None,
    ) -> tuple[SessionRecord, EventRecord]:
        if not isinstance(title, str) or not title.strip():
            raise JournalError("session title must not be blank")
        title = title.strip()
        if len(title) > _MAX_TITLE_LENGTH or any(c in title for c in "\r\n\0"):
            raise JournalError("session title must be one line of at most 160 characters")
        requested_session_id = session_id
        session_id = _validate_id(session_id or _new_id("ses"), "ses")
        event_id = _validate_id(event_id or _new_id("evt"), "evt")
        created_at = _now()
        body_json = _json({"title": title}, label="session event")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing_row = connection.execute(
                    "SELECT * FROM events WHERE id = ?", (event_id,)
                ).fetchone()
                if existing_row is not None:
                    if (
                        existing_row["kind"] != "session.created"
                        or existing_row["actor"] != "user"
                        or existing_row["body_json"] != body_json
                        or existing_row["artifact_id"] is not None
                        or (
                            requested_session_id is not None
                            and existing_row["session_id"] != session_id
                        )
                    ):
                        raise IdempotencyConflict(f"event id was reused: {event_id}")
                    session_row = connection.execute(
                        "SELECT * FROM sessions WHERE id = ?",
                        (existing_row["session_id"],),
                    ).fetchone()
                    if session_row is None:
                        raise JournalError("session creation event has no session")
                    connection.execute("COMMIT")
                    return self._session(session_row), self._event(existing_row)
                connection.execute(
                    """
                    INSERT INTO sessions(
                        id, title, revision, current_artifact_id,
                        latest_render_job_id, created_at, updated_at
                    ) VALUES (?, ?, 1, NULL, NULL, ?, ?)
                    """,
                    (session_id, title, created_at, created_at),
                )
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, revision, kind, actor, body_json,
                        artifact_id, created_at
                    ) VALUES (?, ?, 1, 'session.created', 'user', ?, NULL, ?)
                    """,
                    (event_id, session_id, body_json, created_at),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self.get_session(session_id), self.get_event(event_id)

    def get_session(self, session_id: str) -> SessionRecord:
        _validate_id(session_id, "ses")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            raise SessionNotFound(session_id)
        return self._session(row)

    def referenced_artifact_ids(self) -> frozenset[str]:
        """Every artifact id cited by any event or current-session pointer.

        Store garbage collection treats this set as immortal, so it must be
        complete rather than paged: one query over the whole journal.
        """
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT artifact_id AS ref FROM events"
                " WHERE artifact_id IS NOT NULL"
                " UNION"
                " SELECT current_artifact_id AS ref FROM sessions"
                " WHERE current_artifact_id IS NOT NULL"
            ).fetchall()
        return frozenset(row["ref"] for row in rows)

    def list_sessions(self, *, limit: int = 100) -> tuple[SessionRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
            raise JournalError("session list limit must be between 1 and 500")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM sessions ORDER BY updated_at DESC, id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return tuple(self._session(row) for row in rows)

    def get_event(self, event_id: str) -> EventRecord:
        event = self.find_event(event_id)
        if event is None:
            raise JournalError(f"unknown event: {event_id}")
        return event

    def find_event(self, event_id: str) -> EventRecord | None:
        _validate_id(event_id, "evt")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM events WHERE id = ?", (event_id,)
            ).fetchone()
        if row is None:
            return None
        return self._event(row)

    def list_events(
        self,
        session_id: str,
        *,
        after_revision: int = 0,
        limit: int = 500,
    ) -> tuple[EventRecord, ...]:
        self.get_session(session_id)
        if (
            isinstance(after_revision, bool)
            or not isinstance(after_revision, int)
            or after_revision < 0
        ):
            raise JournalError("after_revision must be a nonnegative integer")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise JournalError("event list limit must be between 1 and 1000")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE session_id = ? AND revision > ?
                ORDER BY revision ASC LIMIT ?
                """,
                (session_id, after_revision, limit),
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def _existing_idempotent(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        session_id: str,
        kind: str,
        actor: str,
        body_json: str,
        artifact_id: str | None,
        compare_artifact: bool = True,
    ) -> EventRecord | None:
        row = connection.execute(
            "SELECT * FROM events WHERE id = ?", (event_id,)
        ).fetchone()
        if row is None:
            return None
        if (
            row["session_id"] != session_id
            or row["kind"] != kind
            or row["actor"] != actor
            or row["body_json"] != body_json
            or (compare_artifact and row["artifact_id"] != artifact_id)
        ):
            raise IdempotencyConflict(f"event id was reused: {event_id}")
        return self._event(row)

    def append_event(
        self,
        session_id: str,
        *,
        expected_revision: int,
        kind: str,
        actor: str,
        body: Mapping[str, Any],
        artifact_id: str | None = None,
        use_current_artifact: bool = False,
        promote_artifact: bool = False,
        event_id: str | None = None,
    ) -> tuple[SessionRecord, EventRecord]:
        _validate_id(session_id, "ses")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise JournalError("expected_revision must be an integer")
        kind = _validate_token(kind, "event kind")
        actor = _validate_token(actor, "event actor")
        if not isinstance(body, Mapping):
            raise JournalError("event body must be an object")
        body_json = _json(dict(body), label="session event")
        artifact_id = _validate_artifact(artifact_id)
        if artifact_id is not None and use_current_artifact:
            raise JournalError("choose an explicit or current artifact, not both")
        if promote_artifact and artifact_id is None:
            raise JournalError("artifact promotion requires an explicit artifact id")
        event_id = _validate_id(event_id or _new_id("evt"), "evt")
        created_at = _now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                session_row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if session_row is None:
                    raise SessionNotFound(session_id)
                resolved_artifact = (
                    session_row["current_artifact_id"]
                    if use_current_artifact
                    else artifact_id
                )
                existing = self._existing_idempotent(
                    connection,
                    event_id=event_id,
                    session_id=session_id,
                    kind=kind,
                    actor=actor,
                    body_json=body_json,
                    artifact_id=resolved_artifact,
                    # On retry, "current" may point to a newer render.  The
                    # stable event id must still return the originally linked
                    # event instead of comparing against today's pointer.
                    compare_artifact=not use_current_artifact,
                )
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._session(session_row), existing
                current = session_row["revision"]
                if current != expected_revision:
                    raise RevisionConflict(session_id, expected_revision, current)
                revision = current + 1
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, revision, kind, actor, body_json,
                        artifact_id, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        session_id,
                        revision,
                        kind,
                        actor,
                        body_json,
                        resolved_artifact,
                        created_at,
                    ),
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET revision = ?,
                        current_artifact_id = CASE WHEN ? THEN ? ELSE current_artifact_id END,
                        latest_render_job_id = CASE WHEN ? THEN NULL ELSE latest_render_job_id END,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        revision,
                        int(promote_artifact),
                        artifact_id,
                        int(promote_artifact),
                        created_at,
                        session_id,
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self.get_session(session_id), self.get_event(event_id)

    def present_artifact(
        self,
        session_id: str,
        *,
        expected_revision: int,
        artifact_id: str,
        message: str | None = None,
        event_id: str | None = None,
    ) -> tuple[SessionRecord, EventRecord]:
        """Atomically present agent-produced evidence to a shared session."""

        if message is not None and (
            not isinstance(message, str) or len(message) > 4000 or "\0" in message
        ):
            raise JournalError("presentation message must be at most 4000 characters")
        body: dict[str, object] = {}
        if message and message.strip():
            body["message"] = message.strip()
        return self.append_event(
            session_id,
            expected_revision=expected_revision,
            kind="artifact.presented",
            actor="agent",
            body=body,
            artifact_id=artifact_id,
            promote_artifact=True,
            event_id=event_id,
        )

    def request_render(
        self,
        session_id: str,
        *,
        expected_revision: int,
        job_id: str,
        request: Mapping[str, Any],
        event_id: str | None = None,
    ) -> tuple[SessionRecord, EventRecord]:
        _validate_id(session_id, "ses")
        _validate_id(job_id, "job")
        if isinstance(expected_revision, bool) or not isinstance(expected_revision, int):
            raise JournalError("expected_revision must be an integer")
        body = {"job_id": job_id, "request": dict(request)}
        body_json = _json(body, label="render request")
        event_id = _validate_id(event_id or _new_id("evt"), "evt")
        created_at = _now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                existing = self._existing_idempotent(
                    connection,
                    event_id=event_id,
                    session_id=session_id,
                    kind="render.requested",
                    actor="user",
                    body_json=body_json,
                    artifact_id=None,
                )
                if existing is not None:
                    connection.execute("COMMIT")
                    return self._session(row), existing
                current = row["revision"]
                if current != expected_revision:
                    raise RevisionConflict(session_id, expected_revision, current)
                revision = current + 1
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, revision, kind, actor, body_json,
                        artifact_id, created_at
                    ) VALUES (?, ?, ?, 'render.requested', 'user', ?, NULL, ?)
                    """,
                    (event_id, session_id, revision, body_json, created_at),
                )
                connection.execute(
                    """
                    UPDATE sessions
                    SET revision = ?, latest_render_job_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (revision, job_id, created_at, session_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self.get_session(session_id), self.get_event(event_id)

    def finish_render(
        self,
        session_id: str,
        *,
        job_id: str,
        status: str,
        artifact_id: str | None = None,
        passed: bool | None = None,
        detail: str | None = None,
    ) -> tuple[SessionRecord, EventRecord]:
        """Append a worker outcome and conditionally promote its artifact."""

        _validate_id(session_id, "ses")
        _validate_id(job_id, "job")
        if status not in {"succeeded", "failed", "timed_out", "cancelled"}:
            raise JournalError("invalid render completion status")
        artifact_id = _validate_artifact(artifact_id)
        if status == "succeeded" and artifact_id is None:
            raise JournalError("a successful render must reference an artifact")
        if status != "succeeded" and artifact_id is not None:
            raise JournalError("a failed render cannot reference an artifact")
        if detail is not None and (not isinstance(detail, str) or len(detail) > 2000):
            raise JournalError("render detail exceeds 2000 characters")
        event_id = _new_id("evt")
        created_at = _now()

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                row = connection.execute(
                    "SELECT * FROM sessions WHERE id = ?", (session_id,)
                ).fetchone()
                if row is None:
                    raise SessionNotFound(session_id)
                requested = None
                terminal = None
                event_rows = connection.execute(
                    """
                    SELECT * FROM events
                    WHERE session_id = ?
                      AND kind IN ('render.requested', 'render.completed', 'render.failed')
                    ORDER BY revision ASC
                    """,
                    (session_id,),
                ).fetchall()
                for event_row in event_rows:
                    event_body = json.loads(event_row["body_json"])
                    if event_body.get("job_id") != job_id:
                        continue
                    if event_row["kind"] == "render.requested":
                        requested = event_row
                    else:
                        terminal = event_row
                if requested is None:
                    raise JournalError(
                        f"render job {job_id} was not requested by session {session_id}"
                    )
                if terminal is not None:
                    prior = self._event(terminal)
                    prior_passed = prior.body.get("passed")
                    if (
                        prior.body.get("status") != status
                        or prior.artifact_id != artifact_id
                        or (passed is not None and prior_passed != bool(passed))
                    ):
                        raise IdempotencyConflict(
                            f"render job has a contradictory terminal result: {job_id}"
                        )
                    connection.execute("COMMIT")
                    return self._session(row), prior
                promoted = (
                    status == "succeeded"
                    and row["latest_render_job_id"] == job_id
                )
                body: dict[str, object] = {
                    "job_id": job_id,
                    "status": status,
                    "promoted": promoted,
                }
                if passed is not None:
                    body["passed"] = bool(passed)
                if detail:
                    body["detail"] = detail
                body_json = _json(body, label="render completion")
                revision = row["revision"] + 1
                kind = "render.completed" if status == "succeeded" else "render.failed"
                connection.execute(
                    """
                    INSERT INTO events(
                        id, session_id, revision, kind, actor, body_json,
                        artifact_id, created_at
                    ) VALUES (?, ?, ?, ?, 'runner', ?, ?, ?)
                    """,
                    (
                        event_id,
                        session_id,
                        revision,
                        kind,
                        body_json,
                        artifact_id,
                        created_at,
                    ),
                )
                current_artifact = artifact_id if promoted else row["current_artifact_id"]
                connection.execute(
                    """
                    UPDATE sessions
                    SET revision = ?, current_artifact_id = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (revision, current_artifact, created_at, session_id),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        return self.get_session(session_id), self.get_event(event_id)

    def reconcile_interrupted_renders(self) -> tuple[EventRecord, ...]:
        """Mark requests abandoned by an earlier server process as failed."""

        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM events
                WHERE kind IN ('render.requested', 'render.completed', 'render.failed')
                ORDER BY session_id, revision ASC
                """
            ).fetchall()
        requested: dict[tuple[str, str], EventRecord] = {}
        terminal: set[tuple[str, str]] = set()
        for row in rows:
            event = self._event(row)
            job_id = event.body.get("job_id")
            if not isinstance(job_id, str):
                continue
            key = (event.session_id, job_id)
            if event.kind == "render.requested":
                requested[key] = event
            else:
                terminal.add(key)
        recovered: list[EventRecord] = []
        for (session_id, job_id), _event in requested.items():
            if (session_id, job_id) in terminal:
                continue
            _session, completion = self.finish_render(
                session_id,
                job_id=job_id,
                status="failed",
                detail="visualizer process ended before the render completed",
            )
            recovered.append(completion)
        return tuple(recovered)

    def iter_jsonl(self, session_id: str):
        """Yield the complete session stream without buffering it in memory."""

        self.get_session(session_id)
        connection = self._connect()
        try:
            cursor = connection.execute(
                "SELECT * FROM events WHERE session_id = ? ORDER BY revision ASC",
                (session_id,),
            )
            for row in cursor:
                event = self._event(row)
                yield (
                    _json(event.as_dict(), label="session export").encode("ascii")
                    + b"\n"
                )
        finally:
            connection.close()

    def export_jsonl(self, session_id: str) -> bytes:
        return b"".join(self.iter_jsonl(session_id))

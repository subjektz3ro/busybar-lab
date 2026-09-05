"""A completed journal operation must not leave SQLite handles to the GC."""

import sqlite3

import pytest

from busybar_viz.journal import RevisionConflict, SessionJournal


@pytest.mark.parametrize("operation", [
    "initialize", "read", "write", "conflict", "partial-export",
])
def test_journal_closes_connections_on_success_failure_and_export_close(
    tmp_path, monkeypatch, operation,
):
    connections = []
    original = sqlite3.connect

    def track(*args, **kwargs):
        connection = original(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    try:
        journal = SessionJournal(tmp_path / "sessions.sqlite3")
        if operation != "initialize":
            session, _ = journal.create_session("Resource lifetime")
            if operation == "read":
                journal.get_session(session.id)
                journal.list_sessions()
            elif operation in ("write", "conflict"):
                def append(revision):
                    return journal.append_event(
                        session.id, expected_revision=revision, kind="agent.note",
                        actor="agent", body={"message": "test"},
                    )
                if operation == "conflict":
                    with pytest.raises(RevisionConflict):
                        append(0)
                else:
                    append(1)
            elif operation == "partial-export":
                stream = journal.iter_jsonl(session.id)
                assert next(stream)
                stream.close()
        assert connections
        for connection in connections:
            with pytest.raises(sqlite3.ProgrammingError, match="closed"):
                connection.execute("SELECT 1")
    finally:
        # The regression also runs against the unfixed code without leaking.
        for connection in connections:
            connection.close()


def test_journal_closes_a_connection_when_pragma_setup_fails(tmp_path, monkeypatch):
    class BadPragma(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql.startswith("PRAGMA"):
                raise sqlite3.OperationalError("setup failed")
            return super().execute(sql, *args, **kwargs)

    connection = sqlite3.connect(":memory:", factory=BadPragma)
    monkeypatch.setattr(sqlite3, "connect", lambda *args, **kwargs: connection)
    try:
        with pytest.raises(sqlite3.OperationalError, match="setup failed"):
            SessionJournal(tmp_path / "sessions.sqlite3")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
    finally:
        connection.close()

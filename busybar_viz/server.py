"""Local collaboration API and drafting UI for ``busybar-viz``.

This is intentionally a separate process from Barkeep.  It renders registered
offline scenarios, journals human/model review events, and serves immutable
evidence artifacts.  It never connects to a BUSY Bar.
"""

from __future__ import annotations

import argparse
import ipaddress
import os
import re
import sqlite3
import stat
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Mapping, Sequence
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .artifacts import VerifiedArtifact, verify_artifact
from .jobs import (
    JobError,
    JobManager,
    JobNotFound,
    JobQueueFull,
    JobRecord,
)
from .journal import (
    IdempotencyConflict,
    JournalError,
    RevisionConflict,
    SessionJournal,
    SessionNotFound,
)
from .limits import MAX_JSON_BYTES
from .models import EvidenceLevel, RenderRequest
from .registry import scenarios

STATIC_DIR = Path(__file__).parent / "static"
_ARTIFACT_RE = re.compile(r"^[0-9a-f]{64}$")
_EVENT_RE = re.compile(r"^evt_([0-9a-f]{32})$")
_MUTATING = {"POST", "PUT", "PATCH", "DELETE"}
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})


class _BodyLimitMiddleware:
    """Cap request streams even when Content-Length is absent or dishonest."""

    def __init__(self, app: ASGIApp, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http" or scope.get("method") not in _MUTATING:
            await self.app(scope, receive, send)
            return
        consumed = 0
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                async def replay_special() -> Message:
                    return message  # noqa: B023 - replay_special is awaited immediately, then returns

                await self.app(scope, replay_special, send)
                return
            chunk = message.get("body", b"")
            consumed += len(chunk)
            if consumed > self.max_bytes:
                response = _error(
                    413,
                    "request exceeds the JSON budget",
                    kind="too_large",
                )
                await response(scope, receive, send)
                return
            chunks.append(chunk)
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay_receive() -> Message:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {
                    "type": "http.request",
                    "body": b"".join(chunks),
                    "more_body": False,
                }
            return {"type": "http.disconnect"}

        await self.app(scope, replay_receive, send)


def _error(status: int, message: str, **extra: object) -> JSONResponse:
    return JSONResponse({"error": message, **extra}, status_code=status)


def _object(value: object, label: str = "body") -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise JournalError(f"{label} must be an object")
    return value


def _only(body: Mapping[str, Any], allowed: set[str]) -> None:
    unknown = set(body) - allowed
    if unknown:
        raise JournalError(f"unknown fields: {', '.join(sorted(unknown))}")


def _expected_revision(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise JournalError("expected_revision must be a positive integer")
    return value


def _event_id(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise JournalError("event_id must be a string")
    return value


def _authority(value: str) -> tuple[str, int | None]:
    raw = value.strip().lower()
    if not raw or any(char in raw for char in " /\\\r\n\0"):
        return "", None
    if raw.startswith("["):
        end = raw.find("]")
        if end < 0:
            return "", None
        host = raw[1:end]
        remainder = raw[end + 1:]
        if not remainder:
            return host, None
        if not remainder.startswith(":") or not remainder[1:].isdigit():
            return "", None
        return host, int(remainder[1:])
    if raw.count(":") == 1:
        host, port = raw.rsplit(":", 1)
        if port.isdigit():
            return host, int(port)
    if ":" in raw:
        # An IPv6 Host header must use brackets when it also carries a port.
        return raw, None
    return raw, None


def _is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return False
    return True


def _allowed_host(value: str) -> str:
    host, port = _authority(value)
    if not host or port is not None:
        raise ValueError(
            "allowed Host must be a hostname or IP literal without a port"
        )
    return host


def _allowed_host_argument(value: str) -> str:
    try:
        return _allowed_host(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _acquire_server_lock(data_root: Path):
    """Prevent a second server from reconciling another server's live jobs."""

    path = data_root / "server.lock"
    if path.is_symlink():
        raise RuntimeError(f"visualizer server lock may not be a symlink: {path}")
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError as exc:
        raise RuntimeError(f"could not safely open visualizer server lock: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        linked = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(linked.st_mode)
            or (opened.st_dev, opened.st_ino) != (linked.st_dev, linked.st_ino)
        ):
            raise RuntimeError(f"visualizer server lock is not a safe regular file: {path}")
        handle = os.fdopen(descriptor, "r+", encoding="utf-8")
        descriptor = -1
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except ImportError:
        # The Pi and development Macs both provide flock. On platforms that do
        # not, SQLite still protects journal integrity, but operators must not
        # run two servers against one data root.
        return handle
    except BlockingIOError as exc:
        handle.close()
        raise RuntimeError(
            f"another busybar-viz server owns {data_root}"
        ) from exc
    handle.seek(0)
    handle.truncate()
    handle.write(f"pid={os.getpid()}\n")
    handle.flush()
    return handle


def _verified_artifact(data_root: Path, artifact_id: str) -> VerifiedArtifact:
    if not _ARTIFACT_RE.fullmatch(artifact_id):
        raise FileNotFoundError("invalid artifact id")
    directory = data_root.resolve() / "artifacts" / artifact_id[:2] / artifact_id
    try:
        verified = verify_artifact(
            directory,
            full=True,
            expected_artifact_id=artifact_id,
        )
    except (OSError, ValueError) as exc:
        raise FileNotFoundError(f"artifact verification failed: {exc}") from exc
    if verified.path != directory.resolve():
        raise FileNotFoundError("artifact directory is not canonical")
    return verified


def _artifact_directory(data_root: Path, artifact_id: str) -> Path:
    return _verified_artifact(data_root, artifact_id).path


def _artifact_file(data_root: Path, artifact_id: str, logical: str) -> Path:
    verified = _verified_artifact(data_root, artifact_id)
    directory = verified.path
    logical_path = Path(logical)
    if (
        not logical
        or logical_path.is_absolute()
        or "\\" in logical
        or "\0" in logical
        or any(part in {"", ".", ".."} for part in logical_path.parts)
    ):
        raise FileNotFoundError("invalid artifact path")
    unresolved = directory / logical
    # Artifact publication never creates symlinks.  Rejecting them preserves
    # the content-addressed directory as the complete serving boundary even if
    # a local process tampers with the scratch tree later.
    current = unresolved
    while current != directory:
        if current.is_symlink():
            raise FileNotFoundError("artifact symlinks are not served")
        current = current.parent
    target = unresolved.resolve()
    try:
        target.relative_to(directory)
    except ValueError as exc:
        raise FileNotFoundError("artifact path escapes its bundle") from exc
    if not target.is_file():
        raise FileNotFoundError("artifact file not found")
    inventory = verified.manifest.get("files")
    inventory = inventory if isinstance(inventory, dict) else {}
    if logical != "manifest.json" and logical not in inventory:
        raise FileNotFoundError("file is not part of the published artifact")
    return target


def create_app(
    repo_root: Path,
    data_root: Path,
    *,
    journal: SessionJournal | None = None,
    jobs: JobManager | None = None,
    allow_remote: bool = False,
    allowed_hosts: Sequence[str] = (),
) -> FastAPI:
    """Build a testable app with no device or network initialization."""

    repo_root = repo_root.resolve()
    data_root = data_root.resolve()
    normalized_allowed_hosts = frozenset(
        _allowed_host(host) for host in allowed_hosts
    )
    data_root.mkdir(parents=True, exist_ok=True)
    server_lock = _acquire_server_lock(data_root)
    try:
        journal = journal or SessionJournal(data_root / "sessions.sqlite3")
        recovered_events = journal.reconcile_interrupted_renders()
        owns_jobs = jobs is None
        jobs = jobs or JobManager(repo_root, data_root)
    except BaseException:
        server_lock.close()
        raise
    render_request_lock = threading.Lock()

    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            try:
                if owns_jobs:
                    jobs.shutdown(wait=True)
            finally:
                server_lock.close()

    app = FastAPI(
        title="busybar-viz",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.repo_root = repo_root
    app.state.data_root = data_root
    app.state.journal = journal
    app.state.jobs = jobs
    app.state.allow_remote = allow_remote
    app.state.allowed_hosts = normalized_allowed_hosts
    app.state.recovered_render_count = len(recovered_events)
    app.state.server_lock = server_lock

    @app.exception_handler(RevisionConflict)
    async def revision_conflict(_request: Request, exc: RevisionConflict):
        return _error(
            409,
            str(exc),
            kind="revision_conflict",
            expected_revision=exc.expected,
            current_revision=exc.current,
        )

    @app.exception_handler(SessionNotFound)
    async def session_missing(_request: Request, exc: SessionNotFound):
        return _error(404, str(exc), kind="not_found")

    @app.exception_handler(JobNotFound)
    async def job_missing(_request: Request, exc: JobNotFound):
        return _error(404, str(exc), kind="not_found")

    @app.exception_handler(JobQueueFull)
    async def queue_full(_request: Request, exc: JobQueueFull):
        response = _error(429, str(exc), kind="queue_full")
        response.headers["Retry-After"] = "1"
        return response

    @app.exception_handler(IdempotencyConflict)
    async def idempotency_conflict(_request: Request, exc: IdempotencyConflict):
        return _error(409, str(exc), kind="idempotency_conflict")

    @app.exception_handler(JournalError)
    async def journal_error(_request: Request, exc: JournalError):
        return _error(422, str(exc), kind="invalid_request")

    @app.exception_handler(JobError)
    async def job_error(_request: Request, exc: JobError):
        return _error(422, str(exc), kind="invalid_request")

    @app.exception_handler(ValueError)
    @app.exception_handler(KeyError)
    async def value_error(_request: Request, exc: Exception):
        message = exc.args[0] if isinstance(exc, KeyError) and exc.args else str(exc)
        return _error(422, str(message), kind="invalid_request")

    @app.get("/api/health")
    def health() -> dict[str, object]:
        return {
            "ok": True,
            "offline": True,
            "device_access": False,
            "scenario_count": len(scenarios()),
            "recovered_render_count": len(recovered_events),
        }

    @app.get("/api/scenarios")
    def list_scenarios() -> dict[str, object]:
        return {"scenarios": [spec.as_dict() for spec in scenarios()]}

    @app.get("/api/sessions")
    def list_sessions(limit: int = 100) -> dict[str, object]:
        return {
            "sessions": [item.as_dict() for item in journal.list_sessions(limit=limit)]
        }

    @app.post("/api/sessions", status_code=201)
    def create_session(body: dict[str, Any]) -> dict[str, object]:
        _only(body, {"title", "event_id"})
        session, event = journal.create_session(
            body.get("title", ""),
            event_id=_event_id(body.get("event_id")),
        )
        return {"session": session.as_dict(), "event": event.as_dict()}

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str) -> dict[str, object]:
        return {"session": journal.get_session(session_id).as_dict()}

    @app.get("/api/sessions/{session_id}/events")
    def list_events(
        session_id: str,
        after_revision: int = 0,
        limit: int = 500,
    ) -> dict[str, object]:
        events = journal.list_events(
            session_id,
            after_revision=after_revision,
            limit=limit,
        )
        return {"events": [event.as_dict() for event in events]}

    @app.get("/api/sessions/{session_id}/export.jsonl")
    def export_session(session_id: str) -> StreamingResponse:
        # Validate before response headers begin; the generator then owns its
        # short-lived SQLite connection and streams every revision in order.
        journal.get_session(session_id)
        return StreamingResponse(
            journal.iter_jsonl(session_id),
            media_type="application/x-ndjson",
            headers={
                "Cache-Control": "no-store",
                "Content-Disposition": f'attachment; filename="{session_id}.jsonl"',
            },
        )

    def finish_job(record: JobRecord) -> None:
        for attempt in range(3):
            try:
                journal.finish_render(
                    record.session_id,
                    job_id=record.id,
                    status=record.state.value,
                    artifact_id=record.artifact_id,
                    passed=record.passed,
                    detail=record.error,
                )
                return
            except sqlite3.OperationalError:
                if attempt == 2:
                    raise
                time.sleep(0.05 * (attempt + 1))

    @app.post("/api/sessions/{session_id}/renders", status_code=202)
    def request_render(session_id: str, body: dict[str, Any]) -> dict[str, object]:
        _only(body, {"expected_revision", "event_id", "request"})
        expected = _expected_revision(body.get("expected_revision"))
        event_id = _event_id(body.get("event_id"))
        if event_id is None or (match := _EVENT_RE.fullmatch(event_id)) is None:
            raise JournalError("event_id must be evt_ followed by 32 lowercase hex digits")
        request_value = _object(body.get("request"), "request")
        request = RenderRequest.from_dict(request_value)
        job_id = f"job_{match.group(1)}"
        with render_request_lock:
            existing = journal.find_event(event_id)
            if existing is not None:
                if (
                    existing.session_id != session_id
                    or existing.kind != "render.requested"
                    or existing.body.get("job_id") != job_id
                    or existing.body.get("request") != request.as_dict()
                ):
                    raise IdempotencyConflict(f"event id was reused: {event_id}")
                try:
                    existing_job = jobs.get(job_id).as_dict()
                except JobNotFound:
                    existing_job = None
                return {
                    "session": journal.get_session(session_id).as_dict(),
                    "event": existing.as_dict(),
                    "job": existing_job,
                    "replayed": True,
                }

            journal.get_session(session_id)
            reservation = jobs.reserve(
                session_id,
                request,
                on_finish=finish_job,
                job_id=job_id,
            )
            try:
                session, event = journal.request_render(
                    session_id,
                    expected_revision=expected,
                    job_id=reservation.id,
                    request=request.as_dict(),
                    event_id=event_id,
                )
            except BaseException:
                jobs.cancel(reservation.id, notify=False)
                raise
            try:
                jobs.start(reservation.id)
            except BaseException as exc:
                # The request is already durable; a terminal event must explain
                # why it did not run. ``cancel`` invokes the completion hook.
                jobs.cancel(reservation.id)
                raise JobError(f"could not start render job: {exc}") from exc
            return {
                "session": session.as_dict(),
                "event": event.as_dict(),
                "job": jobs.get(reservation.id).as_dict(),
                "replayed": False,
            }

    @app.get("/api/jobs/{job_id}")
    def get_job(job_id: str) -> dict[str, object]:
        return {"job": jobs.get(job_id).as_dict()}

    @app.get("/api/sessions/{session_id}/jobs")
    def list_jobs(session_id: str) -> dict[str, object]:
        journal.get_session(session_id)
        return {"jobs": [record.as_dict() for record in jobs.list(session_id=session_id)]}

    def referenced_artifact(
        body: Mapping[str, Any],
        session_id: str,
        *,
        required: bool,
    ) -> tuple[str | None, bool]:
        explicit = body.get("artifact_id")
        if explicit is not None:
            if not isinstance(explicit, str):
                raise JournalError("artifact_id must be a string")
            try:
                _artifact_directory(data_root, explicit)
            except FileNotFoundError as exc:
                raise JournalError(str(exc)) from exc
            return explicit, False
        current = journal.get_session(session_id).current_artifact_id
        if required and current is None:
            raise JournalError("the session has no artifact to approve")
        if current is not None:
            try:
                _artifact_directory(data_root, current)
            except FileNotFoundError as exc:
                raise JournalError(str(exc)) from exc
        return None, current is not None

    @app.post("/api/sessions/{session_id}/feedback", status_code=201)
    def append_feedback(session_id: str, body: dict[str, Any]) -> dict[str, object]:
        _only(body, {"expected_revision", "event_id", "artifact_id", "message"})
        message = body.get("message")
        if not isinstance(message, str) or not message.strip():
            raise JournalError("feedback message must not be blank")
        message = message.strip()
        if len(message) > 4000 or "\0" in message:
            raise JournalError("feedback message exceeds 4000 characters")
        artifact_id, use_current = referenced_artifact(body, session_id, required=False)
        session, event = journal.append_event(
            session_id,
            expected_revision=_expected_revision(body.get("expected_revision")),
            kind="review.feedback",
            actor="user",
            body={"message": message},
            artifact_id=artifact_id,
            use_current_artifact=use_current,
            event_id=_event_id(body.get("event_id")),
        )
        return {"session": session.as_dict(), "event": event.as_dict()}

    @app.post("/api/sessions/{session_id}/approval", status_code=201)
    def append_approval(session_id: str, body: dict[str, Any]) -> dict[str, object]:
        _only(
            body,
            {
                "expected_revision",
                "event_id",
                "artifact_id",
                "approved",
                "note",
                "evidence_level",
            },
        )
        approved = body.get("approved")
        if not isinstance(approved, bool):
            raise JournalError("approved must be a boolean")
        note = body.get("note", "")
        if not isinstance(note, str) or len(note) > 4000 or "\0" in note:
            raise JournalError("approval note must be a string of at most 4000 characters")
        raw_evidence_level = body.get("evidence_level")
        evidence_level = None
        if raw_evidence_level is not None:
            try:
                evidence_level = EvidenceLevel(raw_evidence_level)
            except (TypeError, ValueError) as exc:
                raise JournalError("invalid evidence_level") from exc
            if evidence_level is not EvidenceLevel.GAP_PREVIEWED:
                raise JournalError(
                    "this offline review endpoint can only assert gap-previewed"
                )
        artifact_id, use_current = referenced_artifact(body, session_id, required=True)
        session, event = journal.append_event(
            session_id,
            expected_revision=_expected_revision(body.get("expected_revision")),
            kind="review.approved" if approved else "review.changes_requested",
            actor="user",
            body={
                "approved": approved,
                "note": note.strip(),
                **(
                    {"evidence_level": evidence_level.value}
                    if evidence_level is not None
                    else {}
                ),
            },
            artifact_id=artifact_id,
            use_current_artifact=use_current,
            event_id=_event_id(body.get("event_id")),
        )
        return {"session": session.as_dict(), "event": event.as_dict()}

    @app.get("/api/artifacts/{artifact_id}/{asset_path:path}")
    def artifact_file(artifact_id: str, asset_path: str):
        try:
            path = _artifact_file(data_root, artifact_id, asset_path)
        except FileNotFoundError as exc:
            return _error(404, str(exc), kind="not_found")
        return FileResponse(
            path,
            headers={
                "Cache-Control": "public, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )

    if STATIC_DIR.is_dir():
        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    @app.middleware("http")
    async def request_boundaries(request: Request, call_next):
        raw_host = request.headers.get("host") or ""
        host, host_port = _authority(raw_host)
        permitted = _LOOPBACK_HOSTS | app.state.allowed_hosts
        remote_ip = app.state.allow_remote and _is_ip_literal(host)
        if host not in permitted and not remote_ip:
            return _error(400, "Host is not allowed", kind="invalid_host")
        if request.method in _MUTATING:
            origin = request.headers.get("origin")
            if origin:
                try:
                    parsed = urlsplit(origin)
                    origin_host = (parsed.hostname or "").lower()
                    origin_port = parsed.port
                except ValueError:
                    return _error(403, "Origin is not allowed", kind="forbidden")
                default_port = 443 if request.url.scheme == "https" else 80
                expected_port = host_port or default_port
                actual_port = origin_port or (
                    443 if parsed.scheme == "https" else 80
                )
                same_request_host = origin_host == host
                local_origin = (
                    not app.state.allow_remote
                    and origin_host in _LOOPBACK_HOSTS
                )
                if (
                    parsed.scheme not in {"http", "https"}
                    or not (same_request_host or local_origin)
                    or actual_port != expected_port
                ):
                    return _error(403, "Origin is not allowed", kind="forbidden")
            content_type = (request.headers.get("content-type") or "").split(";", 1)[0]
            if content_type.strip().lower() != "application/json":
                return _error(403, "JSON content-type required", kind="forbidden")
            length = request.headers.get("content-length")
            if length is not None:
                try:
                    too_large = int(length) > MAX_JSON_BYTES
                except ValueError:
                    return _error(400, "invalid Content-Length", kind="invalid_request")
                if too_large:
                    return _error(413, "request exceeds the JSON budget", kind="too_large")
        response = await call_next(request)
        if request.url.path.startswith("/api/artifacts/"):
            return response
        response.headers["Cache-Control"] = "no-store" if request.url.path.startswith(
            "/api/"
        ) else "no-cache"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'"
        )
        return response

    app.add_middleware(_BodyLimitMiddleware, max_bytes=MAX_JSON_BYTES)

    return app


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m busybar_viz.server",
        description="Run the standalone busybar-viz collaboration UI.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--data-root", type=Path)
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help=(
            "allow binding outside loopback; the API has no authentication "
            "and still validates Host"
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        type=_allowed_host_argument,
        metavar="HOST",
        help=(
            "allow this exact DNS Host in remote mode (repeatable; direct IP "
            "literals need no entry)"
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_remote:
        print(
            "busybar-viz: refusing a non-loopback bind without --allow-remote",
            file=__import__("sys").stderr,
        )
        return 2
    import uvicorn

    repo_root = args.repo_root.resolve()
    data_root = (args.data_root or repo_root / "scratch" / "busybar-viz").resolve()
    uvicorn.run(
        create_app(
            repo_root,
            data_root,
            allow_remote=args.allow_remote,
            allowed_hosts=args.allowed_host,
        ),
        host=args.host,
        port=args.port,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

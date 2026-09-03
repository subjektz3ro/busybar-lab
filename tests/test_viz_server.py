import json
from pathlib import Path
import time

import pytest
from fastapi.testclient import TestClient

from busybar_viz import server as viz_server
from busybar_viz.artifacts import ArtifactStore
from busybar_viz.jobs import JobManager, WorkerResult
from busybar_viz.journal import JournalError
from busybar_viz.models import RenderRequest
from busybar_viz.registry import render_registered
from busybar_viz.server import _artifact_file, _parser, create_app

SCENARIO = "conformance/dual-display-input-replay"
REPO_ROOT = Path(__file__).parents[1]


def _artifact(data_root, seed="a" * 64):
    selector = int(seed[0], 16)
    request = RenderRequest.from_values(
        SCENARIO,
        parameters={
            "initial_level": selector % 9,
            "initial_mode": ("cyan", "amber", "violet")[selector % 3],
        },
    )
    published = ArtifactStore(data_root, REPO_ROOT).publish(
        request, render_registered(request),
    )
    return published.artifact_id


def _wait_for_job(client, job_id):
    for _ in range(100):
        job = client.get(f"/api/jobs/{job_id}").json()["job"]
        if job["state"] not in {"queued", "running", "finalizing"}:
            return job
        time.sleep(0.01)
    raise AssertionError("job did not finish")


def test_api_journals_render_feedback_and_approval(tmp_path):
    data_root = tmp_path / "viz"
    artifact_id = _artifact(data_root)
    manager = JobManager(
        tmp_path,
        data_root,
        max_workers=1,
        max_pending=2,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult(artifact_id, True),
    )
    app = create_app(tmp_path, data_root, jobs=manager, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            assert client.get("/api/health").json()["device_access"] is False
            scenario_ids = {
                item["id"] for item in client.get("/api/scenarios").json()["scenarios"]
            }
            assert SCENARIO in scenario_ids

            created = client.post("/api/sessions", json={"title": "Generic draft"})
            assert created.status_code == 201
            session = created.json()["session"]

            requested = client.post(
                f"/api/sessions/{session['id']}/renders",
                json={
                    "expected_revision": session["revision"],
                    "event_id": "evt_00000000000000000000000000000001",
                    "request": {"scenario_id": SCENARIO, "parameters": {}, "inputs": []},
                },
            )
            assert requested.status_code == 202
            job = _wait_for_job(client, requested.json()["job"]["id"])
            assert job["state"] == "succeeded"

            session = client.get(f"/api/sessions/{session['id']}").json()["session"]
            assert session["current_artifact_id"] == artifact_id

            feedback = client.post(
                f"/api/sessions/{session['id']}/feedback",
                json={
                    "expected_revision": session["revision"],
                    "message": "Make the motion clearer",
                },
            )
            assert feedback.status_code == 201
            assert feedback.json()["event"]["artifact_id"] == artifact_id
            session = feedback.json()["session"]

            approved = client.post(
                f"/api/sessions/{session['id']}/approval",
                json={
                    "expected_revision": session["revision"],
                    "approved": True,
                    "evidence_level": "gap-previewed",
                },
            )
            assert approved.status_code == 201
            assert approved.json()["event"]["kind"] == "review.approved"
            assert approved.json()["event"]["body"]["evidence_level"] == "gap-previewed"
            approved_session = approved.json()["session"]

            conflict = client.post(
                f"/api/sessions/{session['id']}/feedback",
                json={"expected_revision": 1, "message": "stale"},
            )
            assert conflict.status_code == 409
            assert conflict.json()["kind"] == "revision_conflict"
            assert conflict.json()["expected_revision"] == 1
            assert conflict.json()["current_revision"] == approved_session["revision"]

            exported = client.get(
                f"/api/sessions/{session['id']}/export.jsonl"
            )
            assert exported.status_code == 200
            kinds = [json.loads(line)["kind"] for line in exported.text.splitlines()]
            assert kinds == [
                "session.created",
                "render.requested",
                "render.completed",
                "review.feedback",
                "review.approved",
            ]
    finally:
        manager.shutdown()


def test_artifact_serving_is_immutable_and_confined(tmp_path):
    data_root = tmp_path / "viz"
    artifact_id = _artifact(data_root)
    (data_root / "secret.txt").write_text("do not serve")
    manager = JobManager(
        tmp_path,
        data_root,
        max_workers=1,
        max_pending=1,
        timeout_s=1,
        worker_runner=lambda _request, _timeout: WorkerResult(artifact_id, True),
    )
    app = create_app(tmp_path, data_root, jobs=manager, allowed_hosts=("testserver",))
    try:
        # Exercise the resolver directly because an ASGI server may normalize
        # an absolute or dot-segment URL before route matching.
        for logical in ("/etc/passwd", "//etc/passwd", "../secret.txt"):
            try:
                _artifact_file(data_root, artifact_id, logical)
            except FileNotFoundError:
                pass
            else:
                raise AssertionError(f"unsafe artifact path accepted: {logical}")
        with TestClient(app) as client:
            served = client.get(f"/api/artifacts/{artifact_id}/front.gif")
            assert served.status_code == 200
            assert "immutable" in served.headers["cache-control"]
            assert served.content.startswith(b"GIF")

            for path in (
                f"/api/artifacts/{artifact_id}/../secret.txt",
                f"/api/artifacts/{artifact_id}/%2e%2e/secret.txt",
                f"/api/artifacts/{artifact_id}/frames/%2e%2e/%2e%2e/secret.txt",
            ):
                response = client.get(path)
                assert response.status_code == 404
                assert b"do not serve" not in response.content

            assert client.post("/api/sessions", content=b"{}", headers={
                "content-type": "text/plain"
            }).status_code == 403
            assert "busybar-viz" in client.get("/").text

            artifact_dir = data_root / "artifacts" / artifact_id[:2] / artifact_id
            (artifact_dir / "front.gif").write_bytes(b"tampered")
            rejected = client.get(f"/api/artifacts/{artifact_id}/front.gif")
            assert rejected.status_code == 404
            assert rejected.json()["error"] == "artifact not found or failed verification"

            session = client.post(
                "/api/sessions", json={"title": "Tamper review"},
            ).json()["session"]
            review = client.post(
                f"/api/sessions/{session['id']}/feedback",
                json={
                    "expected_revision": session["revision"],
                    "artifact_id": artifact_id,
                    "message": "This must not bind to modified evidence",
                },
            )
            assert review.status_code == 422
            assert review.json()["error"] == "artifact not found or failed verification"

            session, _ = app.state.journal.present_artifact(
                session["id"],
                expected_revision=session["revision"],
                artifact_id=artifact_id,
            )
            implicit = client.post(
                f"/api/sessions/{session.id}/approval",
                json={"expected_revision": session.revision, "approved": True},
            )
            assert implicit.status_code == 422
            assert implicit.json()["error"] == "artifact not found or failed verification"
    finally:
        manager.shutdown()


def test_artifact_verification_details_are_logged_not_returned(
    tmp_path, monkeypatch, caplog,
):
    data_root = tmp_path / "viz"
    app = create_app(tmp_path, data_root, allowed_hosts=("testserver",))
    artifact_id = "a" * 64
    sentinel = "SECRET_ASSET /private/reviewer/file Traceback: verifier frame"

    def reject(*_args, **_kwargs):
        raise ValueError(sentinel)

    monkeypatch.setattr(viz_server, "verify_artifact", reject)

    with TestClient(app) as client, caplog.at_level(
        "WARNING", logger="busybar_viz.server"
    ):
        response = client.get(f"/api/artifacts/{artifact_id}/front.gif")

    assert response.status_code == 404
    assert response.json() == {
        "error": "artifact not found or failed verification",
        "kind": "not_found",
    }
    assert all(
        part not in response.text
        for part in ("SECRET_ASSET", "/private", "Traceback")
    )
    assert sentinel in caplog.text


def test_unapproved_exception_details_cannot_cross_the_api_boundary(
    tmp_path, monkeypatch, caplog,
):
    data_root = tmp_path / "viz"
    app = create_app(tmp_path, data_root, allowed_hosts=("testserver",))
    sentinel = "SECRET_DB /private/reviewer.sqlite Traceback: sqlite frame"

    def reject(*_args, **_kwargs):
        raise JournalError(sentinel)

    monkeypatch.setattr(app.state.journal, "create_session", reject)

    with TestClient(app) as client, caplog.at_level(
        "WARNING", logger="busybar_viz.server"
    ):
        response = client.post("/api/sessions", json={"title": "draft"})

    assert response.status_code == 422
    assert response.json() == {
        "error": "request is invalid",
        "kind": "invalid_request",
    }
    assert all(
        part not in response.text
        for part in ("SECRET_DB", "/private", "Traceback")
    )
    assert sentinel in caplog.text


def test_authored_validation_messages_remain_actionable(tmp_path):
    app = create_app(tmp_path, tmp_path / "viz", allowed_hosts=("testserver",))

    with TestClient(app) as client:
        blank = client.post("/api/sessions", json={"title": ""})
        unknown = client.post(
            "/api/sessions", json={"title": "draft", "unexpected": True}
        )

    assert blank.json()["error"] == "session title must not be blank"
    assert unknown.json()["error"] == "request contains unknown fields"


def test_render_request_event_id_recovers_a_lost_response(tmp_path):
    data_root = tmp_path / "viz"
    artifact_id = _artifact(data_root)
    manager = JobManager(
        tmp_path,
        data_root,
        max_workers=1,
        max_pending=2,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult(artifact_id, True),
    )
    app = create_app(tmp_path, data_root, jobs=manager, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            session = client.post(
                "/api/sessions", json={"title": "Retry-safe draft"},
            ).json()["session"]
            payload = {
                "expected_revision": session["revision"],
                "event_id": "evt_00000000000000000000000000000009",
                "request": {"scenario_id": SCENARIO},
            }
            first = client.post(
                f"/api/sessions/{session['id']}/renders", json=payload,
            )
            second = client.post(
                f"/api/sessions/{session['id']}/renders", json=payload,
            )
            assert first.status_code == second.status_code == 202
            assert second.json()["replayed"] is True
            assert second.json()["event"]["id"] == first.json()["event"]["id"]
            assert second.json()["job"]["id"] == first.json()["job"]["id"]
    finally:
        manager.shutdown()


def test_session_create_event_id_recovers_a_lost_response(tmp_path):
    data_root = tmp_path / "viz"
    app = create_app(tmp_path, data_root, allowed_hosts=("testserver",))
    event_id = "evt_0000000000000000000000000000000a"
    with TestClient(app) as client:
        payload = {"title": "Retry-safe create", "event_id": event_id}
        first = client.post("/api/sessions", json=payload)
        replayed = client.post("/api/sessions", json=payload)
        conflict = client.post(
            "/api/sessions",
            json={"title": "Contradictory create", "event_id": event_id},
        )

        assert first.status_code == replayed.status_code == 201
        assert replayed.json()["session"]["id"] == first.json()["session"]["id"]
        assert replayed.json()["event"]["id"] == first.json()["event"]["id"]
        assert conflict.status_code == 409
        assert conflict.json()["kind"] == "idempotency_conflict"


def test_review_level_is_optional_and_cannot_overclaim(tmp_path):
    data_root = tmp_path / "viz"
    artifact_id = _artifact(data_root)
    app = create_app(
        tmp_path,
        data_root,
        jobs=JobManager(
            tmp_path,
            data_root,
            max_workers=1,
            max_pending=1,
            timeout_s=1,
            worker_runner=lambda _request, _timeout: WorkerResult(artifact_id, True),
        ),
        allowed_hosts=("testserver",),
    )
    manager = app.state.jobs
    try:
        with TestClient(app) as client:
            session = client.post("/api/sessions", json={"title": "Evidence"}).json()["session"]
            render = client.post(
                f"/api/sessions/{session['id']}/renders",
                json={
                    "expected_revision": session["revision"],
                    "event_id": "evt_00000000000000000000000000000008",
                    "request": {"scenario_id": SCENARIO},
                },
            ).json()
            _wait_for_job(client, render["job"]["id"])
            session = client.get(f"/api/sessions/{session['id']}").json()["session"]

            overclaim = client.post(
                f"/api/sessions/{session['id']}/approval",
                json={
                    "expected_revision": session["revision"],
                    "approved": True,
                    "evidence_level": "hardware-observed",
                },
            )
            assert overclaim.status_code == 422

            decision = client.post(
                f"/api/sessions/{session['id']}/approval",
                json={"expected_revision": session["revision"], "approved": True},
            )
            assert decision.status_code == 201
            assert "evidence_level" not in decision.json()["event"]["body"]
    finally:
        manager.shutdown()


def test_default_host_origin_and_stream_body_boundaries(tmp_path):
    data_root = tmp_path / "viz"
    manager = JobManager(
        tmp_path,
        data_root,
        max_workers=1,
        max_pending=1,
        timeout_s=1,
        worker_runner=lambda _request, _timeout: WorkerResult("a" * 64, True),
    )
    app = create_app(tmp_path, data_root, jobs=manager, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            assert client.get("/api/health", headers={"host": "attacker.test"}).status_code == 400
            assert client.post(
                "/api/sessions",
                json={"title": "blocked"},
                headers={"origin": "https://attacker.test"},
            ).status_code == 403

            oversized = b'{"title":"' + b"x" * (300 * 1024) + b'"}'
            chunked = client.post(
                "/api/sessions",
                content=(part for part in (oversized[:100], oversized[100:])),
                headers={"content-type": "application/json"},
            )
            assert chunked.status_code == 413
            dishonest = client.post(
                "/api/sessions",
                content=oversized,
                headers={"content-type": "application/json", "content-length": "2"},
            )
            assert dishonest.status_code == 413
    finally:
        manager.shutdown()


def test_remote_mode_accepts_only_ip_literals_and_explicit_host_names(tmp_path):
    data_root = tmp_path / "viz"
    app = create_app(
        tmp_path,
        data_root,
        allow_remote=True,
        allowed_hosts=("reviews.example",),
    )

    with TestClient(app) as client:
        rebound = client.post(
            "/api/sessions",
            json={"title": "must not be created"},
            headers={
                "host": "attacker.test:8765",
                "origin": "http://attacker.test:8765",
            },
        )
        assert rebound.status_code == 400
        assert rebound.json()["kind"] == "invalid_host"

        assert client.get(
            "/api/health", headers={"host": "192.0.2.44:8765"}
        ).status_code == 200

        allowed = client.post(
            "/api/sessions",
            json={"title": "explicitly allowed reviewer"},
            headers={
                "host": "reviews.example:8765",
                "origin": "http://reviews.example:8765",
            },
        )
        assert allowed.status_code == 201


def test_remote_server_parser_accepts_repeatable_hosts_without_ports():
    args = _parser().parse_args([
        "--allow-remote",
        "--allowed-host", "reviews.example",
        "--allowed-host", "review-box.local",
    ])
    assert args.allowed_host == ["reviews.example", "review-box.local"]

    with pytest.raises(SystemExit):
        _parser().parse_args(["--allowed-host", "reviews.example:8765"])


def test_review_binds_to_explicitly_visible_artifact_not_new_current(tmp_path):
    data_root = tmp_path / "viz"
    artifact_a = _artifact(data_root, "a" * 64)
    artifact_b = _artifact(data_root, "b" * 64)
    results = iter((artifact_a, artifact_b))
    manager = JobManager(
        tmp_path,
        data_root,
        max_workers=1,
        max_pending=2,
        timeout_s=2,
        worker_runner=lambda _request, _timeout: WorkerResult(next(results), True),
    )
    app = create_app(tmp_path, data_root, jobs=manager, allowed_hosts=("testserver",))
    try:
        with TestClient(app) as client:
            session = client.post("/api/sessions", json={"title": "Artifact race"}).json()["session"]
            for suffix in ("01", "02"):
                render = client.post(
                    f"/api/sessions/{session['id']}/renders",
                    json={
                        "expected_revision": session["revision"],
                        "event_id": f"evt_{'0' * 30}{suffix}",
                        "request": {"scenario_id": SCENARIO},
                    },
                ).json()
                _wait_for_job(client, render["job"]["id"])
                session = client.get(
                    f"/api/sessions/{session['id']}"
                ).json()["session"]
            assert session["current_artifact_id"] == artifact_b

            decision = client.post(
                f"/api/sessions/{session['id']}/approval",
                json={
                    "expected_revision": session["revision"],
                    "approved": True,
                    "artifact_id": artifact_a,
                },
            )
            assert decision.status_code == 201
            assert decision.json()["event"]["artifact_id"] == artifact_a
            assert decision.json()["session"]["current_artifact_id"] == artifact_b
    finally:
        manager.shutdown()


def test_data_root_allows_only_one_reconciling_server(tmp_path):
    data_root = tmp_path / "viz"
    app = create_app(tmp_path, data_root, allowed_hosts=("testserver",))
    with TestClient(app):
        with pytest.raises(RuntimeError, match="another busybar-viz server"):
            create_app(tmp_path, data_root, allowed_hosts=("testserver",))


def test_server_lock_symlink_is_rejected_without_clobbering_target(tmp_path):
    data_root = tmp_path / "viz"
    data_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("must survive")
    (data_root / "server.lock").symlink_to(outside)

    with pytest.raises(RuntimeError, match="lock may not be a symlink"):
        create_app(tmp_path, data_root, allowed_hosts=("testserver",))

    assert outside.read_text() == "must survive"

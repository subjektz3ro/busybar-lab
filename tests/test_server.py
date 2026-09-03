import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from barkeep import tls as barkeep_tls
from barkeep import server as barkeep_server
from barkeep.preview import BarOffline
from barkeep.registry import AppSpec, ConfigKey
from barkeep.server import MAX_JSON_BYTES, create_app, tls_upload_allowed
from barkeep.statestore import load_state


class FakeSupervisor:
    """Same surface as barkeep.supervisor.Supervisor, no processes."""

    def __init__(self, registry):
        self.registry = registry
        self.foreground = None
        self.switching = False
        self.enabled = set()
        self.restarted = []

    async def set_foreground(self, name):
        if name is not None:
            spec = self.registry[name]
            if spec.kind != "foreground":
                raise ValueError(f"{name} is not a foreground app")
        self.foreground = name

    async def enable(self, name):
        if self.registry[name].kind != "background":
            raise ValueError("wrong kind")
        self.enabled.add(name)

    async def disable(self, name):
        if self.registry[name].kind != "background":
            raise ValueError("wrong kind")
        self.enabled.discard(name)

    async def restart(self, name):
        self.registry[name]
        self.restarted.append(name)

    def enabled_backgrounds(self):
        return set(self.enabled)

    def status(self):
        return [{"name": n, "kind": s.kind, "description": s.description,
                 "status": "stopped", "crash_looping": False, "restarts": 0,
                 "pid": None, "uptime_s": None} for n, s in self.registry.items()]

    def logs(self, name, lines=200):
        self.registry[name]
        return ["line one", "line two"][-lines:]


class FakePreview:
    def __init__(self, offline=False):
        self.offline = offline

    def png(self, display):
        if self.offline:
            raise BarOffline("no bar")
        return b"\x89PNG fake"


JSON = {"content-type": "application/json"}

SCENES = ("house", "skyline", "forest")

REGISTRY = {
    "sky": AppSpec("sky", "foreground", "apps/sky.py", "the sky",
                   (ConfigKey("SKY_VOICE", "voice", "am_michael"),
                    ConfigKey("SKY_CONTACT", "contact, blank = anonymous", "",
                              blank_is_value=True),
                    ConfigKey("SKY_SCENES", "scenes to cycle",
                              ",".join(SCENES), "multiselect", SCENES),
                    ConfigKey("SKY_UNITS", "units", "f", "enum", ("f", "c")),
                    ConfigKey("SKY_RATE", "poll rate", "10", "number"))),
    "pinger": AppSpec("pinger", "background", "apps/pinger.py", "pings"),
}

SKYSTRIP_REGISTRY = {
    "skystrip": AppSpec(
        "skystrip", "foreground", "apps/skystrip.py", "the sky",
        (ConfigKey("SKYSTRIP_LAT", "latitude", "", "number",
                   minimum=-90, maximum=90, requires=("SKYSTRIP_LON",)),
         ConfigKey("SKYSTRIP_LON", "longitude", "", "number",
                   minimum=-180, maximum=180, requires=("SKYSTRIP_LAT",)),
         ConfigKey("SKYSTRIP_TZ", "timezone", "UTC", format="timezone"),
         ConfigKey("SKYSTRIP_UNITS", "units", "f", "enum", ("f", "c")),
         ConfigKey("SKYSTRIP_CONTACT", "contact", "")),
    ),
}


def make_client(tmp_path: Path, offline=False):
    sup = FakeSupervisor(REGISTRY)
    app = create_app(sup, REGISTRY, FakePreview(offline), tmp_path / "config",
                     tmp_path / "config" / "barkeep-state.json")
    # An IP literal, not TestClient's default `Host: testserver`. The server
    # refuses names it is not, and a test suite that silently
    # exercised an exempt hostname would not be testing the deployed path.
    return (TestClient(app, base_url="http://127.0.0.1:8080"), sup,
            tmp_path / "config")


def make_skystrip_client(tmp_path: Path, monkeypatch):
    for key in ("SKYSTRIP_LAT", "SKYSTRIP_LON", "SKYSTRIP_TZ",
                "SKYSTRIP_UNITS", "SKYSTRIP_CONTACT"):
        monkeypatch.delenv(key, raising=False)
    sup = FakeSupervisor(SKYSTRIP_REGISTRY)
    config_dir = tmp_path / "config"
    app = create_app(sup, SKYSTRIP_REGISTRY, FakePreview(), config_dir,
                     config_dir / "barkeep-state.json")
    # IP literal, not TestClient's default `Host: testserver` — the server
    # refuses names it is not.
    return TestClient(app, base_url="http://127.0.0.1:8080"), config_dir


def test_state_blob(tmp_path):
    client, _, _ = make_client(tmp_path)
    body = client.get("/api/state").json()
    assert body["foreground"] is None
    assert {a["name"] for a in body["apps"]} == {"sky", "pinger"}


def test_foreground_set_and_persisted(tmp_path):
    client, sup, config_dir = make_client(tmp_path)
    assert client.post("/api/foreground", json={"app": "sky"}).status_code == 200
    assert sup.foreground == "sky"
    saved = load_state(config_dir / "barkeep-state.json")
    assert saved.foreground == "sky"
    assert client.post("/api/foreground", json={"app": None}).status_code == 200
    assert sup.foreground is None


def test_foreground_wrong_kind_409_unknown_404(tmp_path):
    client, _, _ = make_client(tmp_path)
    assert client.post("/api/foreground", json={"app": "pinger"}).status_code == 409
    assert client.post("/api/foreground", json={"app": "nope"}).status_code == 404


def test_operation_exception_details_are_logged_not_returned(
    tmp_path, monkeypatch, caplog,
):
    client, sup, _ = make_client(tmp_path)
    sentinel = "SECRET_TOKEN /private/operator/path Traceback: internal frame"

    async def reject(_name):
        raise ValueError(sentinel)

    monkeypatch.setattr(sup, "enable", reject)

    with caplog.at_level("WARNING", logger="barkeep.server"):
        response = client.post("/api/apps/pinger/enable", headers=JSON)

    assert response.status_code == 409
    assert response.json() == {"error": "operation is not valid for this app"}
    assert all(part not in response.text for part in ("SECRET_TOKEN", "/private", "Traceback"))
    assert sentinel in caplog.text


def test_background_toggle(tmp_path):
    client, sup, _ = make_client(tmp_path)
    assert client.post("/api/apps/pinger/enable", headers=JSON).status_code == 200
    assert sup.enabled == {"pinger"}
    assert client.post("/api/apps/pinger/disable", headers=JSON).status_code == 200
    assert sup.enabled == set()
    assert client.post("/api/apps/sky/enable", headers=JSON).status_code == 409


def test_logs(tmp_path):
    client, _, _ = make_client(tmp_path)
    assert client.get("/api/apps/sky/logs").json()["lines"] == ["line one", "line two"]
    assert client.get("/api/apps/nope/logs").status_code == 404


def test_config_get_put(tmp_path):
    client, _, config_dir = make_client(tmp_path)
    keys = client.get("/api/apps/sky/config").json()["keys"]
    assert keys[0]["name"] == "SKY_VOICE" and keys[0]["source"] == "default"

    resp = client.put("/api/apps/sky/config",
                      json={"values": {"SKY_VOICE": "am_michael"}})
    assert resp.status_code == 200
    assert resp.json()["keys"][0]["source"] == "app"
    assert "SKY_VOICE=am_michael" in (config_dir / "sky.env").read_text()

    bad = client.put("/api/apps/sky/config", json={"values": {"HACK": "x"}})
    assert bad.status_code == 422


def test_config_file_uses_the_canonical_registry_spec_name(tmp_path):
    spec = REGISTRY["sky"]
    registry = {"route_alias": spec}
    sup = FakeSupervisor(registry)
    config_dir = tmp_path / "config"
    app = create_app(
        sup,
        registry,
        FakePreview(),
        config_dir,
        config_dir / "barkeep-state.json",
    )
    client = TestClient(app, base_url="http://127.0.0.1:8080")

    response = client.put(
        "/api/apps/route_alias/config",
        json={"values": {"SKY_VOICE": "am_michael"}},
    )

    assert response.status_code == 200
    assert (config_dir / "sky.env").is_file()
    assert not (config_dir / "route_alias.env").exists()


@pytest.mark.parametrize(
    "encoded_name",
    (
        "%2e%2e",
        "%252e%252e",
        "sky%2F..%2F..%2Fescape",
        "sky%5C..%5Cescape",
        ".hidden",
    ),
)
def test_malicious_route_names_cannot_select_config_paths(
    tmp_path, encoded_name,
):
    client, _, config_dir = make_client(tmp_path)
    outside = tmp_path / "escape.env"
    outside.write_text("SECRET=must-survive\n")

    response = client.put(
        f"/api/apps/{encoded_name}/config",
        json={"values": {"SKY_VOICE": "attacker"}},
    )

    assert response.status_code in {404, 405}
    assert outside.read_text() == "SECRET=must-survive\n"
    assert not any(path.name != "escape.env" for path in tmp_path.glob("*.env"))
    assert not config_dir.exists() or not list(config_dir.glob("*.env"))


def test_put_config_rejects_multiline_values(tmp_path):
    """A newline would forge extra env vars in the next child process."""
    client, _, config_dir = make_client(tmp_path)
    client.put("/api/apps/sky/config", json={"values": {"SKY_VOICE": "good"}})
    before = (config_dir / "sky.env").read_bytes()

    resp = client.put("/api/apps/sky/config",
                      json={"values": {"SKY_VOICE": "x\nBUSYBAR_HOST=evil.example"}})
    assert resp.status_code == 422
    assert (config_dir / "sky.env").read_bytes() == before   # untouched

    assert client.put("/api/apps/sky/config",
                      json={"values": "not-an-object"}).status_code == 422


def test_blank_clears_only_non_blankable_keys(tmp_path):
    """Blank means 'inherit' — unless the key documents blank as a value."""
    client, _, config_dir = make_client(tmp_path)
    client.put("/api/apps/sky/config",
               json={"values": {"SKY_VOICE": "am_michael", "SKY_CONTACT": "me@x"}})

    # SKY_CONTACT explicitly declares blank as a value, so it persists.
    rows = client.put("/api/apps/sky/config",
                      json={"values": {"SKY_CONTACT": ""}}).json()["keys"]
    contact = next(r for r in rows if r["name"] == "SKY_CONTACT")
    assert contact["source"] == "app" and contact["value"] == ""
    assert "SKY_CONTACT=" in (config_dir / "sky.env").read_text()

    # SKY_VOICE has a real default — blanking it removes the override.
    rows = client.put("/api/apps/sky/config",
                      json={"values": {"SKY_VOICE": ""}}).json()["keys"]
    voice = next(r for r in rows if r["name"] == "SKY_VOICE")
    assert voice["source"] == "default" and voice["value"] == "am_michael"
    assert "SKY_VOICE" not in (config_dir / "sky.env").read_text()


def test_multiselect_stores_a_canonical_list(tmp_path):
    """Click order and duplicates must not change what gets stored."""
    client, _, config_dir = make_client(tmp_path)
    resp = client.put("/api/apps/sky/config",
                      json={"values": {"SKY_SCENES": "forest, house ,forest"}})
    assert resp.status_code == 200
    assert "SKY_SCENES=house,forest" in (config_dir / "sky.env").read_text()


def test_multiselect_rejects_unknown_and_empty(tmp_path):
    client, _, config_dir = make_client(tmp_path)
    client.put("/api/apps/sky/config", json={"values": {"SKY_SCENES": "house"}})
    before = (config_dir / "sky.env").read_text()

    bad = client.put("/api/apps/sky/config",
                     json={"values": {"SKY_SCENES": "house,atlantis"}})
    assert bad.status_code == 422
    assert "atlantis" in bad.json()["error"]

    empty = client.put("/api/apps/sky/config",
                       json={"values": {"SKY_SCENES": " , "}})
    assert empty.status_code == 422
    assert "at least one" in empty.json()["error"]

    assert (config_dir / "sky.env").read_text() == before   # nothing written


@pytest.mark.parametrize(("key", "value", "message"), [
    ("SKY_UNITS", "kelvin", "must be one of"),
    ("SKY_RATE", "not-a-number", "finite number"),
    ("SKY_RATE", "nan", "finite number"),
    ("SKY_RATE", "inf", "finite number"),
    ("SKY_RATE", "-inf", "finite number"),
])
def test_config_rejects_invalid_enum_and_nonfinite_number_atomically(
    tmp_path, key, value, message
):
    client, _, config_dir = make_client(tmp_path)
    client.put("/api/apps/sky/config", json={"values": {"SKY_VOICE": "good"}})
    path = config_dir / "sky.env"
    before = path.read_bytes()

    response = client.put(
        "/api/apps/sky/config", json={"values": {key: value}})

    assert response.status_code == 422
    assert message in response.json()["error"]
    assert path.read_bytes() == before


def test_scalar_validation_preserves_blank_as_inherit(tmp_path):
    client, _, config_dir = make_client(tmp_path)
    assert client.put(
        "/api/apps/sky/config",
        json={"values": {"SKY_UNITS": "c", "SKY_RATE": "1e3"}},
    ).status_code == 200

    response = client.put(
        "/api/apps/sky/config",
        json={"values": {"SKY_UNITS": "", "SKY_RATE": ""}},
    )

    assert response.status_code == 200
    body = (config_dir / "sky.env").read_text()
    assert "SKY_UNITS" not in body
    assert "SKY_RATE" not in body


@pytest.mark.parametrize(("values", "message"), [
    ({"SKYSTRIP_LAT": "10"}, "configured together"),
    ({"SKYSTRIP_LAT": "91", "SKYSTRIP_LON": "0"}, "between -90 and 90"),
    ({"SKYSTRIP_LAT": "0", "SKYSTRIP_LON": "-181"}, "between -180 and 180"),
    ({"SKYSTRIP_LAT": "nan", "SKYSTRIP_LON": "0"}, "finite number"),
])
def test_skystrip_rejects_incomplete_or_out_of_range_coordinates(
    tmp_path, monkeypatch, values, message
):
    client, config_dir = make_skystrip_client(tmp_path, monkeypatch)

    response = client.put(
        "/api/apps/skystrip/config", json={"values": values})

    assert response.status_code == 422
    assert message in response.json()["error"]
    assert not (config_dir / "skystrip.env").exists()


def test_skystrip_accepts_zero_coordinates_and_validates_partial_updates(
    tmp_path, monkeypatch
):
    client, config_dir = make_skystrip_client(tmp_path, monkeypatch)
    response = client.put(
        "/api/apps/skystrip/config",
        json={"values": {"SKYSTRIP_LAT": "0", "SKYSTRIP_LON": "0"}},
    )
    assert response.status_code == 200

    response = client.put(
        "/api/apps/skystrip/config",
        json={"values": {"SKYSTRIP_LAT": "51.5074"}},
    )
    assert response.status_code == 200
    body = (config_dir / "skystrip.env").read_text()
    assert "SKYSTRIP_LAT=51.5074" in body
    assert "SKYSTRIP_LON=0" in body


def test_blank_skystrip_coordinates_remove_overrides_and_reveal_shared(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("SKYSTRIP_LAT", "51.5074")
    monkeypatch.setenv("SKYSTRIP_LON", "-0.1278")
    monkeypatch.delenv("SKYSTRIP_TZ", raising=False)
    monkeypatch.delenv("SKYSTRIP_UNITS", raising=False)
    monkeypatch.delenv("SKYSTRIP_CONTACT", raising=False)
    sup = FakeSupervisor(SKYSTRIP_REGISTRY)
    config_dir = tmp_path / "config"
    app = create_app(sup, SKYSTRIP_REGISTRY, FakePreview(), config_dir,
                     config_dir / "barkeep-state.json")
    client = TestClient(app, base_url="http://127.0.0.1:8080")

    assert client.put(
        "/api/apps/skystrip/config",
        json={"values": {"SKYSTRIP_LAT": "0", "SKYSTRIP_LON": "0"}},
    ).status_code == 200
    response = client.put(
        "/api/apps/skystrip/config",
        json={"values": {"SKYSTRIP_LAT": "", "SKYSTRIP_LON": ""}},
    )

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()["keys"]}
    assert rows["SKYSTRIP_LAT"]["source"] == "shared"
    assert rows["SKYSTRIP_LAT"]["value"] == "51.5074"
    assert rows["SKYSTRIP_LON"]["source"] == "shared"
    body = (config_dir / "skystrip.env").read_text()
    assert "SKYSTRIP_LAT" not in body
    assert "SKYSTRIP_LON" not in body


def test_skystrip_rejects_unknown_timezone_before_writing(tmp_path, monkeypatch):
    client, config_dir = make_skystrip_client(tmp_path, monkeypatch)
    for timezone in ("Mars/Olympus", "A" * 300):
        response = client.put(
            "/api/apps/skystrip/config",
            json={"values": {"SKYSTRIP_TZ": timezone}},
        )
        assert response.status_code == 422
        assert "unknown IANA timezone" in response.json()["error"]
        assert not (config_dir / "skystrip.env").exists()

    assert client.put(
        "/api/apps/skystrip/config",
        json={"values": {"SKYSTRIP_TZ": "Etc/UTC"}},
    ).status_code == 200


def test_mutations_require_a_json_content_type(tmp_path):
    """A form POST from any LAN page must not be able to drive the bar."""
    client, sup, _ = make_client(tmp_path)
    assert client.post("/api/apps/sky/restart",
                       data={"x": "1"}).status_code == 403
    assert client.post("/api/apps/sky/restart").status_code == 403
    assert sup.restarted == []
    assert client.post("/api/apps/sky/restart", headers=JSON).status_code == 200
    assert sup.restarted == ["sky"]
    # Reads stay open: the UI polls them constantly.
    assert client.get("/api/state").status_code == 200


def test_preview_ok_and_offline(tmp_path):
    client, _, _ = make_client(tmp_path)
    ok = client.get("/api/preview/0")
    assert ok.status_code == 200
    assert ok.headers["content-type"] == "image/png"

    offline_client, _, _ = make_client(tmp_path, offline=True)
    assert offline_client.get("/api/preview/0").status_code == 503


def test_responses_deny_framing_and_content_sniffing(tmp_path):
    client, _, _ = make_client(tmp_path)

    for response in (
        client.get("/"),
        client.get("/api/state"),
        client.get("/api/state", headers={"host": "attacker.test"}),
    ):
        assert response.headers["x-frame-options"] == "DENY"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert "frame-ancestors 'none'" in response.headers[
            "content-security-policy"
        ]


# --- Host validation --------------------------------------------------------
#
# Verified against the running daemon before this existed: a request carrying
# `Host: evil.example.com` from another machine on the LAN returned 200. That
# is the precondition for DNS rebinding, and rebinding is the one browser-borne
# path the JSON content-type rule cannot close — once the attacker's name
# resolves here their page is same-origin, so it sends application/json with no
# preflight and the rule is satisfied honestly.


def test_an_unknown_host_name_is_refused(tmp_path):
    client, _, _ = make_client(tmp_path)
    response = client.get("/api/state", headers={"host": "evil.example.com"})
    assert response.status_code == 421
    assert "Host" in response.json()["error"]


def test_rebinding_cannot_ride_a_correct_content_type(tmp_path):
    """The mutation guard passes for this request; the Host rule is what stops
    it. Without the Host check this is exactly the request a rebound page
    sends, and it used to succeed."""
    client, sup, _ = make_client(tmp_path)
    response = client.post("/api/foreground", json={"app": "sky"},
                           headers={"host": "attacker.test"})
    assert response.status_code == 421
    assert sup.foreground is None, "the mutation must not have been applied"


def test_ip_literals_are_allowed_because_they_cannot_be_rebound(tmp_path):
    client, _, _ = make_client(tmp_path)
    for host in ("198.51.100.7:8080", "127.0.0.1:8080", "[::1]:8080", "192.0.2.1"):
        assert client.get("/api/state", headers={"host": host}).status_code == 200, host


def test_localhost_and_this_machines_own_names_are_allowed(tmp_path):
    import socket

    short = socket.gethostname().split(".")[0]
    client, _, _ = make_client(tmp_path)
    for host in ("localhost", "localhost:8080", short, f"{short}.local"):
        assert client.get("/api/state", headers={"host": host}).status_code == 200, host


def test_an_operator_can_name_extra_hosts(tmp_path, monkeypatch):
    monkeypatch.setenv("BARKEEP_ALLOWED_HOSTS", "bar.example.net, Pi.Home ")
    client, _, _ = make_client(tmp_path)
    assert client.get("/api/state",
                      headers={"host": "bar.example.net"}).status_code == 200
    # Matching is case-insensitive and tolerates whitespace in the setting.
    assert client.get("/api/state",
                      headers={"host": "PI.HOME:8080"}).status_code == 200
    assert client.get("/api/state",
                      headers={"host": "other.example.net"}).status_code == 421


def test_host_parsing_handles_ports_and_ipv6():
    from barkeep.server import host_name

    assert host_name("Example.COM:8080") == "example.com"
    assert host_name("[fe80::1]:8080") == "fe80::1"
    assert host_name("[::1]") == "::1"
    # A bare IPv6 literal has no port to strip and must survive intact.
    assert host_name("fe80::1") == "fe80::1"
    assert host_name("  pi.local  ") == "pi.local"


def test_a_missing_host_header_is_refused(tmp_path):
    from barkeep.server import host_allowed

    assert not host_allowed("", frozenset(), frozenset({"localhost"}))


# --- authentication (opt-in, for anything beyond a trusted LAN) -------------
#
# barkeep parents processes and writes config that becomes their environment.
# Unauthenticated is a defensible trade on a network you own — the bar itself
# has no auth there either — and a bad one anywhere else. So it is opt-in and
# off by default, and the daemon says so at startup when it is exposed.


def _token_client(tmp_path, monkeypatch, token="s3cret-token"):
    monkeypatch.setenv("BARKEEP_TOKEN", token)
    sup = FakeSupervisor(REGISTRY)
    app = create_app(sup, REGISTRY, FakePreview(), tmp_path / "config",
                     tmp_path / "config" / "barkeep-state.json")
    return TestClient(app, base_url="http://127.0.0.1:8080"), sup, token


def test_without_a_token_everything_still_works(tmp_path, monkeypatch):
    """Off by default. A homelab install must not grow a password for nothing."""
    monkeypatch.delenv("BARKEEP_TOKEN", raising=False)
    client, _, _ = make_client(tmp_path)
    assert client.get("/api/state").status_code == 200


def test_a_protected_server_refuses_an_anonymous_read(tmp_path, monkeypatch):
    """The READ routes are the ones that leak — framebuffer and app logs — so
    there is no partially-protected mode."""
    client, _, _ = _token_client(tmp_path, monkeypatch)
    for path in ("/api/state", "/api/apps/sky/logs", "/api/preview/0"):
        assert client.get(path).status_code == 401, path


def test_a_protected_server_refuses_an_anonymous_mutation(tmp_path, monkeypatch):
    client, sup, _ = _token_client(tmp_path, monkeypatch)
    response = client.post("/api/foreground", json={"app": "sky"})
    assert response.status_code == 401
    assert sup.foreground is None, "the mutation must not have been applied"


@pytest.mark.parametrize("header", [
    lambda t: {"authorization": f"Bearer {t}"},
    lambda t: {"x-barkeep-token": t},
])
def test_either_header_carries_the_credential(tmp_path, monkeypatch, header):
    client, _, token = _token_client(tmp_path, monkeypatch)
    assert client.get("/api/state", headers=header(token)).status_code == 200


def test_a_wrong_token_is_refused(tmp_path, monkeypatch):
    client, _, _ = _token_client(tmp_path, monkeypatch)
    assert client.get(
        "/api/state",
        headers={"authorization": "Bearer wrong"}).status_code == 401


def test_the_session_endpoint_exchanges_a_token_for_a_cookie(tmp_path, monkeypatch):
    """The UI's preview panes are <img> tags and cannot attach a header."""
    client, _, token = _token_client(tmp_path, monkeypatch)
    assert client.post("/api/session", json={"token": "wrong"}).status_code == 401
    opened = client.post("/api/session", json={"token": token})
    assert opened.status_code == 200
    # The cookie is now on the client; a bare <img>-style GET must work.
    assert client.get("/api/preview/0").status_code == 200


def _oversized_session_body() -> bytes:
    return b'{"token":"' + (b"x" * MAX_JSON_BYTES) + b'"}'


def test_session_rejects_an_honestly_declared_oversized_body(
    tmp_path, monkeypatch,
):
    client, _, _ = _token_client(tmp_path, monkeypatch)
    body = _oversized_session_body()

    response = client.post(
        "/api/session",
        content=body,
        headers={
            "content-type": "application/json",
            "content-length": str(len(body)),
        },
    )

    assert response.status_code == 413
    assert response.json()["error"] == "request exceeds the JSON budget"
    assert response.headers["x-frame-options"] == "DENY"


def test_session_rejects_an_oversized_chunked_body_without_content_length(
    tmp_path, monkeypatch,
):
    client, _, _ = _token_client(tmp_path, monkeypatch)
    body = _oversized_session_body()

    response = client.post(
        "/api/session",
        content=(chunk for chunk in (body[:100], body[100:])),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"] == "request exceeds the JSON budget"


def test_session_stream_limit_ignores_a_dishonest_small_content_length(
    tmp_path, monkeypatch,
):
    client, _, _ = _token_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/session",
        content=_oversized_session_body(),
        headers={
            "content-type": "application/json",
            "content-length": "2",
        },
    )

    assert response.status_code == 413


def test_session_replays_a_bounded_chunked_body_to_fastapi(tmp_path, monkeypatch):
    client, _, token = _token_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/session",
        content=(
            chunk for chunk in (b'{"token":"', token.encode("utf-8"), b'"}')
        ),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 200


def test_the_session_cookie_is_httponly_and_samesite(tmp_path, monkeypatch):
    client, _, token = _token_client(tmp_path, monkeypatch)
    response = client.post("/api/session", json={"token": token})
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie, "script-readable credential"
    assert "samesite=strict" in cookie, "cookie would ride cross-site requests"


def test_the_session_cookie_is_secure_over_https(tmp_path, monkeypatch):
    """Served over TLS, the cookie must refuse to travel back in plaintext."""
    client, _, token = _token_client(tmp_path, monkeypatch)
    https = TestClient(client.app, base_url="https://127.0.0.1:8080")
    response = https.post("/api/session", json={"token": token})
    assert "secure" in response.headers["set-cookie"].lower()


def test_the_session_cookie_stays_usable_over_plain_http(tmp_path, monkeypatch):
    """A Secure cookie set over http would be discarded by the browser and
    lock the UI into an endless token prompt, so the flag must track the
    scheme actually serving the request."""
    client, _, token = _token_client(tmp_path, monkeypatch)
    response = client.post("/api/session", json={"token": token})
    assert "secure" not in response.headers["set-cookie"].lower()


def test_the_session_endpoint_is_absent_when_no_token_is_configured(
    tmp_path, monkeypatch,
):
    monkeypatch.delenv("BARKEEP_TOKEN", raising=False)
    client, _, _ = make_client(tmp_path)
    assert client.post("/api/session", json={"token": "x"}).status_code == 404


def test_static_assets_stay_reachable_so_the_ui_can_prompt(tmp_path, monkeypatch):
    """A 401 on index.html would leave nowhere to type the token."""
    client, _, _ = _token_client(tmp_path, monkeypatch)
    assert client.get("/").status_code in (200, 404)


# --- the startup warning ----------------------------------------------------


@pytest.mark.parametrize("bind,token,warns", [
    ("0.0.0.0", "", True),
    ("::", "", True),
    ("192.0.2.10", "", True),
    ("0.0.0.0", "s3cret", False),
    ("127.0.0.1", "", False),
    ("::1", "", False),
    ("localhost", "", False),
])
def test_exposure_is_announced_exactly_when_it_matters(bind, token, warns):
    from barkeep.server import exposure_warning

    warning = exposure_warning(bind, token)
    assert bool(warning) is warns, (bind, token, warning)
    if warns:
        assert "BARKEEP_TOKEN" in warning and "BARKEEP_BIND" in warning


def test_the_ui_only_links_to_external_provider_credits():
    """The control plane must render identically on an air-gapped LAN: no
    CDN fonts, external scripts, or other hosts the page phones on load.
    Ordinary anchor links for mandatory provider credits are inert until
    selected.

    data: URIs are exempt — the inline noise-texture SVG carries the W3C
    namespace as an identifier, which no browser ever fetches."""
    import re
    from html.parser import HTMLParser

    static = Path(__file__).resolve().parent.parent / "barkeep" / "static"
    allowed_links = {
        "https://open-meteo.com/en/terms",
        "https://creativecommons.org/licenses/by/4.0/",
        "https://www.rainviewer.com/api.html",
    }
    for name in ("app.js", "style.css"):
        text = re.sub(r'data:[^"]*', "", (static / name).read_text())
        assert "https://" not in text and "http://" not in text, (
            f"{name} references an external origin")

    html = (static / "index.html").read_text()

    class ExternalAttributeParser(HTMLParser):
        def __init__(self):
            super().__init__()
            self.external: list[tuple[str, str, str]] = []

        def handle_starttag(self, tag, attrs):
            for attr, value in attrs:
                if value and re.match(r"https?://", value):
                    self.external.append((tag, attr, value))

    parser = ExternalAttributeParser()
    parser.feed(html)
    assert set(parser.external) == {
        ("a", "href", url) for url in allowed_links
    }, "only the three inert provider-credit anchors may use external URLs"
    # Scan every HTML context too: an external script/img/iframe/form/preload
    # URL would bypass an href-only assertion, as would a URL in inline CSS or
    # script. Requiring each URL exactly once also rules out a hidden duplicate.
    external_urls = re.findall(r'https?://[^\s"\'<>]+', html)
    assert sorted(external_urls) == sorted(allowed_links)
    device_panel = html.split('<section id="device"', 1)[1].split(
        "</section>", 1
    )[0]
    assert "Weather data by Open-Meteo.com" in device_panel
    assert "Weather radar data by RainViewer" in device_panel
    assert "Review those linked terms before selecting Skystrip" in device_panel
    assert html.count('target="_blank" rel="noopener noreferrer"') == len(
        allowed_links
    )


def test_the_vendored_fonts_ship_with_their_licences():
    fonts = Path(__file__).resolve().parent.parent / "barkeep" / "static" / "fonts"
    for family in ("silkscreen", "archivo", "martianmono"):
        licence = fonts / f"OFL-{family}.txt"
        assert licence.is_file(), f"missing {licence.name}"
        assert "SIL OPEN FONT LICENSE" in licence.read_text().upper()


# --- the TLS admin section ---------------------------------------------------

_requires_openssl = pytest.mark.skipif(
    shutil.which("openssl") is None,
    reason="pair fixtures are minted with the openssl command",
)


def _tls_env_clear(monkeypatch):
    for var in ("BARKEEP_TLS", "BARKEEP_TLS_CERT", "BARKEEP_TLS_KEY"):
        monkeypatch.delenv(var, raising=False)


def _minted_pair(monkeypatch, tmp_path, name):
    monkeypatch.setenv("BARKEEP_TLS", "1")
    cert, key = barkeep_tls.resolve_tls(tmp_path / f"mint-{name}")
    monkeypatch.delenv("BARKEEP_TLS")
    return cert.read_text(), key.read_text()


def _https(client):
    return TestClient(client.app, base_url="https://127.0.0.1:8080")


@pytest.mark.parametrize(("scheme", "client_host", "allowed"), [
    ("https", "192.0.2.4", True),
    ("http", "127.0.0.1", True),
    ("http", "::1", True),
    ("http", "localhost", True),
    ("http", "192.0.2.4", False),
    ("http", None, False),
])
def test_tls_private_key_transport_policy(scheme, client_host, allowed):
    assert tls_upload_allowed(scheme, client_host) is allowed


def test_tls_admin_reports_plain_http_when_unconfigured(tmp_path, monkeypatch):
    _tls_env_clear(monkeypatch)
    client, _, _ = make_client(tmp_path)

    body = client.get("/api/tls").json()

    assert body["source"] == "off"
    assert body["managed"] is True
    assert body["restart_required"] is False
    assert body["cert"] is None
    assert body["upload_allowed"] is False


@_requires_openssl
def test_tls_admin_refuses_a_private_key_over_lan_http(tmp_path, monkeypatch):
    _tls_env_clear(monkeypatch)
    cert_pem, key_pem = _minted_pair(monkeypatch, tmp_path, "good")
    client, _, config_dir = make_client(tmp_path)

    response = client.put(
        "/api/tls",
        json={"certificate_pem": cert_pem, "key_pem": key_pem},
    )

    assert response.status_code == 403
    assert "HTTPS" in response.json()["error"]
    assert not (config_dir / "tls" / "barkeep-operator.key").exists()


@_requires_openssl
def test_tls_admin_installs_a_pasted_pair(tmp_path, monkeypatch):
    _tls_env_clear(monkeypatch)
    cert_pem, key_pem = _minted_pair(monkeypatch, tmp_path, "good")
    client, _, config_dir = make_client(tmp_path)

    resp = _https(client).put(
        "/api/tls",
        json={"certificate_pem": cert_pem, "key_pem": key_pem},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "uploaded"
    assert body["restart_required"] is True
    assert body["cert"]["fingerprint_sha256"]
    assert (config_dir / "tls" / "barkeep-operator.crt").is_file()
    followup = client.get("/api/tls").json()
    assert followup["source"] == "uploaded"
    assert followup["restart_required"] is True


@_requires_openssl
def test_tls_admin_rejects_a_mismatched_pair_without_staging(
    tmp_path, monkeypatch,
):
    _tls_env_clear(monkeypatch)
    cert_pem, _ = _minted_pair(monkeypatch, tmp_path, "one")
    _, other_key = _minted_pair(monkeypatch, tmp_path, "two")
    client, _, config_dir = make_client(tmp_path)

    resp = _https(client).put(
        "/api/tls",
        json={"certificate_pem": cert_pem, "key_pem": other_key},
    )

    assert resp.status_code == 422
    assert not (config_dir / "tls" / "barkeep-operator.crt").exists()
    assert client.get("/api/tls").json()["source"] == "off"


def test_tls_upload_exception_details_are_logged_not_returned(
    tmp_path, monkeypatch, caplog,
):
    _tls_env_clear(monkeypatch)
    client, _, _ = make_client(tmp_path)
    sentinel = "SECRET_KEY /private/operator/key.pem Traceback: ssl frame"

    def reject(*_args, **_kwargs):
        raise ValueError(sentinel)

    monkeypatch.setattr(barkeep_server, "stage_operator_pair", reject)

    with caplog.at_level("WARNING", logger="barkeep.server"):
        response = _https(client).put(
            "/api/tls",
            json={"certificate_pem": "certificate", "key_pem": "key"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "error": "certificate and key must be usable PEM and match"
    }
    assert all(part not in response.text for part in ("SECRET_KEY", "/private", "Traceback"))
    assert sentinel in caplog.text


def test_tls_status_exception_details_are_logged_not_returned(
    tmp_path, monkeypatch, caplog,
):
    _tls_env_clear(monkeypatch)
    client, _, _ = make_client(tmp_path)
    sentinel = "SECRET_CERT /private/operator/cert.pem Traceback: tls frame"

    def reject(_tls_dir):
        raise ValueError(sentinel)

    monkeypatch.setattr(barkeep_tls, "resolve_tls", reject)

    with caplog.at_level("WARNING", logger="barkeep.tls"):
        response = client.get("/api/tls")

    assert response.status_code == 200
    assert response.json()["detail"] == (
        "TLS configuration is invalid; inspect the Barkeep service logs"
    )
    assert all(part not in response.text for part in ("SECRET_CERT", "/private", "Traceback"))
    assert sentinel in caplog.text


def test_tls_admin_defers_to_an_environment_pinned_pair(tmp_path, monkeypatch):
    """When .env pins the pair the UI must say so, not silently shadow it."""
    _tls_env_clear(monkeypatch)
    cert = tmp_path / "env.crt"
    key = tmp_path / "env.key"
    cert.write_text("not parsed by resolution")
    key.write_text("not parsed by resolution")
    monkeypatch.setenv("BARKEEP_TLS_CERT", str(cert))
    monkeypatch.setenv("BARKEEP_TLS_KEY", str(key))
    client, _, _ = make_client(tmp_path)

    body = client.get("/api/tls").json()
    assert body["source"] == "env"
    assert body["managed"] is False

    put = client.put("/api/tls", json={"certificate_pem": "x", "key_pem": "y"})
    assert put.status_code == 409
    delete = client.delete("/api/tls", headers=JSON)
    assert delete.status_code == 409


@_requires_openssl
def test_tls_admin_revert_removes_the_upload(tmp_path, monkeypatch):
    _tls_env_clear(monkeypatch)
    cert_pem, key_pem = _minted_pair(monkeypatch, tmp_path, "good")
    client, _, config_dir = make_client(tmp_path)
    _https(client).put(
        "/api/tls",
        json={"certificate_pem": cert_pem, "key_pem": key_pem},
    )

    resp = client.delete("/api/tls", headers=JSON)

    assert resp.status_code == 200
    body = resp.json()
    assert body["source"] == "off"
    assert body["restart_required"] is True
    assert not (config_dir / "tls" / "barkeep-operator.crt").exists()
    assert not (config_dir / "tls" / "barkeep-operator.key").exists()


def test_tls_admin_mutations_require_json_content_type(tmp_path, monkeypatch):
    _tls_env_clear(monkeypatch)
    client, _, _ = make_client(tmp_path)

    resp = client.put(
        "/api/tls",
        content=b"certificate_pem=x",
        headers={"content-type": "text/plain"},
    )

    assert resp.status_code == 403

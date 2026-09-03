"""REST API — thin adapters between HTTP and the supervisor/config/preview.
No business logic here; that lives in the modules this wires together."""

from __future__ import annotations

import hmac
import ipaddress
import os
import socket
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from .configstore import (
    app_env_path,
    effective_config,
    normalize_multiselect,
    read_env_file,
    write_env_file,
)
from .config_validation import validate_effective_config, validate_submitted_values
from .preview import BarOffline
from .registry import AppSpec
from .statestore import DesiredState, save_state
from .tls import remove_operator_pair, stage_operator_pair, tls_status

STATIC_DIR = Path(__file__).parent / "static"
MAX_JSON_BYTES = 256 * 1024
_MUTATING = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SECURITY_HEADERS = {
    "Content-Security-Policy": "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class _JsonBodyLimitMiddleware:
    """Reject oversized mutation bodies before FastAPI parses their JSON."""

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

        raw_length = next(
            (
                value
                for name, value in scope.get("headers", ())
                if name.lower() == b"content-length"
            ),
            None,
        )
        if raw_length is not None:
            try:
                declared_length = int(raw_length)
            except (TypeError, ValueError):
                await self._reject(scope, receive, send, 400, "invalid Content-Length")
                return
            if declared_length < 0:
                await self._reject(scope, receive, send, 400, "invalid Content-Length")
                return
            if declared_length > self.max_bytes:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    "request exceeds the JSON budget",
                )
                return

        consumed = 0
        chunks: list[bytes] = []
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                async def replay_special() -> Message:
                    return message  # noqa: B023 - consumed immediately below

                await self.app(scope, replay_special, send)
                return
            chunk = message.get("body", b"")
            consumed += len(chunk)
            if consumed > self.max_bytes:
                await self._reject(
                    scope,
                    receive,
                    send,
                    413,
                    "request exceeds the JSON budget",
                )
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

    @staticmethod
    async def _reject(
        scope: Scope,
        receive: Receive,
        send: Send,
        status: int,
        message: str,
    ) -> None:
        response = JSONResponse({"error": message}, status_code=status)
        await response(scope, receive, send)


class _SecurityHeadersMiddleware:
    """Apply browser security headers to normal and middleware responses."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_hardened(message: Message) -> None:
            if message.get("type") == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _SECURITY_HEADERS.items():
                    headers[name] = value
            await send(message)

        await self.app(scope, receive, send_hardened)


def host_name(raw: str) -> str:
    """The hostname out of a Host header, without its port or IPv6 brackets."""
    value = raw.strip().lower()
    if value.startswith("["):                 # [::1]:8080
        end = value.find("]")
        return value[1:end] if end != -1 else value[1:]
    # A bare IPv6 literal has several colons; only strip a real :port.
    if value.count(":") == 1:
        value = value.split(":", 1)[0]
    return value


def is_ip_literal(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def tls_upload_allowed(scheme: str, client_host: str | None) -> bool:
    """Whether a private key can safely travel on this request.

    HTTPS protects it on the wire. Plain HTTP is acceptable only from the
    loopback interface, which covers local administration and an SSH tunnel;
    a token authenticates a LAN caller but does not encrypt the pasted key.
    """
    if scheme.lower() == "https":
        return True
    if not client_host:
        return False
    try:
        return ipaddress.ip_address(client_host).is_loopback
    except ValueError:
        return client_host.strip().lower() == "localhost"


def local_host_names() -> set[str]:
    """Names this machine legitimately answers to."""
    names = {"localhost"}
    try:
        hostname = socket.gethostname().lower()
    except OSError:                            # pragma: no cover - hostile host
        return names
    names.add(hostname)
    names.add(hostname.split(".")[0])
    # Avahi/Bonjour reach a Pi as <name>.local, which is how the UI is opened.
    names.add(f"{hostname.split('.')[0]}.local")
    return names


def host_allowed(raw: str, extra: frozenset[str], local: frozenset[str]) -> bool:
    """Whether a Host header may reach this server.

    DNS rebinding is the gap the JSON content-type check below cannot close:
    once an attacker's domain resolves to this machine, their page is
    same-origin, so it needs no preflight and the content-type rule passes.
    The defence is to refuse names we are not, which costs nothing because
    rebinding fundamentally requires a NAME — an IP literal cannot be rebound.

    So: IP literals are fine because they cannot be rebound, localhost and this
    machine's own names are fine, and anything else must be named in
    BARKEEP_ALLOWED_HOSTS.
    """
    name = host_name(raw)
    if not name:
        return False                           # HTTP/1.1 requires a Host
    if is_ip_literal(name):
        return True
    return name in local or name in extra


def allowed_hosts_from_env(value: str | None) -> frozenset[str]:
    return frozenset(
        part.strip().lower() for part in (value or "").split(",") if part.strip()
    )


def presented_token(request) -> str:
    """The credential a caller offered, from either accepted carrier.

    A header for programmatic callers, a cookie for the browser UI — the UI
    cannot attach a header to an <img> or a plain navigation, and the preview
    panes are <img> tags.
    """
    header = request.headers.get("authorization", "")
    scheme, _, value = header.partition(" ")
    if scheme.lower() == "bearer" and value.strip():
        return value.strip()
    direct = request.headers.get("x-barkeep-token", "")
    if direct.strip():
        return direct.strip()
    return request.cookies.get("barkeep_token", "")


def token_accepted(presented: str, expected: str) -> bool:
    """Constant-time comparison, so a wrong token leaks no prefix length."""
    if not expected:
        return True                    # unauthenticated by configuration
    if not presented:
        return False
    return hmac.compare_digest(presented, expected)


def exposure_warning(bind: str, token: str) -> str:
    """The one sentence an operator needs before this reaches a real network.

    barkeep parents processes and writes config that becomes their environment.
    Loopback is the default; any unauthenticated non-loopback bind is an
    explicit but hazardous override that must be impossible to miss.
    """
    if token:
        return ""
    try:
        loopback = ipaddress.ip_address(bind).is_loopback
    except ValueError:
        loopback = bind.strip().lower() == "localhost"
    if loopback:
        return ""
    return (
        f"barkeep is listening on {bind} with NO authentication. Anything that "
        f"can reach this port can switch apps, restart them, read app logs and "
        f"the framebuffer, and write app config. Set BARKEEP_TOKEN to require "
        f"a credential, or BARKEEP_BIND=127.0.0.1 to keep it on this host. "
        f"Do not expose this configuration to an untrusted network."
    )

def create_app(supervisor, registry: dict[str, AppSpec], preview,
               config_dir: Path, state_path: Path) -> FastAPI:
    app = FastAPI(title="barkeep", docs_url=None, redoc_url=None)

    def persist() -> None:
        save_state(state_path, DesiredState(
            supervisor.foreground, supervisor.enabled_backgrounds()))

    def state_blob() -> dict:
        return {
            "foreground": supervisor.foreground,
            "switching": supervisor.switching,
            "bar_host": os.environ.get("BUSYBAR_HOST") or "usb (10.0.4.20)",
            "apps": supervisor.status(),
        }

    def error(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status)

    async def _operate(op, name) -> Response | dict:
        try:
            await op(name)
        except KeyError:
            return error(404, f"unknown app: {name}")
        except ValueError as exc:
            return error(409, str(exc))
        persist()
        return state_blob()

    @app.get("/api/state")
    def get_state():
        return state_blob()

    @app.post("/api/foreground")
    async def set_foreground(body: dict):
        return await _operate(supervisor.set_foreground, body.get("app"))

    @app.post("/api/apps/{name}/enable")
    async def enable(name: str):
        return await _operate(supervisor.enable, name)

    @app.post("/api/apps/{name}/disable")
    async def disable(name: str):
        return await _operate(supervisor.disable, name)

    @app.post("/api/apps/{name}/restart")
    async def restart(name: str):
        return await _operate(supervisor.restart, name)

    @app.get("/api/apps/{name}/logs")
    def logs(name: str, lines: int = 200):
        try:
            return {"lines": supervisor.logs(name, lines)}
        except KeyError:
            return error(404, f"unknown app: {name}")

    def _config_rows(name: str) -> list[dict]:
        return effective_config(
            registry[name], read_env_file(app_env_path(config_dir, name)), os.environ)

    @app.get("/api/apps/{name}/config")
    def get_config(name: str):
        if name not in registry:
            return error(404, f"unknown app: {name}")
        return {"keys": _config_rows(name)}

    @app.put("/api/apps/{name}/config")
    def put_config(name: str, body: dict):
        if name not in registry:
            return error(404, f"unknown app: {name}")
        values = body.get("values", {})
        if not isinstance(values, dict):
            return error(422, "values must be an object")
        declared = {k.name for k in registry[name].config}
        unknown = sorted(set(values) - declared)
        if unknown:
            return error(422, f"undeclared config keys: {', '.join(unknown)}")
        coerced = {k: str(v) for k, v in values.items()}
        # One key per line IS the file format, and the file becomes the child's
        # environment: a newline in a value would smuggle in undeclared vars
        # (BUSYBAR_HOST, LD_PRELOAD, PYTHONPATH) on the next spawn.
        bad = sorted(k for k, v in coerced.items() if set(v) & {"\n", "\r", "\0"})
        if bad:
            return error(422, f"values must be single-line: {', '.join(bad)}")

        # multiselect values name a subset of the declared choices. Validate
        # and canonicalize here rather than only in the UI — the API is
        # curl-able, and an app reading a bogus scene name would just ignore
        # it silently.
        for key in registry[name].config:
            if key.type != "multiselect" or key.name not in coerced:
                continue
            selected, unknown = normalize_multiselect(coerced[key.name], key.choices)
            if unknown:
                return error(422, f"{key.name}: not valid choices: "
                                  f"{', '.join(unknown)}")
            if not selected:
                return error(422, f"{key.name}: select at least one")
            coerced[key.name] = ",".join(selected)

        validation_error = validate_submitted_values(registry[name], coerced)
        if validation_error:
            return error(422, validation_error)

        # Blank normally removes the app override and reveals the shared .env
        # or registry default. A few keys explicitly declare blank as their
        # value (anonymous contact, automatic station); only those persist
        # ``KEY=``. A blank registry default alone cannot encode both meanings.
        blankable = {
            k.name for k in registry[name].config if k.blank_is_value
        }
        path = app_env_path(config_dir, name)
        merged = read_env_file(path)
        for key, value in coerced.items():
            if value == "" and key not in blankable:
                merged.pop(key, None)
            else:
                merged[key] = value
        validation_error = validate_effective_config(
            registry[name], merged, os.environ)
        if validation_error:
            return error(422, validation_error)
        write_env_file(path, merged)
        return {"keys": _config_rows(name)}

    # --- TLS admin: replacing the certificate is a paste, not an ssh session.
    # Uploads stage to config/tls/ after validation; nothing here restarts the
    # daemon (the unit is Restart=on-failure, so a deliberate exit would stay
    # down) — the operator restarts, and restart_required says so.
    tls_dir = config_dir / "tls"
    tls_pending = {"restart": False}
    ENV_PINNED = ("the certificate is pinned by BARKEEP_TLS_CERT/"
                  "BARKEEP_TLS_KEY in the environment; unset them to "
                  "manage it here")

    def tls_blob(request: Request) -> dict:
        status = tls_status(tls_dir)
        status["restart_required"] = tls_pending["restart"]
        status["upload_allowed"] = tls_upload_allowed(
            request.url.scheme,
            request.client.host if request.client else None,
        )
        return status

    @app.get("/api/tls")
    async def tls_state(request: Request):
        return tls_blob(request)

    @app.put("/api/tls")
    async def tls_install(body: dict, request: Request):
        if not tls_status(tls_dir)["managed"]:
            return error(409, ENV_PINNED)
        if not tls_upload_allowed(
            request.url.scheme,
            request.client.host if request.client else None,
        ):
            return error(
                403,
                "refusing to receive a private key over non-loopback HTTP; "
                "open Barkeep over HTTPS or an SSH tunnel",
            )
        try:
            stage_operator_pair(tls_dir,
                                str(body.get("certificate_pem", "")),
                                str(body.get("key_pem", "")))
        except ValueError as exc:
            return error(422, str(exc))
        tls_pending["restart"] = True
        return tls_blob(request)

    @app.delete("/api/tls")
    async def tls_revert(request: Request):
        if not tls_status(tls_dir)["managed"]:
            return error(409, ENV_PINNED)
        if remove_operator_pair(tls_dir):
            tls_pending["restart"] = True
        return tls_blob(request)

    @app.get("/api/preview/{display}")
    def get_preview(display: int):
        if display not in (0, 1):
            return error(404, "display must be 0 (front) or 1 (back)")
        try:
            png = preview.png(display)
        except BarOffline:
            return error(503, "bar offline")
        return Response(png, media_type="image/png",
                        headers={"Cache-Control": "no-store"})

    if STATIC_DIR.is_dir():
        @app.get("/")
        def index():
            return FileResponse(STATIC_DIR / "index.html")

        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    EXTRA_HOSTS = allowed_hosts_from_env(os.environ.get("BARKEEP_ALLOWED_HOSTS"))
    LOCAL_HOSTS = frozenset(local_host_names())

    @app.middleware("http")
    async def json_only_mutations(request, call_next):
        # Body-less POSTs are CORS "simple requests", so a page able to reach
        # the service could otherwise restart apps in the background.
        # Requiring JSON forces a preflight this server never authorizes; the
        # bind/token policy remains the actual access boundary.
        if request.method in _MUTATING:
            ctype = (request.headers.get("content-type") or "").split(";")[0]
            if ctype.strip().lower() != "application/json":
                return JSONResponse({"error": "JSON content-type required"},
                                    status_code=403)
        return await call_next(request)

    @app.middleware("http")
    async def no_stale_ui(request, call_next):
        # The UI ships with the daemon; a browser must never run last
        # deploy's frontend against this deploy's backend. no-cache still
        # allows conditional requests (etag), so revalidation stays cheap.
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    TOKEN = (os.environ.get("BARKEEP_TOKEN") or "").strip()

    @app.post("/api/session")
    async def open_session(body: dict, request: Request):
        """Exchange a token for a cookie, so the browser UI can carry it.

        The preview panes are <img> tags and cannot attach a header, so a
        header-only scheme would work for curl and not for the UI it ships.
        """
        if not TOKEN:
            return error(404, "this server requires no token")
        if not token_accepted(str(body.get("token", "")), TOKEN):
            return error(401, "invalid token")
        response = JSONResponse({"ok": True})
        # Secure only when actually served over TLS: set over plain http the
        # browser would discard it and lock the UI into a token-prompt loop.
        response.set_cookie(
            "barkeep_token", TOKEN,
            httponly=True, samesite="strict", path="/",
            secure=request.url.scheme == "https",
        )
        return response

    @app.middleware("http")
    async def token_required(request, call_next):
        # A token, when configured, protects every API route at once. There is
        # no partially protected mode because read routes expose sensitive
        # operational data such as the framebuffer and app logs.
        if TOKEN and request.url.path.startswith("/api/") \
                and request.url.path != "/api/session":
            if not token_accepted(presented_token(request), TOKEN):
                return JSONResponse(
                    {"error": "authentication required"}, status_code=401)
        return await call_next(request)

    @app.middleware("http")
    async def known_host_only(request, call_next):
        # Verified against the running daemon: `Host: evil.example.com` used to
        # return 200. That is the precondition for DNS rebinding, which is the
        # one browser-borne path the content-type check below cannot close —
        # after a rebind the attacker's page is same-origin, so no preflight is
        # required and the JSON rule is satisfied honestly.
        if not host_allowed(request.headers.get("host", ""),
                            EXTRA_HOSTS, LOCAL_HOSTS):
            return JSONResponse(
                {"error": "unrecognised Host; set BARKEEP_ALLOWED_HOSTS to "
                          "reach this server by a name"},
                status_code=421)
        return await call_next(request)

    # The size limit is outside FastAPI's request parsing and token middleware,
    # so the deliberately unauthenticated /api/session route cannot allocate an
    # arbitrary JSON body before checking the credential. Security headers wrap
    # every application response, including an early 413 from that limit.
    app.add_middleware(_JsonBodyLimitMiddleware, max_bytes=MAX_JSON_BYTES)
    app.add_middleware(_SecurityHeadersMiddleware)

    return app

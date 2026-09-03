# Security

## Reporting

Use the **Report a vulnerability** button on this repository's **Security**
tab. It opens a private GitHub security advisory; do not put vulnerability
details, proof-of-concept code, logs, tokens, network addresses, or coordinates
in a public issue.

If that button is unavailable, open a public issue titled **Private security
contact requested** with no diagnostic details. A maintainer can then provide
or enable a private channel. There is no formal response SLA, but reports are
read and credited.

## Supported versions

Until the first public release is tagged, security fixes target `main`. After
that, the latest tagged release and current `main` are supported; older
releases are unsupported unless a security advisory explicitly says
otherwise.

## What this software is

Two things run, and they have different exposure:

**Apps** (`apps/`) are outbound-only. They fetch public feeds — NWS,
Open-Meteo, RainViewer, NASA DSN — and draw to a BUSY Bar on your network.
Nothing listens.

**barkeep** (`barkeep/`) is a control plane. It listens on port 8080, supervises
the app processes as children, serves a web UI, and writes the per-app config
files that become those children's environment.

Because Barkeep controls child processes and configuration, access to it should
be treated as administrative access to Barkeep and its managed applications.

## Network configurations

Barkeep supports local access and direct LAN access:

| Configuration | Bind | Authentication and transport |
|---|---|---|
| Fresh installation | `127.0.0.1:8080` | Local HTTP or an SSH tunnel; no Barkeep token is configured |
| Direct LAN access | A LAN address or `0.0.0.0` | Recommended: `BARKEEP_TOKEN` authentication over HTTPS; `BARKEEP_TLS=1` creates a persistent self-signed certificate, or the operator can provide a certificate pair |

The installer selects the local configuration. LAN access is enabled through
the host's gitignored `.env` file.

Anything that can reach the port can:

- switch, stop, or restart apps
- read the bar's framebuffer (`/api/preview/*`) and each app's stdout
  (`/api/apps/*/logs`)
- write any config key declared in `apps.toml`

Barkeep exposes more than a display API: it reads logs and configuration,
writes app configuration, and controls child processes. Configure
authentication and encrypted transport for direct LAN deployments.

## Exposing it to a network

For remote access, the safest option is to leave the default bind unchanged and
reach the UI through an SSH tunnel. A VPN still requires an explicit bind to
an address reachable through that VPN.

To serve barkeep directly on a controlled LAN, set `BARKEEP_BIND` explicitly
and configure a strong token. Generate one, for example, with:

```bash
uv run python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Then paste that value into `.env`; dotenv files do not execute shell command
substitutions:

```dotenv
BARKEEP_BIND=0.0.0.0
BARKEEP_TOKEN=paste-generated-value-here
BARKEEP_TLS=1
```

Callers send `Authorization: Bearer <token>` or `X-Barkeep-Token: <token>`. The
web UI prompts once and exchanges the token for an httpOnly, SameSite=strict
cookie, because its preview panes are `<img>` tags and cannot carry a header.
By default the token travels over plain HTTP. Set `BARKEEP_TLS=1` to serve
HTTPS instead: a self-signed certificate is generated once under
`config/tls/` with the optional `openssl` command and reused across restarts.
Its directory and private key are reset to owner-only permissions and the pair
is validated at each startup. An untrusted certificate still
negotiates real encryption — passive capture of the token off the network is
closed — but it proves no server identity, so browsers warn on first visit
and an active man-in-the-middle is not excluded. Open the browser's certificate
details and compare its SHA-256 fingerprint against what the host holds
(`openssl x509 -noout -fingerprint -sha256 -in
config/tls/barkeep-selfsigned.crt`) before accepting. To serve a certificate
clients actually trust, point `BARKEEP_TLS_CERT` and `BARKEEP_TLS_KEY` at
your own pair (for example from a local CA such as mkcert), or paste a PEM
pair into the web UI's HTTPS section (`PUT /api/tls`, protected like every
operational API route). Barkeep accepts a pasted private key only over HTTPS or
a loopback/SSH-tunnel connection: authentication alone does not encrypt a key
sent over LAN HTTP. The pair is validated before either live file is replaced
under `config/tls/`, with an owner-only key; a rejected upload changes nothing,
and an env-pinned pair is reported rather than silently shadowed. The
API returns only public certificate facts — source, SHA-256 fingerprint,
expiry — never key material. Setting only
half the pair refuses startup rather than silently serving plaintext. Keep the
private key protected and readable by the service account. TLS settings and
certificate replacements take effect on daemon restart. The port serves one
protocol with no HTTP redirect, so use `https://HOST:8080` after enabling it.
If that browser already used Barkeep over HTTP, clear its old Barkeep site data
or close the browser session and sign in again so the newly issued cookie has
the `Secure` flag. An SSH tunnel or VPN remains the strongest option anywhere
the network itself is not trusted.

The daemon logs a warning at startup when it is bound to a non-loopback
interface with no token set. That configuration remains available for an
operator who has deliberately accepted the risk, but it is never the default.

## Implemented controls

These controls are covered by automated tests:

- **Host header allowlist.** Requests must carry an IP literal, `localhost`,
  this machine's name, or something in `BARKEEP_ALLOWED_HOSTS`. Without it a
  page that rebinds its own DNS to this host becomes same-origin and can
  satisfy the JSON content-type requirement below.
- **JSON content-type required on mutations,** which forces a CORS preflight
  this server never answers, closing drive-by CSRF.
- **Mutation bodies are capped at 256 KiB before JSON parsing,** including the
  unauthenticated token-exchange route and requests with chunked, missing, or
  incorrect length metadata.
- **Browser responses deny framing and MIME sniffing** with CSP,
  `X-Frame-Options`, and `X-Content-Type-Options` headers.
- **Config keys are allowlisted** to what `apps.toml` declares, and values must
  be single-line — so `LD_PRELOAD`, `PYTHONPATH` and friends cannot be
  smuggled into a child's environment.
- **No declared key may look like a credential.** The config API returns
  declared values verbatim, so secrets stay in owner-readable `.env` and are
  scrubbed from offline workers. `SKYSTRIP_LIGHTNING_WS` is the documented
  exception and the pattern to follow.
- **Remote records are validated before acceptance into app state.** Identity
  lengths, accepted record counts, and timestamp skew are bounded, and XML is
  parsed with `defusedxml` because the stdlib parser expands entities. DSN XML
  and radar PNG paths also reject oversized application payloads after
  download. HTTP polling responses are buffered first, and JSON feeds do not
  all have a transport-size cap, so these checks bound parsing and accepted
  state rather than universal ingress or peak memory.
- **Nothing personal in tracked files,** enforced over `git ls-files`:
  addresses, coordinates, private IPs and machine names.

## Recommended deployment

1. Keep the default loopback bind and use an SSH tunnel where practical. For
   VPN or direct-LAN access, bind explicitly to the intended interface, set
   `BARKEEP_TOKEN`, and set `BARKEEP_TLS=1` so the token is encrypted in
   transit.
2. Run the systemd unit as shipped — it sets `NoNewPrivileges=yes`,
   `ProtectSystem=strict`, `ProtectHome=read-only`.
3. Scope the deploy account's sudo to `systemctl stop` and `systemctl start` for
   its exact Barkeep unit; `deploy/README.md` has the drop-in. A blanket
   `NOPASSWD: ALL` means anything that gets
   execution as that account is also root.
4. Run `deploy/install.sh` as that unprivileged account, never as root. It
   invokes sudo itself for the individual installation operations that need it.
5. `chmod 600 .env` — it holds `BUSYBAR_TOKEN` and your coordinates.
6. Do not port-forward barkeep. Use a VPN or an SSH tunnel.

## Not in scope

- The BUSY Bar firmware and its HTTP API. Report those to the vendor.
- Third-party feeds. This project validates what they return but does not
  vouch for them.

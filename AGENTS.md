# BUSY Bar Lab

This is a source-first environment for building, testing, and deploying custom
apps for the BUSY Bar (model BB.1), directly or with AI coding agents. Skystrip
and DSN are larger examples; Hello and the app template are smaller starting
points. Barkeep is the control plane that runs registered apps.

Read the first-party guides and retained busylib references locally; use the
vendor's current developer documentation for the device API.

If you are a coding agent, this file is the fast path. Read it, then read the
`busybar-app` skill before writing anything that draws. Use the `busybar-viz`
skill when previewing, comparing, or making claims about display output.

Project skills have one authored copy under `.claude/skills/`; relative links
under `.agents/skills/` expose those same files to Codex. Never copy a skill
between the two trees.

## Where the documentation lives

| Path | What it is |
|---|---|
| `docs/README.md` | Index of first-party guides, app docs, external references, and historical design records |
| [docs.busy.app/bar/dev](https://docs.busy.app/bar/dev) | Current vendor developer docs; see its HTTP API and API-token pages for network setup |
| `docs/api/openapi.yaml` | Optional, gitignored device OpenAPI snapshot for local inspection; fetch it from a reachable bar |
| `docs/busylib/` | Redistributable official Python client docs: guides, API reference, README, `AGENTS.md`, examples, and MIT licence |

With a bar reachable, refresh the ignored local device specification with
`uv run scripts/refresh_docs.py`. The public repository does not redistribute
a scraped mirror of the vendor documentation.

## The device

- **USB (primary):** `10.0.4.20` or `busybar.local`, no auth. Wi-Fi/LAN needs
  HTTP API access enabled in the local web UI plus its PIN in `X-API-Token`.
  Internet: `https://api.busy.app/busybar` with a Bearer token from
  cloud.busy.app.
- **Front display:** 72×16 RGB. **Back display:** 160×80. On firmware 1.1.1,
  the device wire framebuffer is RGB888 in BGR byte order for both displays;
  older firmware returned the back display as 4-bit grayscale. Busylib 2
  normalizes every `screen()` result to canonical row-major RGB888, so callers
  must not swap channels or unpack grayscale again. The back takes 160×80 PNGs
  for image elements.
- **Draw model:** draws carry `application_name` + `priority` (1–100, busylib
  default 50). Idle built-in apps sit at ~10. An **active BUSY/CUSTOM session
  refuses outside draws even at priority 95** — the device returns HTTP 409
  `"Not drawn due to low priority"` (it rejects, it does not layer-under).
  Treat 409 as "yield and retry later", never as a failure. Don't try to
  out-priority a running focus session. Draws merge by element `id` within an
  app; `clear_before_draw=True` costs an extra DELETE per draw — prefer stable
  ids and element `timeout` (seconds) so dead apps self-clear.
- **PRIORITY IS NOT Z-ORDER.** Per the device's documented draw contract, a
  draw is accepted when its priority is `>=`
  the running system app's, and *"equal-priority requests from a different
  application_name override whatever is on screen."* So two apps never
  composite — the last writer takes the display and the other gets it back on
  its next redraw (up to 60s for skystrip). Layering exists only **within** one
  `application_name`, where elements merge by id. Design interruptions as
  "own the strip for a bounded window, then release", never as an overlay.
- **Same-app transient ids compose; they do not replace one another.** A picker,
  status, or readout with new ids can expire and reveal an older opaque event
  card underneath. When one transient interrupts another, retire every old id
  and create the new top-layer ids in the same accepted draw where possible.
  A POST-order test is insufficient: advance past the newer timeout and prove
  the interrupted card cannot resurface.
- **Animations:** the device plays `.anim` files natively (`AnimationElement`,
  `loop=True`) — its own format, magic `bicycle0`, BGR888 + firmware RLE.
  `busybar_dev/anim.py` encodes it from PIL frames (ported from the firmware's
  GPL-2.0-or-later `scripts/seq2anim.py`). Prefer one `.anim` upload over host-driven
  redraw loops for any motion; the device cycles frames itself.
- **Address gotcha:** when the bar is also on Wi-Fi (API disabled there by
  default), `busybar.local` resolves to BOTH the USB and Wi-Fi addresses and
  requests randomly hang with ConnectTimeout. Always prefer `10.0.4.20`;
  `connect()`/`aconnect()` already order USB first.
- **Text that looks right:** `font="condensed"`, `align="center", x=36, y=8`.
  `scroll_rate` is pixels per **minute** (~1400).
- **Containment is not completeness.** A clip can keep every emitted pixel in
  bounds while silently amputating a label (`NO LINK` once lost most of its
  `K`; `UPLINK` became `UPLI`). For every static/status label, independently
  render the full unclipped ink, prove its bounding box fits, and compare it
  with the final composed pixels across all animation frames/pages/states. If
  it cannot fit, marquee, alternate, explicitly abbreviate, or omit it—never
  slice arbitrary text. `_text` clip ends are inclusive; PIL crop ends are
  exclusive, so centralise that conversion and use layout constants.
- **LED gamma crushes subtle contrast:** brightness deltas under ~30% are
  invisible on the physical panel even when clearly visible in preview PNGs
  (verified on-device). Design texture and detail at ≥30% contrast, and treat
  screenshots and previews as optimistic about subtlety.
- **The LEDs are spaced almost their own width apart** — 1.23 mm lit on a
  2.2 mm pitch ([vendor tech specs](https://docs.busy.app/bar/tech-specs)), so the dark gap is 79% of
  the LED. Design mostly-OFF; a filled background reads as a haze, not a
  surface. One pixel is one RGB package: colour subpixels you control fully,
  **no spatial subpixels**. But brightness reads as apparent *size*, so
  splitting a mark across two LEDs buys ~3 sub-positions (not 256 — gamma
  crushes the rest). That is the only way to show motion slower than a pixel
  per redraw.
- **One wheel detent is ONE encoder count** (verified in skystrip and dsn).
  Never invent a counts-per-detent divisor; grep the other apps for measured
  hardware constants before choosing your own.
- **Nothing interactive may wait on an asset upload.** An `.anim` is ~80 KB
  and ~1 s; per-detent rendering makes the wheel feel dead. Draw an instant
  read-out with Text/Rectangle elements and commit the scene on rest.
- **Speech costs ~1× realtime** on a small ARM host. Pre-bake lines and cache
  them on-device under a hash of *text + voice*; never synthesise on a
  keypress.
- **Audio:** `.snd` = headerless raw PCM s16le mono 44100 Hz. Assets persist
  on-device per `application_name`.

## Visual evidence and tooling boundary

`busybar-viz` is how an agent sees the bar without hardware: it audits exact
pixels against the panel's measured physics and publishes immutable, diffable
evidence. It is an offline tool, not a Barkeep feature. Barkeep's preview is
live framebuffer readback; do not add visualizer routes, state, or runtime
dependencies to Barkeep, and do not treat its still image as the frame
currently visible during native `.anim` playback.

The main agent loop is self-serve — no session, server, or person: render
(`uv run busybar-viz view FRAMES --json` for ad-hoc in-development frames,
`uv run busybar-viz run SCENARIO --json` for registered provenance), read
`audit.json` and the contact-sheet/gap previews, fix, and `compare` the new
SHA against the prior one. Its exact CLI, adapter, artifact, comparison, and
collaboration contracts are in [`docs/busybar-viz.md`](docs/busybar-viz.md).

When a person is collaborating, Codex and Claude Code share the same
append-only review journal: atomically expose an artifact's exact SHA with
`uv run busybar-viz session present SESSION_ID ARTIFACT_SHA --revision
REVISION --json`, then keep the current turn listening from the returned
revision with `uv run busybar-viz session events SESSION_ID --after REVISION
--wait 55 --json`. Optional harness-native bridges are convenience layers,
not the portable contract.

A change that alters pixels is not evidenced until there is an artifact SHA
and a `compare` against the prior artifact. For a registered scenario, render
the before artifact before editing (or from the corresponding earlier
checkout), and record deliberate acceptance by updating `viz-baselines.toml`.
CI runs both `doctor` for required audits and `baseline check` for pixel drift;
an unacknowledged registered-scenario change fails. The device's physical
limits are checks in `busybar_viz/device_laws.py` — the ~30% contrast floor
and the minimum
feature size — so a region carrying text or a meaning-bearing colour should
have a `region.contrast_floor` check rather than a reviewer's opinion. That
floor went unenforced for months and a status clock shipped below it.

With a bar reachable, `uv run busybar-viz capture --json` publishes the
current framebuffer as a read-only, correctly-labelled artifact — the
`framebuffer-captured` rung of the evidence ladder in the same store and
`compare` pipeline as offline renders. It is the only visualizer command that
acts as an external network client; `serve` only opens the requested review
socket.

State visual confidence precisely: **renderer-verified** for deterministic
native pixels and tests, **gap-previewed** after simulated LED spacing,
**framebuffer-captured** for device readback, and **hardware-observed** only
after checking the physical panel. Only the last supports claims about actual
contrast, apparent pixel size, animation feel, or on-device legibility.
Generating a gap preview does not prove it was inspected; record that reviewed
assertion in the session journal against the exact immutable artifact SHA.

## Architecture

Develop against a bar on your desk over USB; run it in production on any
always-on host that can reach the bar over the network. Same code both places —
that is the point of the split, and the reason nothing in `apps/` may hardcode
a host. See [`docs/architecture.md`](docs/architecture.md).

**No vendor cloud.** The bar is reachable on your own network, so routing
through `api.busy.app` would add a dependency, a round-trip and a privacy
surface for no capability you don't already have. It also doesn't work in the
other direction: hosted platforms (Lambda, Railway and friends) cannot reach a
bar behind your router, so they are not the app tier. The most a cloud
component should ever be here is a dumb mailbox the host polls outbound.

Write every app as if it will be moved to another machine:

- All device access through `busybar_dev.connect()`; configuration via
  environment (`BUSYBAR_HOST`, `BUSYBAR_TOKEN`), never hardcoded hosts.
- No platform-specific calls in app logic. The one exception is
  `busybar_dev/tts.py`: supported Linux production installs require Kokoro and
  its verified model bank. Linux selects `espeak-ng` if Kokoro cannot be
  imported or its model bank is unavailable; direct macOS development uses
  `say`. The supported installer rejects a fallback-only Linux configuration.
- Long-running apps must shut down cleanly on SIGINT/SIGTERM — they run under
  systemd — and clear their draws on exit. Note that `asyncio.run()` joins
  default-executor threads at shutdown and can delay SIGTERM handling until
  systemd escalates to SIGKILL; use a daemon thread and `call_soon_threadsafe`.

## barkeep — the control plane

`barkeep/` is the always-on daemon. It parents every app process (one
*foreground* app owns the display; *background* apps stay dark until an event,
then **take** the display briefly and hand it back — they do not overlay, see
the draw model above) and serves a web UI and JSON API on port 8080.

Apps are declared in `apps.toml`: kind, entrypoint, and typed config keys —
`type = "number"|"email"|"text"`, `choices = [...]` for a one-of enum, or
`type = "multiselect"` + `choices` for a subset stored comma-separated. Those
drive the UI's generated editor; multiselect values are validated and reordered
into declared order server-side, and an empty selection is a 422. Number keys
may declare finite `minimum`/`maximum` bounds, `requires = [...]` expresses
cross-key presence constraints, and `format = "timezone"` validates an IANA
zone. A submitted blank normally removes the per-app override and reveals
shared/default config; declare `blank_is_value = true` only when `KEY=` is
itself meaningful (for example, anonymous contact or automatic station
discovery).

Per-app overrides live in gitignored `config/<app>.env`, layered on the shared
`.env`. Desired state persists in `config/barkeep-state.json` and is validated
against the registry at startup, so a stale entry can never keep the daemon
down. Children get `BARKEEP_MANAGED=1`. The radio keepalive is **off** by
default — it measured harmful under load; `BARKEEP_KEEPALIVE=1` only to re-run
the A/B.

Run it with `uv run -m barkeep`. Deploy unit is `deploy/barkeep.service`; see
[`deploy/README.md`](deploy/README.md).

**Its trust model, stated plainly:** barkeep has no authentication by default
and binds to `127.0.0.1`. Every route — read and write — is open to anything
that can reach port 8080. That includes reading the framebuffer and app logs,
switching or stopping the foreground app, restarting an app, and writing any
declared config key. Loopback is the safe default because barkeep controls host
processes and configuration, not merely the display.

Keep the default bind and reach the UI through an SSH tunnel where practical.
To expose it on a controlled LAN, set `BARKEEP_BIND` explicitly and configure
a strong `BARKEEP_TOKEN`; operational `/api/*` routes then require the
credential, while `/api/session` validates the token supplied for login.
`BARKEEP_TLS=1` serves HTTPS with a generated self-signed certificate —
encryption without identity, closing passive token capture — and
`BARKEEP_TLS_CERT`/`BARKEEP_TLS_KEY` swap in an operator-trusted pair.
The web UI's HTTPS section (`/api/tls`) stages a pasted PEM pair to
`config/tls/` after validating it; private-key uploads are accepted only over
HTTPS or loopback/SSH-tunnel HTTP. An uploaded pair alone turns HTTPS on at the
next restart, env-pinned pairs stay authoritative, and barkeep never restarts
itself to apply one (the unit is `Restart=on-failure`).
The daemon warns when it is exposed with no token. Do not port-forward it.
`SECURITY.md` is the full statement, and is what an outside reader should be
pointed at.

Two additional controls reduce browser-originated requests but do not
authenticate clients:

- Mutating requests must carry `Content-Type: application/json`, which forces
  a CORS preflight this server never answers. That closes drive-by CSRF from
  a page on the LAN.
- The `Host` header must be an IP literal, `localhost`, this machine's own
  name, or something in `BARKEEP_ALLOWED_HOSTS`. Without that, a page that
  rebinds its own domain to this host becomes same-origin and the JSON rule
  above is satisfied. Set `BARKEEP_ALLOWED_HOSTS` if you reach the UI
  through a reverse proxy or any name that is not the machine's own.

Config keys reaching a child are restricted to those declared in `apps.toml`,
and values are single-line — so an undeclared `LD_PRELOAD` or `PYTHONPATH`
cannot be smuggled into a spawn. Keep it that way: a new config key that
reaches a shell, a filesystem path, or an interpreter is controllable by every
caller allowed to use Barkeep's API.

**Backend/frontend separation is a hard rule here:** `supervisor.py` knows
nothing about HTTP, routes are thin adapters, and `barkeep/static/` talks only
to `/api/*`.

## Working in this repo

- Client library is **busylib** (official, on PyPI). Follow
  `docs/busylib/AGENTS.md` — especially: don't invent methods; prefer busylib
  over raw HTTP; payloads as `busylib.types` models; stable element `id`s.
- `busybar_dev/` has the repo helpers: `connect()` (host/token resolution),
  `tts.py` (speech → `.snd`), `screen.py` (screenshot both displays to PNG —
  use it to *show* results; the panel disagrees with the preview more often
  than you would think).
- New app: `uv run scripts/new_app.py <name>` copies the template and
  registers the app in `apps.toml` in one collision-safe step (with a commented
  viz block ready to enable). Then `uv run apps/<name>.py --dry-run` before
  touching the device — the template's dry run also runs
  `busybar_dev.lawcheck` over the built payload, catching draw-law violations
  such as non-ASCII text and duplicate element ids before hardware sees them.
- While a visual is still moving, audit every iteration with
  `uv run busybar-viz view FRAMES.png --json` — any in-development PNG frames
  or an app's enlarged `--preview` output, zero setup. Declare what must
  read: `--region name=x0,y0,x1,y1 --ink name='#RRGGBB'` runs the device-law
  checks there. Do not hand-roll a render script; `view` gives the same look
  plus an artifact SHA and the checks.
- For raster/native-asset visuals, expose one pure zero-argument production
  renderer that returns `{display: (PIL frames, fps)}` and is also used by
  the real app path, then register it as data with a `[<name>.viz]` table in
  `apps.toml` (renderer seam, displays, regions with rects/inks). That gives
  the app a registered `<name>/default` scenario with device-law checks and no
  adapter module. CI runs its required audits with `doctor` and pins its default
  pixels with `baseline check`. Never make a separate visualizer-only imitation
  or import `busybar_viz` from the app.
- Promote to a hand-written adapter only when the app needs typed controls,
  semantic input replay, fault injection, or ink-reference proofs. Plan the
  no-overwrite adapter/test pair with `uv run busybar-viz scaffold <name>
  --renderer apps.<name>:render_visual --display front --json`; review, rerun
  with `--write`, then make the printed explicit registry edits.
- Before a visual claim, render its registered scenario, inspect exact and
  gap-aware artifacts under the `busybar-viz` skill, and compare it with the
  prior accepted SHA. This is offline evidence, not a device or physical-panel
  check.
- Smoke test the stack: `uv run apps/hello.py` (draw + screenshot),
  `uv run apps/hello.py --clear` when done.
- **Leave the bar as you found it:** clear test draws, don't leave loops
  running. Don't run an app by hand while barkeep is running the same one —
  two writers fight over the display.
- Run `uv run pytest -q` before you commit. The suite needs no hardware.

## Nothing personal in tracked files

No personal addresses, operator coordinates, hostnames, or tokens in anything
git tracks. Personal values live in `.env` (gitignored) and are documented in
`.env.example`. Where a service requires a contact — the NWS User-Agent asks
for one — it comes from configuration (`SKYSTRIP_CONTACT`) and defaults to
blank, never to somebody's address. A User-Agent string is not the place to
leak an email, and it has happened here before.

`SKYSTRIP_LIGHTNING_WS` is an intentional exception to the usual “declare
every app key in `apps.toml`” rule. A relay URL may contain credentials;
Barkeep's config API returns declared values without redaction. Keep this one
only in owner-readable `.env`, never in the generated app editor, until
Barkeep has a real secret-field contract.

## Licensing

GPL-2.0-or-later. The copyleft requirement comes from `busybar_dev/anim.py`, a
port of upstream GPL-2.0-or-later firmware tooling. The or-later grant keeps
the Apache-2.0 Astral dependency compatible under GPLv3. Keep the attribution
header intact. Anything newly
vendored gets recorded in [`NOTICE.md`](NOTICE.md) with its licence. Skystrip's optional
lightning input ships without an endpoint or data: Blitz compatibility is only
a schema contract. Blitzortung restricts raw access to participants or people
it explicitly approves, and external apps must use a separate authorized
relay; see [`NOTICE.md`](NOTICE.md). NWS is public domain; busylib is MIT.

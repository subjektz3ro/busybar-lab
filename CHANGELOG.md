# Changelog

## Unreleased

### Busylib 2 framebuffer contract

- Upgrade the locked official client to Busylib 2.0.2. Barkeep preview,
  screenshot helpers, and `busybar-viz capture` now consume the client's
  canonical RGB888 framebuffer bytes directly; asymmetric red/blue fixtures
  guard both displays against a second channel swap. The installed client's
  private USB-controller seam and transparent notification-only payloads are
  pinned by offline regressions.

### BUSY Bar Lab project identity

- Rename the repository and Python distribution to **BUSY Bar Lab**
  (`busybar-lab`); the existing `busybar_dev` and `busybar_viz` import names
  remain unchanged. Deploys whose remote checkout is still named
  `busybar-dev` must set `BUSYBAR_DEPLOY_PATH=busybar-dev`, or migrate/reclone
  that checkout at `busybar-lab`, before using `deploy/ship.sh`.

### Verified speech installation

- Define the complete supported production stack as Python 3.11–3.13 on
  64-bit glibc 2.28+ Linux (`x86_64`/`aarch64`) with Kokoro required. The
  installer now rejects unsupported hosts, verifies the model hashes, and
  completes a real synthesis before installing or starting Barkeep; routine
  deploys recheck that same engine before restart.
- Keep `espeak-ng` only as emergency runtime resilience and `say` for direct
  macOS development. Retained model files no longer force a broken neural
  import, and a blank voice-directory setting again means the checkout's
  default `voices/` directory.

### Public provider-use boundary

- Start a fresh Barkeep state in **STANDBY**, with Skystrip's linked
  Open-Meteo/RainViewer credits and public-service limits visible above the
  foreground selector. Existing saved foreground choices still restore.
- Require standalone Skystrip modes that poll built-in providers to use the
  explicit `--enable-network-providers` flag and print mode-specific
  attribution before their first request. `--preview` remains fully offline;
  `--once` still talks only to the bar and starts no provider poller.

### Status corner: hue is the contrast, and the hue is yours

- The clock/temperature ink is now `SKYSTRIP_CLOCK_INK` — a closed choice
  of `orange` (default — the bar's own accent colour; teal was tried and
  panel-vetoed), `pink`, or `red`, flippable live from Barkeep's config
  editor. The Time Machine tell moved from amber to lilac: amber is the
  orange clock's next-door neighbour, and lilac clears every corner
  background on merit, which amber never did. Hardware proved the mechanism ("red reads very well"):
  a saturated hue clears the panel's floor by channel separation at every
  hour and scene, so the corner needs no card, no shadow, no halo, and no
  ink-flipping machinery — the black/white flip, its weather estimator,
  the sun-in-corner override, the forest exception, and the overhanging
  bough are all deleted. Every offered ink is swept by the contract tests
  against the corner's measured background extremes (including a
  dawn-blue pixel and two horizon-glow blues the viz check caught under
  the floor), and the lilac tell is swept with them — so neither an
  unreadable clock nor an unreadable scrub state is configurable. The
  `legacy_amber` fault keeps its era's luminance-only criterion so the
  audit still demonstrably rejects what once shipped.

### Status corner: shadow instead of card

- Replace the black status card with a translucent text shadow: every
  pixel touching a stroke is dimmed to 22% instead of a block being
  blacked out, so the scene stays visible through the corner while the
  ink keeps at least twice the panel's ~76 contrast floor against its
  worst neighbour under any sky. The corner's reserved span still
  anchors the backroads train band; the contract tests now pin deep
  shadow, scene-through-shadow, and no-card-creep instead of a box.

### Skystrip scenes under blind review

- Give backroads the life it measured last in. Against the other five
  scenes it had the fewest distinct hues (8, tied last) and the least
  motion (7 changed pixels per frame against house's 35 and lakefront's
  322) — and moving traffic out of the loop, while right, had removed its
  only continuous motion. Two additions, both measured: a wind wave and
  summer fireflies through the near verge, which was 216 pixels of one
  flat colour in the foreground; and a real farmstead — a red barn with
  its white X-brace door and yard lamp, placed left of centre where the
  scene's structures live between the poplars, plus a second lit window,
  porch lamp and cold-weather woodsmoke on the farmhouse. Backroads now
  measures 14 hues and 30 moving pixels per frame. The verge wave lives
  below the road on purpose: rows 9-11 belong to the traffic overlay and
  must stay still. The wind is drawn as a change of SHAPE, not of
  brightness: each verge column carries a blade height, the travelling
  gust lifts crests a row taller, and the tallest tips bend downwind —
  a luminance ripple was invisible on the panel, which is the same
  lesson the house scene's grass fringe learned. The gust itself is one
  broad soft-edged front sweeping the width, not a periodic wave: real
  wind over a field is coherent, so every blade inside the front bends
  the same way at the same time and what travels is a single band of
  bent grass. A periodic sine read as a metronome; per-tuft randomness
  read as "things moving"; only the coherent front reads as wind.

- Take backroads traffic out of the scene loop. Baked into an 8-second
  animation the cars repeated ~7.5 times a minute, and because the texture
  seed only turned over every ten minutes the SAME vehicles made the same
  trip about 75 times before anything changed. Traffic is now a one-shot
  overlay on the three rows it actually occupies (~50 kB per episode
  against 186 kB for the whole scene), with its own entropy every time:
  individual speeds, random arrival gaps, mixed vehicles, and a density
  that follows the clock — rush hums, the small hours are a lone pair of
  headlights. Freed from a loop, vehicles no longer have to complete whole
  journeys per cycle, which is what lets them have separate speeds at all.
  Cars still pass behind the poplar trunks, using the same
  renderer-derived foreground mask as the freight.
- Reshape the train after My Neighbor Totoro: a short rural passenger
  train — three to five carriages in one dark livery with paired warm
  windows and a headlamp — instead of a mile of American boxcars, and it
  now crosses every 2.5-6 minutes rather than every 4-10.

- A crossing freight now passes BEHIND the poplars instead of slicing
  their crowns: the overlay learns which sky-band pixels are foreground
  trees by diffing the same frame rendered with `lane=False`, and repaints
  them over the consist. No geometry is restated — the mask comes from the
  renderer itself. Verified across a full 215-frame crossing with zero
  crown pixels overwritten.
- Delete the road-step softening loop, dead since the road went level
  (`_road_R` returns a constant, so no step ever occurs), along with the
  seam colour the settled-snow exclusion no longer needs.

- Fix the poplar lane's occlusion: the crowns reached the road itself,
  five pixels wide, so foliage owned 25 of the road's 52 columns at car
  height and a 4px car vanished entirely between trees before
  reappearing on the far side. The crown now sits above the traffic band
  and the single-pixel trunk crosses it, so a passing car is split by one
  pixel and never disappears. Two contract tests pin it, rendering the
  scene with and without the lane so the assertion measures the trees
  themselves rather than restating their colours.
- Narrow two false positives in the personal-data guard (Rec. 709 luma
  coefficients read as a coordinate pair; a quoted no-reply commit
  trailer read as a contact address) with exact-match allowlist entries,
  plus a self-test proving both detectors still fire and that neither
  exemption widened into a domain or a real coordinate.

- The freight now crosses the FULL panel, edge to edge: the train band
  once abutted an opaque corner card, and when the card era ended the
  trains were left materializing in open sky at x=19. The overlay now
  spans the whole width with the status digits repainted on top of every
  frame by the production painter — the train passes behind the clock —
  and each crossing waits for a window inside the current minute so the
  baked clock can never go stale mid-crossing.

- Backroads becomes a poplar lane, chosen from a browser lookbook of
  six scene directions and four tree designs (real-renderer skies, LED-gap
  simulated, day/fall/night): five seeded poplars — each its own height,
  its own autumn hue, a bare twig spire in winter, tips swaying on whole
  gust cycles — replace the barn, oak, bushes, and pole line entirely.
  Traffic strobes behind five trunks per crossing; the farmhouse and the
  Christmas conifer keep their places.
- (Superseded within the day, kept for the record:) the bushes were first
  replaced by a red barn the road runs behind —
  off to the left under the clock's shoulder, not centre stage — wearing
  the white X-brace door that says barn at any distance, a hayloft lamp
  at night, and snow on its gambrel roof. Two foreground trees rise
  through the road band mid-scene and far right, so traffic now threads
  barn, tree, oak, tree: vanishing and re-emerging four times per
  crossing (the occlusion moment, kept and multiplied by request).
- Densify the grove: six front trees plus a hazy trunkless back row
  standing offset in the gaps — density through depth while every tree
  stays countable. Western crowns stay below the status corner: autumn
  foliage is orange-family, the clock's own hue.
- Ease the unlit night rail (a blind squint-review read it as "a broken
  underline attached to the clock") — steel by day, a whisper by night.


- Rework the grove: five separated, grounded trees (2px trunks, crowns on
  the treeline) on a dark meadow with seasonal sun dapples and moon pools,
  replacing a haze-wall composition that read as one continuous wash.
- Make backroads make sense: the invented grain-elevator occluder is gone —
  a crossing freight now rides a continuous ridge-top rail from one screen
  edge to the other, passing only behind the status card. Level road, lobed
  hedgerow above the 30% contrast floor, recognizable farmhouse, oak that
  stands on the verge instead of through the pavement.
- Fix the status corner card: a fixed, centered 20x7 card that reaches the
  frame corner, replacing a text-bounding-box punch that left a lit sky
  sliver at column 0 in every scene and breathed with the string
  (baselines re-pinned).
- busybar-viz: parameter budgets now bind leaf values rather than the
  str() of whole lists — `view` on frame directories works to the
  documented 240-frame limit instead of failing at four — and `view --each`
  publishes several inputs as separate artifacts in one invocation.
- Both skills gain the session's composition lesson: a naive-viewer pass
  (verify the picture, not your intent) and the "would a stranger name
  this object correctly" law, each with the day's failures as examples.

### Pre-release sweep

- Fix `ship.sh`'s post-deploy watch hint: the `journalctl` command it prints
  now resolves the service user on the host (and respects a fully-qualified
  `BUSYBAR_DEPLOY_SERVICE`) instead of expanding the local username.
- Make the installer safe non-interactively: the coordinate prompt exits
  with instructions at EOF instead of looping forever, the service prompt
  accepts `Y`/`yes` and re-asks on garbage, `curl` is required only on Linux
  where it is actually used, and the bar-reachability probe uses
  `--no-sync`. A pre-existing `.env` now explains that it skips the
  interview and how to re-run it; a generated `.env` documents the
  `BARKEEP_TOKEN`/`BARKEEP_TLS` keys an operator needs next.
- Refuse or survive malformed configuration by name: a non-numeric
  `BARKEEP_PORT` stops startup with the key named, a malformed Skystrip
  coordinate/timezone exits with one clear sentence instead of a systemd
  restart-loop traceback, a bad `SKYSTRIP_TTS_SPEED` warns and speaks at
  1.0, and an unreachable bar greets `apps/hello.py` with the connection
  message rather than a stack trace.
- Put Skystrip before DSN in the foreground-card order because it is what the
  installer just interviewed the operator about. Card order no longer means
  auto-start: the fresh-state behavior is now **STANDBY**, as noted above.
- Complete the `.env.example` contract: `BARKEEP_PORT`,
  `BARKEEP_KEEPALIVE`, `BUSYBAR_CACHE_DIR`/`BUSYBAR_STATE_DIR` (runtime
  overrides, not just installer inputs), `SKYSTRIP_TTS_SPEED`, and
  `SKYSTRIP_VOICE_DIR` are now documented, and a two-direction registry
  test enforces that the template and the runtime's actual reads match.
- Self-host the barkeep UI's three typefaces (SIL OFL 1.1, licences
  vendored) so a loopback control plane makes no CDN request and renders
  identically air-gapped; a test pins the no-external-origin property.
- Correct documentation that had drifted from the code: git is required by
  the installer (not "install once, never update"), CONTRIBUTING's CI
  block now includes the visual gates and the `new_app.py` scaffold,
  `docs/architecture.md`'s module map includes `busybar_viz/`, and the
  README Quickstart starts with the offline smoke test and states the full
  clone-to-deploy path, including that copying `.env.example` skips the
  installer interview.

## v0.3.0 — 2026-08-11

This release adds a device-law-aware visual audit workflow, hardens the
repository and deployment path for public distribution, secures optional
remote Barkeep administration, and repairs alert-aware Skystrip navigation
and weather narration.

### busybar-viz

- Add a standalone visual-debugger framework with registered Skystrip and
  app-neutral conformance scenarios: deterministic front/back tracks, timed
  semantic input replay, exact RGB evidence, LED-gap/native previews, contact
  sheets, heatmaps, structured audits, and content-addressed comparisons.
  Decode repository PNG and native `.anim` assets with bounded validation and
  preserved firmware timing while keeping renderer, framebuffer, and physical
  evidence explicitly separate.
- Add an app-neutral conformance adapter, a production-renderer scaffold, and
  a closed adapter registry so app-specific state stays outside the visualizer
  core. Render workers accept only registered scenarios, strip app/device
  credentials, block ordinary sockets, and enforce finite queue, time, frame,
  output, and artifact budgets. The tool remains outside Barkeep.
- Add `busybar-viz view`: audit ad-hoc in-development PNG frames — single
  files, ordered sets, directories, or exact integer enlargements such as an
  app's `--preview` output — with no registered scenario. Declared
  `--region`/`--ink` areas run the same device-law contrast-floor and
  feature-size checks as registered scenarios, and the result is the same
  immutable, `compare`-able evidence bundle, with track provenance and notes
  recording that the frames are unregistered input (`approximate` when
  downsampled). `--emit-declaration APP` returns a paste-ready `[APP.viz]`
  block for the invocation's regions, inks, and budgets.
- Register default visualizer scenarios as data: an optional `[<app>.viz]`
  table in `apps.toml` names one pure zero-argument production seam inside
  the apps package (plus declared regions with rects, inks, and feature-size
  budgets) and materializes `<app>/default` through one generic adapter — no
  hand-written adapter module. Declarations parse fail-closed, import lazily
  at render time inside the offline guard, and cannot select code outside
  `apps.*`. Additional `[<app>.viz.scenarios.<name>]` tables register fixed
  named views; DSN declares representative Network, Instrument, and Distance
  fixtures this way. Hand-written adapters remain the tier for typed controls,
  semantic input replay, fault injection, and independent ink-reference
  proofs.
- Pin every registered scenario's accepted pixels in `viz-baselines.toml`.
  CI now runs `doctor` for required audits and `baseline check` for pixel
  drift, missing baselines, and stale entries; `baseline update` records a
  deliberate change. Digests cover fps, frame count, and RGB pixels rather
  than artifact ids, so tool-only changes do not churn acceptance.
- `busybar-viz capture` publishes the device framebuffer as a read-only
  artifact whose request metadata omits the device host, with
  `framebuffer_observed` provenance and an automatic `framebuffer-captured`
  evidence level in the same store and `compare` pipeline as renderer evidence.
- `busybar-viz audio` reports measurable properties of headerless `.snd` PCM
  — duration under the required format, peak, RMS, DC offset, clipping, and
  silence — with an optional waveform PNG. `busybar-viz gc` reclaims
  unreferenced artifacts and comparisons in dry-run-first mode, retaining
  session-cited evidence and protecting recent artifacts for a configurable
  window; baselines pin digests rather than stored artifacts.
- Add a loopback review UI and append-only SQLite session journal shared by
  people, Codex, and Claude Code. Agent-presented artifact SHAs, simulated
  inputs, feedback, change requests, approvals, and explicit gap inspection
  use the same revisioned event stream.
- `scripts/new_app.py` creates a template app and its `apps.toml` registration
  together, includes a ready-to-enable viz block, refuses relevant collisions,
  and rolls back partial output after a failure. `busybar_dev.lawcheck`
  validates printable-ASCII text and unique element ids; the template applies
  it to dry runs and refuses payloads that violate the device draw laws.
- Reframe the visualizer documentation and skill around its founding intent:
  busybar-viz is the coding agent's eyes on a panel it cannot see. The
  self-serve loop (render or `view` → read `audit.json` and gap previews →
  fix → `compare`) is documented as the primary workflow; presenting to the
  shared session journal is the collaboration loop when a person is
  reviewing. Registered scenarios remain the provenance tier that regression
  baselines and review decisions rest on.
- Add `docs/agent-cookbook.md`, a compact end-to-end path through creating an
  app, seeing each iteration, registering a production renderer, pinning its
  pixels, and collecting separately labelled device evidence. Repository
  instructions and the matching `busybar-app`/`busybar-viz` skills give fresh
  Codex and Claude Code sessions the same workflow and evidence language.

### Public-release hardening

- Make Barkeep loopback-only by default. Direct LAN exposure is now an
  explicit `BARKEEP_BIND` choice, documented alongside strong token, tunnel,
  VPN, and TLS guidance; startup tests cover safe defaults and deliberate
  network binds.
- Serve Barkeep over HTTPS on request: `BARKEEP_TLS=1` generates a
  self-signed certificate once under `config/tls/` and reuses it — real
  encryption without a proven identity, closing passive token capture on a
  LAN — while `BARKEEP_TLS_CERT`/`BARKEEP_TLS_KEY` swap in an
  operator-trusted pair. A half-configured pair refuses startup instead of
  silently serving plaintext, and the session cookie is marked `Secure`
  when served over TLS. Generated credentials are validated and returned to
  owner-only permissions on every restart.
- Make replacing that certificate a paste, not an ssh session: the web UI's
  HTTPS section and `/api/tls` validate a pasted PEM pair before staging it
  under `config/tls/` with an owner-only key, and refuse to receive private
  keys over non-loopback HTTP. An uploaded pair alone enables HTTPS on the
  next restart, a rejected upload changes
  nothing, env-pinned pairs are reported rather than shadowed, and the API
  exposes only public certificate facts (source, SHA-256 fingerprint,
  expiry).
- Make clean systemd installs reproducible: create owner-only config, log,
  cache, and state directories before startup; render the unit with the actual
  checkout, `uv`, and writable paths; detect a deployed unit that needs an
  installer refresh. Refuse root-owned installs, require a preinstalled `uv`
  instead of executing a mutable remote bootstrap, and keep every deployment
  origin-backed under narrowly scoped stop/start permissions. DSN cache and
  Skystrip scene state now stay inside those managed roots.
- Replace Barkeep's Skystrip-specific config branch with registry-declared
  finite bounds, cross-key requirements, and IANA-timezone validation.
- Remove generated coverage data, the unlicensed vendor-document mirror, and
  the owner-local device schema from the distributable snapshot. Keep official
  links, busylib documentation pinned to an explicit upstream revision, and
  precise third-party provenance in their place.
- Correct the project grant to GPL-2.0-or-later, matching the firmware-tool
  source and permitting the Apache-2.0 Astral dependency under GPLv3.
- Add a current-tree public-release gate for private/generated paths, absolute
  home paths, and high-confidence credential signatures. Expand the mypy gate
  to DSN, Skystrip, and the visualizer without blanket ignores, and make the
  Python 3.11/3.13 CI matrix select the Python version it names.
- Cap Barkeep mutation bodies before JSON parsing, deny framing and MIME
  sniffing on every response, and keep busybar-viz host validation active when
  its review UI is deliberately exposed beyond loopback.
- Make every checked-in demo GIF reproducible from an explicit public
  Greenwich fixture, with a byte-for-byte `--check` mode that cannot inherit
  an owner's environment.
- Add a device-free Hello `--dry-run`, a first-party documentation index, and
  accurate app-specific offline instructions. Remove stale implementation
  work orders and unreachable private-history references.

### Skystrip

- Round floating-point precipitation percentages before narration, pronounce
  noon and midnight naturally, and speak decimal points in generic TTS input
  instead of treating them as sentence breaks.
- Speak the NWS event name in severe-weather reports instead of collapsing
  every product to a generic warning, while retaining the generic fallback
  when a source supplies no event name.
- Demand a fresh versioned scene after alert acknowledgement or all-clear, so
  the device cannot remain black behind a stale path-cached scene until the
  next wall-clock redraw.
- Remove the hardcoded undocumented raw-lightning servers. Live strike effects
  are now disabled by default and start only with an operator-supplied,
  authorized `wss://` relay using the bounded Blitz-compatible strike schema;
  insecure endpoints and malformed, stale, future, or invalid-coordinate input
  fail closed without taking down the rest of Skystrip. Credential-bearing
  relay URLs stay in owner-only `.env` and never enter Barkeep's config API or
  application/transport logs. Documentation calls out the required Barkeep
  daemon restart after this hidden shared setting changes.
- Keep rain and cloud motion running through the full native lightning lease;
  the unchanged half-second strike pulse now hands off through moving storm
  frames instead of pinning a still frame over the live sky for 1.5 seconds.
- Reject a delayed lightning upload when a newer same-scene asset has already
  reached the display.
- Publish an explicit geographic support matrix: global coordinate-based model
  weather, NWS-only station/forecast/alert enhancements, provider-dependent
  radar and lightning, northern-temperate seasonal art, and unsupported
  offshore/polar edge cases. Fresh Barkeep config now leaves the required
  coordinate pair blank instead of presenting `0,0` as a meaningful default.
- Validate Skystrip's coordinate pair, ranges, units, and IANA timezone before
  Barkeep or the installer writes them; bundle the small `tzdata` fallback so
  timezone validation also works on slim hosts without a system zone database.
- Check RainViewer's coverage mask before allowing a blank radar tile to
  declare the location dry; uncovered and polar points now fall through to
  Open-Meteo or station evidence instead of suppressing modeled rain. Validate
  bounded echo PNGs and provider frame time, and retain source-aged rain,
  snow, thunder, and snow-depth leases instead of refreshing old phenomena at
  HTTP receipt time.
- Use NWS `/points` itself as the boundary for every NWS enhancement: outside
  coverage, station history and CAP alert/siren polling stand down while the
  global model keeps running. A missing station list at an otherwise covered
  point no longer disables point alerts, and the one-shot report path no
  longer waits for an NWS forecast that cannot exist there.

## v0.2.1 — 2026-08-09

This patch repairs two v0.2.0 device regressions and removes the retired
speech engine from the supported install.

### Skystrip controls and lightning

- Restore START scene cycling after a daemon restart while the physical
  selector is already OFF. Because the device status stream sends deltas
  rather than an initial selector snapshot, an unknown selector now requires
  both a committed Skystrip view and a `NOT_STARTED` BUSY timer snapshot.
- Keep explicit OFF as the immediate app-control path, while selector changes,
  stream gaps, snapshot failures, and active BUSY/CUSTOM timers continue to
  fail closed. The same ownership check protects alert acknowledgement and
  global audio STOP.
- Return distant lightning to a backdrop-only effect. Only a strike within
  25 km pulses the top LEDs; farther reports can subtly relight the sky but no
  longer look like an unexplained white status flash over clear local weather.
- Timestamp detector reports and discard them after ten seconds, so a strike
  queued during a weather/display stall cannot replay after recovery.
- Snapshot the weather LED together with the rendered scene before upload, so
  a feed update during that await cannot put one weather state on the panel
  and another on the top LEDs.

### Kokoro-only speech

- Standardize Skystrip on Kokoro `am_michael` and keep DSN on `af_nova`.
- Remove the retired speech package, model download branch, speaker-index
  configuration, lock entries, and first-party documentation.
- Download the shared model bank through verified temporary files with pinned
  SHA-256 digests, so a partial or corrupt transfer cannot become resident.
- Normalize an old non-Kokoro Skystrip voice setting to `am_michael` whenever
  the shared Kokoro model bank is present. A macOS system voice remains usable
  when that bank is absent.
- Existing hosts without `voices/kokoro-v1.0.onnx` and
  `voices/voices-v1.0.bin` should rerun `./deploy/install.sh` once after
  updating; display features continue to work and speech otherwise falls back
  to the host engine.

### Forecast narration UX

- A cold double-START now acknowledges immediately with a complete
  `PREPARING...` card and queues one shared background generation instead of
  holding the interaction open through Kokoro synthesis and asset upload.
- When that exact report is resident, Skystrip shows `START TWICE` once. Audio
  never begins later by itself; the next double-START plays the cached take
  immediately, while navigation or a weather alert invalidates the old notice.
- Cache reports under a deterministic hash of exact text and voice, keep the
  live take and its predecessor bounded, and advance a path the device reports
  missing or unplayable to an immutable repair generation.

### Deployment

- Fetch from the configured remote on the host, then run an explicit
  `uv sync --locked` before restarting barkeep. Non-login SSH shells fall back
  to the installer's `~/.local/bin/uv`, and a failed sync aborts before a new
  process can start against stale dependencies.

## v0.2.0 — 2026-08-09

This release makes Skystrip's alert interruption episode-aware and narrows the
audible alarm to the explicitly selected CAP policy.

### Alert safety

- Parse bounded NWS CAP fields and reject non-Actual, inactive, expired,
  cancelled, test, drill, malformed, or oversized products.
- Show the alert card and red status-light pulse for active Actual
  Severe/Extreme Warnings and Emergencies. Watches and advisories do not take
  the alert presentation.
- Sound the siren only when CAP `severity` is exactly `Extreme`; ordinary
  thunder, Watches, and Warnings whose severity is `Severe` remain silent.
- Track acknowledgement by CAP identifier/reference lineage. A new episode or
  material escalation rearms; a routine update to the same acknowledged
  episode does not.
- Serialize PLAY/STOP operations so acknowledgement, authoritative clearance,
  and shutdown cannot be overtaken by a late siren request.

### Alert and Time Machine UX

- Accept acknowledgement from an available button or wheel action without
  also applying that input to the underlying view.
- Treat START as app-owned only after an explicit slider-OFF status, so BUSY
  and CUSTOM controls cannot acknowledge alerts or disturb global audio.
- Retire the alert card and stale transient IDs on acknowledgement, then
  restore the selected live or exact Time Machine view. An expired live
  weather lease is never resurrected underneath the dismissal.
- Keep the red status-light pulse active after acknowledgement until a
  successful feed all-clear or the CAP product's own expiry ends the episode.
- Preserve dismissal/restore intent across BUSY Bar priority refusal and stop
  an active siren when its alert clears.
- Replace firmware text scrolling with a host-rendered native-animation
  marquee, and bound/sanitize the input so every product name remains complete
  inside the 72×16 display.
- Generate the Extreme-alert tone locally as deterministic 44.1 kHz PCM and
  cache it on-device under a content hash, removing the dependency on an
  untracked siren file.
- Verify resident siren size, retry provisioning for the daemon lifetime, and
  move partial/unplayable content to a fresh immutable repair path. Older
  generations retire only after a full-tone playback grace, so a restarted
  process cannot delete audio that its predecessor may still be playing.

### Native effects

- Replace the repeated opaque white-panel flash with a short native animation
  that relights only the rendered sky backdrop. Scene foregrounds and status
  ink stay visible while the top status LEDs pulse with the strike.
- Remove recurring synthetic sheet flashes from generic thunder scenes; only
  a nearby live strike report now triggers the finite lightning effect.
- Bound detector bursts and collapse queued reports to the nearest strike, so
  a backlog cannot keep flashing after the live storm has gone quiet.
- Suppress full-scene lightning and meteor assets until a fresh live scene is
  actually resident; an effect can never become a plausible fallback picture
  while base weather is unknown or expired.
- Bake each meteor into one native animation so its geometry actually moves
  on firmware that ignores geometry mutations on an existing element ID.

### Weather and astronomy truth

- Wait for a complete, source-timestamped NWS observation or Open-Meteo
  current snapshot before the long-running app makes its first live draw.
- Validate provider snapshots before committing them, so stale, incomplete,
  non-finite, or schema-wrong fields cannot half-overwrite last-known-good
  weather.
- Stage the NWS observation across the Open-Meteo await and atomically publish
  one fused snapshot; if the model call fails, commit the already-validated
  station fallback once instead of exposing a transient half-update.
- Give live weather a two-hour lease; when both base-weather sources are
  missing or stale, stop refreshing the old scene and let its native timeout
  clear instead of presenting defaults or expired data as current conditions.
- Describe Time Machine history as recent-past model rows from Open-Meteo's
  forecast endpoint, not observations or archive reanalysis.
- Convert Astral's 0–28 lunar-phase index to synodic-month age before drawing
  the phase cue, while continuing to describe its on-strip position as art.
- Respect each NWS forecast period's declared temperature unit when composing
  the spoken report.

### Operations and documentation

- Install barkeep—not a nonexistent standalone Skystrip unit—as the sole
  optional systemd service.
- Declare the directly imported HTTP and WebSocket clients as runtime
  dependencies.
- Run report synthesis in a daemon worker so a real-time TTS job cannot hold a
  systemd shutdown open after Skystrip has stopped.
- Add the public scene-rotation configuration and accept an email or URL as
  the NWS User-Agent contact.
- Keep installer-generated `.env` content as parser data rather than sourcing
  it as shell code, so ordinary URL query strings remain inert.
- Validate coordinates as a finite, in-range pair and warn explicitly when an
  install is still rendering the deliberate 0,0 fallback.
- Document observed/model/decorative content boundaries, alert limitations,
  and Open-Meteo/RainViewer source terms and attribution.

Skystrip remains a secondary ambient warning channel. Network, source, host,
BUSY Bar ownership, polling, and audio failures can delay or suppress an
alert; retain official emergency-warning channels.

## v0.1.0 — 2026-08-07

Known-good pre-DSN-live-instrument baseline, including the original Skystrip
scene system.

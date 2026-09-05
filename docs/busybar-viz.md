# busybar-viz

`busybar-viz` renders and audits BUSY Bar display output for automated and human
review. It produces deterministic renders, per-pixel checks against measured
device constraints, LED-gap simulations, and immutable comparison artifacts.
The CLI and review UI expose the same artifacts.

The basic offline workflow is:

1. **Draw** — write or edit rendering code.
2. **Look** — `view` the frames it produced (zero setup), or `run` its
   registered scenario.
3. **Read** — `audit.json` names the failing check and its frame-specific
   measurements; contact sheets show exact output, and gap views simulate the
   panel's measured LED spacing for eyes and vision models alike.
4. **Fix and diff** — render again, `compare BEFORE_SHA AFTER_SHA`.
5. **Accept** — for a registered scenario, `baseline update` pins deliberate
   new pixels in `viz-baselines.toml`, committed in the same change. CI fails
   unacknowledged drift.

The tool supports three input modes:

1. **`view`** audits unregistered in-development PNG frames and records their
   source as unregistered input.
2. **A `[<app>.viz]` declaration in `apps.toml`** registers the app's default
   scenario as data: one pure zero-argument renderer seam plus declared
   regions, no adapter module. The pixels provably come from production code;
   CI runs `doctor` for required audits and `baseline check` for pixel drift.
3. **A hand-written adapter** adds typed controls, semantic input replay, fault
   injection, and independent ink-reference checks. Use an adapter when those
   features are required.

The hand-written adapters currently cover the app-neutral conformance fixture
and Skystrip; DSN registers `dsn/default` through its `apps.toml`
declaration. Any app can use either registered tier; the core does not know
about Skystrip, DSN, weather, or other product-specific state.

## Boundary

The visualizer is deliberately separate from Barkeep:

```text
pure production renderer            ad-hoc PNG frames or repository PNG/.anim
          |                                          |
explicitly registered adapter            direct file ingestion (view/asset)
          \                                         /
         exact front/back tracks + input/signals
                         |
       audits + content-addressed evidence bundle
                    /                 \
                  CLI       localhost review UI
                                      |
                            append-only SQLite journal
                                      |
                            Codex and Claude Code
```

- Rendering does not connect to a BUSY Bar, call a device API, or require
  external network access. The optional review UI opens an HTTP server,
  loopback-only by default, as described below. `capture` is the only command
  that acts as an external network client: it reads the device framebuffer and
  is read-only. It is never wrapped in the offline guard and never runs as part
  of rendering, audits, or CI.
- It does not add routes, state, or dependencies to Barkeep.
- Barkeep's preview remains live framebuffer readback. It is not a visualizer
  render and does not identify the frame currently playing inside a native
  `.anim`.
- Registered adapters are a closed Python registry. HTTP and CLI request data
  can select a scenario id, but cannot select a module, function, executable,
  path, or environment. The CLI-only `view` and `asset` commands ingest image
  files by path from inside the repository — file decoding, never code
  selection or execution — and the HTTP surface has no ad-hoc ingestion at
  all.
- Production app code must not import `busybar_viz`. A narrow adapter wraps a
  pure app-owned renderer in visualizer types.

Every render surface blocks ordinary socket connections while an adapter runs.
The standalone server additionally strips device/app credentials from worker
environments and applies subprocess resource limits. This is a correctness
guard, not an operating-system sandbox: adapters remain trusted repository
code, so their pure/offline contract still matters.

The default data directory is `scratch/busybar-viz/`, which is gitignored. Use
global `--data-dir PATH` before the subcommand to keep an independent store.
Run commands inside a `busybar-lab` checkout; the CLI locates the checkout by
walking upward from the current directory.

The project wheel packages the visualizer CLI and core, but not standalone
copies of `apps/` and their assets. CI installs that wheel in an isolated
environment and deliberately runs it against the active checkout. This is the
supported contract: an installed CLI operating on a cloned `busybar-lab` tree,
not a self-contained visualizer distribution.

## Quick start

No bar is needed:

```bash
uv sync --locked
uv run busybar-viz doctor --json
uv run busybar-viz scenarios --json

uv run busybar-viz run conformance/dual-display-input-replay \
  --set initial_level=3 \
  --set initial_mode=amber \
  --input '{"t_us":250000,"kind":"encoder.delta","control":"level","value":2}' \
  --input '{"t_us":750000,"kind":"button.press","control":"mode","value":true}' \
  --json
```

`doctor` renders and validates every registered scenario with its declared
default controls, without publishing evidence. It returns nonzero and names
the broken scenario when an adapter cannot import, render, or pass its required
default audits.

`python -m busybar_viz` has the same interface as `busybar-viz`. `--json`
emits compact JSON suitable for a model harness or script. Exact request and
result schemas are available without opening source files:

```bash
uv run busybar-viz schema scenario --json
uv run busybar-viz schema render-request --json
uv run busybar-viz schema trace --json
uv run busybar-viz schema evidence --json
uv run busybar-viz schema comparison --json
uv run busybar-viz schema session --json
uv run busybar-viz schema session-event --json
```

## View in-development frames

Use `view` to audit PNG frames without adding an adapter, registry entry, or
review session.

```bash
uv run busybar-viz view scratch/candidate.png --json
uv run busybar-viz view scratch/frames/ --fps 5 --json
uv run busybar-viz view scratch/preview.png \
  --region clock=1,0,26,8 --ink 'clock=#FFFFFF' --json
```

- Native-size PNGs (72x16 front, 160x80 back) preserve their decoded RGB pixel
  values exactly. An exact integer enlargement — such as an app's `--preview`
  output — is detected (or declared with `--scale N`), nearest-neighbour
  downsampled, and marked `approximate`.
- A directory is read as frames sorted by name; multiple paths keep argument
  order. `--fps` defaults to 5 for animations and 1 for a single frame.
- `--region NAME=X0,Y0,X1,Y1` (half-open rect) runs the device-law
  feature-size check on that area; adding `--ink NAME=#RRGGBB[,#RRGGBB...]`
  also runs the contrast-floor check against the declared ink. These are the
  same checks registered scenarios nominate, with the numbers from
  `busybar_viz/device_laws.py`. `--max-isolated N` tolerates deliberate
  single-pixel art such as stars.
- The result is the same immutable, content-addressed artifact `run`
  publishes — audits, contact sheets, gap views, a `compare`-able SHA — plus
  recorded notes stating that nothing verifies these frames came from
  production rendering code.
- `--emit-declaration APP` adds a paste-ready `[APP.viz]` block to the JSON
  result, carrying this invocation's regions, inks, and budgets, so promoting
  an iterated design to a registered declaration is a copy, not a retype.

`view` provides sight; a registered scenario adds declared source provenance.
Iterate with `view`, and when the visual stabilizes, register its app-owned
renderer (see below) so regression baselines and review decisions rest on
pixels that production code provably produced.

## Scenarios and semantic input

A scenario declares:

- a stable id and explicit adapter;
- expected `front`, `back`, or both display tracks;
- typed controls with defaults and bounds or choices;
- semantic input controls such as `encoder.delta` or `button.press`.

`--set KEY=VALUE` supplies a control. Values are parsed as JSON when possible,
so `3`, `true`, arrays, and objects keep their types; otherwise the value is a
string. Repeat `--input JSON` to build a deterministic timeline. Each input is
an object with:

```json
{"t_us":750000,"kind":"button.press","control":"mode","value":true}
```

`t_us` is an integer offset in microseconds. Events must be passed in
nondecreasing timestamp order. A scenario owns the supported control names,
input kinds, values, and boundary semantics; use `scenarios --json` rather
than guessing them.

The conformance scenario is intentionally app-neutral. It renders both display
profiles and responds to timed wheel/button events, making it the first check
for a new client or harness integration:

```bash
uv run busybar-viz run conformance/dual-display-input-replay --json
```

## Declare a default scenario in apps.toml

A table in `apps.toml`, next to the Barkeep app entry, registers the default
visualizer scenario:

```toml
[myapp.viz]
renderer = "apps.myapp:render_visual"
displays = ["front"]
description = "What the fixed fixture shows"

[myapp.viz.regions.label]
rect = [1, 0, 26, 8]          # half-open [x0, y0, x1, y1]
ink = ["#FFFFFF"]             # enables the contrast-floor check
max_isolated = 0              # feature-size budget (0 = no lone pixels)
```

That registers `myapp/default`: `run`, `doctor`, the review UI, and sessions
all see it, with the generic checks plus the device-law checks on every
declared region. Additional fixed views get their own named scenarios with
the same keys — `[myapp.viz.scenarios.night]` registers `myapp/night` — which
is how DSN's `instrument` and `distance` boards are declared without any
adapter module. The renderer must be a pure,
deterministic, zero-argument function inside the `apps` package returning
`{display: (PIL frames, fps)}` — fixed time, fixed fixture state, no I/O —
and it must be the same code path the live app draws with, fed by a
deterministic fixture (see `apps/dsn.py:render_visual` for the shape).

The checked-in declaration is the sole code-selection surface: its renderer
field accepts only `apps.*` module paths, unknown keys fail loudly, and the
renderer is imported lazily at render time inside the offline guard. Runtime
HTTP and CLI request data selects only a registered scenario id, never a
module, function, executable, or arbitrary path. Declared scenarios accept no
controls or inputs — when the app needs those, promote it to a hand-written
adapter below; the declaration can then be removed or kept for the plain
default.

## Add an app renderer

The production seam is ordinary Python, not visualizer state. The initial
scaffold expects a zero-argument pure renderer returning a mapping from display
id to `(frames, fps)`:

```python
def render_visual():
    return {
        "front": (front_frames, 5),
        "back": (back_frames, 5),
    }
```

`frames` is a nonempty sequence of PIL RGB images. Front frames are exactly
72x16; back frames are exactly 160x80; `fps` is an integer. The function must
use the same rendering code that produces the app's real asset. Do not create
a separate visualizer-only imitation of the scene, perform device I/O, or
fetch live data inside it. Pass already normalized, deterministic app state
through an app-owned pure seam when the visual depends on inputs.

Plan the adapter and test first:

```bash
uv run busybar-viz scaffold yourapp \
  --renderer apps.yourapp:render_visual \
  --display front \
  --json
```

The default is read-only. After reviewing the plan, add `--write`. The command
creates `busybar_viz/adapters/yourapp.py` and
`tests/test_viz_yourapp_adapter.py`, refuses every existing target rather than
overwriting it, and prints the two explicit registry edits to make manually.

```bash
uv run busybar-viz scaffold yourapp \
  --renderer apps.yourapp:render_visual \
  --display front \
  --write
```

The generated scenario accepts no controls or inputs. Extend its adapter with
`ControlSpec`, `InputSpec`, deterministic app-state fixtures, semantic regions,
independent ink references, signals, and named checks as the design requires.
If the production renderer needs normalized state, replace the scaffold's
zero-argument wrapper with a narrow, explicit call that passes that state.
Keep imports of the production app lazy so listing scenarios has no side
effects.

Native firmware elements such as Text and Countdown are not emulated. Do not
write a raster facsimile and call it production evidence. Prefer a shared
native asset renderer where possible; otherwise use device framebuffer and
physical review as separate, correctly labelled evidence.

## Inspect repository assets

`asset` decodes an exact-size repository PNG or a native BUSY Bar `.anim`:

```bash
uv run busybar-viz asset path/to/front.png --display front --json
uv run busybar-viz asset path/to/weather.anim \
  --display front --section default --json
```

Paths must resolve inside the repository. The loader first takes one bounded
snapshot of at most 32 MiB and records its SHA-256 in the normalized request;
decode and publication use that snapshot, so replacing the path afterwards
cannot change the evidence identity. PNG input must be a real PNG, is converted
to RGB, and must match the selected display. `.anim` decoding uses
`busybar_dev.anim`, including native section repeat timing, then publishes the
expanded playback frames. It proves the snapshotted file pixels and timing were
decoded; it does not emulate firmware element composition or panel optics.

## Evidence bundles

`run`, `view`, `asset`, and `capture` publish an atomic, content-addressed
directory at:

```text
scratch/busybar-viz/artifacts/<first-two-digest-chars>/<artifact-sha256>/
```

The digest covers the normalized request (including an asset snapshot hash when
applicable), renderer/core source-file hashes, track metadata, exact RGB bytes,
baselines, signals, audits, ink references, notes, and automatic evidence
level. Rendering identical evidence returns the existing immutable bundle; it
does not overwrite files.

Important files include:

| Path | Meaning |
|---|---|
| `manifest.json` | Artifact identity, source hashes, display/timing metadata, checks, and evidence metadata |
| `summary.md` | Short human-readable result and limitations |
| `scenario.normalized.json` | Canonical render request |
| `audit.json` | Structured check results and overall pass/fail |
| `trace.jsonl` | Time-ordered frames, logical signals, check results, and run boundaries |
| `signals.json` | Non-frame logical output such as top-LED intent |
| `frames/<display>/frame-NNN.rgb` | Authoritative packed RGB888 bytes |
| `frames/<display>/frame-NNN.png` | Lossless frame preview |
| `<display>.gif` | Nearest-neighbor native-pixel animation |
| `<display>-gap.gif` | Physical-spacing simulation, with sampled timing preserved when necessary |
| `<display>-contact-sheet.png` | Labelled native-frame samples |
| `<display>-gap-contact-sheet.png` | Labelled gap-aware samples |
| `<display>-change-heatmap.png` | Frequency of per-pixel change across the segment |
| `baselines/` | Optional authoritative comparison frames used by semantic checks |
| `references/` | Optional independent full-ink data and previews |

Every generated file except the self-describing `manifest.json` is inventoried
there by hash and role. The authoritative files are the `.rgb` frames; GIFs,
enlarged PNGs, contact sheets, heatmaps, and LED-gap views are derived
inspection aids.

Generating a gap preview does **not** mean a person or agent inspected it.
Published manifests remain immutable and therefore keep
`evidence.reviewed_level` as `null`. An explicit reviewed-evidence assertion
belongs in the session journal, bound to the exact artifact SHA.

Inspect a bundle by digest, artifact directory, or manifest path:

```bash
uv run busybar-viz inspect ARTIFACT_SHA --json
uv run busybar-viz inspect scratch/busybar-viz/artifacts/ab/ARTIFACT_SHA
```

## Audits and text completeness

Adapters nominate checks that express the design contract. The current audit
engine can verify:

- RGB frame dimensions;
- near-white coverage and global luminance jumps;
- preservation or required motion in named semantic regions against baselines;
- unique animation states and loop seam metrics;
- logical top-LED policy;
- density and luminance summary metrics;
- full independent text ink in final composed frames.

An artifact passes only when every error-severity check passes. Audit results
cover the declared checks; they do not evaluate overall design quality.

Containment alone cannot prove a complete label. To prove text fit, the app
adapter must independently render the full, unclipped ink, preserve its
coordinates even when they fall outside the display, and nominate a
`text.full_ink_preserved` check. The audit then compares every expected sample
with final composed pixels in the promised frames. Without that independent
reference, report text completeness as unverified.

## Compare revisions

Compare two evidence manifests or artifact ids:

```bash
uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
```

The comparison verifies authoritative RGB hashes before reading them. Its
content-addressed result under `scratch/busybar-viz/comparisons/` reports added
or removed displays, frame-count/FPS changes, exact changed-pixel counts and
fractions, channel deltas, and half-open change bounding boxes for each common
frame. Bright diff contact sheets make changed areas readable to people and
vision models.

`compare` exits successfully when it produced a valid comparison, even when
pixels changed. Read the JSON `changed` and per-display `state` fields to make
a regression decision. The stored `comparison.json` contains the required core
fields; CLI JSON adds optional paths and `same_*` conveniences. Both forms
validate against `busybar.comparison/v1`.

## Pinned baselines

`viz-baselines.toml` at the checkout root is the durable record of accepted
pixels: one content digest per display track (fps, frame count, exact RGB
bytes) for every registered scenario rendered with its default controls.
Digests deliberately do not use artifact ids — those also hash the request
and tool sources, and a baseline should fail only when what the panel would
show changed.

Production-backed app artifacts inventory checkout app/helper Python and
shipped raster assets, not just the thin launcher. This is conservative:
an unrelated app-source edit may change artifact identity without changing
pixels. Owner configuration, state and caches are excluded from this source
inventory. The pixel baseline remains the behavior gate across such moves.

```bash
uv run busybar-viz baseline check --json     # CI's pixel-drift gate
uv run busybar-viz baseline update           # accept current pixels, all scenarios
uv run busybar-viz baseline update dsn/default
```

`check` exits 1 naming every scenario whose pixels drifted (publishing the
drifted render as an inspectable artifact), every registered scenario missing
a baseline, and every stale entry. A pull request that changes registered
pixels must carry the matching `viz-baselines.toml` update. Inspect a fresh
`run` artifact (or the drift artifact from `check`) first; the baseline edit
then makes the acceptance explicit in the same diff.

## Capture the device framebuffer

`capture` reads the front and/or back framebuffer once over the device API and
publishes an immutable artifact with contact sheets, gap views, a comparable
SHA, `framebuffer_observed` track provenance, and an automatic
`framebuffer-captured` evidence level.

```bash
uv run busybar-viz capture --json
uv run busybar-viz capture --display front --json
```

It is read-only: it draws nothing, clears nothing, and leaves the panel as
found. The recorded request deliberately excludes the device host and machine
identity, so those private values do not enter the shareable artifact. Source
hashes and tool versions still participate in artifact identity. The capture
caveats are recorded in its notes: a framebuffer still is a composited moment,
not the currently visible frame of a native `.anim`, and not a physical-panel
observation.

## Inspect audio assets

`.snd` is headerless PCM (s16le mono 44100), so metadata cannot expose a wrong
channel layout or sample rate. `audio` reports what the bytes can establish —
duration under the required format, levels, clipping, and silence — and can
draw a min/max envelope PNG readable by people and vision models:

```bash
uv run busybar-viz audio scratch/report.snd --json
uv run busybar-viz audio scratch/report.snd --waveform scratch/wave.png --json
```

The JSON carries duration, peak/RMS/DC-offset fractions, clipped-sample
count, a silence flag, and the source sha256. This is an inspection aid, not
an evidence bundle: audio has no display track, so it does not enter the
artifact store.

## Housekeeping

Content-addressed stores grow over time. `gc` removes eligible artifacts while
retaining journal references, recent artifacts, and usable comparisons:

```bash
uv run busybar-viz gc --json             # dry run: prints the plan only
uv run busybar-viz gc --delete --json
```

Kept unconditionally: every artifact cited by a session journal event or a
session's current pointer, everything newer than `--keep-recent-hours`
(default 24), and comparisons whose endpoints both survive. Baselines pin
pixel digests rather than artifact ids, so deleting an artifact never deletes
acceptance. Recreate a prior artifact by rendering its corresponding checkout;
the current checkout may intentionally produce different pixels.

## Collaborative review sessions

The standalone UI combines registered scenario controls, generated buttons for
declared semantic inputs, an advanced JSON timeline, asynchronous bounded
render jobs, exact audit results, immutable artifact previews, saved-session
selection, feedback, change requests, and approvals. Input buttons append a
canonical event at the selected virtual microsecond timestamp, so the model
and the renderer receive the same timeline. Start it on loopback:

```bash
uv run busybar-viz serve
# open http://127.0.0.1:8765
```

A simulated-input click edits the draft timeline in the browser; clicking
**Render evidence** journals that complete timeline inside `render.requested`.
The click alone is not an unsolicited harness message. Feedback and review
decisions are journaled immediately when their buttons are submitted.

This process is not Barkeep and never connects to a bar. Its API has no
authentication, so it refuses a non-loopback bind unless `--allow-remote` is
also supplied. Every mode validates Host and same-origin mutations, requires
JSON, bounds streamed request bodies even without an accurate Content-Length,
and hash-checks served artifact files. Remote mode accepts IP-literal Host
headers because they cannot be DNS-rebound. Every DNS name must be declared
exactly with a repeatable `--allowed-host` option:

```bash
uv run busybar-viz serve --bind 0.0.0.0 --allow-remote \
  --allowed-host review-box.local
```

An undeclared DNS Host receives HTTP 400 even in remote mode. `--allowed-host`
takes a hostname without a port; repeat it when the service legitimately has
more than one name. Treat `--allow-remote` as an explicit decision to expose an
unauthenticated development service on a trusted network; do not use it as an
installation default.

Sessions and their append-only events live in
`scratch/busybar-viz/sessions.sqlite3`. Render requests run only registered
scenarios in bounded subprocess jobs. Each mutation uses the session's exact
`expected_revision`, and render/review requests accept caller-supplied event ids
for idempotent replay, so concurrent or lost-response retries fail or replay
visibly rather than duplicating history. A job remains `finalizing` until its
durable completion event commits; a completed older job cannot replace the
artifact from a newer request. On server restart, any durable render request
without a terminal event is reconciled once as failed instead of remaining
permanently ambiguous.

The journal, not a harness-specific callback, is the portable collaboration
contract. Codex and Claude Code use the same commands and see the same ordered
events. Either create a session in the UI or create/reuse one from the CLI:

```bash
uv run busybar-viz session create "Clock layout iteration" --json
uv run busybar-viz session list --json
uv run busybar-viz session show SESSION_ID --json
```

When a person is reviewing, the collaboration loop is **run → present →
wait**. (While iterating alone, skip the session entirely — render, read the
audit, compare; that loop is at the top of this document.) Render a candidate,
then atomically make that exact artifact current in the shared session at the
revision just read:

```bash
uv run busybar-viz run myapp/default --json

uv run busybar-viz session present SESSION_ID ARTIFACT_SHA \
  --revision REVISION \
  --message "Candidate from the current edited checkout" \
  --json
```

`present` accepts an artifact id, artifact directory, or manifest path, but
validates that its manifest belongs at the matching content-addressed path
inside the command's configured data store. It appends `artifact.presented` as
actor `agent` and updates the session's current artifact in the same journal
transaction. The UI's periodic refresh then loads those exact previews and
checks. Use optional
`--event-id evt_<32-lowercase-hex>` when a harness needs an idempotent retry.
If `--data-dir` is used, pass the same value to `run`, `session`, and `serve`.

The UI can also request a render itself. That path records
`render.requested` and its terminal event, then promotes the newest successful
artifact. Both paths converge on the same current-artifact pointer and event
stream.

After `present`, use the revision returned in its `session` object to keep the
current agent turn waiting for the next UI action with one bounded long poll:

```bash
uv run busybar-viz session events SESSION_ID \
  --after REVISION --wait 55 --json
```

The command returns as soon as a newer event exists, or after 55 seconds with
an empty event list. Read `next_revision`, handle every returned event in
order, and issue the command again with that revision while review is still
active. `--jsonl` emits one event per line. This is model-readable in both
Codex and Claude Code, but it is not unsolicited push: the agent must be
running the long-poll command.

An agent can attach a response to an exact artifact:

```bash
uv run busybar-viz session note SESSION_ID \
  --revision REVISION \
  --artifact ARTIFACT_SHA \
  --message "Moved the label and preserved the full-ink check." \
  --json
```

`--current-artifact` is a convenience when the current session pointer was
just read; `--artifact` is clearer when several renders are in flight. On a
revision conflict, read the new events, reconsider them, and retry against the
new revision. Export the complete review history with:

```bash
uv run busybar-viz session export SESSION_ID
```

UI approval or change-request events are bound to the selected artifact SHA.
When the user explicitly affirms that they inspected the LED-gap view, that
decision event may assert `gap-previewed`; simply loading or generating that
view does not. The immutable artifact manifest still retains
`reviewed_level: null`, while the journal preserves who made the reviewed
assertion and which artifact it concerned.

Optional native Codex App Server or Claude Code bridges may later adapt this
journal to harness-specific turn APIs. They are convenience layers, not part
of the portable contract, and their absence does not split the workflow.

## Evidence language

Use the highest evidence level supported by the completed checks:

| Level | What it proves |
|---|---|
| **renderer-verified** | A production-owned deterministic renderer produced exact native pixels and the nominated tests passed. |
| **gap-previewed** | A person or agent also inspected the simulated 1.23 mm LED / 2.2 mm pitch spacing view for the exact artifact. |
| **framebuffer-captured** | The device readback API returned a composited still. It does not prove the currently visible frame of a native animation. |
| **hardware-observed** | A person inspected the physical panel. Only this supports final claims about contrast, apparent pixel size, animation feel, or on-device legibility. |

Track provenance such as `source_exact`, `emulated_conformant`, or
`logical_only` is separate from the reviewed evidence ladder. For example, the
conformance fixture has deterministic `emulated_conformant` pixels but is not a
production renderer, and a top-LED signal records logical intent rather than a
visible LED observation. For `view` artifacts, native-size
input stays `source_exact` (the decoded RGB pixel values are preserved
exactly, while the source file hash is recorded separately), downsampled
preview input is `approximate`, and both record in their notes that the frames
are unregistered ad-hoc input rather than proven production renderer output.

Never promote evidence implicitly:

- A generated gap GIF is not `gap-previewed` until it was inspected and that
  assertion was recorded.
- A repository PNG passed through `asset` is not automatically a framebuffer
  capture, even if its bytes originally came from a device.
- A framebuffer still is not a physical observation or a reliable `.anim`
  playback frame.
- Passing audits cannot establish untested intent, physical contrast, or
  animation feel.

## Limits and exit status

The offline work budgets are intentionally finite: at most two named display
tracks, 240 frames per track, 20 FPS, 60 seconds, 1,000 input or signal events,
256 KiB JSON requests, and 64 MiB per published artifact. The UI uses at most
two concurrent render workers, eight pending/running jobs, and a 45-second
worker timeout.

CLI exit status is stable:

| Status | Meaning |
|---|---|
| `0` | Command completed; for `compare`, inspect `changed` separately. |
| `1` | `doctor` found a broken registered scenario; `run`, `view`, `asset`, `capture`, or `inspect` produced/read valid evidence with a failed required audit; or `baseline check` found drift, a missing baseline, or a stale entry. |
| `2` | Invalid request, unknown scenario/artifact, unsafe scaffold target, or refused remote server bind. |
| `3` | Unexpected runtime failure. |

## What remains outside the tool

`busybar-viz` does not emulate device element merging, priority/refusal,
native fonts, Countdown/Text behavior, firmware compositor details, LED gamma,
optical bloom, viewing angle, or network/upload jitter. It does not replace a
framebuffer capture or a final physical-panel check. Record framebuffer and
physical-panel checks separately.

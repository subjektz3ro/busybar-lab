---
name: busybar-viz
description: Use while creating, changing, or debugging anything drawn for the BUSY Bar — and when previewing, inspecting, comparing, or validating front or back display images and animations. This is the agent's eyes on a panel it cannot see; render or ingest frames, read exact audits (clipping, text fit, contrast, frame timing, LED-gap legibility), diff revisions, and decide what renderer output, a framebuffer capture, or physical observation can actually prove.
---

# See what you draw

You cannot look at the physical panel; `busybar-viz` is your eyes. Use it on
every iteration *while* building a visual, not only when validating a finished
one. A visual change without an artifact SHA is a change you have not seen.

Read `AGENTS.md` and the `busybar-app` skill before evaluating anything drawn
for the device. Those files own the device rules; do not restate or override
them here.

## The loop while you build

1. Draw — write or edit the rendering code, produce frames (the app's own
   preview path or the production renderer).
2. Look, with zero setup:

   ```bash
   uv run busybar-viz view scratch/candidate.png --json
   uv run busybar-viz view scratch/frames/ --fps 5 --json
   ```

   `view` preserves native-size PNGs' decoded RGB pixels exactly, or accepts
   an exact integer enlargement (an app's `--preview` output) which it
   downsamples and labels `approximate`. A directory is frames sorted by name.
3. Declare what must read, so the device-law checks run on it:

   ```bash
   uv run busybar-viz view scratch/candidate.png \
     --region clock=1,0,26,8 --ink 'clock=#FFFFFF' --json
   ```

   `--region` runs `region.min_feature_size` on that rect; `--ink` adds
   `region.contrast_floor` against the declared colours. `--max-isolated N`
   tolerates deliberate single-pixel art such as stars.
4. Read the result yourself: `audit.json` names the failing check and its
   frame-specific measurements; open the contact sheet and the `*-gap` view
   with your own vision.
5. Fix, re-render, and diff:

   ```bash
   uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
   ```

6. **The naive-viewer pass — verify the picture, not your intent.** The
   checks and your own inspection share a blind spot: you look FOR the
   properties you designed, so those are what you see. Before accepting,
   describe the composed artifact as a viewer who never read the code — or
   hand the preview paths to a context-free subagent with no scene names
   and no stated intent, and ask what it sees and what reads as odd.
   Anything that has to be explained is a finding: an object floating
   unattached, an occlusion physics couldn't produce, a structure with no
   referent in the place being drawn. Born 2026-08-11: a grove rework
   passed every declared check and its own author's inspection with two
   crowns floating in the sky on invisible 1px trunks, while a 3-pixel
   "grain elevator" — scenery invented to solve the code's occlusion
   problem — ate 25-car freight trains whole.
   When thinning an animation for this pass, pick a stride co-prime with
   any spatial period in the motion: a 1 px/frame train sampled every 15
   frames against a 5 px car pitch showed cars frozen in place "flickering
   through colours" — a finding true of the strip and false of the
   animation.
7. Accept deliberately. Registered scenarios' pixels are pinned in
   `viz-baselines.toml`; CI runs `doctor` for required audits and `baseline
   check` for pixel drift. After inspecting the fresh artifact, run `uv run
   busybar-viz baseline update` and commit the file in the same change.
   Unacknowledged drift fails the build.

No session, server, or person is required for any of this. Do not hand-roll a
render script instead: `view` is the same one-liner of effort and it returns
an immutable artifact SHA, the device-law checks, and gap previews — a scratch
PNG returns none of those. A change that alters pixels is evidenced when there
is an artifact SHA and a `compare` against the prior artifact. Render that
before editing; if it is gone, render the corresponding earlier checkout.
When the iterated design settles, `view ... --emit-declaration APP` hands back
the paste-ready `[APP.viz]` block so registration is a copy, not a retype.
`scratch/busybar-viz` grows forever otherwise; `uv run busybar-viz gc --json`
plans a cleanup that keeps journal-cited artifacts and protects recent ones
for the configured retention window. Only `--delete` applies the plan.

## Keep the boundary

- `busybar-viz` is a general development tool whose render and audit paths are
  offline. Do not add it to Barkeep's routes, supervisor, state, or runtime
  dependencies. `capture` is the explicit read-only device-network exception.
- Treat Barkeep's preview as live framebuffer readback, not as the renderer or
  a physical-panel simulator.
- Use the app's own renderer as the source of native frames. Do not rebuild a
  scene in a separate mockup.
- Production apps never import `busybar_viz`; app-specific adapters wrap a
  pure production-renderer seam. HTTP callers select only scenarios from the
  explicit registry; `view`/`asset` are CLI-only file ingestion, never code
  selection.
- Offline workers must scrub both registry-declared app values and every
  deliberately undeclared secret named in `AGENTS.md`. When adding a secret
  exception, add its exact key to `busybar_viz.offline` and a worker-boundary
  regression before importing the production app.
- Read `docs/busybar-viz.md` for the full contract and inspect
  `uv run busybar-viz --help` before assuming a flag.

## Audit a registered scenario

Registered scenarios are the provenance tier: their adapter and declared
source kind establish where the pixels came from, with the audit contract
pinned for CI, sweeps, and review decisions. Only production-owned renderer
scenarios support the `renderer-verified` label.

1. Verify the checkout and discover the declared contract:

   ```bash
   uv run busybar-viz doctor --json
   uv run busybar-viz scenarios --json
   ```

2. Render the registered scenario with explicit controls and timestamp-ordered
   semantic input. Pin time, source data, seed, and every other value that can
   change pixels in the adapter:

   ```bash
   uv run busybar-viz run SCENARIO_ID \
     --set key=value \
     --input '{"t_us":250000,"kind":"button.press","control":"ok","value":true}' \
     --json
   ```

3. Read `manifest.json`, `audit.json`, `trace.jsonl`, and the exact frame hashes.
   Inspect the native animation/contact sheet for pixel claims and every
   relevant frame, page, input state, and last-to-first seam. A passing audit
   establishes only its named invariants.

4. Open and actually inspect the `*-gap.gif` or
   `*-gap-contact-sheet.png` before using **gap-previewed**. Generating the file
   is not inspection; the immutable manifest deliberately keeps
   `reviewed_level: null`. Record reviewed evidence in the session journal,
   bound to the exact artifact SHA.

5. For static/status text, require an independent full-unclipped-ink reference
   and a `text.full_ink_preserved` result over final composed frames. In-bounds
   emitted pixels, a clip, or a draw-call spy cannot prove the intended label
   was complete.

6. Compare revisions with exact authoritative RGB data. `compare` exits zero
   when comparison succeeded even if pixels changed; read `changed`, each
   display `state`, per-frame metrics, and the diff contact sheet.

7. Use semantic or pixel invariants that fail when the named feature is
   removed. Avoid assertions that merely detect any non-black pixel or any
   difference between naturally changing frames.

## The device's physics are checks, not prose

`region.contrast_floor` and `region.min_feature_size` enforce the contrast and
feature-size laws described by the `busybar-app` skill. Enforcement thresholds
live in `busybar_viz/device_laws.py`; cite that module rather than duplicating
its constants in workflow instructions. `view --region/--ink` runs the same
checks on ad-hoc frames, so there is no iteration too early for them.

`region.contrast_floor` measures what borders the ink, not the region mean. A
mean is dominated by pixels the ink never touches; it reported a comfortable
115 for a status clock whose worst neighbour was 9.

## Exploring, not yet auditing

`sweep` renders one scenario across a control's values and audits every cell,
so the common design question — *does this still read at every hour, under any
cloud* — is one command rather than a render script:

```bash
uv run busybar-viz sweep skystrip/status-clock \
  --over hour=0,6,12,18 --over cloud_frac=0.0,1.0 --json
```

Every cell is its own immutable artifact with its own SHA and gap contact
sheet, and the command exits non-zero naming the combination that failed. Use
it while iterating; the provenance comes along for free, which is the point —
a render script gives you the picture and no evidence.

## Sight now, provenance when it stabilizes

`view` gives sight with zero ceremony; a registered scenario records where the
pixels came from. Iterate with `view`. When the visual stabilizes — or when a
regression baseline, a review approval, or a CI gate will rest on it —
register it. Registration is a ramp, not a wall:

1. **Declare it in `apps.toml`** (the normal case). One `[<app>.viz]` table
   naming a pure zero-argument seam inside the apps package, plus declared
   regions with rects and inks, registers `<app>/default` with device-law
   checks and `doctor` coverage — no adapter module. See the annotated
   example in `apps.toml`'s header and `docs/busybar-viz.md`.
2. **Scaffold a hand-written adapter** only when the app needs what a
   declaration cannot carry — typed controls, semantic input replay, fault
   injection, or independent ink-reference proofs:

   ```bash
   uv run busybar-viz scaffold yourapp \
     --renderer apps.yourapp:render_visual --display front --json
   ```

   Review the plan, rerun with `--write`, then make the printed explicit
   registry edits. The scaffold never overwrites existing paths. Extend the
   adapter deliberately; keep deterministic state and audit semantics
   app-specific rather than adding app assumptions to the visualizer core.

**Importing `busybar_viz` as a library still produces no artifact.**
`panelise` will hand you a gap-simulated PNG in one line, and that PNG has no
SHA, no manifest, no checks, and no journal entry, so it cannot support
**gap-previewed** or any other confidence label. There is no reason to: `view`
is the supported one-liner that publishes real evidence.

## Work with a person in the current agent turn

The standalone UI and the CLI share an append-only SQLite session journal.
This is the portable integration for both Codex and Claude Code; it does not
depend on a harness-specific callback. Reach for it when a person is
reviewing; the solo loop above needs none of it.

```bash
uv run busybar-viz serve
# user opens http://127.0.0.1:8765
```

The UI derives live buttons from each scenario's general `InputSpec` and appends
clicks to the canonical timeline at the chosen virtual timestamp, so the model
reads the same simulated inputs the person used; there is no Barkeep-specific
input channel.

Create or obtain the session id and read its revision. The collaboration loop
is **run → present → wait**: render with the edited checkout, atomically present
that exact stored artifact to the shared session, then wait from the revision
returned by `present`:

```bash
uv run busybar-viz session create "Design iteration" --json
uv run busybar-viz run SCENARIO_ID --json
uv run busybar-viz session present SESSION_ID ARTIFACT_SHA \
  --revision REVISION --message "Candidate from the current checkout" --json
uv run busybar-viz session events SESSION_ID \
  --after PRESENTED_REVISION --wait 55 --json
```

`present` accepts only a manifest at its matching content-addressed path in the
configured data store, appends an artifact-bound `artifact.presented` event as
the agent, and makes it current atomically. The UI refreshes to that artifact.
Supply a stable
`--event-id evt_<32-lowercase-hex>` when retrying after a lost response. When
using `--data-dir`, use the same value for `run`, `session`, and `serve`.
UI-requested renders remain supported and converge on the same session pointer
and journal.

When the command returns, process every event in revision order, use
`next_revision` for the next poll, and repeat while collaboration is active.
An empty result means only that this 55-second poll expired. It is not
unsolicited push; the agent must run the command. Add an artifact-bound model
note with optimistic concurrency:

```bash
uv run busybar-viz session note SESSION_ID \
  --revision REVISION --artifact ARTIFACT_SHA \
  --message "What changed and what remains unverified" --json
```

On a revision conflict, reread events and retry only after incorporating them.
UI feedback, change requests, approvals, and explicit gap-inspection
affirmations are journal events tied to an artifact; they never mutate its
manifest. Optional native Codex/Claude bridges are deferred convenience
layers, not part of this contract.

## Label confidence honestly

- **Renderer-verified:** deterministic native pixels and tests only.
- **Gap-previewed:** a simulated LED-spacing view was also inspected.
- **Framebuffer-captured:** the device readback API returned a composited
  still; this does not prove the currently visible native animation frame.
- **Hardware-observed:** a person checked the physical panel. Only this level
  supports claims about real contrast, apparent pixel size, animation feel,
  or overall on-device legibility.

Never promote one level to another. A screenshot can support a review without
making the result hardware-verified.

Track provenance (`source_exact`, `emulated_conformant`, `approximate`,
`logical_only`, and so on) is separate from this ladder. A `view` artifact is
honest about being unregistered input: exact-size frames stay `source_exact`,
downsampled previews are `approximate`, and neither claims the pixels came
from a production renderer. A registered production-renderer scenario can
establish that stronger provenance. A logical top-LED signal proves intent,
not a visible LED pulse. A decoded
repository asset can preserve its source pixels exactly without being
renderer-, framebuffer-, or hardware-verified.

## Device safety

Do not connect to the bar unless the task requires it. Before a live check,
confirm Barkeep is not running the same app by hand; afterwards clear test
draws and leave the device as found.

When the task does need device truth, prefer
`uv run busybar-viz capture --json`: it reads the framebuffer once, draws and
clears nothing, and publishes a correctly-labelled `framebuffer-captured`
artifact into the same store — so a device still can be `compare`d against
the renderer-verified artifact it was supposed to match. For `.snd` assets,
`uv run busybar-viz audio PATH --json` reports duration, levels, clipping,
and silence offline, with an optional `--waveform` PNG.

The complete command, artifact, collaboration, evidence, and limitation
contract is in `docs/busybar-viz.md`; the maintained design-record index is in
`docs/design/README.md`.

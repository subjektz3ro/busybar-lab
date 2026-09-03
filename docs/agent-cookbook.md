# Agent workflow for developing a BUSY Bar app

This workflow produces machine-readable JSON, exact pixel output, and named
audit failures. Steps 1–5 do not require hardware. Run commands from the
checkout root; visualizer commands shown with `--json` return machine-readable
output.

Normative requirements are in
[`AGENTS.md`](../AGENTS.md) (house rules), [`busybar-viz.md`](busybar-viz.md)
(the full visualizer contract), and the two skills under `.claude/skills/`
(device constraints and visual validation). This page lists the implementation
sequence.

## 1. Create the app

```bash
uv run scripts/new_app.py pomodoro --description "A focus timer"
uv run apps/pomodoro.py --dry-run
```

One command creates `apps/pomodoro.py` from the template and registers
`[pomodoro]` in `apps.toml`, with a commented `[pomodoro.viz]` block for
later. The dry run builds the real draw request without a device and runs
`busybar_dev.lawcheck` over it — a non-ASCII string from a feed fails here,
at exit code 1 before a request reaches the device.

## 2. Design before you build

You do not need device I/O working to know whether a raster visual reads.
Start the app-owned renderer with fixed fixture state, write its candidate
frames to `scratch/`, and look:

```bash
uv run busybar-viz view scratch/candidate.png \
  --region timer=1,0,26,8 --ink 'timer=#FFFFFF' --json
```

`view` publishes an immutable evidence bundle with a contact sheet, an LED-gap
simulation, and the two device checks on the declared region: contrast and
minimum feature size using thresholds from `busybar_viz/device_laws.py`. An
enlarged preview that is an exact integer multiple of 72×16 is downsampled and
marked `approximate`. A failing check names the frame and relevant measurement.

An early PIL sketch is useful for exploration, but it must evolve into (or
call) the production renderer below. Do not leave a separate visualizer-only
facsimile as evidence for what the live app draws.

Diff any two candidates by their artifact SHAs:

```bash
uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
```

## 3. Expose a deterministic renderer

Write the app so one zero-argument function returns exactly what the panel
will show, fed by fixed fixture state — no clock, no network, no
environment:

```python
def render_visual():
    return {"front": (frames, fps)}   # PIL RGB frames, native 72x16
```

The live app path uses the same rendering code with live state. Deterministic
pixels can be diffed, baselined, and checked in CI. Native firmware Text and
Countdown elements have no raster renderer; use `capture` for those apps.

## 4. Register the renderer

Uncomment and fill the viz block `new_app.py` left in `apps.toml` (or paste
the block `view --emit-declaration pomodoro` printed during step 2):

```toml
[pomodoro.viz]
renderer = "apps.pomodoro:render_visual"
displays = ["front"]

[pomodoro.viz.regions.timer]
rect = [1, 0, 26, 8]
ink = ["#FFFFFF"]
```

That registers `pomodoro/default` — no adapter module, no registry edit.
More fixed views become `[pomodoro.viz.scenarios.<name>]` tables. Verify:

```bash
uv run busybar-viz doctor --json
uv run busybar-viz run pomodoro/default --json
```

Promote to a hand-written adapter (`busybar-viz scaffold`) only when you
need typed controls, timed wheel/button replay, fault injection, or
full-ink text proofs.

## 5. Record the baseline

```bash
uv run busybar-viz baseline update pomodoro/default
uv run busybar-viz baseline check --json
```

`viz-baselines.toml` now carries the accepted pixel digests, and CI fails
any future change that drifts them without a same-diff acceptance. From this
commit on, your visual has regression protection: change the rendering,
inspect the fresh artifact `baseline check` publishes, then accept
deliberately.

## 6. Test with hardware

```bash
uv run apps/pomodoro.py            # draw for real (a 409 refusal is normal)
uv run busybar-viz capture --json  # read-only framebuffer, published as evidence
```

`capture` reads the device's composited framebuffer into an immutable artifact
for comparison with the renderer result from step 4. Use the evidence levels
renderer-verified → gap-previewed → framebuffer-captured → hardware-observed.
Only physical-panel inspection supports claims about contrast or animation
feel. Clear the app draw after testing:
`uv run apps/pomodoro.py --clear` if the app drew.

## 7. Working with a person

For collaborative review, create a session:

```bash
uv run busybar-viz session create "Pomodoro design" --json
uv run busybar-viz session present SESSION_ID ARTIFACT_SHA --revision REV --json
uv run busybar-viz session events SESSION_ID --after REV --wait 55 --json
```

They see exactly the artifact you rendered in the loopback review UI
(`busybar-viz serve`); their feedback, change requests, and approvals come
back as ordered journal events bound to that SHA.

## Housekeeping

- `.snd` audio: `uv run busybar-viz audio PATH --json` (duration, levels,
  clipping, silence, optional `--waveform` PNG).
- Evidence store: `uv run busybar-viz gc --json` plans a cleanup;
  journal-cited artifacts are retained and recent artifacts are protected for
  the configured retention window; add `--delete` to apply.
- Sweeps: `uv run busybar-viz sweep SCENARIO --over key=v1,v2` audits a
  whole control matrix when a hand-written adapter declares controls.

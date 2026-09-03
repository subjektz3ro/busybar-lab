# The agent cookbook: build a bar app you can see

This repository is built to be developed by AI coding agents, and an agent
cannot look at an LED panel. Every step below therefore produces something a
model can read — JSON, exact pixels, named check failures — and nothing
requires hardware until the very end. Commands run from a checkout root;
visualizer commands shown with `--json` return machine-readable output.

This is the narrative version. The contracts live in
[`AGENTS.md`](../AGENTS.md) (house rules), [`busybar-viz.md`](busybar-viz.md)
(the full visualizer contract), and the two skills under `.claude/skills/`
(the device laws that fail silently, and the visual evidence discipline).
Read those before shipping; read this to know what order things happen in.

## 1. Create the app

```bash
uv run scripts/new_app.py pomodoro --description "A focus timer"
uv run apps/pomodoro.py --dry-run
```

One command creates `apps/pomodoro.py` from the template and registers
`[pomodoro]` in `apps.toml`, with a commented `[pomodoro.viz]` block for
later. The dry run builds the real draw request without a device and runs
`busybar_dev.lawcheck` over it — a non-ASCII string from a feed fails here,
at exit code 1, instead of violating the device's draw contract at 2am.

## 2. Design before you build

You do not need device I/O working to know whether a raster visual reads.
Start the app-owned renderer with fixed fixture state, write its candidate
frames to `scratch/`, and look:

```bash
uv run busybar-viz view scratch/candidate.png \
  --region timer=1,0,26,8 --ink 'timer=#FFFFFF' --json
```

`view` publishes an immutable evidence bundle: a contact sheet and an
LED-gap simulation you (or a vision model) can actually read, plus the two
device-law checks on the region you declared — contrast and minimum feature
size using the thresholds in `busybar_viz/device_laws.py`, measured rather
than eyeballed. An enlarged preview
(an exact integer multiple of 72x16) is accepted and honestly downsampled.
Iterate here; a failing check names the frame and relevant measurement.

An early PIL sketch is useful for exploration, but it must evolve into (or
call) the production renderer below. Do not leave a separate visualizer-only
facsimile as evidence for what the live app draws.

Diff any two candidates by their artifact SHAs:

```bash
uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
```

## 3. Build around a pure seam

Write the app so one zero-argument function returns exactly what the panel
will show, fed by fixed fixture state — no clock, no network, no
environment:

```python
def render_visual():
    return {"front": (frames, fps)}   # PIL RGB frames, native 72x16
```

The live app path uses the same rendering code with live state. This seam is
what makes the app *visible*: deterministic pixels are diffable, pinnable,
and CI-checkable. (Native firmware elements like Text and Countdown have no
raster seam; those apps skip to step 6 and lean on `capture`.)

## 4. Register it — data, not code

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

## 5. Pin the pixels

```bash
uv run busybar-viz baseline update pomodoro/default
uv run busybar-viz baseline check --json
```

`viz-baselines.toml` now carries the accepted pixel digests, and CI fails
any future change that drifts them without a same-diff acceptance. From this
commit on, your visual has regression protection: change the rendering,
inspect the fresh artifact `baseline check` publishes, then accept
deliberately.

## 6. Only now, hardware

```bash
uv run apps/pomodoro.py            # draw for real (a 409 refusal is normal)
uv run busybar-viz capture --json  # read-only framebuffer, published as evidence
```

`capture` gives the device's composited truth as an immutable artifact you
can `compare` against the renderer-verified one from step 4. Label claims
honestly — the ladder is renderer-verified → gap-previewed →
framebuffer-captured → hardware-observed, and only the last says anything
about real contrast or animation feel. Leave the panel as you found it:
`uv run apps/pomodoro.py --clear` if the app drew.

## 7. Working with a person

When a human joins, presence is a session, not a screenshot thread:

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

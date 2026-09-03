# BUSY Bar Lab

See **[AGENTS.md](AGENTS.md)** — it is the canonical guide for this repo, and
this file exists only so Claude Code finds it.

`AGENTS.md` is the vendor-neutral convention, read by most coding agents, and
busylib itself ships one. Keeping a single source avoids the usual failure
where two guides drift and one of them is quietly wrong.

Before writing anything that draws to the device, read the
[`busybar-app` skill](.claude/skills/busybar-app/SKILL.md). Before previewing,
comparing, or validating display output, read the
[`busybar-viz` skill](.claude/skills/busybar-viz/SKILL.md).

busybar-viz is your eyes: while iterating on any visual, audit each candidate
with `uv run busybar-viz view FRAMES --json` (ad-hoc frames, zero setup) or
`uv run busybar-viz run SCENARIO --json` (registered provenance), read
`audit.json` and the contact-sheet/gap previews yourself, then `compare` the
new SHA against the prior one. Keep visualization offline and outside Barkeep.
Use the confidence labels in `AGENTS.md`; never call a preview or framebuffer
still hardware verification. The exact standalone workflow is in
[`docs/busybar-viz.md`](docs/busybar-viz.md).

When a person is reviewing, Claude Code and Codex use the same durable
journal: render, run `uv run busybar-viz session present SESSION_ID
ARTIFACT_SHA --revision REVISION --json`, then wait from the returned revision
with `uv run busybar-viz session events SESSION_ID --after REVISION --wait 55
--json`. Handle events in revision order and repeat from `next_revision`. This
is an explicit bounded long poll, not an automatic harness callback.

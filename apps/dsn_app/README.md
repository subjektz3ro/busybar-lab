# Maintaining DSN

User controls and data semantics are documented in [dsn.md](../dsn.md).
`apps/dsn.py` remains the launcher and the stable renderer-registration seam.
This package owns implementation; its initializer intentionally imports nothing.

## Follow the data

`cli.main` applies `settings` and starts `runtime.run`. The runtime owns one
`model.State`, launches feed/input/narration tasks and handles bounded shutdown.

NASA XML passes through `source` into `feed`; `reconcile` updates state without
inventing observations. `telemetry` interprets freshness and bounded values.
`selection` and `input` express the current view and user intent. `device`
renders/uploads that intent, retries a rejected draw and advances ownership
only when the device accepts it. Audio preparation runs independently; playback
must still match the current narration generation.

| Responsibility | Owner |
|---|---|
| Immutable config and cache containment | `config.py`; applied by `settings.py` |
| Bounded remote XML and feed domain models | `source.py` |
| Polling and reconciliation | `feed.py`, `reconcile.py` |
| Range lookups and JSON cache | `ranges.py` |
| Arrival/departure history and recoverable JSONL cache | `history.py` |
| Mission descriptions, review dates and speech/display facts | `missions.py`, `formatting.py` |
| Shared state, requests and notices | `model.py` |
| Freshness, selection, focus and rotation | `telemetry.py`, `selection.py` |
| Wheel/button transitions | `input.py` |
| Startup, redraw scheduling, cancellation and cleanup | `runtime.py` |

## Rendering, device effects and speech

`render/` never imports device or audio operations. `network_data.py` prepares
the network view; `network_rows.py`, `network_dishes.py` and `network_skies.py`
are its three styles. `distance.py` and `instrument.py` own their distinct
views. Text, palette, globe, dish, craft, timing and carrier modules own reused
drawing primitives. `events.py` renders transient cards. `examples.py` exposes
the three deterministic production renderer entry points used by the visualizer.

`device/scene_policy.py` decides when a scene is needed and which intent it
represents. `scenes.py` prepares and pushes it; `display.py` owns native status,
picker and live-lease elements. `assets.py` and `events.py` own uploads and
transient lifecycle. Keep accepted-draw state changes here, not in pixel code.

`audio/words.py` composes speech without loading renderers or device code.
`policy.py` defines request validity; `worker.py` isolates synthesis;
`assets.py` owns cache repair and upload; `output.py` owns bounded device stops.
`narration.py` connects those operations while enforcing request generations.

## Change safely

Tests import and patch the owner of an operation, not `apps.dsn`. Retain module
references for settings and replaceable operations so tests and runtime use
the same binding. Do not duplicate state or copy generation counters into a
new coordinator. `tests/test_app_architecture.py` checks the dependency graph.

Run the relevant `test_dsn_*.py` contracts, then the complete gates in
[CONTRIBUTING.md](../../CONTRIBUTING.md). For a visual change, compare the
registered `dsn/default`, `dsn/distance` and `dsn/instrument` artifacts under
the `busybar-viz` skill. Network-style, paging, marquee, transient-expiry and
freshness tests cover additional states that those three defaults do not.

History is untrusted cached data: a malformed row must not discard neighboring
valid observations or prevent startup. Asset names, element IDs, timeout
leases and content hashes are device contracts, not incidental implementation
details. Structural edits must preserve them.

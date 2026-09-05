# Maintainer map and refactoring boundaries

Start here when deciding where a change belongs. Setup and the complete CI
commands are in [CONTRIBUTING.md](../CONTRIBUTING.md); device and display rules
remain in [AGENTS.md](../AGENTS.md).

## Change the smallest owner

| Change | Owner | Focused tests |
|---|---|---|
| Skystrip weather decoding and source precedence | `apps/skystrip_app/weather.py`, `weather_state.py`, `weather_timeline.py` | `test_skystrip_source_regressions.py`, `test_skystrip_weather_truth.py`, `test_skystrip_hourly_truth.py` |
| Skystrip polling and source failures | `apps/skystrip_app/providers/` | `test_skystrip_radar.py`, `test_skystrip_alert_runtime.py`, `test_skystrip_lightning_ingest.py` |
| Skystrip scenes, status clock and effects | `apps/skystrip_app/render/` | `test_skystrip.py`, `test_skystrip_effects_contract.py`, `test_skystrip_status_contrast.py`; registered visual scenarios |
| Skystrip interaction, generation checks and device ownership | `apps/skystrip_app/input.py`, `device/`, `audio/`, `runtime.py` | `test_skystrip_scrubber.py`, `test_skystrip_report_ux.py`, `test_skystrip_alert_restore_contract.py` |
| DSN XML decoding and curated mission policy | `apps/dsn_app/source.py`, `missions.py` | `test_dsn_xml_hardening.py`, `test_dsn_source_validation_contract.py`, `test_dsn_narration_truth_gate.py` |
| DSN feed, ranges and history | `apps/dsn_app/feed.py`, `ranges.py`, `history.py` | `test_dsn.py`, `test_dsn_history_recovery.py`, `test_dsn_source_boundary.py` |
| DSN selection, rendering and live leases | `apps/dsn_app/selection.py`, `render/`, `device/` | `test_dsn_runtime.py`, `test_dsn_visual_contract.py`, `test_dsn_network_paging_contract.py`; registered DSN scenarios |
| DSN narration text, worker and playback ownership | `apps/dsn_app/audio/` | `test_dsn_narration_ux_contract.py`, `test_dsn_narration_truth_gate.py`, `test_dsn_runtime_device_fixes.py` |
| App configuration parsing and application | Each app's `config.py` (pure) and `settings.py` (explicit startup) | `test_dsn_config_boundary.py`, `test_skystrip_config_boundary.py`, `test_dsn_cache_dir.py` |
| Config declarations and scalar/cross-key rules | `apps.toml`, `barkeep/registry.py`, `barkeep/config_validation.py` | `test_registry.py`, `test_barkeep_config_surface.py` |
| Config update normalization and the read/validate/write transaction | `barkeep/config_service.py` | `test_config_boundaries.py`, `test_server.py` |
| Env-file storage, effective layers and child-env filtering | `busybar_dev/config.py`, `barkeep/configstore.py`, `barkeep/__main__.py` | `test_configstore.py`, `test_startup.py`, `test_config_boundaries.py` |
| HTTP input/authentication and response mapping | `barkeep/server.py` | `test_server.py`, `test_config_boundaries.py` |
| Process ownership, restart and shutdown | `barkeep/supervisor.py` | `test_supervisor.py` |
| Firmware-specific startup brightness mitigation | `busybar_dev/brightness.py`, called by both app runtimes | `test_brightness_workaround.py`; [known issues](known-issues.md) |
| Browser config selection, edits and asynchronous responses | `barkeep/static/app.js` | `frontend/config-editor.test.cjs` (Node's built-in test runner) |
| Offline render evidence and audits | `busybar_viz/` | `test_viz_*.py`, `busybar-viz doctor`, `busybar-viz baseline check` |
| Review journal transactions and connection lifetime | `busybar_viz/journal.py` | `test_viz_journal.py`, `test_viz_journal_resources.py` |

Test paths in this table are relative to `tests/`. New app/helper Python
modules are automatically included by the directory-based mypy configuration;
there is no per-app type-check allowlist to remember.

## Keep dependencies one-way

The launchers `apps/dsn.py` and `apps/skystrip.py` only dispatch the CLI and
expose the established production renderer entry points. Both direct-script
and `python -m apps.<name>` invocation remain supported; `apps.toml` and its
renderer registrations keep the same paths. Internal functions have one owner
in `dsn_app/` or `skystrip_app/`, not a second compatibility facade. Import and
patch that owner in tests. The `apps` directory remains a checkout-local Python
namespace; its two implementation packages have inert `__init__.py` files.

Read the [DSN](../apps/dsn_app/README.md) or
[Skystrip](../apps/skystrip_app/README.md) package guide for the runtime flow and
the responsibilities within each directory. In both apps:

- `cli.py` parses arguments and applies configuration before starting work.
- `runtime.py` owns startup, background tasks, signal handling and shutdown.
- `model.py` owns the shared state and generation counters. Input, provider and
  device code operate on that same state; there is no mirrored state facade.
- `render/` produces pixels or render data, never provider/device operations.
- `device/` owns accepted draws, asset uploads, leases and transient retirement.
- `audio/` separates speech composition from synthesis/cache/playback effects.

The weather and mission modules are standard-library-only leaves. They do not
read configuration, fetch data, import an app runtime, or draw. Pass timezone
and reference time explicitly when calling the weather leaf. `config.py`
parses an explicit mapping into an immutable value; `settings.py` applies it
once at CLI startup. Importing an app does not load owner dotenv values or
create its cache directories. Consumers refer to the settings module rather
than copying mutable configuration into their own module globals. Barkeep
still runs one app per process; these settings are not a multi-instance API.

`tests/test_app_architecture.py` checks the import graph, including transitive
dependencies: no cycles, no render-to-runtime/device/provider/audio path, and
no speech-composition-to-render/device/provider path. It also keeps launchers
small, package initializers inert and app modules below a 750-line guardrail.
That limit is a backstop, not a reason to split an unrelated responsibility
across arbitrary files. Add functionality to its owner, not to the launcher.

Barkeep routes resolve an app, call a service, and translate errors to HTTP.
`ConfigService` owns one daemon's complete config transaction, including the
lock across read/merge/write. Atomic file replacement alone does not prevent
lost updates. This is an in-process lock, not cross-process coordination:
do not run multiple Barkeep writers against the same config directory.
`prepare_config_update` is the pure candidate-building seam and does not
mutate submitted, shared, or current values.

Config rules must agree with the parser: every separator recognized by
`str.splitlines()`, plus NUL, is invalid in a machine-written value. Only
registry-declared per-app overrides reach a child. The owner's base process
environment is a separate trust boundary and is not filtered away.

The browser must bind each async config response to both the selected app and
the edit revision. Discarding a stale response is intentional: a completed
request must not overwrite a newer selection or an unsaved edit. Frontend tests
execute the shipped script with controlled response ordering; they need no npm
packages. Real-browser checks complement, rather than replace, those tests.
After a save succeeds, preserve newer edits but update their comparison
baseline to the saved values, including a blank override's effective fallback.

The review journal similarly owns its resource lifetime: a SQLite transaction
context does not close its connection. Use the journal's scoped connection
helper for eager operations, and close streaming exports when a reader stops.

## Prove behavior at the boundary

For structural changes, preserve the control flow, data contracts and public
CLI/renderer seams. Migrate internal callers and test patch targets explicitly;
do not keep a monolith-shaped facade just to make old imports pass. For behavior
fixes, reproduce the bug against the original implementation before changing it.

Exercise source precedence, lease expiry, stale data, wheel/button ordering,
cancellation and shutdown—not just a successful draw. An interrupted card must
not reappear after a newer transient expires. Speech may be prepared in the
background, but playback still requires the current request generation.

Capture registered visual artifacts before editing, then compare exact pixels,
frame timing and audits afterward. A structural refactor should leave
`viz-baselines.toml` unchanged. Deliberate pixel changes require a reviewed
comparison and an explicit baseline update under the `busybar-viz` skill.

Keep shared device/protocol helpers in `busybar_dev/`. Do not introduce a common
app base class merely because both apps draw to the same device: their source
truth, scheduling and interruption policies differ. The visualizer stays an
offline consumer, never an app or Barkeep runtime dependency.

Run focused tests while editing, then every gate in CONTRIBUTING before
handoff. A matching visual baseline proves the registered scenarios, not every
possible live state; offline evidence is not physical-panel observation.

Coverage is a per-file ratchet, not a claim that every branch is tested. The
exceptions in `pyproject.toml` make existing gaps visible under their package
owners; the old Skystrip-wide average no longer hides runtime and device-effect
coverage. Extend those boundary tests when changing the corresponding behavior,
and raise the owner floor when coverage improves. Never exclude moved lines or
weaken a behavior assertion merely to make a refactor pass.

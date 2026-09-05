# Maintaining Skystrip

User controls, source policy and provider terms are in [skystrip.md](../skystrip.md).
`apps/skystrip.py` remains the launcher and public renderer seam. This package
owns implementation; its initializer intentionally imports nothing.

## Follow the data

`cli.main` validates configuration and the provider opt-in before starting work.
`runtime.run` owns one `model.SkyState`, provider/input/effect tasks, signals and
cleanup. Provider observations pass through pure decoding and explicit source
precedence before entering state. Input changes intent immediately; device work
commits a rendered scene after the wheel rests.

| Responsibility | Owner |
|---|---|
| Immutable config, endpoints, paths and clock inks | `config.py`; applied by `settings.py` |
| Weather payload validation and vocabulary | `weather.py` |
| Source leases, precipitation precedence and timeline lookup | `weather_state.py`, `weather_timeline.py` |
| NWS weather, radar, CAP alerts and optional lightning relay | `providers/weather.py`, `radar.py`, `alerts.py`, `lightning.py` |
| Bounded lightning wire decoding and eclipse calculations | `lightning.py`, `eclipse.py` |
| Shared state, report requests, statuses and owned tasks | `model.py` |
| Scene persistence and alert policy | `selection.py`, `alerts.py` |
| Wheel/button input and report requests | `input.py` |
| Startup, task ownership, redraw scheduling and shutdown | `runtime.py` |

## Rendering, device effects and speech

`render/scene.py` composes production scenes and animation loops from the
astronomy, season, precipitation and atmosphere layers. `city.py`, `grove.py`,
`backroads.py`, `forest.py`, `lakefront.py` and `traffic.py` own scene-specific
drawing. `art.py` loads checkout assets; `primitives.py` owns pixel operations.
`status.py` bakes the status clock; `effects.py` renders lightning and freight
frames; `alerts.py` renders alert cards. None may import providers, the runtime,
device operations or audio orchestration.

`device/display.py` owns scene payloads, stale-source notices and restoration.
`scrubber.py` owns immediate native readouts and deferred scene commits.
`report_status.py` owns report cards and retirement of interrupted IDs.
`alerts.py` and `effects.py` schedule bounded interruptions; `assets.py` manages
uploads and cleanup; `ambient.py` owns the status-light mood.

`audio/report_facts.py`, `report_plain.py` and `report_genz.py` compose speech
without render/device dependencies. `report_policy.py` checks request validity,
`report_assets.py` repairs and uploads cached speech, and `report.py` orchestrates
preparation and playback. `output.py` owns device-audio generations and stopping;
`siren.py` owns the locally generated alert sound.

## Change safely

Patch the module that owns an operation in tests, not `apps.skystrip`. Settings
have one explicit startup application; import modules rather than copying their
mutable values. Source timestamps, report generations and accepted scene state
remain on the same `SkyState` across these boundaries.

Run focused `test_skystrip_*.py` contracts, then all gates in
[CONTRIBUTING.md](../../CONTRIBUTING.md). Rendering changes also need before/after
artifacts under the `busybar-viz` skill. Registered clock, lightning and thunder
scenarios are deterministic samples, not proof of every weather/scene state;
scene, eclipse, precipitation, alert and scrubber contracts provide additional
coverage. `tests/test_app_architecture.py` guards the import boundaries.

Do not let a refactor change last-good-data policy, stale-source leases or
precipitation precedence. Uploads must not delay wheel feedback, and an expired
report/picker must never reveal an interrupted old alert. The optional lightning
endpoint stays in owner-readable `.env`, outside Barkeep's unredacted editor.

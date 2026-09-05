# Maintainer refactor: validation record

This is a change record, not a claim about future revisions. The public source
baseline was `65f734c84b78f01afee92f45b922f60cebeb2b9f`; the full package move
was additionally compared with a local preparatory boundary-fix snapshot.
The public commit consolidates that preparation without publishing private
development commit metadata.
Current ownership and contributor commands live in [maintaining.md](maintaining.md)
and [CONTRIBUTING.md](../CONTRIBUTING.md).

## Scope

Both complete app implementations now live in packages with separate source,
state/selection, rendering, device, speech and lifecycle owners:

| App | Original launcher/implementation | New launcher | Largest implementation module |
|---|---:|---:|---:|
| DSN | 8,192 lines | 29 lines | `dsn_app/source.py`: 677 lines |
| Skystrip | 9,467 lines | 32 lines | `skystrip_app/weather.py`: 599 lines |

This is not a rewrite of the visuals or scheduling policies. The direct-script
and module CLI commands, registry configuration, production renderer entry
points, device asset/element contracts and visual baselines remain unchanged.
Internal imports and test patch targets deliberately move to their actual
owners; the launchers do not retain a monolith-shaped compatibility facade.

The repo-wide work also separates Barkeep config transactions from HTTP,
hardens config storage/spawn boundaries, fixes browser config races, scopes
visualizer journal connections, expands type-check discovery and updates the
maintainer guides and single authored app skill. Existing supervisor/HTTP/UI
and production/offline-visualizer separations remain intact. No dependencies
or dependency versions were added or changed.

## Reproduced failures

Each behavioral correction was exercised against the earlier implementation
before the fix, then retained as a regression test.

| Failure | Permanent evidence |
|---|---|
| Env-file separators could forge additional assignments; undeclared overrides could reach a child | `tests/test_config_boundaries.py`, `tests/test_startup.py` |
| Concurrent config saves could lose an unrelated update | `tests/test_config_boundaries.py` |
| Non-ASCII login or malformed foreground names produced server errors | `tests/test_config_boundaries.py` |
| Malformed station fields could abort otherwise usable observations | `tests/test_skystrip_source_regressions.py` |
| DST wall-clock comparisons selected the wrong observation/history interval | `tests/test_skystrip_source_regressions.py` |
| Delayed browser responses replaced a newer app's form or unsaved edits | `tests/frontend/config-editor.test.cjs` |
| Completed journal transactions leaked SQLite connections | `tests/test_viz_journal_resources.py` |
| Malformed history rows or invalid UTF-8 prevented DSN cache recovery | `tests/test_dsn_history_recovery.py`: 14 failing-before/passing-after cases |
| The clock artifact omitted its app source; launcher-only provenance missed extracted code | `tests/test_viz_app_provenance.py`: seven failing-before/passing-after scenario checks |

The source-inventory test also verifies that owner configuration and generated
state are not included. The package-move provenance cases guard a regression
introduced by thin launchers; the clock's missing source record predated it.

## Verification results

- Full hardware-free suite, including coverage: **2,334 passed, one skipped**
  on Python **3.11.15** and **3.13.12**. The original public baseline was
  **2,224 passed, one skipped**.
- Coverage: **87.6%** on both interpreters; per-owner coverage floors pass.
  The split makes existing runtime/device-effect gaps explicit instead of
  hiding them inside an app average. Additional watcher-lifecycle tests raised
  Skystrip runtime coverage from 34.3% to 61.1%; uncovered branches remain.
- Frontend behavior: **nine passed on Node 22**, using the shipped script.
  An earlier real-browser check also exercised out-of-order app selection.
- Ruff, mypy (**163 source files**), byte compilation, shell syntax, diff
  whitespace and the public-release snapshot scan pass.
- The wheel builds and installs into a fresh environment. Installed CLI and
  module entry points pass scenario checks from the checkout root and `apps/`;
  imports are confirmed to come from `site-packages`.
- All **eight** registered scenarios pass required audits and baseline checks.
  `viz-baselines.toml` is unchanged: no accepted pixel drift, missing baselines
  or stale baselines.

`tests/test_app_architecture.py` additionally rejects import cycles, transitive
render-to-I/O dependencies, speech-composition-to-render/I/O dependencies,
eager package facades and renewed app monoliths. Entry-point tests exercise
direct scripts, original module commands and implementation-package commands.
Watcher tests run the real coordinator through provider startup, fresh/stale
weather, draw refusal/failure, SIGTERM and cleanup failures using controlled
external edges.

## Before/after equivalence

A migration-time AST comparison accounted for all **549** top-level function,
async-function and class definitions across both apps and their helper modules.
After projecting explicit owner-qualified references back to their old names,
**543** matched. The six differences were individually checked: DSN history
recovery; a radio-band membership set decoupled from palette data; an explicit
weather annotation; the moved checkout-root reference; the preview's settings
assignment; and the clock-ink choices' single owner. This is structural
evidence, not a formal proof of every runtime state.

A separate before/after production-output corpus covered **238 cases**:
192 Skystrip loops (six scenes, eight weather states, four timestamps), 40 DSN
view/event loops and six narration samples. All **12,260 frames**, frame rates,
hold values, encoded native-animation bytes and narration strings matched.
The corpus used fixed public reference inputs, not owner location or live data.

All eight registered artifact comparisons separately reported `changed: false`,
with zero changed pixels and identical frame counts/rates. Two representative
records, including inspected native and LED-gap contact sheets:

| Scenario | Before artifact | After artifact |
|---|---|---|
| Skystrip clock | `d940b18743129b869ea944b403d03eb3b799d99ef1c591650c0127035365b934` | `307a4907eff78853fe6ef7d2498b01dab5d587f5b8558425872069d0a238910a` |
| DSN default | `4913f55d11e8ee69e8465a9e92835392a701f6afd3d621c281d1959a336ed6c4` | `0fff23a34a68acab6991dd4c6aad1640c2e792b1063ce98da70d8bc866b59a34` |

The immutable artifacts and comparison records are runner-local under
`scratch/busybar-viz/`, not committed generated media. Review notes bind the
inspected artifacts to session `ses_9fa18b8d9a864dc8afe3f7919b3eb9a6`, revisions
2 and 3. Re-run the documented scenario gates to verify the current checkout;
artifact identity can change with source metadata while pixel baselines match.

## Limits

The initial execution above was on macOS. A subsequent Linux aarch64 canary
of the same refactor source passed **2,335 tests** on Python **3.13.5**, with
87.6% coverage and all coverage floors passing. All eight visual audits and
baselines passed there too. Both apps acquired live provider data and received
successful native-asset uploads/draws under systemd, switched cleanly and exited
with status zero. Real Kokoro synthesis and framebuffer readback succeeded;
these checks did not exercise every physical interaction or spoken report.
Private configuration, service identifiers, logs and device images are not
included in the public record.

The [firmware 1.2.3 brightness mitigation](known-issues.md) was added after that
canary. It deliberately changes device brightness at app startup, not renderer
pixels. Its real startup-path regression tests first failed on the refactor
without the mitigation. The automated policy is tested offline; the physical
evidence is the separately observed manual-brightness contrast restoration,
not a live deployment of the new startup helper.

The final public candidate, including the brightness helper, passed **2,383
tests with one skipped** on macOS/Python **3.11.15**. Coverage was **87.6%**;
all per-owner floors passed and the new helper had 100% statement coverage.
Its 49 focused tests include both real app startup paths and the installed
client's wire contract. Nine frontend tests, Ruff, mypy (164 source files),
compilation, shell syntax and the 434-file public snapshot scan passed.
All eight visual baselines remained unchanged. Fresh clock and DSN artifact
comparisons also reported zero changed pixels; native and gap contact sheets
were inspected and recorded in a separate runner-local review journal.

The unchanged dependency audit was not rerun locally. Offline app evidence is
**renderer-verified**; the two inspected gap simulations are **gap-previewed**.
Live framebuffer readback is separate from physical observation. Passing these
checks establishes no observed regression within their coverage, not an
absolute guarantee for every live state.

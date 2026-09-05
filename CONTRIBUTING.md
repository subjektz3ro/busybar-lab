# Contributing

Contributions may add apps, fix existing behavior, improve the development
tools, or update documentation.

## Getting set up

```bash
cd busybar-lab                    # after cloning the repository
uv sync --locked --dev
uv run pytest -q              # the whole suite, no hardware required
```

Use the [maintainer map](docs/maintaining.md) to find the owner and focused
tests for a change. Browser behavior tests additionally need Node.js 22 or
newer, with no npm dependencies to install.

**You do not need a BUSY Bar to contribute.** Device access goes through
`busybar_dev.connect()`, and CI makes no calls to hardware or external
services. The offline command depends on the app: `hello --dry-run` builds its
native request, `dsn --dry-run` exercises the live NASA data path without a
bar, and `skystrip --preview out.png` renders without either a bar or network.

With a bar plugged in over USB:

```bash
uv run apps/hello.py          # draw, screenshot both displays
uv run apps/hello.py --clear  # put it back
```

## Building an app

```bash
uv run scripts/new_app.py yourapp     # template + apps.toml entry in one step
uv run apps/yourapp.py --dry-run      # also law-checks the payload offline
```

Then read [`.claude/skills/busybar-app/SKILL.md`](.claude/skills/busybar-app/SKILL.md).
It documents draw priority, immutable element geometry, asset caching, LED
spacing, and other device behavior that may fail without an obvious error.

`new_app.py` registers the app in [`apps.toml`](apps.toml) for you, which is
what makes it appear in barkeep's UI with a generated config editor. If
Barkeep is already running, restart the Barkeep daemon so it reloads the
registry; restarting only a child app does not reload `apps.toml`. An app not
listed there cannot be run.

## Run the same checks as CI

The local quality gates are the commands used by CI. The test job is repeated
on both ends of the complete supported range, Python 3.11 and 3.13. Both jobs
install and import the required Linux Kokoro engine; model files are not needed
for the hardware-free test suite:

```bash
uv run python -m compileall -q apps barkeep busybar_dev busybar_viz scripts
uv run ruff check --output-format github .
uv run mypy
node --test tests/frontend/*.test.cjs
BUSYBAR_HOST=203.0.113.1 uv run pytest -q \
  --cov=apps --cov=barkeep --cov=busybar_dev --cov=busybar_viz \
  --cov-report=json:coverage.json --cov-report=term:skip-covered
uv run python scripts/check_coverage.py coverage.json
uv run busybar-viz doctor --json
uv run busybar-viz baseline check --json
uv run --no-project --python 3.11 scripts/check_public_release.py
for f in deploy/*.sh; do bash -n "$f"; done
```

To reproduce the engine import smoke on a supported Python 3.11–3.13 Linux
host, run `uv sync --locked --dev`, then
`uv run python -c 'from kokoro_onnx import Kokoro'`. The model files are not
needed merely to import the engine.

`doctor` and `baseline check` are the visual gates: any change to a registered
visual must carry its updated pixel digests
(`uv run busybar-viz baseline update`) in the same diff, or CI fails on drift
the local block would otherwise miss. CI's
`clean-wheel` job additionally builds the wheel and imports its entry point
from an empty directory, which catches packaging mistakes no local test sees.

The dependency audit and installed-wheel smoke need package-index access. To
reproduce those jobs as well:

```bash
ci_artifacts=$(mktemp -d)
uv export --locked --no-dev --no-emit-project --format requirements-txt \
  -o "$ci_artifacts/requirements.txt"
uvx --from pip-audit==2.9.0 pip-audit \
  -r "$ci_artifacts/requirements.txt" --progress-spinner off

uv build --wheel --out-dir "$ci_artifacts/wheel"
uv venv "$ci_artifacts/wheel-venv" --python 3.11
uv pip install --python "$ci_artifacts/wheel-venv/bin/python" \
  "$ci_artifacts"/wheel/*.whl
"$ci_artifacts/wheel-venv/bin/busybar-viz" doctor --json
"$ci_artifacts/wheel-venv/bin/busybar-viz" \
  --data-dir "$ci_artifacts/busybar-viz" \
  run skystrip/lightning-near --json

(
  cd apps
  "$ci_artifacts/wheel-venv/bin/python" -c \
    'import busybar_viz; assert "site-packages" in busybar_viz.__file__'
  "$ci_artifacts/wheel-venv/bin/python" -m busybar_viz \
    --data-dir "$ci_artifacts/busybar-viz-module" \
    run conformance/dual-display-input-replay --json
)
```

The visualizer wheel contains its CLI and core, not a standalone copy of the
apps. Its app adapters deliberately resolve production code and assets from
the current `busybar-lab` checkout; run the smoke commands above from the
checkout root.

## House rules

**Tests describe behaviour, not implementation.** The suite reads as claims
about what the thing does — `test_the_arctic_gets_a_midnight_sun_in_july`, not
`test_globe_2`. If a test would still pass with the feature broken, it is not
finished.

**Comments say why, not what.** Most of the comments in this repo exist
because something was tried and failed on the physical panel. Those are the
valuable ones. If you fix a bug that could plausibly come back, leave the
reason behind.

**Nothing personal in the repo.** No addresses, coordinates, hostnames or
tokens in tracked files. Personal values live in `.env` (gitignored) and are
documented in `.env.example`. Services that want a contact address get one
through config, never a hardcoded default — see `SKYSTRIP_CONTACT`.

**No hardcoded hosts.** Apps reach the device only via `busybar_dev.connect()`,
which resolves `BUSYBAR_HOST` / `BUSYBAR_TOKEN`. This is what lets the same app
run against a bar on your desk over USB and one on a server across the network.

**Long-running apps must shut down cleanly.** They run under systemd. Handle
SIGINT and SIGTERM, and clear draws before exit. `asyncio.run()` joins
default-executor threads during shutdown, which can delay SIGTERM handling
until systemd sends SIGKILL; use a daemon thread and `call_soon_threadsafe`.

**Portability.** The supported service target is 64-bit glibc 2.28+ Linux on
`x86_64` or `aarch64`; CI runs Ubuntu 24.04 x86_64 with Python 3.11 and 3.13,
and production uses systemd. macOS has a non-CI-gated development/direct-run
path and no service installer. Other Linux combinations and Windows are
unsupported. `busybar_dev/tts.py` is the one place a platform-specific call is
allowed: Kokoro is required on supported Linux, with `espeak-ng` available as
a runtime fallback; direct macOS development uses `say`. Keep app logic
platform-neutral, and look up executables on `PATH` rather than hardcoding a
bundle path for one OS.

## Licensing

The project is **GPL-2.0-or-later**. Its copyleft requirement comes from
`busybar_dev/anim.py`, a port of upstream GPL-2.0-or-later firmware tooling.
Contributions are accepted under that licence. Keep the attribution header in
`anim.py` intact,
and record newly vendored material in [`NOTICE.md`](NOTICE.md) with its licence.

## Pull requests

Keep pull requests focused. Describe the change, list the checks run, and state
whether the result was rendered, framebuffer-captured, or observed on physical
hardware. `busybar_dev/screen.py` captures both displays for screenshots.

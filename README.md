# BUSY Bar Lab

Live apps for the [BUSY Bar](https://busy.app) — a 72×16 RGB LED strip on your
desk — plus a control plane to run them on an always-on host.

This is an independent community project, not an official Flipper FZCO/BUSY
product and not endorsed by the device vendor.

Two of them are finished pieces of work rather than demos: **NASA's Deep Space
Network, live**, and **an ambient rendering of the sky outside your window**.

| | |
|---|---|
| ![dsn network](docs/media/dsn-network.gif) | **dsn, Network view** — the default, and the only one that shows the whole network at once: every site, its physical dishes, and how many live links each is carrying. `G3` means three live dish-to-spacecraft links at Goldstone; `34(2)` means DSS-34 carries two of them. Turn the wheel and it opens the selected dish's aim and co-dish links |
| ![dsn instrument](docs/media/dsn-instrument.gif) | **dsn, Instrument view** — one antenna and one contact, in detail. The RF lanes move because the numbers behind them do: received power, band, rate, and what Earth is transmitting back |
| ![dsn](docs/media/dsn-browsing.gif) | **dsn, Distance view** — one of three live scales. Network defaults to an explicit three-site dish/link roster; wheel rest opens the selected physical dish's aim and co-dish links. Instrument shows one real antenna/contact; Distance preserves the Earth-to-spacecraft light-time journey shown here |
| ![dsn real time](docs/media/dsn-realtime.gif) | **dsn**, locked to real time — one represented carrier slice, creeping. Frames are 27 minutes apart, because that is what one pixel of travel costs at Voyager's distance |
| ![skystrip](docs/media/skystrip-day.gif) | **skystrip** — a whole day at the solstice at the public Greenwich demo fixture, half an hour a frame. Real solar elevation drives the light; the icon's horizontal placement is an artistic composition |
| ![seasons](docs/media/skystrip-seasons.gif) | **skystrip** through the year — same hour, same weather, only the date moves |
| ![weather](docs/media/skystrip-weather.gif) | **skystrip** weather — clear, overcast, drizzle, downpour, storm, severe, snow |
| ![christmas](docs/media/skystrip-christmas.gif) | **skystrip** at Christmas — the house roofline and the skyline windows, decorations off then on |

## Quickstart

Run the apps from a source checkout. You need **Git** and
[`uv`](https://docs.astral.sh/uv/getting-started/installation/); `uv` selects or
downloads a compatible Python 3.11–3.13 interpreter. The initial sync, dry run,
preview, and test suite do not need a BUSY Bar.

```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
uv sync --locked
uv run apps/hello.py --dry-run # prove the stack offline, no bar needed
uv run apps/hello.py           # with a bar: draw HELLO, save proof PNGs
uv run apps/hello.py --clear   # take it back down
```

The built Python wheel contains the reusable Python packages, Barkeep, and the
`busybar-viz` CLI. It is not a standalone bundle of the apps, their assets,
`apps.toml`, or the deployment scripts; use the source checkout to run or
develop this project.

### Platform support

| Platform | Support level |
|---|---|
| **Linux** | The supported service target is 64-bit glibc 2.28+ Linux on `x86_64` or `aarch64`, with Python 3.11–3.13. CI gates Ubuntu 24.04 x86_64 on Python 3.11 and 3.13; the systemd deployment path supports both CPU families. Other Linux combinations are unsupported. |
| **macOS** | A source-development and direct-run path is provided, with `say` for speech. It is not CI-gated and has no systemd service installer. |
| **Windows** | Not currently supported or tested. The deployment tooling requires Bash and the service path requires systemd; use a Linux host (or an unverified WSL setup) instead. |

The complete supported stack uses CPython 3.11–3.13. Kokoro is the required
production voice for Skystrip and DSN on Linux, not a reduced-experience extra.
The installer refuses unsupported Linux hosts and does not call setup complete
until the package imports, both hash-verified model files are present, and a
real short synthesis succeeds. `espeak-ng` remains emergency runtime
resilience if an installed neural engine later becomes unavailable.

For the same live Skystrip/DSN setup used in production, clone the repository
on the server and run [`./deploy/install.sh`](deploy/README.md). That one command
syncs the locked environment, writes or preserves `.env`, downloads and checks
the Kokoro models, proves speech with real audio, and optionally installs
Barkeep as a service. Copying `.env.example` is useful for direct development,
but it does not install or validate the production speech stack.

```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
./deploy/install.sh
```

Plugged in over USB the bar answers at `10.0.4.20` with no authentication. For
local Wi-Fi/LAN access set `BUSYBAR_HOST` and `BUSYBAR_TOKEN`; this repository
does not route device traffic through the vendor cloud.

**You don't need a bar for most development.** Offline entry points are
app-specific rather than pretending a device-native text draw and a raster
scene have the same preview path:

| Command | BUSY Bar | External network | What it checks |
|---|---:|---:|---|
| `uv run apps/hello.py --dry-run` | no | no | Builds the native draw request |
| `uv run apps/dsn.py --dry-run` | no | yes | Fetches and describes the live NASA feed |
| `uv run apps/skystrip.py --preview sky.png` | no | no | Renders one PNG without device I/O |
| `uv run pytest -q` | no | no | Runs the hardware-free test suite |

```bash
uv run apps/skystrip.py --preview sky.png --at 03:30 --storm
uv run pytest -q
```

### The agent's eyes: busybar-viz

This repository is built to be developed by AI coding agents, and an agent
cannot look at an LED panel. `busybar-viz` closes that gap: it audits exact
front/back pixels against the panel's measured physics, simulates the
physical LED gaps, replays timed button and wheel input, and publishes
immutable evidence bundles an agent can read, diff, and cite — so a visual
bug is caught by a named check with frame-specific measurements, not only by
a person squinting at the bar.

```bash
uv run busybar-viz view scratch/candidate.png --json   # see any frames, zero setup
uv run busybar-viz run conformance/dual-display-input-replay --json
uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
uv run busybar-viz baseline check --json   # CI-enforced pixel regression gate
uv run busybar-viz serve       # optional human review UI at http://127.0.0.1:8765
```

Every registered scenario's accepted pixels are pinned in
[`viz-baselines.toml`](viz-baselines.toml): CI runs `doctor` for required
audits and `baseline check` for pixel drift. A pull request that deliberately
changes what the panel shows records that acceptance in the same diff;
unacknowledged drift fails.

`view` audits ad-hoc in-development frames — including an app's enlarged
`--preview` output — with no registration, and `--region`/`--ink` bring the
device-law legibility checks to them. Registered scenarios (`run`) add
declared source provenance; production-renderer scenarios prove that app-owned
code produced the pixels. Renderer workers block ordinary network/device
access. Registration is one `[<app>.viz]` table in `apps.toml` (how
`dsn/default` is declared);
hand-written adapters cover Skystrip and an app-neutral conformance fixture.
Hello draws native firmware text, which the visualizer deliberately does not
emulate.

“Offline” describes rendering: it needs neither a bar nor external network
access. The optional `serve` command is a loopback HTTP development UI and
stores its append-only review journal in a local SQLite file. It is not part of
Barkeep. The CLI wheel is tested in isolation, but app adapters intentionally
load production source and assets from the active checkout, so run it from a
clone rather than treating the wheel as a standalone app bundle.

The everyday loop is self-serve — render, read the audit, fix, compare —
with no session or person required. When a person joins the review, the UI's
append-only session journal is shared by people, Codex, and Claude Code: an
agent renders a candidate, makes its exact SHA current with
`uv run busybar-viz session present`, then waits for design feedback in the
same turn with `uv run busybar-viz session events SESSION_ID --after REVISION
--wait 55 --json`. See [the complete visualizer guide](docs/busybar-viz.md) for
app adapters, semantic inputs, artifacts, comparisons, and the evidence levels
that distinguish a renderer result from an actual panel observation.

## The apps

| App | What it is | |
|---|---|---|
| **dsn** | NASA's Deep Space Network, live — global Dish Roster, selected-dish Focus, selected-contact Instrument, and the original Distance/light-time journey | [docs](apps/dsn.md) |
| **skystrip** | An ambient sky outside — coordinate-based global model weather, NWS observations/alerts where its point API covers the location, and northern-temperate seasonal art | [support + docs](apps/skystrip.md#geographic-support) |
| **hello** | The smoke test: draw and screenshot, with optional speech | [docs](apps/hello.md) |

Each doc covers what the app shows, **what every button does**, its config keys
and where its data comes from. [`apps/README.md`](apps/README.md) has the
conventions they share.

Some of what came out of building them: the spacecraft are drawn as
themselves — 25 portraits, so Perseverance is a rover with wheels and Juno is
three enormous blades that glint as it spins at its real 2 rpm. The globe's
terminator is computed from the true subsolar point, so the Arctic keeps its
midnight sun in July. The antenna icon leans at the pass's real elevation.

## barkeep — the control plane

![barkeep](docs/media/barkeep.png)

_Representative capture; it predates the provider-use line now shown below
the display previews._

An always-on daemon that supervises the apps and serves a web UI on `:8080`: a
live mirror of both displays, which app owns the bar, per-app logs, and a
config editor generated from each app's declared keys.

A fresh Barkeep starts in **STANDBY**. Before selecting Skystrip, Barkeep's
display-preview panel shows linked Open-Meteo/RainViewer credits and
public-service limits; selecting Skystrip is what starts those provider
requests. Previously saved foreground choices still restore on restart.

```bash
uv run -m barkeep      # then open http://localhost:8080
```

One app owns the display at a time — [apps cannot
overlay](apps/README.md#why-apps-dont-overlay), which is a property of the
device, not a design choice.

**barkeep is unauthenticated and loopback-only by default.** Anything that can
reach it can read logs and the framebuffer, write declared app configuration,
and control child processes, so LAN exposure is an operator choice rather than
the installation default. Keep `BARKEEP_BIND=127.0.0.1` and use an SSH tunnel,
or set an explicit LAN bind plus a strong `BARKEEP_TOKEN` and `BARKEEP_TLS=1`
— HTTPS with a generated self-signed certificate, replaceable later from the
UI itself. The token lives only in the host's gitignored `.env`; the web UI
asks for it once per browser and keeps a cookie after that
([how to log in](deploy/README.md#logging-in-from-another-machine)). The
daemon warns when it is exposed without a token.
[`SECURITY.md`](SECURITY.md) is the full statement.

## Running it on a server

The supported service host is a 64-bit glibc 2.28+ Linux machine on `x86_64` or
`aarch64` that stays on and can reach the bar — for example, a Pi 4/5 with
64-bit Raspberry Pi OS and at least 2 GiB RAM, a NUC, a 64-bit laptop, or a VM.
Containers are an advanced manual-run path rather than the documented systemd
deployment. The host never needs to accept connections from the internet.

What it needs before you start, none of which ships on a minimal image:

| | Why |
|---|---|
| **`uv`** | Creates the exact locked environment and supplies a compatible Python 3.11–3.13 interpreter when needed |
| **`git`** | Required by the installer, not just to clone: `ship.sh` deploys by fetching into the checkout *on the host*, so `install.sh` refuses a host without git rather than leaving one that can never update |
| **`curl`** | Downloads the required, hash-pinned Kokoro model files on Linux |
| **`bash`** | `install.sh` and `ship.sh` are bash, not POSIX sh |
| **`openssl`** | Optional: generates the persistent self-signed certificate for `BARKEEP_TLS=1` |
| **`sudo`** | For the emergency `espeak-ng` fallback, and for `systemctl` if you run it as a service |
| **systemd** | Only if you want it supervised across reboots |
| **an ssh server** | Only if you deploy with `ship.sh` rather than pulling by hand |

Install `uv` through a trusted package manager or its
[official instructions](https://docs.astral.sh/uv/getting-started/installation/),
then run `./deploy/install.sh` as the unprivileged account that will own the
checkout and run barkeep — never with `sudo`. The installer syncs the Python
packages and speech voice, and invokes `sudo` itself only for the optional
package-manager and systemd operations that require it. On a systemd host it
offers to install barkeep as the one service that supervises every app.

Have at least **1 GB of free disk** and **2 GiB of RAM**. A
clean installed checkout is about **600–650 MiB**, including the roughly 340
MiB model bank; the `uv` download cache can temporarily retain a few hundred
MiB more, and neural synthesis can approach 1 GiB of resident memory.
[`docs/dependencies.md`](docs/dependencies.md) has the full support and resource
matrix.

```bash
./deploy/install.sh                    # environment, required Kokoro, optional service
```

If you skip the service prompt, the manual commands remain in
[`deploy/README.md`](deploy/README.md).

On a remote server, reach the loopback-only control plane through SSH:

```bash
ssh -N -L 8080:127.0.0.1:8080 your-user@server.example
```

Then open `http://127.0.0.1:8080` on your own computer and select **Skystrip**
or **DSN**.

To update an ordinary install from this public repository, run this in the
server checkout:

```bash
sudo systemctl stop "barkeep@$USER"
git pull --ff-only
./deploy/install.sh
```

Stopping first prevents the running daemon from loading a mixture of old and
new files during the update. The installer keeps `.env`, resyncs the lockfile,
rechecks the Kokoro models and real synthesis, refreshes the service unit when
needed, and starts the enabled service only after those checks pass. If you
run Barkeep manually instead, stop that process before pulling and start it
again after the installer succeeds. If you develop your own changes, use the
separate maintainer deploy flow after pushing them to a remote you control:

```bash
git push origin main
./deploy/ship.sh myhost
```

`ship.sh` refuses to deploy a commit that isn't on origin, because the host
couldn't fetch it. Full detail, including the read-only deploy key a
private-repo host should use, is in [`deploy/README.md`](deploy/README.md).

## Building an app

```bash
uv run scripts/new_app.py yourapp     # template + apps.toml entry in one step
uv run apps/yourapp.py --dry-run      # also law-checks the payload offline
```

[`docs/agent-cookbook.md`](docs/agent-cookbook.md) walks the whole loop —
create, see, iterate, register, pin, capture — written for the coding agents
this repository is built around.

Then read [**`.claude/skills/busybar-app/SKILL.md`**](.claude/skills/busybar-app/SKILL.md).
It's easy to miss because `.claude/` is a dotfile directory, and it is the
single most valuable file here: the device behaviours that fail *silently*,
each one learned by shipping it wrong.

While a visual is moving, audit each iteration with `uv run busybar-viz view`
— any PNG frames, zero setup, with the device-law legibility checks on the
regions you declare. When it stabilizes, register its default scenario as
data: a `[<app>.viz]` table in `apps.toml` naming one pure production
renderer seam (see the header comment there). Hand-written adapters, planned
with `uv run busybar-viz scaffold`, are only for apps that need typed
controls, input replay, or fault injection. The
[`busybar-viz` skill](.claude/skills/busybar-viz/SKILL.md) gives agents the
working loop; [`docs/busybar-viz.md`](docs/busybar-viz.md) is the exact
CLI and evidence contract. Production apps never import the visualizer, and a
generated LED-gap preview is not evidence that anyone inspected it.

Priority is not z-order. Most element fields are immutable after the first
draw. Assets are cached by path forever. `timeout` is whole seconds. Text is
printable ASCII only. One felt click of the wheel is one encoder count. And the
LEDs are spaced nearly their own width apart — 1.23 mm lit on a 2.2 mm pitch —
so a filled shape reads as a haze, brightness reads as *size*, and anything
under about 30% contrast is invisible on the panel however good it looks in a
preview.

`new_app.py` has already added the [`apps.toml`](apps.toml) entry that makes the
app appear in Barkeep with a generated config editor. If Barkeep is already
running, restart the Barkeep daemon to reload the registry; restarting only a
child app does not reload `apps.toml`. See
[`CONTRIBUTING.md`](CONTRIBUTING.md).

## Documentation and agent guidance

| | |
|---|---|
| [`AGENTS.md`](AGENTS.md) | The canonical guide: the device's behaviour, the architecture, the house rules. Read first |
| [`docs/README.md`](docs/README.md) | Index of first-party guides, app docs, external references, and historical design records |
| [`docs/dependencies.md`](docs/dependencies.md) | Everything the host needs, measured — packages, model files, disk, and which TTS engine speaks when |
| [Official BUSY developer docs](https://docs.busy.app/bar/dev) | Current device setup, API access, and HTTP API guidance from the vendor |
| `docs/api/openapi.yaml` | Optional, gitignored OpenAPI snapshot fetched from a connected device for local inspection |
| `docs/busylib/` | Redistributable official Python client docs, examples, licence, and its own `AGENTS.md` |
| `.claude/skills/busybar-app/` | The device laws that fail silently |
| `.claude/skills/busybar-viz/` | The agent's eyes: offline visual iteration, inspection, comparison, and evidence discipline |

With a bar reachable, `uv run scripts/refresh_docs.py` can refresh the ignored
local device specification. The public repository does not redistribute a
scraped copy of the vendor documentation.

Release notes are in [`CHANGELOG.md`](CHANGELOG.md).
Source on `main` may include the changelog's **Unreleased** section;
`pyproject.toml` keeps the most recently released version until the next tag is
cut. Use a release tag when you need a fixed public version.

## Architecture

Same app code on your laptop over USB and on the host in the corner — which is
why nothing in `apps/` may hardcode an address. Diagrams, the directory map,
and the one rule that shapes the whole design are in
[`docs/architecture.md`](docs/architecture.md).

## Licence

**GPL-2.0-or-later.** The copyleft requirement comes from
`busybar_dev/anim.py`, a port of upstream GPL-2.0-or-later firmware tooling.
The or-later grant also permits use under GPLv3 alongside the Apache-2.0 Astral
dependency. Third-party material, data-source terms, licence details, and
attribution are in
[`NOTICE.md`](NOTICE.md).

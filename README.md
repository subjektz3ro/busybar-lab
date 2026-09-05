# BUSY Bar Lab

Build and run custom apps for the [BUSY Bar](https://busy.app). Start with the
included weather and space displays, or make your own using the Python app
template, local guides, and offline preview tools. Work directly or with an
AI coding agent.

The apps run on **your computer**, which talks to the bar over USB or your
local network. Keep that computer awake for live updates; no firmware flashing
or vendor-cloud account is needed for this workflow. **Barkeep** is the
included web UI that starts, stops, and configures the apps.

This is an independent community project, not an official BUSY/Flipper product.

[Run an app on your bar](docs/quickstart.md) ·
[Build your own app](#build-your-own-app) ·
[All documentation](docs/README.md)

## Quickstart

Start here on **macOS or supported 64-bit Linux**. Install
[Git](https://git-scm.com/downloads) and
[uv](https://docs.astral.sh/uv/getting-started/installation/) first, then open
a terminal. `uv` manages Python and the project's dependencies for you.

<!-- quickstart:setup -->
```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
uv sync --locked
uv run apps/hello.py --dry-run
```

You should see `Dry run payload:` containing `HELLO`, with no connection to a
bar. Setup downloads packages; the dry run itself needs neither hardware nor
internet access.

Want to see a scene before connecting anything?

<!-- quickstart:preview -->
```bash
mkdir -p scratch
uv run apps/skystrip.py --preview scratch/sky.png --at 12:00
```

Open `scratch/sky.png` in an image viewer. This is an offline sample, **not
your live weather**. Generated files stay in the gitignored `scratch/` folder.
The warning about unset coordinates is expected for this unconfigured demo.

**Ready for the real bar?** Continue with the
[step-by-step quickstart](docs/quickstart.md#2-connect-your-bar): USB or Wi-Fi
setup, private configuration, a HELLO check, then your first app in Barkeep.
It also covers Linux service installation and common setup problems.

### Platform support

- **Linux:** supported service target: 64-bit `x86_64` or `aarch64`, glibc
  2.28+, Python 3.11–3.13. Ubuntu 24.04 and 64-bit Raspberry Pi OS are examples.
  For the full speech-enabled install, allow at least 2 GiB RAM and 1 GB free
  disk. [Complete requirements](docs/dependencies.md).
- **macOS:** development and direct-run support; speech uses `say`. No
  systemd installer and no macOS CI gate.
- **Windows:** not currently supported or tested.

Use a **source checkout**, not a standalone wheel: the apps, assets,
`apps.toml`, and deployment scripts are needed alongside the Python packages.

### Firmware brightness note

On firmware **1.2.3**, Skystrip and DSN switch **Auto to fixed 35%** at startup
to mitigate washed-out dark scenes. Existing manual levels are preserved.
This disables ambient-light adjustment; it is not a firmware repair.
[Known issues and opt-out](docs/known-issues.md).

## Included example apps

| App | What it shows | Setup |
|---|---|---|
| [DSN](apps/dsn.md) | Live activity from NASA's Deep Space Network: antennas, spacecraft links, and signal travel time | No location required; a good first live app |
| [Skystrip](apps/skystrip.md) | An ambient scene following daylight and weather, with a spoken report | Set latitude, longitude, and timezone first |
| [Hello](apps/hello.md) | A simple text draw and screenshots | Use it to check the device connection |

Live apps need internet access to their data providers. Skystrip uses
Open-Meteo model weather for ordinary land locations worldwide; NWS
observations and alerts are available only within NWS coverage. Seasonal art
is northern-temperate. Read its [geographic support](apps/skystrip.md#geographic-support)
and [provider terms](apps/skystrip.md#provider-terms-and-commercial-use) before
enabling it. Skystrip is not a life-safety warning system.

## Example app gallery

| DSN: live antenna activity | Skystrip: daylight and weather |
|---|---|
| ![DSN dish roster](docs/media/dsn-network.gif) | ![Skystrip day cycle](docs/media/skystrip-day.gif) |

These are illustrative examples; Skystrip's day cycle uses a public Greenwich
fixture and accelerated time. [More views and animations](docs/gallery.md).

## barkeep — the control plane

Barkeep lets you pick the foreground app, change its configuration, and read
its logs in a browser. A fresh install starts in **STANDBY**, with no foreground
app selected. The [quickstart](docs/quickstart.md#4-start-barkeep) walks through
starting it and choosing an app.

![Barkeep web UI](docs/media/barkeep.png)

*Representative capture; the current UI also shows provider credits and use
limits below the display previews. Previews are framebuffer stills, not a
live view of native animation playback.*

One app owns the bar at a time. Do not run a second manual copy alongside
Barkeep. Closing the browser does not stop the apps.

Barkeep controls host processes and configuration. Keep its default
`127.0.0.1` bind and use an SSH tunnel for remote access. Direct LAN exposure
needs explicit authentication and TLS setup; do not port-forward it.
[Security model](SECURITY.md) · [Remote access](deploy/README.md#run-it-as-a-service)

## Running it on a server

For unattended use, install on a supported Linux computer that stays on and
can reach the bar. The [quickstart's Linux path](docs/quickstart.md#on-linux)
uses `./deploy/install.sh` to verify speech dependencies and optionally start
Barkeep at boot. Run the installer as your normal user, **not with sudo**.

Already installed? Follow [updating a normal installation](deploy/README.md#updating-a-normal-installation).
Stop Barkeep before updating; the installer preserves private configuration.
Fork maintainers have a separate [deploy workflow](deploy/README.md#deploying-your-own-changes).

## Build your own app

After the offline quickstart, choose a new lowercase app name:

<!-- quickstart:scaffold -->
```bash
uv run scripts/new_app.py yourapp
uv run apps/yourapp.py --dry-run
```

The first command creates `apps/yourapp.py` and registers it in `apps.toml`.
The second validates its draw request without a bar. Existing names are
refused, not overwritten. Restart Barkeep after adding an app so it reloads
the registry.

Follow the [app-building walkthrough](docs/agent-cookbook.md), then use the
[maintainer map](docs/maintaining.md) to find the right code and tests.
Coding agents should start with [AGENTS.md](AGENTS.md), which routes them to
the shared device and visual-validation skills.

### Visual validation with busybar-viz

The offline visualizer audits frames, simulates LED spacing, and checks
accepted pixel baselines. It is separate from Barkeep and does not need a
device. Start with the [visualizer guide](docs/busybar-viz.md); previews do
not prove physical-panel contrast.

## Documentation and agent guidance

- [Quickstart and troubleshooting](docs/quickstart.md) — first run through
  choosing a live app.
- [Documentation index](docs/README.md) — app controls, providers, and guides.
- [Contributing](CONTRIBUTING.md) — development setup and the CI checks.
- [Configuration reference](.env.example) — edit your own `.env`, never this
  tracked example with personal values.
- [Known issues](docs/known-issues.md) — firmware limitations and workarounds.
- [Official BUSY developer docs](https://docs.busy.app/bar/dev) — device API
  and connection setup.

Release notes are in [CHANGELOG.md](CHANGELOG.md). `main` may include
unreleased work; use a release tag when you need a fixed version.

## Architecture

Apps use shared connection helpers and separate source, state, rendering,
device, and speech modules. Barkeep manages processes; the visualizer stays
offline. See the [architecture](docs/architecture.md) and
[maintainer map](docs/maintaining.md) for ownership and dependencies.

## Licence

**GPL-2.0-or-later.** The copyleft requirement comes from
`busybar_dev/anim.py`, a port of upstream GPL-2.0-or-later firmware tooling.
Third-party attribution, dependency licences, and data-source terms are in
[NOTICE.md](NOTICE.md).

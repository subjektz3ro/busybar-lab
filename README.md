# BUSY Bar Lab

Put a live weather scene on your [BUSY Bar](https://busy.app) with **Skystrip**,
or watch NASA's spacecraft links with **DSN**. **Barkeep**, the included web
dashboard, runs the apps and lets you switch between them.

The apps run on a Mac or Linux computer connected to your bar, not inside the
bar itself. Keep that computer awake and connected for live updates. You do
not need to write code, flash firmware, or create a vendor-cloud account.

This is an independent community project, not an official BUSY/Flipper product.

## Quickstart

This gets **Barkeep and Skystrip running on your bar**. Use a Mac or supported
64-bit Linux computer, a BUSY Bar, a USB data cable, and internet access.
Windows is not currently supported. For a first setup, use the same computer
for the commands and the web browser.

### 1. Connect your bar

Connect the bar to that computer with a USB **data** cable, not a charge-only
cable. End any active BUSY/CUSTOM focus session so an app can use the display.
No device password is needed over USB.

Prefer Wi-Fi, or using a Raspberry Pi/server?
The [walkthrough](docs/quickstart.md#1-connect-your-bar) covers those options.

### 2. Download and install

Open **Terminal**. You need [Git](https://git-scm.com/downloads) to download the
project and [uv](https://docs.astral.sh/uv/getting-started/installation/) to
install its software. New to these tools? Follow the
[one-time tool setup](docs/quickstart.md#2-install-the-tools-and-download-the-apps),
then return here.

Run these lines one at a time, pressing Enter after each:

<!-- quickstart:setup -->
```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
uv sync --locked
```

Wait for the installation to finish and the prompt to return. Keep this
terminal open; run the remaining commands from this same folder.
You do not need to install Python separately.

### 3. Set your weather location

Create your private settings file; this leaves any existing settings intact:

<!-- quickstart:config -->
```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

Open it in the terminal's text editor:

<!-- quickstart:editor -->
```bash
nano .env
```

Use the arrow keys to find and fill in **three lines**: `SKYSTRIP_LAT=`
(latitude), `SKYSTRIP_LON=` (longitude), and `SKYSTRIP_TZ=` (timezone).
Use decimal coordinates for the place you want weather for—a nearby town
centre is fine—and a timezone such as `Europe/London`, not an abbreviation.
[Help finding and entering your location](docs/quickstart.md#3-set-your-weather-location).

Leave `BUSYBAR_HOST=` and `BUSYBAR_TOKEN=` blank for USB. To use Celsius,
change `SKYSTRIP_UNITS=f` to `SKYSTRIP_UNITS=c`. Everything else can stay
at its default.

Save with **Ctrl+O**, then **Enter**; exit with **Ctrl+X**. On a Mac, use
Control, not Command. Edit `.env`, never the public `.env.example`;
do not post your settings in an issue.

Check the saved settings before continuing:

<!-- quickstart:config-check -->
```bash
uv run python -m deploy.check_setup --config-only
```

Fix any **FAIL** it reports, and resolve missing-location/timezone warnings
before using Skystrip. Private values are not printed.

### 4. Start Barkeep

**On macOS**, run:

<!-- quickstart:start -->
```bash
uv run -m barkeep
```

Leave this terminal open. It may be quiet while Barkeep is waiting; continue
to the next step. Ctrl+C stops Barkeep and its apps.

**On Linux**, run the installer as your normal user, **not with sudo**:

<!-- quickstart:install -->
```bash
./deploy/install.sh
```

It keeps your settings and downloads and verifies the speech files. When
asked whether Barkeep should run at boot, choose `y` to run it in the
background. Wait for the installer to finish. Do not also start a manual copy.
If you choose `n`, run `uv run -m barkeep` afterward and leave that terminal
open. [Linux setup details](docs/quickstart.md#on-linux).

The installer checks the bar connection and, for a service install, the web
page. Follow any warning or failed check before expecting a scene on the bar.

### 5. Open Barkeep and start Skystrip

On the **same computer**, open **[http://127.0.0.1:8080](http://127.0.0.1:8080)**
in your browser. You should see Barkeep with **STANDBY** selected.

Before selecting an app: on **firmware 1.2.3**, Skystrip and DSN change Auto
brightness to **fixed 35%** to mitigate washed-out scenes. Existing manual
levels stay unchanged. This disables ambient-light adjustment; see
[known issues and opt-out](docs/known-issues.md).

Read the provider credits and use limits in Barkeep, then click **skystrip**.
It fetches weather for your configured location and prepares the scene.
**Success is the weather scene appearing on your physical bar**, not just a
“running” label in the browser. The first scene waits for fresh weather;
use **LOGS** under Skystrip to see its progress.

You're set. **CONFIG** → **Save & restart** changes app settings later.
Select **dsn** to try the space display, or **STANDBY** to stop the app.
Closing the browser does not stop Barkeep; keep the app computer awake.

Need a hand? [Barkeep won't open](docs/troubleshooting.md#barkeep-wont-open) ·
[Barkeep opens but cannot reach the bar](docs/troubleshooting.md#barkeep-opens-but-cannot-reach-the-bar) ·
[Full walkthrough](docs/quickstart.md)

## Included apps

| App | What it shows |
|---|---|
| [Skystrip](apps/skystrip.md) | Daylight and weather for your location, with a spoken report |
| [DSN](apps/dsn.md) | Live NASA Deep Space Network antennas, spacecraft links and signal travel time; no location setup needed |

| Skystrip | DSN |
|---|---|
| ![Skystrip day cycle](docs/media/skystrip-day.gif) | ![DSN network](docs/media/dsn-network.gif) |

Illustrative examples, not live data. Skystrip's day cycle uses a public
Greenwich fixture and accelerated time. [More views and animations](docs/gallery.md).

Skystrip uses Open-Meteo model weather for ordinary land locations worldwide;
NWS observations and alerts are available within NWS coverage. Seasonal art
is northern-temperate. See [geographic support](apps/skystrip.md#geographic-support)
and [provider terms](apps/skystrip.md#provider-terms-and-commercial-use).
This is not a life-safety warning system.

## Try without a bar

Optional: after the download/install step, these commands let you explore
without connecting hardware. They are not needed to run Skystrip.

<!-- quickstart:offline -->
```bash
uv run apps/hello.py --dry-run
```

Expect `Dry run payload:` containing `HELLO`. To save a sample image:

<!-- quickstart:preview -->
```bash
mkdir -p scratch
uv run apps/skystrip.py --preview scratch/sky.png --at 12:00
```

Open `scratch/sky.png` in an image viewer. It uses built-in weather inputs,
not live weather. With no location configured, the unset-coordinates warning
is expected. These app commands make no device or provider requests.

## Build your own app

Once you have installed the project, choose a new lowercase app name:

<!-- quickstart:scaffold -->
```bash
uv run scripts/new_app.py yourapp
uv run apps/yourapp.py --dry-run
```

This creates `apps/yourapp.py`, registers it with Barkeep, and checks its draw
request without a bar. Existing names are not overwritten. Restart Barkeep to
load a newly registered app.

Follow the [app-building walkthrough](docs/agent-cookbook.md).
The [visualizer guide](docs/busybar-viz.md) covers offline visual checks;
previews cannot prove physical-panel contrast. Contributors and coding agents
can start with [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## More help and documentation

- [Quickstart walkthrough](docs/quickstart.md) and [troubleshooting](docs/troubleshooting.md).
- [Platform requirements](docs/dependencies.md): supported Linux is 64-bit
  `x86_64`/`aarch64`, glibc 2.28+, Python 3.11–3.13. Allow at least 2 GiB
  RAM and 1 GB free disk for the Linux speech-enabled installation.
  macOS supports direct running but has no macOS CI gate or systemd installer.
- [Run at boot, remote access and updates](deploy/README.md). Barkeep controls
  host processes: keep its local-only default, use an SSH tunnel remotely,
  and do not port-forward it. [Security model](SECURITY.md).
- [Documentation index](docs/README.md), [architecture](docs/architecture.md)
  and [maintainer map](docs/maintaining.md).
- [Release notes](CHANGELOG.md). `main` may contain unreleased work; use a
  release tag for a fixed version.

Use the cloned source folder to run the apps: a standalone Python wheel does
not include the app scripts, assets, registry and deployment files.

## Licence

**GPL-2.0-or-later.** The copyleft requirement comes from
`busybar_dev/anim.py`, a port of upstream GPL-2.0-or-later firmware tooling.
Third-party attribution and data-source terms are in [NOTICE.md](NOTICE.md).

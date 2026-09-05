# Quickstart: from a clone to your first app

This guide starts with an offline check, then connects a BUSY Bar and runs
an example app through Barkeep, the included web UI. You do not need to write
Python or flash firmware. Run terminal commands from the `busybar-lab` folder
unless a step says otherwise.

Already completed the README's offline demo? Skip to
[connecting your bar](#2-connect-your-bar).

## 1. Install and try it without a bar

Use macOS or [supported 64-bit Linux](dependencies.md#support-and-resource-matrix).
Windows is not currently supported. Install [Git](https://git-scm.com/downloads)
and [uv](https://docs.astral.sh/uv/getting-started/installation/), then open a
new terminal and check that `git --version` and `uv --version` work. You do not
need to install Python separately: uv manages the compatible interpreter.

Setup needs internet access. Linux's full speech-enabled setup later in this
guide also needs `bash`, `curl`, at least 2 GiB RAM and 1 GB free disk; systemd
and working `sudo` access are needed if you want the run-at-boot service.

<!-- quickstart:setup -->
```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
uv sync --locked
uv run apps/hello.py --dry-run
```

**Success:** the terminal prints `Dry run payload:` containing `HELLO`, then
returns to the prompt. No bar, configuration, voice files or live data are
needed for this check.

Optionally make a sample scene:

<!-- quickstart:preview -->
```bash
mkdir -p scratch
uv run apps/skystrip.py --preview scratch/sky.png --at 12:00
```

**Success:** the terminal prints `saved scratch/sky.png`. Open that file in an
image viewer. It is a sample using built-in inputs, not your current weather.
The warning about unset coordinates is expected: this unconfigured demo uses
0° latitude, 0° longitude. You will set your own location in the next step.
It makes no device or weather-provider requests. Previews cannot prove how
contrast looks on the physical LEDs.

You can stop here if you only want to explore the code or
[build an app](../README.md#build-your-own-app).

## 2. Connect your bar

The computer running the apps must be able to reach the bar. If you are using
a remote Linux host, connect the bar to **that host**, not to the laptop from
which you are opening the web UI.

**USB is the simplest first connection.** Connect the bar with a USB data
cable. The connection helper tries the device's USB address, `10.0.4.20`,
first; USB requires no API token. Leave the connection settings blank in the
next step.

**For Wi-Fi/LAN instead:** put the bar and app host on a network where they
can reach each other. Enable HTTP API access in the bar's own local web UI,
then note its LAN address and access PIN for the next step. See the vendor's
[HTTP API setup](https://docs.busy.app/bar/dev/http-api). This workflow does
not use a vendor-cloud account or cloud API token.

### Create your private configuration

Create `.env` without replacing an existing file:

<!-- quickstart:config -->
```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

Open **`.env`**, not `.env.example`, in a text editor. Keep the format
`KEY=value`, one setting per line. Change only what you need:

| What you want | Settings to edit |
|---|---|
| USB connection | Leave `BUSYBAR_HOST` and `BUSYBAR_TOKEN` blank |
| Wi-Fi/LAN connection | `BUSYBAR_HOST`: the bar's LAN address; `BUSYBAR_TOKEN`: the PIN from its web UI |
| DSN only | No location settings are required |
| Skystrip | Set both `SKYSTRIP_LAT` and `SKYSTRIP_LON` to your decimal-degree coordinates, plus `SKYSTRIP_TZ` to the matching IANA timezone, such as `Europe/London` |
| Celsius instead of Fahrenheit | Set `SKYSTRIP_UNITS=c` (the default is `f`) |

Save the file before continuing. Skystrip does **not** detect your location or
infer the timezone from coordinates. Do not start it with blank coordinates
and expect local weather. Other defaults can stay as they are; optional
lightning input is off unless you configure an authorized source.

`.env` and Barkeep's per-app `config/*.env` files are gitignored. Never put
personal coordinates, addresses, PINs or tokens in tracked files or public
issue reports. The two tokens have different jobs: `BUSYBAR_TOKEN` connects
to the **device**; `BARKEEP_TOKEN` protects the **Barkeep web UI**.

## 3. Check the physical connection

End any active BUSY/CUSTOM focus session on the bar first. If Barkeep is already
running an app, select **STANDBY** before this test; do not run competing copies.

```bash
uv run apps/hello.py
```

**Success:** `HELLO` appears on the front display, and the terminal reports
screenshot paths under `scratch/`. The command exits but leaves the text up.
When you have seen it, clear the test draw:

```bash
uv run apps/hello.py --clear
```

If nothing appears, use [troubleshooting](#troubleshooting) before starting
the full apps. You do not need to set up speech for this test.

## 4. Start Barkeep

Choose the instructions for the computer that will run the apps. Run only
one Barkeep instance for a bar.

### On Linux

From the checkout, run the installer as your normal user, **not with sudo**:

```bash
./deploy/install.sh
```

It preserves the `.env` you just created, so it skips its location interview.
It installs locked dependencies, downloads and verifies roughly 340 MiB of
Kokoro voice/model files, and tests speech synthesis. Let those checks finish;
a plain `uv sync` does not install the voice files.

When offered **Install barkeep so the apps run at boot?**, choose `y` for an
always-on setup. The installer starts the service; do not also launch a manual
copy. To check it:

```bash
systemctl status "barkeep@$USER"
```

**Success:** the service is `active (running)`. Press `q` if the status viewer
opens a pager.

If you choose `n`, or the host has no working systemd service manager, start
Barkeep manually after the installer succeeds:

```bash
uv run -m barkeep
```

Leave that terminal running. Press Ctrl+C to stop Barkeep and its apps.

### On macOS

The earlier `uv sync --locked` is enough for direct development. Speech uses
macOS `say`; there is no Linux voice-bank download or systemd service step.

```bash
uv run -m barkeep
```

Leave the terminal running. Press Ctrl+C to stop Barkeep and its apps.

## 5. Open the UI and choose an app

**On the same computer:** open [http://127.0.0.1:8080](http://127.0.0.1:8080).
A fresh installation opens in **STANDBY**; that is normal, not an error.

**On a remote host:** on your own computer, open another terminal and run:

```bash
ssh -N -L 8080:127.0.0.1:8080 your-user@server.example
```

Replace `your-user@server.example` with your SSH login and host. Leave the
tunnel running, then open the same `http://127.0.0.1:8080` URL in your local
browser. A quiet terminal is expected: `-N` creates the tunnel without a shell.
Keep the default loopback bind; do not open a public port just to reach the UI.
For intentional LAN sharing, follow the [authenticated TLS setup](../deploy/README.md#who-can-reach-the-control-plane).

**Before starting an app on firmware 1.2.3:** Skystrip and DSN change Auto
brightness to fixed 35% to mitigate dark-scene washout. Existing manual levels
are preserved. This turns off ambient-light adjustment; [known issues](known-issues.md)
explains the fallback setting and how to opt out and restore Auto.

1. Click **dsn** for a first live app with no location setup. It fetches NASA
   data and takes the bar's display.
2. To try **skystrip**, confirm you saved your coordinates and timezone, read
   the UI's provider credits and use limits, then click its card. Selecting it
   enables weather-provider polling. It waits for fresh weather before drawing.
3. Use **LOGS** beneath the selected app to check progress. Use **CONFIG** and
   **Save & restart** to apply later per-app edits. Editing the shared `.env`
   instead requires restarting **Barkeep itself**, not just its selected app.

**Success:** the selected app is running and its scene appears on the bar.
The UI's framebuffer preview is a still, not a video of the native animation.
Select **STANDBY** to stop the foreground app and return the bar to its built-in
apps. Closing the browser does not stop Barkeep. Keep the host awake and
connected for live updates; a service restores the saved selection on restart.

## Troubleshooting

| Symptom | First thing to check |
|---|---|
| `uv` or `git` is not found | Finish the tool's installation, open a new terminal, and rerun its `--version` command. |
| A script or `pyproject.toml` cannot be found | Run from the cloned `busybar-lab` folder, not its parent or `apps/`. |
| Bar unreachable or request timeout | Check the data cable and that the bar is connected to the app host. For LAN, check HTTP API access, address and PIN. USB uses `10.0.4.20`; prefer it over ambiguous `busybar.local`. |
| HTTP 409 / a BUSY session owns the display | End the device's focus session, then retry. Raising app priority is not the solution. |
| HELLO disappears or two scenes alternate | Select STANDBY and stop any second manual app instance before testing again. |
| Wrong location, time or weather | Check both coordinates and `SKYSTRIP_TZ`. A preview is a sample, not live data. Restart Barkeep after changing shared `.env`. |
| Linux speech/model verification fails | Rerun `./deploy/install.sh` and read its first failing check; verify disk space and outbound internet. Do not skip verification. |
| Web UI will not open | Check the Barkeep terminal or service status. On a remote host, keep the SSH tunnel open. A bar's own web UI is not Barkeep. |
| Port 8080 is already in use | Do not launch a manual Barkeep alongside its service. If another application uses the port, configure `BARKEEP_PORT` and adjust the URL/tunnel. |
| UI asks for a token | Use your configured `BARKEEP_TOKEN`, not the device PIN. See [login help](../deploy/README.md#logging-in-from-another-machine). |
| Night scene looks gray or washed out | Check [firmware 1.2.3 brightness mitigation](known-issues.md). |

Still stuck? Check the selected app's LOGS tab or, for a service,
`journalctl -u "barkeep@$USER" -n 50 --no-pager`. When
[opening an issue](https://github.com/subjektz3ro/busybar-lab/issues), include
your OS/CPU, firmware version, command and error. Redact private locations,
hostnames, addresses and credentials; do not attach `.env` or unreviewed logs.

## Next steps

- [App controls and configuration](../apps/README.md)
- [Build your own app](../README.md#build-your-own-app)
- [Keep a normal installation updated](../deploy/README.md#updating-a-normal-installation)
- [Contribute and run the test suite](../CONTRIBUTING.md)

# Get Skystrip running on your BUSY Bar

This walkthrough takes you from a connected bar to a live weather scene,
controlled from Barkeep in your browser. No coding or firmware flashing is
required. The [README](../README.md#quickstart) has the shorter version of
the same steps.

## Before you start

Have your BUSY Bar, a USB **data** cable, internet access and a Mac or
[supported 64-bit Linux computer](dependencies.md#support-and-resource-matrix).
Windows is not currently supported. Barkeep and the apps run on that computer,
so it needs to stay awake and connected for live updates.

Start with the bar, commands and browser on **one computer**. You can move to
an always-on Linux computer later. If you are already using a Pi or server,
run the commands there, connect the bar to it, and use the
[remote browser instructions](#using-a-browser-on-another-computer) at step 5.

## 1. Connect your bar

Plug the bar into the computer with a USB data cable. A cable that only
charges the bar will not work. End any active BUSY/CUSTOM focus session so
Skystrip can use the display. USB does not require a device password.

For a quick connection check, open
[http://10.0.4.20/](http://10.0.4.20/) in a browser on the connected computer.
You should see the bar's own settings page—this is **not Barkeep**, which you
will install next. If it does not open, try another data cable or USB port;
[connection help](troubleshooting.md#barkeep-opens-but-cannot-reach-the-bar)
has the next checks.

<details>
<summary>Use Wi-Fi instead of USB</summary>

First connect over USB and open the bar's settings at
[http://10.0.4.20/](http://10.0.4.20/). On **Network**, enable **HTTP API access**
and set its access password/PIN. Connect the bar to your Wi-Fi network and
find its IP address under **Settings → Wi-Fi → your network → View IP address**
on the bar. See the vendor's [connection instructions](https://docs.busy.app/bar/dev/http-api).

The app computer must be able to reach that address. Guest networks may block
devices from talking to one another. In step 3, put the address in
`BUSYBAR_HOST` and the password/PIN in `BUSYBAR_TOKEN`. These are the bar's
local-network credentials, not a vendor-cloud token.

</details>

## 2. Install the tools and download the apps

Open **Terminal** on the computer that will run the apps. On a Mac, press
Command+Space, type “Terminal” and press Enter. On Linux, open your system's
Terminal application. Paste commands one line at a time and press Enter after
each. If a command fails, stop at that step rather than continuing.

Two small tools handle setup: **Git** downloads the project, and **uv**
installs Python and its packages for you. Check whether you already have them:

```bash
git --version
uv --version
```

If both print version numbers, skip the tool installation below.

<details>
<summary>Install missing tools</summary>

**Git on macOS:** run `xcode-select --install` and follow Apple's installer,
or choose another method from [Git's macOS instructions](https://git-scm.com/install/mac).
If the tools are already installed, you do not need to reinstall them.

**Git on Ubuntu, Debian or Raspberry Pi OS:** these commands install Git,
the download tool and the text editor used below:

```bash
sudo apt update
sudo apt install git curl nano
```

Enter your computer login password if asked; the terminal does not show
characters while you type it. Other Linux distributions use their own package
manager; see [Git's Linux instructions](https://git-scm.com/install/linux).

**uv on macOS or Linux:** the following is the
[official uv installer](https://docs.astral.sh/uv/getting-started/installation/).
It downloads and runs Astral's installation script; that page also offers
package-manager installation options.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Close and reopen Terminal after installation, then rerun `git --version`
and `uv --version`. You do not need to install Python separately.

</details>

Now download Barkeep and the apps:

<!-- quickstart:setup -->
```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
uv sync --locked
```

The first line creates a `busybar-lab` folder. The second moves your terminal
into it. The third installs the required software. **Success:** installation
finishes without an error and the terminal prompt returns. Keep this window
open and run the remaining commands from this folder.

Already cloned the project? Open its existing folder rather than downloading
another copy. Use the [update instructions](../deploy/README.md#updating-a-normal-installation)
for an installation that is already running.

## 3. Set your weather location

Create a private settings file. These commands do not overwrite an existing one:

<!-- quickstart:config -->
```bash
test -f .env || cp .env.example .env
chmod 600 .env
```

Open that file:

<!-- quickstart:editor -->
```bash
nano .env
```

Nano is a text editor inside the terminal. Use the arrow keys to find the
following lines and type your values after the `=`. Leave the setting names
unchanged and put each setting on its own line.

| Setting | What to enter |
|---|---|
| `SKYSTRIP_LAT=` | Latitude in decimal degrees |
| `SKYSTRIP_LON=` | Longitude in decimal degrees |
| `SKYSTRIP_TZ=` | The timezone for that location, such as `Europe/London`, `America/New_York` or `Australia/Sydney` |

Look up the place you want weather for in a map or coordinate finder. A nearby
town centre is fine; you do not need to supply your home address. Use decimal
numbers, not degrees/minutes/seconds or an address. North/east are positive;
south/west need a minus sign. Keep latitude and longitude in the correct order.
The timezone is a separate setting: do not use an abbreviation such as `EST`
or assume it will be inferred from the coordinates.

For **USB**, leave `BUSYBAR_HOST=` and `BUSYBAR_TOKEN=` blank.
For **Wi-Fi**, fill in the bar's IP address and access password/PIN from step 1.
For Celsius, change `SKYSTRIP_UNITS=f` to `SKYSTRIP_UNITS=c`.
Leave the other settings at their defaults for now.

Press **Ctrl+O**, then **Enter** to save; press **Ctrl+X** to exit. On a Mac,
these use Control, not Command. **Success:** you are back at the terminal
prompt with your three location settings saved.

If `nano` is unavailable, open `.env` in any plain-text editor. On macOS,
`open -e .env` opens it in TextEdit. Do not rename it to `.env.txt`.

Keep these settings private. `.env` and Barkeep's per-app settings are excluded
from Git, but do not attach them to public issues or screenshots. Edit
`.env`, not `.env.example`. Your configured location is sent to weather
providers when Skystrip fetches local weather; it is not published to GitHub.

Now check your saved settings:

<!-- quickstart:config-check -->
```bash
uv run python -m deploy.check_setup --config-only
```

**Success:** configuration checks pass, with your location set. Fix any
**FAIL** before continuing; the message names the setting, not its private
value. A warning about missing location is expected only if you intend to
run DSN without configuring Skystrip.

## 4. Start Barkeep

Follow **only the section for your operating system**. Run one Barkeep instance
per bar; do not launch an app separately while Barkeep is running it.

### On macOS

<!-- quickstart:start -->
```bash
uv run -m barkeep
```

Leave this terminal open. It may show little output while waiting; open the
browser in step 5 to check that Barkeep is ready. Ctrl+C stops it and its apps.
Speech uses the Mac's built-in voice system.

### On Linux

The installer sets up speech and can keep Barkeep running after you close the
terminal or reboot. Run it as your normal user, **not with sudo**:

<!-- quickstart:install -->
```bash
./deploy/install.sh
```

Choose `y` when asked **Install barkeep so the apps run at boot?** for an
always-on setup. It may ask for your computer login password when installing
the service. Because you created `.env` in step 3, it keeps your settings and
skips its configuration questions.

Allow at least 2 GiB RAM and 1 GB free disk. The installer downloads roughly
340 MiB of speech files and verifies speech synthesis before starting the
service. Wait for it to finish; do not skip a failed check.

**Success:** the service is started and you can continue to step 5.
Do not also run `uv run -m barkeep` while the service is running.

The installer checks device connectivity and waits for Barkeep's web page to
respond. If it finishes host setup but says the bar connection still needs
attention, follow that message before selecting an app. You can rerun the
[setup checker](troubleshooting.md#run-the-setup-checks-again) at any time.

If you choose `n`, or the computer has no working systemd service manager,
start it manually after the installer succeeds with `uv run -m barkeep`.
Leave that terminal open; Ctrl+C stops it. See
[installer/service help](troubleshooting.md#linux-installation-or-service-problems)
if a check fails.

## 5. Open Barkeep and start Skystrip

On the **same computer**, open **[http://127.0.0.1:8080](http://127.0.0.1:8080)**
in your browser's address bar. Use `http`, not `https`, for this default setup.
**Success:** Barkeep opens with **STANDBY** selected. That is normal: no app
has started yet. [Page won't open?](troubleshooting.md#barkeep-wont-open)

Before selecting an app on **firmware 1.2.3**, note that Skystrip and DSN change
Auto brightness to **fixed 35%** to mitigate washed-out scenes. Existing manual
levels are preserved. This disables ambient-light adjustment; see
[known issues](known-issues.md) for the setting, opt-out and restoring Auto.

Read the provider credits and use limits below the previews, then click the
**skystrip** card. It starts weather requests using your saved location.
The first scene waits for fresh weather; **LOGS** under Skystrip shows progress.
**Success is the weather scene appearing on the physical bar.** A “running”
status alone does not prove it can draw to the device.
[App runs but nothing appears?](troubleshooting.md#barkeep-opens-but-cannot-reach-the-bar)

Once it is running:

- **CONFIG** → **Save & restart** changes Skystrip's settings.
- **dsn** switches to NASA's space display; it needs no location setup.
- **STANDBY** stops the foreground app and lets the bar return to its built-in apps.

Closing the browser does not stop Barkeep. Keep the app computer awake and
connected. A Linux service remembers the selected app across restarts.
If you edit the shared `.env` later, restart **Barkeep itself**; restarting
only Skystrip does not reload that file.

### Using a browser on another computer

If Barkeep is running on a Pi/server, `127.0.0.1` in your laptop's browser
means the laptop, not the server. Use an SSH tunnel to reach Barkeep safely.

On your **laptop**, open another terminal and run:

<!-- quickstart:tunnel -->
```bash
ssh -N -L 8080:127.0.0.1:8080 your-user@server.example
```

Replace `your-user@server.example` with the login and address you use to SSH
into the server. You need working SSH access first. Leave this terminal open;
a quiet window is normal. Now open
[http://127.0.0.1:8080](http://127.0.0.1:8080) on your laptop and follow step 5.

Keep Barkeep's local-only default. Do not open a public router port. If you
specifically need direct LAN access, use the
[authenticated TLS setup](../deploy/README.md#who-can-reach-the-control-plane).

## After your first app

- [Skystrip controls](../apps/skystrip.md) and [other apps](../apps/README.md).
- [Troubleshooting](troubleshooting.md) if a connection, setting or service is not working.
- [Build your own app](../README.md#build-your-own-app) when you want to customize the bar.
- [Optional offline demos](../README.md#try-without-a-bar) for exploring without hardware.
- [Updates and running at boot](../deploy/README.md).

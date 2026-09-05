# Setup help

Start with the [quickstart](quickstart.md). If a step fails, use the matching
section below; you do not need to work through every check.

## Run the setup checks again

On the **computer running Barkeep**, open another terminal in the
`busybar-lab` folder and run:

<!-- quickstart:diagnostic -->
```bash
uv run python -m deploy.check_setup
```

It checks saved settings, the bar's API connection, and Barkeep's web page
separately. Each network check has a ten-second deadline. Read the next step
beside any **FAIL** or **WARN**. Missing weather coordinates do not block a
DSN-only setup, but must be filled in before selecting Skystrip.

The checker does not change settings, draw, change brightness, play audio,
start apps or fetch weather. It does not print your settings or raw request
errors. A passing local check cannot prove access from another computer or
that an app is visible on the physical bar.

The installer runs these checks automatically: invalid saved settings stop
setup before speech-file downloads or service changes. An unavailable bar
does not prevent host installation, but is reported as unfinished connection
setup. A service whose web page does not become ready is not reported as a
successful setup.

## Barkeep won't open

1. **Check where Barkeep is running.** `http://127.0.0.1:8080` means the
   computer where your browser is open. If Barkeep is on a Pi/server, use the
   [SSH tunnel](quickstart.md#using-a-browser-on-another-computer), not the
   server's address with the default local-only settings.
2. **Check that it is still running.** For a manual launch, leave the terminal
   running `uv run -m barkeep` open. For a Linux service, see the service
   checks below. Do not start a second copy alongside the service.
3. **Check the address.** The default is **http**, not https, and includes
   **:8080**. The bar's own `http://10.0.4.20/` settings page is not Barkeep.
   The setup checker reports your configured port and HTTP/HTTPS choice.
4. **If it works on the Barkeep computer but not your laptop**, keep the SSH
   tunnel open. A quiet tunnel terminal is normal. An SSH login/connection
   error must be resolved before that tunnel can carry browser traffic.

If port 8080 is already used on your laptop, use a different **local** port:

```bash
ssh -N -L 8081:127.0.0.1:8080 your-user@server.example
```

Replace the example login/host and then open `http://127.0.0.1:8081` locally.
The last `8080` is Barkeep's port on the server; change it too only if you
configured a different `BARKEEP_PORT`. A deliberate non-loopback bind needs a
tunnel destination matching that bind; see the [remote-access reference](../deploy/README.md#who-can-reach-the-control-plane).

If another application uses the port on the **Barkeep computer**, choose a
free `BARKEEP_PORT` in `.env`, restart Barkeep, and update the URL/tunnel.
Do not disable authentication or open a router port as a shortcut.

## Barkeep opens but cannot reach the bar

The **LINK** indicator means your browser can reach Barkeep. It does not
confirm a device connection. “bar unreachable — previews paused” refers to
the separate connection from Barkeep's computer to the physical bar.

For **USB**:

1. Connect the bar to the computer actually running Barkeep. A bar plugged
   into your laptop is not a USB device on a remote Pi/server.
2. Use a data cable. Power alone does not prove data connectivity; try a
   different cable and USB port.
3. Leave `BUSYBAR_HOST` and `BUSYBAR_TOKEN` blank in that installation's
   `.env`. Restart Barkeep after editing it.
4. On that same computer, try `http://10.0.4.20/`. If it opens but the app
   still cannot connect, check the app's **LOGS**, confirm you edited the
   correct installation, and rerun the setup checker.

For **Wi-Fi**:

1. Enable HTTP API access in the bar's local web UI; it is disabled over
   Wi-Fi by default. [Vendor setup instructions](https://docs.busy.app/bar/dev/http-api).
2. Set `BUSYBAR_HOST` to the bar's current LAN address and `BUSYBAR_TOKEN`
   to the access password/PIN set in that UI. Do not use `BARKEEP_TOKEN` or a
   cloud token here. Restart Barkeep after editing `.env`.
3. Check that the bar and Barkeep computer can reach each other. Guest Wi-Fi,
   client isolation, a VPN or firewall can prevent this; use USB to narrow
   down whether the problem is the LAN connection. Do not turn protections
   off indiscriminately.

**An API access-denied/403 message** means a request reached a device that
refused access. Recheck HTTP API access and the device password/PIN. A timeout
instead points to an unresolved connectivity problem; it does not prove a bad
password.

### Connected, but the app is not appearing

End any BUSY/CUSTOM focus session on the bar. **HTTP 409** means the session
owns the display, not that the network is broken. Raising app priority will
not fix it. Also stop any second app instance running outside Barkeep.

If Skystrip is waiting for fresh weather, inspect its **LOGS** and the
computer's internet connection. Device reachability does not test weather
provider availability. Optional live lightning is off by default; a message
saying it is disabled is not a startup error.

<details>
<summary>Optional HELLO display test</summary>

Select **STANDBY** first and end the bar's focus session. Run these from a
second terminal in the project folder:

<!-- quickstart:hello -->
```bash
uv run apps/hello.py --dry-run
uv run apps/hello.py
```

The first command checks the request without a bar. The second draws **HELLO**
and saves screenshots under `scratch/`. Once you have seen it, clear the test:

<!-- quickstart:clear -->
```bash
uv run apps/hello.py --clear
```

Then select Skystrip again. These display commands change the bar; the setup
checker above does not.

</details>

## Location, clock or weather looks wrong

Check both `SKYSTRIP_LAT` and `SKYSTRIP_LON`, their order/signs, and
`SKYSTRIP_TZ`. The timezone is not inferred from coordinates. A preview PNG
uses sample weather, not a live observation.

Changes made through **CONFIG** → **Save & restart** take effect for that app.
If you edited shared `.env`, restart **Barkeep itself**. Per-app settings saved
in the web UI override shared settings; check CONFIG if an old value persists.
Open-Meteo model weather and NWS observations need not match exactly; see
[Skystrip's data sources and coverage](../apps/skystrip.md).

For a gray or washed-out night scene, read the
[firmware 1.2.3 brightness issue](known-issues.md).

## Linux installation or service problems

Run these on the Linux computer running Barkeep:

```bash
systemctl status "barkeep@$USER" --no-pager
journalctl -u "barkeep@$USER" -n 50 --no-pager
```

The expected service status is **active (running)**. This means the process
is running, not necessarily that it can reach the bar; use the setup checker
for the web and device connections. If you deliberately chose manual setup,
not having this service is normal.

- **Speech download/verification failed:** check free disk space and internet
  access, then rerun `./deploy/install.sh`. It reuses verified downloads and
  preserves settings. Do not bypass failed model or synthesis checks.
- **Missing tools or sudo access:** follow the installer's named prerequisite.
  Run it as your normal account, not as root or with `sudo` in front of it.
- **Saved settings rejected:** run `uv run python -m deploy.check_setup --config-only`,
  fix the named keys, then rerun the installer. Values are not printed.
- **Service starts but web check fails:** use the journal above. Check
  `BARKEEP_PORT`, `BARKEEP_BIND` and any TLS settings; do not start a competing
  manual copy just because the page is unavailable.

## Token prompt or HTTPS warning

Barkeep's browser login asks for **BARKEEP_TOKEN**. The bar's own web UI/API
uses the device password/PIN stored in **BUSYBAR_TOKEN**. They are different.

Default local setup uses plain HTTP and no Barkeep token. If you enabled
HTTPS, use the HTTPS URL. A generated self-signed certificate produces a
browser trust warning; see [HTTPS and login help](../deploy/README.md#logging-in-from-another-machine)
before accepting or replacing a certificate. The diagnostic checks the local
page; it cannot configure another computer's browser trust for you.

## Still stuck?

[Open an issue](https://github.com/subjektz3ro/busybar-lab/issues) with the step
that failed, your OS, device firmware, USB/Wi-Fi connection choice and the
setup check's result. Review anything you attach: do not include `.env`,
unreviewed logs, personal coordinates, private hostnames, passwords or tokens.

Return to the [quickstart](quickstart.md).

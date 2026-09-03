# Running this on a server

Nothing here is Raspberry Pi specific. The supported service target is 64-bit
glibc 2.28+ Linux on `x86_64` or `aarch64`, using CPython 3.11–3.13; a Pi 4/5
with 64-bit Raspberry Pi OS and at least 2 GiB RAM, NUC, 64-bit laptop, or VM
can all fit. Containers are an advanced manual-run path. CI exercises Ubuntu
24.04 x86_64. macOS has
a non-CI-gated direct-run/development path but no systemd installer. Other
Linux combinations are unsupported, and Windows is not supported by these
Bash deployment scripts.

- **A host** that stays on. A Pi is a good fit because it's silent and cheap;
  a NUC, an old laptop, or a VM works equally well. Containers are possible
  as an advanced manual-run setup, but are not the systemd path below. On it:
  **`uv`**, **`git`**, **`curl`**, and **`bash`**, plus
  **`sudo`** if you want the emergency speech package or systemd service. A
  minimal server image may omit several of them, and `install.sh` stops with a
  clear message rather than discovering that partway through.

  `uv` selects or downloads a compatible Python 3.11–3.13 interpreter. Kokoro
  is required for the supported Linux experience and CI verifies its
  import on Python 3.11 and 3.13. `curl` downloads the verified model bank.
  The installer stops before installing a service unless Kokoro imports and
  completes a real synthesis; `espeak-ng` is emergency runtime resilience,
  not a substitute for a successful production setup.

  `git` is the one people are surprised by, because it looks like a
  build-machine concern. It isn't: `ship.sh` deploys by fetching into the
  checkout **on the host**, which is why `install.sh` refuses to run without
  git rather than producing an install that can never update.

  Add **systemd** if you want it supervised across reboots, and an **ssh
  server** if you want to deploy with `ship.sh` instead of pulling by hand.
  Neither is required to simply run it. Add the **`openssl` command** only if
  `BARKEEP_TLS=1` should generate a self-signed certificate; an explicitly
  supplied certificate/key pair does not need it.
- **A network path from that host to the bar.** Either the bar plugged into
  the host over USB (it presents itself at `10.0.4.20`, no auth), or the bar
  on your Wi‑Fi with HTTP API access enabled and its PIN in `BUSYBAR_TOKEN`.
- **Outbound internet during setup and updates** to clone/fetch the repository,
  resolve locked packages, and download the Kokoro model bank; live-data apps
  also need outbound access while running.

The host does *not* need to be reachable from the internet. Everything the
apps do is outbound.

Budget at least **1 GB of free disk** and **2 GiB of RAM**. The
installed checkout is about 600–650 MiB and neural synthesis can approach 1
GiB resident. See the complete [dependency and resource
matrix](../docs/dependencies.md).

## Install

Install `uv` through a trusted package manager or its
[official instructions](https://docs.astral.sh/uv/getting-started/installation/).
Then run the repository installer as the unprivileged account that will own the
checkout and run barkeep. Do **not** invoke it with `sudo`; it elevates only the
individual package-manager and systemd commands that need it and refuses to run
as root.

```bash
git clone https://github.com/subjektz3ro/busybar-lab.git
cd busybar-lab
./deploy/install.sh          # .env, verified Kokoro, optional barkeep service
```

`install.sh` asks a handful of questions and writes `.env`. The interview
runs only when no `.env` exists yet — if you already copied `.env.example`
to `.env` by hand, the installer keeps your file untouched and skips the
questions, so make sure the coordinates in it are real. That file is
gitignored, owner-readable (`0600`), and belongs to this machine. Barkeep's
editor can later put per-app overrides in the likewise-gitignored
`config/<app>.env`; those two locations are where personal values live, never
in tracked files. A fresh install binds Barkeep to `127.0.0.1`; LAN access is
an explicit follow-up choice and should always be paired with `BARKEEP_TOKEN`.

The installer also creates owner-only `config/`, `logs/`, `cache/`, and
`state/` directories before systemd starts. DSN uses `cache/dsn/`; Skystrip's
last selected scene uses `state/`. To move cache or state to another mounted
volume, supply an absolute path when running the installer:

```bash
BUSYBAR_CACHE_DIR=/mnt/bar-cache BUSYBAR_STATE_DIR=/mnt/bar-state \
  ./deploy/install.sh
```

Those values are rendered into both the service environment and its filesystem
allow-list. Supply them again on later installer reruns so the same roots stay
selected.

The installer downloads both Kokoro files to temporary paths, verifies their
pinned SHA-256 digests, imports the installed engine, and synthesizes a short
line before it installs or starts Barkeep. Any failure aborts the setup
instead of silently downgrading the experience. Routine `ship.sh` updates use
the same locked dependency set; rerun `./deploy/install.sh` if the model bank is
removed or the host platform changes.

The optional `SKYSTRIP_LIGHTNING_WS` is `.env`-only because a relay URL may
contain credentials and Barkeep's config API does not yet redact declared
values. Fresh installs leave it blank, so live strike flashes are off unless
the operator supplies an authorized secure feed. Barkeep reads shared `.env`
at daemon startup; after editing this hidden key, stop and start the Barkeep
service (`sudo systemctl stop "barkeep@$USER"` followed by
`sudo systemctl start "barkeep@$USER"`) or the manually launched daemon.
The web UI's app-only restart does not reload shared `.env`.

Skystrip's latitude and longitude select the place it depicts. Its IANA
timezone is currently a separate setting and must match that place; the
installer offers the host timezone only as a suggestion, which is wrong when
the always-on host and depicted sky are in different zones. Open-Meteo model
weather is available for ordinary land locations worldwide. NWS station,
forecast, and alert features require the configured point to be inside NWS
API coverage; see the [geographic support matrix](../apps/skystrip.md#geographic-support).

On Linux with systemd it also offers to install and start Barkeep. With no
saved state, the first boot opens in **STANDBY** and makes no Skystrip provider
requests. Open the web UI, read the provider limits and linked credits above
the foreground selector, then select **skystrip** if they cover your use;
selection starts its public weather polling. A saved foreground choice still
restores on later starts. Do not run a second copy of an app by hand against
the same bar.

## Run it as a service

`barkeep` is the control plane: it supervises the apps and serves a web UI on
port 8080. One unit runs everything. The installer normally offers to set this
up. If you skipped that prompt, rerun it; existing `.env` and verified voice
files are retained:

```bash
./deploy/install.sh
systemctl status "barkeep@$USER"
```

The checked-in unit is a path-neutral template, not a file to copy directly.
The installer resolves the current checkout, the exact `uv` executable and uv
cache, and every writable runtime directory before installing it. Custom
checkout paths and nonstandard home directories therefore need no hand edits.

Then open `http://127.0.0.1:8080` locally. If Barkeep is on a remote host, run
this on your own computer and leave it open:

```bash
ssh -N -L 8080:127.0.0.1:8080 your-user@server.example
```

Open `http://127.0.0.1:8080` in your local browser, then select **Skystrip** or
**DSN** as the foreground app. The tunnel preserves Barkeep's safe
loopback-only default; no public port or LAN bind is needed.

## Updating a normal installation

If you are using the public project unchanged, update from the checkout on the
server:

```bash
cd ~/busybar-lab
sudo systemctl stop "barkeep@$USER"
git pull --ff-only
./deploy/install.sh
```

Stopping first prevents the old daemon or one of its children from loading a
mixture of releases while Git and uv replace files. The installer preserves
`.env`, syncs the exact lockfile, verifies the required Kokoro package, model
hashes, and real synthesis, refreshes the systemd unit when needed, and starts
the enabled service only after those checks pass. Do not replace this with
`git pull` alone: code can change together with dependencies, models, or the
unit template. If you run Barkeep manually, stop that process before pulling
and start it again after the installer succeeds.

### Narrow the deploy account's sudo

`ship.sh` needs sudo only to stop and start its exact unit. Many hosts
are set up with a blanket `NOPASSWD: ALL` for the login account, which means
anything that gets execution as that user — including barkeep if an operator
deliberately exposes it to the LAN — is also root. Scope it instead:

```bash
# /etc/sudoers.d/barkeep   (edit with visudo -f, never a plain editor)
<your-user> ALL=(root) NOPASSWD: /usr/bin/systemctl stop barkeep@<your-user>, \
  /usr/bin/systemctl start barkeep@<your-user>
```

Check the path first — `command -v systemctl` — because a sudoers rule whose
path does not match is a rule that does nothing. `sudo -n -l` shows what the
account can actually do.

The unit itself sets `NoNewPrivileges=yes`, `ProtectSystem=strict` and
`ProtectHome=read-only`, with rendered `ReadWritePaths` for `config/`, `logs/`,
cache, state, the uv cache, and `.venv`. The installer creates every target
before systemd enters the mount namespace; a clean clone has none of the
gitignored runtime directories yet.

### Who can reach the control plane

barkeep has **no authentication by default**, so it binds only to loopback —
see `SECURITY.md`. Set a strong `BARKEEP_TOKEN` before deliberately changing
`BARKEEP_BIND` to a LAN address, and pair it with `BARKEEP_TLS=1` so the
token is not readable off the wire. Restart the daemon after changing TLS
settings, then open `https://HOST:8080`; this port serves either HTTP or HTTPS,
not both and with no redirect. To move from the self-signed certificate to
one your devices trust, open the UI over that HTTPS connection (or through a
loopback SSH tunnel), paste the PEM pair into its HTTPS section, and restart —
no `.env` edit needed. Barkeep refuses private-key uploads over LAN HTTP.
It refuses
`Host` headers that are not an IP literal, `localhost`, this machine's name,
or listed in `BARKEEP_ALLOWED_HOSTS`. If you front it with a reverse proxy or
reach it by an alias, set that variable or every request gets a 421.

#### Logging in from another machine

The credential is the `BARKEEP_TOKEN` line in the host's gitignored,
owner-only `.env` — it exists nowhere else, and no API route will echo it.
Read it over ssh when you need it:

```bash
ssh <host> 'grep ^BARKEEP_TOKEN ~/busybar-lab/.env'
```

Then open `https://<host>:8080`. With the generated self-signed certificate
the browser warns on first visit; before accepting, compare the fingerprint
it shows against the one the host actually serves:

```bash
ssh <host> 'openssl x509 -noout -fingerprint -sha256 \
  -in ~/busybar-lab/config/tls/barkeep-selfsigned.crt'
```

Paste the token when the page asks. It asks once per browser — the token is
exchanged for an httpOnly cookie, so bookmarks just work from then on. A
mistyped token simply re-prompts. A lost token cannot be recovered through
the UI: put a new value in `.env` and restart the daemon, which also signs
every browser out.

## Deploying your own changes

This section is for maintainers or people running a fork. Ordinary users should
follow [Updating a normal installation](#updating-a-normal-installation)
instead. **Origin is the source of truth:** the host pulls from your git
remote, while your laptop only tells it when to go and look.

```bash
git push origin main
./deploy/ship.sh myhost              # or set BUSYBAR_DEPLOY_HOST
```

`ship.sh` refuses to deploy a commit that is not on `origin/main`, because the
host would not be able to fetch it:

```
error: a1b2c3d is not on origin/main, so the host cannot fetch it.

       Push it first:   git push origin HEAD:main
```

On the host it fetches from the configured remote and **hard-resets** to the
commit — the host is a deploy target, not a working copy, so anything edited
there is discarded on purpose and a merge conflict on an unattended machine
is impossible. Before mutating live files, it verifies that the installed unit
was rendered from the target commit's template. It then stops Barkeep, resets
the checkout, runs the locked sync, verifies that the required Kokoro package,
models, and real synthesis still work, starts the unit, and confirms it came
back up. A failed sync or speech check leaves the service stopped instead of
starting a degraded or half-deployed release.
The service unit itself is not installed by `ship.sh`, because that would make
the deploy account root-equivalent. Its embedded template digest detects drift
and tells the operator to rerun `install.sh`, which
regenerates the host-specific unit, reloads systemd, and safely restarts an
already-running service.

| | |
|---|---|
| `--dry-run` | print exactly what would happen on the host, touch nothing |
| `--ref REF` | ship a tag or branch instead of `HEAD` |

Configuration, all optional except the host: `BUSYBAR_DEPLOY_HOST`,
`BUSYBAR_DEPLOY_PATH`, `BUSYBAR_DEPLOY_SERVICE`, `BUSYBAR_DEPLOY_REMOTE`,
`BUSYBAR_DEPLOY_BRANCH`.

### Giving the host read access

The host needs to be able to fetch from your remote:

- **Public repository** — nothing to do, `https://` works anonymously.
- **Private repository** — add a read-only **deploy key**. On the host:

  ```bash
  ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_deploy -C "busybar-lab deploy"
  cat ~/.ssh/id_deploy.pub
  ```

  Paste that into the repository's *Settings → Deploy keys*, leaving "allow
  write access" **off**. Then point the checkout at the SSH URL:

  ```bash
  git remote set-url origin git@github.com:<you>/busybar-lab.git
  ```

A deploy key is scoped to the one repository and read-only, which is what a
puller should have. Don't put a personal access token on a device that sits in
a cupboard.

## Logs

```bash
journalctl -u "barkeep@$USER" -f
```

barkeep also keeps per-app logs and shows them in its web UI, which is usually
the faster way to see why an app is unhappy.

## Testing the deploy path

`tests/test_deploy.py` builds throwaway git repositories and runs the real
`ship.sh --dry-run` against them, so every guard — unpushed commit, missing
remote, missing host, unknown ref — is checked without a host or a bar. CI
runs it on every push.

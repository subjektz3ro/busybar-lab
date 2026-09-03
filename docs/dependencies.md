# What this actually needs

Everything the apps and the control plane depend on, in one place, because it
was previously spread across `pyproject.toml`, `deploy/install.sh` and nobody's
memory. Disk figures are conservative budgets derived from a running host.

`./deploy/install.sh` sets all of this up. This page is for when you need to
know *what* it did, size a machine, put it in a container, or work out why
speech is coming out in the wrong voice.

## Before any of this: what must already be on the host

`install.sh` cannot bootstrap itself out of nothing. These have to be there
first, and a minimal server image may omit several of them:

| Tool | Needed for | Optional? |
|---|---|---|
| `uv` | the locked Python environment and every project command | no |
| `git` | `ship.sh` fetches into the checkout **on the host** | no — `install.sh` requires it unconditionally |
| `curl` | downloading the required, hash-pinned Kokoro model files on Linux | no on the supported Linux service path |
| `bash` | `install.sh` and `ship.sh` are bash, not POSIX sh | no |
| `openssl` command | generating Barkeep's persistent self-signed TLS pair | yes — only for `BARKEEP_TLS=1` |
| `sudo` | emergency `espeak-ng`; `systemctl` for the service | only for those optional operations |
| systemd | supervising across reboots | yes — you can run it in a terminal |
| ssh server | `ship.sh` deploys over ssh | yes — a normal install can stop Barkeep, then update on-host with `git pull --ff-only` followed by `./deploy/install.sh` |

Install `uv` through a trusted package manager or its
[official instructions](https://docs.astral.sh/uv/getting-started/installation/).
`uv` selects or downloads a compatible Python 3.11–3.13 interpreter, so a
separate system Python is not a pre-install requirement.
Run `./deploy/install.sh` as the unprivileged account that will own the checkout
and run barkeep, never as root; it invokes `sudo` itself for the individual
operations above.

`git` is the one that surprises people, because it reads like a build-machine
concern. It is not: the deploy model has the host pull from origin, so a host
without git installs once and can never be updated.

**Package manager:** `install.sh` tries to install `espeak-ng` as emergency
runtime resilience. It is not accepted as a substitute for Kokoro: a supported
Linux production install must pass the neural synthesis check below.

## Support and resource matrix

The supported service target is 64-bit glibc 2.28+ Linux on `x86_64` or
`aarch64`, using CPython 3.11–3.13. CI exercises Ubuntu 24.04 x86_64. The
aarch64/glibc 2.28+ boundary is enforced by installer and dependency contracts
but does not have a hosted CI runner. macOS has a non-CI-gated
development/direct-run path. Other Linux ABIs and CPU families, Python 3.14,
and Windows are unsupported.

| | Supported Linux service | macOS direct development |
|---|---|---|
| Python | 3.11–3.13; CI gates 3.11 and 3.13 | 3.11–3.13, not CI-gated |
| Primary speech | required Kokoro package + verified model pair | `say` |
| Runtime resilience | `espeak-ng` if a previously verified Kokoro install later fails | platform `say` command |
| Voice/model files | **~340 MiB** | none |
| Installed checkout | about **600–650 MiB** | not formally budgeted |
| Runtime memory | use at least **2 GiB RAM**; synthesis can approach 1 GiB resident | not formally budgeted |

The disk and RAM figures are different budgets. Keep at least **1 GB of free
disk** for a Kokoro installation because `uv` may temporarily retain a few
hundred MiB of downloaded wheels in addition to the 600–650 MiB checkout.
Budget **2 GiB of RAM** because the neural worker can approach 1 GiB while
synthesising. Hardware-free tests and visual previews do not need the model
files, but narration is part of the production Skystrip and DSN experience. The
production installer therefore rejects hosts outside the supported binary
envelope and fails if Kokoro cannot complete a real synthesis.

## Python packages

The core packages are declared in `pyproject.toml` and installed by a plain
`uv sync`:

| Package | Why |
|---|---|
| `busylib` | The official BUSY Bar client. Everything device-facing goes through it |
| `astral` | Solar-elevation and lunar-phase inputs for skystrip; the on-strip icon positions are artistic |
| `httpx` | Direct HTTP clients used by the first-party live-data apps |
| `websockets` | Optional Skystrip client for an authorized secure lightning relay/source configured only in gitignored `.env`; the repo ships no raw provider endpoint and leaves the feature off by default |
| `tzdata` | IANA timezone database fallback for slim hosts that do not ship `/usr/share/zoneinfo`; Skystrip still requires an explicit zone matching its coordinates |
| `fastapi`, `uvicorn` | barkeep's web UI and JSON API on :8080 |
| `pillow` | Frame rendering — every scene is a PIL image before it becomes an `.anim`. Busylib 2 makes Pillow an optional media extra, while first-party modules import PIL directly, so it remains a required direct dependency here |

On Linux, `kokoro-onnx` is part of the default locked dependency set and pulls
`onnxruntime` plus `numpy`, which make up most of the neural Python environment.
The project and installer deliberately fail outside Python 3.11–3.13 and the
supported glibc 2.28+ `x86_64`/`aarch64` envelope instead of silently creating
a speech-degraded production host.

Use `./deploy/install.sh` for the complete setup. A direct `uv sync --locked`
installs the Python engine but does not fetch the separately hash-verified
model bank under `voices/`, nor does it perform the installer's real synthesis
smoke. Routine deploys preserve the same required locked engine and recheck
that it can synthesize before restarting Barkeep.

`busybar_dev/tts.py` imports Kokoro and NumPy inside the synthesis path so app
startup does not eagerly load a large neural runtime. Those local imports look
like a style mistake and are not one.

## System packages

**`espeak-ng`** — installed by `install.sh` via the host package manager on
Linux. It is the last fallback in the TTS chain. If it cannot be installed,
the panel still works and only speech is unavailable when Kokoro cannot run.

**`openssl`** — optional and not installed by `install.sh`. Barkeep invokes its
command once when `BARKEEP_TLS=1` needs to generate `config/tls/`; supplying
`BARKEEP_TLS_CERT` and `BARKEEP_TLS_KEY` uses those files instead. The normal
HTTP/SSH-tunnel path does not need it. TLS integration tests skip cleanly when
the command is absent.

No ffmpeg, audio server, or display server is required — the apps render to
PIL and push bytes to the bar over HTTP.

## Voice and model files

`voices/` is not in git and is not installed by `uv sync`. On a supported Linux
service host, `install.sh` downloads it. If `SKYSTRIP_VOICE_DIR` names another
checkout-relative or absolute directory, the installer downloads, hashes, and
tests that exact effective directory instead:

| File | What |
|---|---|
| `kokoro-v1.0.onnx`, `voices-v1.0.bin` | Required Linux Kokoro models used by Skystrip and DSN narration |

The two downloaded files total about **354 MB (337 MiB)**. The installer
downloads each to a temporary path, verifies its pinned SHA-256 digest, and
only then moves it into place, so an interrupted transfer is never accepted as
a model. Every configured Kokoro voice uses this same pair, so changing
`SKYSTRIP_VOICE` or `DSN_VOICE` does not download another model. An existing
supported checkout upgrading from an older speech setup can rerun
`./deploy/install.sh`; its `.env` is preserved while the shared bank is fetched,
verified, and exercised with real audio generation.

## How a voice is actually chosen

`busybar_dev/tts.py` walks a chain, and this is the usual source of "why does
it sound wrong":

1. **Kokoro**, when its required Linux package and complete shared model files
   are available. Supported `xx_yyy` voice names select that narrator (for example, Skystrip's
   `am_michael` or dsn's `af_nova`). A non-Kokoro value left in a host's
   gitignored configuration by an older release maps to `am_michael`.
2. **Emergency runtime resilience.** `espeak-ng` on Linux if a previously
   verified Kokoro install later becomes unusable; `say` on the macOS direct
   development path. An explicit macOS voice name is preserved.

Runtime falls through so a post-install failure does not silence an already
running bar. The production installer is intentionally stricter: missing
models, an unavailable package, or failed audio generation abort setup before
the service is installed or started.

## Speech is the expensive thing at runtime, too

Kokoro runs at roughly **1× realtime on a Pi 5**: a 30-second line costs about
30 seconds of CPU to synthesise. That is why dsn bakes lines ahead and caches
them on the device rather than synthesising on a keypress, and why its cache is
sized to hold a whole rotation. Its Linux worker can approach **1 GiB resident**;
DSN uses a disposable process so that memory returns to the OS after a cold
bake. See `apps/dsn.md`.

## Optional, not needed to run anything

- **Chrome or Chromium** — only for the barkeep clip in
  `scripts/make_demo_gifs.py`, which screenshots the live web UI. Every other
  demo GIF renders from the apps' own code and needs nothing. The script skips
  the barkeep clip cleanly when no browser is found.
- **A BUSY Bar.** The test suite needs no hardware. Hello and the app template
  have `--dry-run`, DSN has a no-device live-feed `--dry-run`, and Skystrip has
  an offline `--preview`; these are app-specific seams, not universal flags.

## What is deliberately absent

No vendor cloud, message broker, or hosted database is required. The live-data
apps poll outward and push to a bar on the local network. Barkeep is an inbound
HTTP service on port 8080 when enabled (loopback by default; an operator may
explicitly bind it to the LAN), and `busybar-viz serve` similarly opens a
loopback development UI on port 8765. Barkeep state is JSON/JSONL under
`config/`; the visualizer's optional review journal is a local SQLite file
under its gitignored data directory.

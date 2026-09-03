# Dependencies and system requirements

This page lists host prerequisites, supported platforms, resource budgets,
model files, and TTS selection. Requirements are defined by `pyproject.toml`
and `deploy/install.sh`; disk figures are conservative measurements from a
running host.

## Host prerequisites

`install.sh` requires these tools. Minimal server images may omit some of them:

| Tool | Needed for | Optional? |
|---|---|---|
| `uv` | the locked Python environment and every project command | no |
| `git` | `ship.sh` fetches into the checkout **on the host** | no — `install.sh` requires it unconditionally |
| `curl` | downloading the required, hash-pinned Kokoro model files on Linux | no on the supported Linux service path |
| `bash` | `install.sh` and `ship.sh` are bash, not POSIX sh | no |
| `openssl` command | generating Barkeep's persistent self-signed TLS pair | yes — only for `BARKEEP_TLS=1` |
| `sudo` | installing runtime-fallback `espeak-ng`; `systemctl` for the service | only for those optional operations |
| systemd | supervising across reboots | yes — you can run it in a terminal |
| ssh server | `ship.sh` deploys over ssh | yes — a normal install can stop Barkeep, then update on-host with `git pull --ff-only` followed by `./deploy/install.sh` |

Install `uv` through a trusted package manager or its
[official instructions](https://docs.astral.sh/uv/getting-started/installation/).
`uv` selects or downloads a compatible Python 3.11–3.13 interpreter, so a
separate system Python is not a pre-install requirement.
Run `./deploy/install.sh` as the unprivileged account that will own the checkout
and run barkeep, never as root; it invokes `sudo` itself for the individual
operations above.

Git remains required after cloning because the host deploys and updates by
fetching revisions from origin. `install.sh` exits if Git is unavailable.

**Package manager:** `install.sh` tries to install `espeak-ng` as a runtime
fallback. A supported Linux installation must still pass the Kokoro synthesis
check below.

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
| Runtime fallback | `espeak-ng` if Kokoro cannot be imported or its model bank is unavailable | None separately defined |
| Voice/model files | **~340 MiB** | none |
| Installed checkout | about **600–650 MiB** | not formally budgeted |
| Runtime memory | use at least **2 GiB RAM**; synthesis can approach 1 GiB resident | not formally budgeted |

The disk and RAM figures are different budgets. Keep at least **1 GB of free
disk** for a Kokoro installation because `uv` may temporarily retain a few
hundred MiB of downloaded wheels in addition to the 600–650 MiB checkout.
Budget **2 GiB of RAM** because the neural worker can approach 1 GiB while
synthesising. Hardware-free tests and visual previews do not need the model
files. Skystrip and DSN narration on a supported Linux service host requires
them. Installation fails if the host-compatibility or Kokoro synthesis checks
fail.

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
Supported installs are limited to Python 3.11–3.13 on glibc 2.28+
`x86_64`/`aarch64`; installation exits nonzero on other platforms.

Use `./deploy/install.sh` for the complete setup. A direct `uv sync --locked`
installs the Python engine but does not fetch the separately hash-verified
model bank under `voices/`, nor does it perform the installer's real synthesis
smoke. Routine deploys preserve the same required locked engine and recheck
that it can synthesize before restarting Barkeep.

`busybar_dev/tts.py` imports Kokoro and NumPy only when synthesis starts, which
avoids loading the neural runtime during general app startup.

## System packages

**`espeak-ng`** — `install.sh` attempts to install it through the host package
manager on Linux. The runtime selects it when Kokoro cannot be imported or its
model bank is unavailable. If it cannot be installed, display output remains
available.

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

## TTS engine selection

`busybar_dev/tts.py` selects a speech engine in this order:

1. **Kokoro**, when its required Linux package and complete shared model files
   are available. Supported `xx_yyy` voice names select that narrator (for example, Skystrip's
   `am_michael` or dsn's `af_nova`). A non-Kokoro value left in a host's
   gitignored configuration by an older release maps to `am_michael`.
2. **Runtime fallback.** `espeak-ng` on Linux if Kokoro cannot be imported or
   its model bank is unavailable; `say` on the macOS direct development path.
   An explicit macOS voice name is preserved.

The installer requires Kokoro and its model bank even though the runtime can
select `espeak-ng`. Missing models, an unavailable package, or failed audio
generation abort setup before the service is installed or started.

## Runtime speech cost

Kokoro runs at roughly **1× realtime on a Pi 5**: a 30-second line costs about
30 seconds of CPU to synthesise. DSN pre-generates lines and caches enough on
the device for a full rotation. Its Linux worker can approach **1 GiB
resident**; DSN uses a disposable process so worker memory returns to the OS
after generation. See `apps/dsn.md`.

## Optional components

- **Chrome or Chromium** — only for the barkeep clip in
  `scripts/make_demo_gifs.py`, which screenshots the live web UI. Every other
  demo GIF renders from the apps' own code and needs nothing. The script skips
  the barkeep clip cleanly when no browser is found.
- **A BUSY Bar.** The test suite needs no hardware. Hello and the app template
  have `--dry-run`, DSN has a no-device live-feed `--dry-run`, and Skystrip has
  an offline `--preview`; these are app-specific seams, not universal flags.

## External services

No vendor cloud, message broker, or hosted database is required. The live-data
apps poll outward and push to a bar on the local network. Barkeep is an inbound
HTTP service on port 8080 when enabled (loopback by default; an operator may
explicitly bind it to the LAN), and `busybar-viz serve` similarly opens a
loopback development UI on port 8765. Barkeep state is JSON/JSONL under
`config/`; the visualizer's optional review journal is a local SQLite file
under its gitignored data directory.

# Third-party notices

This project is **GPL-2.0-or-later** (see [`LICENSE`](LICENSE)). The
copyleft requirement comes from `busybar_dev/anim.py`, a port of
GPL-2.0-or-later firmware tooling. Keeping that grant makes GPLv3 available
for the distributed combination with the Apache-2.0 Astral dependency.

## Why GPL-2.0-or-later

`busybar_dev/anim.py` encodes the BUSY Bar's native `.anim` format. It was
ported from the official
[`busy-app/busybar-firmware`](https://github.com/busy-app/busybar-firmware/tree/2cfd1f3ad94071056f3f96784183bab62dea423e)
repository at the revision named in the source header, GPL-2.0-or-later,
Copyright 2024-2026 Flipper FZCO — specifically
`scripts/seq2anim.py`, `scripts/flipper/rle.py` and
`lib/anim_file/anim_file_format.h`. The source header pins the upstream
revision reviewed for this port. Keep that attribution intact.

## Bundled and fetched material

| What | Where | Licence | Notes |
|---|---|---|---|
| **busylib** | dependency, and docs vendored under `docs/busylib/` | MIT — **Copyright © 2025 Flipper FZCO** | `docs/busylib/LICENSE` ships alongside the copy, as MIT requires. The snapshot and `scripts/refresh_docs.py` are pinned to upstream revision [`23875e1c0201`](https://github.com/busy-app/busylib-py/commit/23875e1c0201265365ab78ed9a1caa98d21de8ad). |
| **Astral** | runtime dependency | Apache-2.0 | Computes sun and moon positions for Skystrip. Apache-2.0 is compatible with GPLv3, which is available under this project's or-later grant |
| **BUSY documentation and device API spec** | linked documentation; owner-local ignored `docs/api/openapi.yaml` | © Flipper FZCO; not redistributed | The public source tree links to the vendor documentation. `scripts/refresh_docs.py` may read an API schema from the owner's own device into a gitignored path, but it does not scrape or redistribute the documentation site |
| **Kokoro v1.0 model and voice bank** | fetched by `deploy/install.sh`; never bundled | Apache-2.0 model weights | The installer downloads hash-pinned release assets from [kokoro-onnx](https://github.com/thewh1teagle/kokoro-onnx/releases/tag/model-files-v1.0). The inference package is MIT; the upstream model card identifies the weights as [Apache-2.0](https://huggingface.co/hexgrad/Kokoro-82M) |

## Data sources the apps call

| Source | Terms | What we do about it |
|---|---|---|
| **NWS / api.weather.gov** | US Government work, public domain | Asks callers to identify themselves in the User-Agent. `SKYSTRIP_CONTACT` fills that in; blank stays anonymous. Never put someone's address in there without asking them |
| **Open-Meteo** | API data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the free endpoint is limited to non-commercial use and published request limits | Skystrip adapts model current, recent-past/forecast and snow-depth values into pixel scenes. Credit: [Weather data by Open-Meteo.com](https://open-meteo.com/). Barkeep repeats the linked credit and CC BY 4.0 link beside its live display preview. The app treats availability as best-effort |
| **RainViewer** | Public API for personal, educational and small-scale community use; attribution required; no availability guarantee | Skystrip adapts composite radar tiles into a precipitation-intensity estimate. Credit: [Weather radar data by RainViewer](https://www.rainviewer.com/api.html). Barkeep repeats the linked credit beside its live display preview. The numeric Universal Blue anchors in `busybar_dev/radar.py` are sampled directly from RainViewer's [published CSV](https://www.rainviewer.com/files/rainviewer_api_colors_table.csv); no third-party implementation is copied |
| **Operator-supplied lightning feed (optional)** | Terms and coverage depend on the operator's chosen source | Disabled by default; the repository supplies no endpoint or data. `SKYSTRIP_LIGHTNING_WS` accepts an authorized secure relay using a Blitz-compatible [strike schema](https://www.limaps.org/json-data-archive.html), which is a wire contract rather than permission or a coverage claim. If the source carries Blitzortung data, its [official terms](https://www.blitzortung.org/en/contact.php) restrict raw access to participants or explicitly approved users, require external apps to retrieve data through a separate server, and prohibit use for storm-warning systems. That page contains additional project conditions; authorization and a relay do not replace them. Skystrip's optional input drives ambient flashes only; NWS CAP independently controls alert cards and the Extreme-alert siren. The operator is responsible for access rights, attribution, permitted use, and every source-specific condition and safety limit |
| **NASA DSN Now** | NASA — generally not copyrighted | The live antenna feed behind the `dsn` app |
| **JPL Horizons** | NASA/JPL | Spacecraft distances |

## Generated alert audio

Skystrip's Extreme-alert tone is synthesized deterministically by
`siren_pcm()` from sine-wave math and uploaded as raw PCM under a content hash.
No siren recording or other third-party audio sample is bundled or fetched.

## A note on scope

The GPL-2.0-or-later grant in `LICENSE` and the project notices covers
**this project's own code and artwork**.
Vendored busylib documentation remains under its MIT licence and copyright;
its licence travels with the copy. Nothing in this repository relicenses
third-party material.

## Artwork

The pixel art in `apps/assets/` and the spacecraft portraits in `apps/dsn.py`
were made for this project and are covered by the repository licence.
The repository owner specifically confirms that `apps/assets/house.png`,
`apps/assets/house_dark.png`, and `apps/assets/flock/*.png` are original work
by the project author. Those files were recovered from the author's own device
backup/storage during development; they are not vendor artwork.

## Webfonts (barkeep UI)

`barkeep/static/fonts/` vendors three typefaces, latin subsets, so the
control plane makes no CDN request:

- **Silkscreen** — © 2001 The Silkscreen Project Authors
  (github.com/googlefonts/silkscreen)
- **Archivo** — © 2020 The Archivo Project Authors
  (github.com/Omnibus-Type/Archivo)
- **Martian Mono** — © 2021 The Martian Mono Project Authors
  (github.com/evilmartians/mono)

All three are licensed under the SIL Open Font License 1.1; the full licence
for each travels alongside the files as
`barkeep/static/fonts/OFL-<family>.txt`.

## defusedxml

PSFL-licensed, © Christian Heimes. Installed as a dependency rather than a
source copy. Used for the DSN feed and config documents: stdlib
`xml.etree.ElementTree` expands internal entities, so the byte budget on those
documents did not bound the work they could cause.

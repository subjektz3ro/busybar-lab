# Architecture

Two ways the bar gets driven: a host that runs it all day, and your laptop over
USB while you're building. They run the *same* app code — that's the point of
the split, and the reason nothing in `apps/` is allowed to hardcode a host.

## What you need

| | |
|---|---|
| **A host** | For the supported service path, 64-bit glibc 2.28+ Linux on `x86_64` or `aarch64`, CPython 3.11–3.13, and an always-on network path. A Pi 4/5 with 64-bit Raspberry Pi OS and at least 2 GiB RAM, NUC, 64-bit laptop, or VM can fit; see the [support and resource matrix](dependencies.md) |
| **A path to the bar** | The bar plugged into the host over USB (it answers at `10.0.4.20`, no auth), *or* the bar on your network with HTTP API access enabled and its PIN in `BUSYBAR_TOKEN` |
| **Outbound internet** | During setup/updates for Git, packages, and Kokoro models; also while running live-data apps |

The host does **not** need to be reachable from the internet. Everything the
apps do is outbound.

## Production

```mermaid
flowchart TB
    subgraph lan["your network"]
        subgraph host["host — always on"]
            sd["systemd<br/>barkeep@user"]
            bk["barkeep<br/>supervisor + FastAPI"]
            fg["foreground app<br/>owns the display"]
            bg["other apps<br/>stopped"]
            sd --> bk
            bk -->|"spawns"| fg
            bk -.->|"idle"| bg
        end
        bar["BUSY Bar BB.1<br/>72×16 front · 160×80 back"]
        br["local browser or SSH tunnel<br/>:8080"]
    end

    net(["the internet"])

    fg -->|"HTTP · draws, assets, audio"| bar
    fg <-->|"websocket · wheel + buttons"| bar
    br -->|"loopback /api/*<br/>LAN only when opted in"| bk
    net -->|"live data<br/>(NASA DSN, NWS, …)"| fg

    classDef box fill:#1d2430,stroke:#4a5568,color:#e2e8f0
    classDef hw fill:#2d3748,stroke:#718096,color:#f7fafc
    class sd,bk,fg,bg,br box
    class bar,net hw
```

**How the bar is reached is your choice.** Over USB it answers at `10.0.4.20`
with no authentication, which keeps it off your network entirely. Over the LAN
you enable HTTP API access in the bar's local web UI and put its PIN in
`BUSYBAR_TOKEN`. Both work — apps only ever see `connect()` — but they are not
the same trust model, so pick deliberately rather than by accident.

barkeep is the only thing systemd knows about. It parents every app process, so
restarting the unit restarts the apps. One foreground app owns the display at a
time, because [apps cannot overlay](../apps/README.md#why-apps-dont-overlay).

## Development

```mermaid
flowchart LR
    subgraph dev["your machine"]
        code["apps/*.py"]
        payload["hello/template --dry-run<br/>build native requests"]
        data["dsn --dry-run<br/>live feed, no device"]
        prev["skystrip --preview out.png<br/>local raster render"]
        once["dsn/skystrip --once<br/>one device update"]
    end
    bar["BUSY Bar<br/>10.0.4.20 over USB-C<br/>no auth"]

    code --> payload
    code --> data
    code --> prev
    code --> once
    once -->|"HTTP"| bar

    classDef box fill:#1d2430,stroke:#4a5568,color:#e2e8f0
    classDef hw fill:#2d3748,stroke:#718096,color:#f7fafc
    class code,payload,data,once,prev box
    class bar hw
```

Over USB the bar answers at `10.0.4.20` with **no token**. Prefer that address
over `busybar.local`: when the bar is also on Wi-Fi, the hostname resolves to
*both* addresses and requests hang at random. `connect()` already tries USB
first.

**Don't run an app by hand while barkeep is running the same one** — two
writers fight over the display, and the loser only gets it back on its next
redraw.

You do not need a bar to work on this. The offline seam matches what each app
actually owns: Hello and the template build native request data, DSN exercises
its live NASA feed without device I/O, and Skystrip renders its app-owned raster
scene to PNG. The test suite uses neither a bar nor external services. There is
no generic firmware-text PNG renderer, so not every app exposes `--preview`.

## Shipping a change

**Origin is the source of truth.** The host pulls from your git remote; your
laptop only tells it when to go and look. That way the host can be rebuilt from
a clean clone without your laptop existing.

```mermaid
sequenceDiagram
    autonumber
    participant D as your machine
    participant G as origin<br/>(GitHub)
    participant P as host
    participant B as bar

    D->>D: uv run pytest
    D->>D: git commit
    D->>G: git push origin main
    Note over D,P: ./deploy/ship.sh <host><br/>refuses if the commit is not on origin
    D->>P: ssh: go and fetch <sha>
    P->>G: git fetch origin
    P->>P: verify target unit; stop barkeep@user
    P->>P: git reset --hard <sha>
    P->>P: locked sync<br/>verify Kokoro synthesis; start barkeep@user
    P->>P: verify the unit came back
    P-->>D: "<sha> live on <host>"
    P->>B: apps redraw within seconds
```

`ship.sh` **refuses to deploy a commit that is not on `origin`**, because the
host would not be able to fetch it. Origin remains the complete, reproducible
source of every deployed revision.

The host is a **deploy target, not a working copy**: it hard-resets to the
commit, so anything edited there is discarded on purpose and a merge conflict
on an unattended machine is impossible. Config is the exception — `.env` and
`config/` are gitignored and per-machine.

Full detail, including the read-only deploy key a private-repo host should use,
is in [`deploy/README.md`](../deploy/README.md).

## Where the pieces live

```
apps/            the apps, plus a per-app .md and _template.py
  README.md      every control binding in one table
barkeep/         supervisor (no HTTP), server (thin routes), static/ (talks only to /api/*)
busybar_dev/     connect(), anim.py (GPL-2.0-or-later), tts.py, screen.py
busybar_viz/     the offline visual debugger (`busybar-viz`) — renders, audits,
                 and pins app pixels; production apps never import it
apps.toml        the app registry — an app not listed here cannot be run
deploy/          ship.sh, install.sh, render_service.py, barkeep.service, README.md
docs/            index, first-party guides, media, retained busylib docs, design records
AGENTS.md        the canonical guide — read this first
.claude/skills/busybar-app/SKILL.md
                 the device laws that fail SILENTLY — read before building
scripts/         new_app.py, refresh_docs.py, make_demo_gifs.py,
                 check_coverage.py, check_public_release.py
tests/           hardware-free behavior and contract tests
```

## The one rule that shapes everything

Priority is **not** z-order. A draw is accepted when its priority is `>=` the
running app's, so an equal-priority draw from a different `application_name`
simply takes the display — two apps never composite. That is why barkeep has a
single foreground slot instead of a layering model, and why an app that wants
to interrupt must own the strip for a bounded window and hand it back.

Full detail, and the rest of the failure modes that pass code review and only
show up on the physical panel, are in
[`.claude/skills/busybar-app/SKILL.md`](../.claude/skills/busybar-app/SKILL.md).

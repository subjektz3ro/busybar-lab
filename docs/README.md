# Documentation

Start with the repository [README](../README.md) to run an example app or
build your own, then use the guides below for the part you are changing.

## First-party guides

| Guide | Scope |
|---|---|
| [Architecture](architecture.md) | Laptop and always-on-host topology, deployment flow, and module map |
| [Maintainer map](maintaining.md) | Package ownership, enforced dependency boundaries and focused tests |
| [Dependencies](dependencies.md) | Runtime packages, speech engines, host prerequisites, and disk budget |
| [Known issues](known-issues.md) | Firmware-specific limitations, brightness mitigation and opt-out |
| [Agent cookbook](agent-cookbook.md) | Step-by-step app workflow: create, render, iterate, register, pin, and capture |
| [BUSY Bar visualizer](busybar-viz.md) | Ad-hoc frame audits, offline renderer checks, evidence bundles, comparisons, and review sessions |
| [Scene checklist](scenes.md) | Physical-panel and weather-state checks for a new Skystrip scene |
| [App conventions](../apps/README.md) | Shared device behavior and control bindings |
| [Hello](../apps/hello.md) | Minimal device smoke test |
| [DSN](../apps/dsn.md) | Data model, displays, controls, configuration, and sources |
| [Skystrip](../apps/skystrip.md) | Geographic support, displays, controls, configuration, and sources |
| [Deployment](../deploy/README.md) | Installation, systemd, and SSH deployment |
| [Security](../SECURITY.md) | Barkeep's exposure model and safe deployment choices |

The canonical device and contributor rules for coding agents are in
[`AGENTS.md`](../AGENTS.md). Human contributors should also read
[`CONTRIBUTING.md`](../CONTRIBUTING.md).

## Device and client references

- The vendor maintains the current [BUSY developer
  documentation](https://docs.busy.app/bar/dev), including HTTP API access and
  API-token setup.
- A reachable bar exposes its own OpenAPI document. Running
  `uv run scripts/refresh_docs.py` stores a local snapshot at
  `docs/api/openapi.yaml`; that path is gitignored and is not part of the
  public source distribution.
- [`docs/busylib/`](busylib/) contains the redistributable documentation and
  examples for the official Python client. Its licence and its own agent guide
  live in that directory.

## Design records

[`docs/design/`](design/) contains selected visual explorations that still help
explain the shipped displays. They are historical and non-normative: current
behavior is defined by the app documentation, production code, and tests.

The [maintainer refactor validation record](refactor-validation.md) records the
scope, regression evidence and verification limits of the September 2026
package migration.

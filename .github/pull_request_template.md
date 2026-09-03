## What changed

<!-- Describe the user-visible result and the reason for the change. -->

Closes #

## Verification

<!-- List exact commands and results. Explain any skipped or failing check. -->

- [ ] Relevant tests pass (`uv run pytest -q` for the full hardware-free suite).
- [ ] Static checks pass (`uv run ruff check --output-format github .` and `uv run mypy`).
- [ ] The public-snapshot check passes (`uv run --no-project --python 3.11 scripts/check_public_release.py`).
- [ ] User, app, deployment, and agent documentation is updated, or this change needs no documentation update.

## Visual and device evidence

<!--
If pixels changed, provide the registered scenario, before/after artifact SHAs,
busybar-viz comparison, affected frames/states, and the strongest evidence
level actually reached: renderer-verified, gap-previewed, framebuffer-captured,
or hardware-observed. Do not call a preview hardware-observed.
-->

- Pixels changed: <!-- yes/no -->
- Scenario and artifact SHA(s):
- Comparison result:
- Evidence level:
- Physical device and firmware, if used:

- [ ] Registered pixel changes pass `uv run busybar-viz doctor --json`, or no registered pixels changed.
- [ ] Intentional registered pixel changes update `viz-baselines.toml`, or no registered pixels changed.
- [ ] Static/status labels were checked for complete ink across every affected frame, page, and state, or no labels changed.

## Safety and review notes

- [ ] No token, private URL, personal coordinate, hostname, address, generated runtime data, or other owner-specific value is tracked.
- [ ] New external data, code, or assets include their source, usage terms, licence, and attribution in the appropriate documentation, or none were added.
- [ ] Hardware tests left the bar as it was found, or no hardware was used.

<!-- Note compatibility risks, follow-up work, and anything reviewers should inspect closely. -->

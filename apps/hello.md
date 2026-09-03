# hello — the smoke test

Use this command to check device connectivity, drawing, and framebuffer
screenshots. Add `--say` to check synthesis, upload, and audio playback.

```bash
uv run apps/hello.py                          # draw HELLO, save screenshots
uv run apps/hello.py --text "GOOD EVENING"    # draw something else
uv run apps/hello.py --say "good evening"     # draw and speak
uv run apps/hello.py --clear                  # take it back down
uv run apps/hello.py --dry-run                # build the request, no device I/O
```

| Flag | Default | What it does |
|---|---|---|
| `--text` | `HELLO` | What to draw on the front strip |
| `--say TEXT` | — | Also speak `TEXT` aloud (synthesises, uploads, plays) |
| `--clear` | — | Remove this app's draws and exit |
| `--dry-run` | — | Build and report the requested draw/clear/speech operation without connecting |
| `--shots DIR` | `scratch` | Where the screenshot PNGs go |

## Controls

None. It's a one-shot command, not a watcher — it draws and exits.

## Checks performed

1. `connect()` resolves a host and reaches the device
2. A draw is accepted (or refused with 409, which is its own useful answer)
3. Both displays screenshot back to PNG
4. With `--say`, a speech asset synthesises, uploads, and plays

`--dry-run` checks argument parsing and native draw-payload construction only.
Native text is rendered by device firmware, so
the repository does not fabricate a PNG preview for it.

If step 1 fails, the address is the usual culprit. Prefer `10.0.4.20` over
`busybar.local`: when the bar is also on Wi-Fi, that hostname resolves to
*both* addresses and requests hang at random. `connect()` already tries USB
first for this reason.

## Reusable patterns

**The text recipe.** There is no font-metrics API on the device, so don't
compute layout from a glyph table you invented. The arrangement verified on
real hardware is `font="condensed", align="center", x=36, y=8`, and about
**12 characters** fit across 72 px before it overflows. Longer text needs
`scroll_rate` (which is pixels per *minute*, ~1400 for a readable scroll).

**Screenshots.** `busybar_dev/screen.py` captures both displays as PNG files.
PNG previews do not reproduce physical LED spacing and can overstate the
visibility of low-contrast details.

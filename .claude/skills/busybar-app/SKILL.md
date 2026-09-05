---
name: busybar-app
description: Use when building, changing, or debugging an app for the BUSY Bar LED device in this repo — anything that draws to its displays, plays audio, animates, reads the wheel or buttons, uploads assets, or registers with the barkeep control plane.
---

# Building BUSY Bar apps

The bar is a 72×16 LED strip that punishes assumptions. Every rule below was
learned by breaking something on real hardware; the failure modes are quiet
(a silently ignored draw, a file the firmware won't overwrite six hours later)
rather than loud, so they survive code review and testing and only show up on
the physical panel.

Read `AGENTS.md` first — it covers the repo layout, `connect()`, config, and
privacy. `CLAUDE.md` is only a harness discovery shim. This skill adds the
device-specific failure modes and app-building workflow.

## The laws that actually bite

### 1. Priority is not z-order. Between apps, the last writer wins.

The single most expensive misconception. From the device's own spec
(BUSY Bar HTTP API 25.0.0; fetch the device's schema into the gitignored
`docs/api/openapi.yaml` with `uv run scripts/refresh_docs.py`):

> A draw request is accepted when its priority is greater than or equal to
> the priority of the currently running system app. **Equal-priority requests
> from a different application_name override whatever is on screen.**

So a second app drawing at a higher number does **not** layer on top of the
first. It takes the display. The first app gets it back on its next redraw —
for skystrip that's up to 60 seconds later, or instantly if a flash overlay
fires. Two apps drawing concurrently produce a fight, not a composite.

- **Within one `application_name`**, elements *do* compose, and repeat draws
  merge by element `id`. That is the only real layering you have.
- An **active BUSY/CUSTOM session** refuses outside draws entirely, returning
  HTTP 409 even at priority 95. Numbers do not buy your way in.
- Consequence for design: if your app needs to interrupt another app, you own
  the display for the duration and must hand it back. Don't design a
  "transparent overlay on top of the sky."

### 2. Only three things on an existing element id are honored

Once an element id is drawn, redrawing it can change:

| Field | Honored? |
|---|---|
| `path` (animation/image) | **yes** — skystrip re-paths `id="sky"` every minute |
| text element's string | **yes** |
| `timeout` on an otherwise-identical redraw | **yes** |
| `type` | no — HTTP 400 |
| x / y / width / height | **no, silent no-op** |
| colors, fill | **no, silent no-op** |

The silent ones are the trap: your code runs, the API returns 200, and the
panel ignores you. A metronome that recolors `dot0..dot7` per beat will draw
its first frame and then appear frozen forever.

**To change appearance you need BOTH halves — mint a new id AND retire the
old.** Those are two logical operations. They may be two accepted draws, or a
single accepted `DisplayElements` payload that renews the old ids with a
one-second timeout and then creates the new top-layer ids:

```python
await bb.display_draw(payload(id=f"beat{gen}", ...))       # 1. the new look
await bb.display_draw(payload(id=f"beat{gen-1}", ..., timeout=1))  # 2. retire
```

Elements **accumulate**. A new id does not replace the old one — within an app
they composite, so skipping step 2 leaves both on the panel. Drawing the new
one with `timeout=1` instead of retiring the old is the tempting shortcut and
it is wrong: it stacks, and it can't work anyway (see below). Z-order is pinned
at element *creation*, so the replacement does land on top — that's why
skystrip rotates `rv0..rv99` and bumps `readout_gen`.

This matters most for mutually exclusive opaque transients. A picker drawn
after an event card does not replace the event if their ids differ; when the
picker expires, the older four-second card can resurface. Retire every element
of the interrupted transient and create the bounded new layer in the same
accepted payload when possible. POST order alone is not proof: advance past
the newer element's timeout and assert that the older card cannot reappear.

**A lost response is not a rejected draw.** A transport timeout after POST may
mean the element exists on the device. Keep every possibly committed id in a
small registry even if desired state changes before retry. The next accepted
draw must renew or explicitly retire those ids; abandoning the old id and
minting a new one can leave both visible until timeout. Test commit-then-lost-
response ordering, not just clean HTTP outcomes. Expire registry entries after
their native element timeout: a long device outage must not grow the eventual
recovery payload forever after the elements themselves are already gone.

**`timeout` is WHOLE SECONDS** (API 25.0.0, integer, "time in seconds";
`display_until` takes a unix timestamp, also seconds). It cannot gate anything
faster than 1 Hz. Every sub-second effect — a three-frame flash, a meteor tail,
a beat pulse — must be either an explicit retire draw or, better, frames inside
one `.anim` the device plays itself. Three flash steps drawn 60 ms apart with
`timeout=1` do not flicker; they pile up and all stay lit for a second.

### 3. Assets are cached by path, forever, across processes

The firmware caches by filename and may hold a file open while playing it.

- **Never reuse a path for different bytes.** Overwriting serves stale content;
  writing a path the firmware still holds returns HTTP 508 `"Failed to open
  file for writing"`. Unique timestamp/counter names are appropriate for
  mutable scenes; an immutable version + content digest is appropriate for
  deterministic speech/effects and may be reused for identical bytes.
- **Version mutable generated files** (`sky_142233_7.anim`) and
  `storage_remove` old generations — but keep the live one *plus its
  predecessor*, because an element may still be playing what you just replaced.
- **Sweep orphans at startup.** Your in-memory list of "files I uploaded" dies
  with the process. A crash, a SIGKILL, or a systemd restart abandons files on
  device flash permanently. `apps/skystrip_app/device/assets.py::sweep_stale_assets` is the
  reference: list `/ext/user_assets/<app>`, delete anything matching your
  generated-file pattern, and do it *before* your first draw. Without this,
  orphans accumulate until uploads start failing — 213 files / 40 MB, observed.
- Timestamp-modulo filenames wrap. `int(time.time()) % 1000000` repeats every
  11.6 days; add a monotonic generation counter.
- **Content-addressed does not mean playable.** A resident file can be partial
  or corrupt while retaining the expected pathname and size, and some PLAY
  404 responses mean "unplayable" rather than missing. Repair under a new,
  immutable and discoverable generation (`..._r01.snd`), verify/adopt that
generation, then retire the bad path. Startup adoption must select the
newest valid repair generation so a restart does not rediscover the poison.
If retirement of the corrupt ancestor fails, protect that newest good
successor from ordinary LRU eviction; otherwise a restart can rediscover only
the poison and forget that a repair generation ever existed.

### 4. A 409 means yield — on every endpoint, and reclaim what you uploaded

409 is normal operation, not an error. Log it at debug, back off, retry. Never
escalate priority to fight it.

Three halves people miss:

- **Every endpoint and every code path.** `audio_play` refuses during a session
  the same way `display_draw` does. And it's not enough to guard your main
  loop: the one-shot `--demo` / `--once` CLI paths are exactly what the
  workflow tells you to run on hardware, so an unguarded one greets you with a
  traceback the first time you test during a focus session.
- **A yield must not consume the intent.** Yielding is "do it later", not
  "drop it". If the user turned the wheel and the draw was refused, the new
  tempo must still be pending when the yield lifts — otherwise the app quietly
  lies about its own state. Re-derive desired state after a yield and diff it
  against what's actually on the panel; don't clear the flag on the way in.
- **Reclaim the upload.** If you uploaded an asset and *then* the draw was
  refused, the device never opened that file — delete it, or you leak one per
  attempt. Only on a refusal: after a transport error the draw may have landed,
  and deleting a file the firmware holds is the 508 trap again. (Exception: a
  *bounded, reusable* cache keyed by content — e.g. one click sound per tempo,
  capped — is better than re-uploading. Keep it swept and bounded.)

`busybar_dev/device.py::is_refusal` is the shared test. busylib exposes the code as
`exc.status_code` — `http_status` does not exist and silently never matches.

### 5. The panel is not your preview

- **Brightness deltas under ~30% are invisible.** Verified on hardware. Two
  colors that differ by `0x10` in one channel (6%) look identical, no matter
  how distinct the PNG looks. If a color encodes meaning — severity, state —
  compute the delta and make it ≥30% per channel, or change hue instead.
- **There is no font-metrics API.** Do not invent a glyph-width table to
  compute layout. The one recipe verified on the panel is
  `font="condensed", align="center", x=36, y=8`, and the sanctioned budget is
  **~12 characters** before it overflows 72px (`apps/hello.py:35`). Longer text
  gets `scroll_rate = 1400` (pixels per **minute**) — that is the device-native
  way to move text, and it beats host-side paging, which costs an HTTP POST per
  page over Wi-Fi.
- **Printable ASCII only.** The API's text pattern is `^[\x20-\x7E]+$`
  (API 25.0.0 specifies bitmap ASCII). Any third-party string can
  contain accents: USGS place names, track titles, headlines. `Zürich` and
  `Ōsaka` will **400 the whole draw**. Transliterate or strip before drawing —
  busylib's `sanitize_text` only removes emoji/controls, not Latin-1 accents.
- **Check contrast against your own background too**, not just between two
  meaning-bearing colors. A full-panel wash plus same-color text is a blank
  panel.
- **The LEDs are physically spaced almost their own width apart.** Not a
  guess — the vendor's [technical specifications](https://docs.busy.app/bar/tech-specs)
  give **LED size 1.23 × 1.2 mm,
  pitch 2.2 mm**, so the dark gap is 0.97 mm, 79% as wide as the lit part.
  This is the rule a screenshot cannot teach you, because the framebuffer
  renders as solid adjacent squares while the panel has real gaps.
  Consequences:
  - **A filled background never reads as a surface** — it reads as a haze of
    separated dots, and it drowns whatever you draw on top. Design
    **mostly-OFF**: black is genuinely off, the gaps disappear into it, and
    only lit pixels carry shape. Gradients are wasted; sparse bright shapes win.
  - **Single-pixel details vanish.** A one-pixel-wide feature is an isolated
    dot, not a line. Shapes need 2–3px of body to read as shapes.
  - **Preview with the gaps simulated** before you believe anything:

    ```python
    # 1.23mm LED / 0.97mm dark gap ~= 10 lit / 8 dark in this preview.
    # Center each LED in its pitch; this is qualitative, not a gamma oracle.
    LED, GAP = 10, 8
    PITCH, PAD = LED + GAP, GAP // 2
    out = Image.new("RGB", (W * PITCH, H * PITCH), (0, 0, 0))
    for y in range(H):
        for x in range(W):
            c = frame.getpixel((x, y))
            if c != (0, 0, 0):
                x0, y0 = x * PITCH + PAD, y * PITCH + PAD
                out.paste(c, (x0, y0, x0 + LED, y0 + LED))
    ```

- **There are no spatial subpixels — but brightness is a position channel.**
  The spec says "RGB with common anode": one pixel is one RGB package with
  three colour dies. You control those fully (that *is* an RGB value), but you
  cannot address them spatially — they sit inside 1.23 mm on a 2.2 mm pitch.
  What you *can* exploit is that a dimmer LED has a smaller apparent bright
  core, so **brightness reads as size**, and splitting one mark's intensity
  across the two LEDs it straddles reads as a position *between* them:

  ```python
  base, frac = int(math.floor(x)), x - int(math.floor(x))
  for xi, weight in ((base, 1 - frac), (base + 1, frac)):
      if weight < 0.25:                     # below this it is simply invisible
          continue
      step = 1.0 if weight > 0.8 else (0.65 if weight > 0.5 else 0.4)
      px[xi, y] = tuple(int(c * step) for c in color)
  ```

  The 30% rule above caps what this buys: about **three usable positions per
  pixel, not 256**. Quantise deliberately rather than writing a smooth ramp
  that the panel will crush anyway. It is the only way to show motion slower
  than one pixel per redraw.

### 6. A hand-rolled pixel font must be proportional, and audited by test

Three widths, each forced by the hardware rather than chosen. This rule said
"use 4x5" for a while and that was wrong — it survived because the two glyphs
it breaks are the two nobody types when testing.

**3 columns cannot hold the alphabet.** Measured: `0` and `O` came out
**byte-identical**, `5` and `S` **byte-identical**, and 26 more pairs differed
by exactly one pixel — invisible once the LED gaps are in play. `ACE` read as
`55` on the panel. Slash the zero so it can never be an `O`.

**4 columns holds everything except `M` and `W`,** which need two outer
strokes *and* a centre one. Forced into four they came out 14 cells of 20 with
two **adjacent fully-filled rows** — a filled rectangle, not a letter — and on
a panel this sparse, the densest glyph is exactly the one that loses its
shape. Every lighter 4-wide attempt landed a single pixel from `N`. Give `M`
and `W` five columns; `I` and `1` only need three, which buys the space back.

A single filled row is a legitimate crossbar (`E`, `T`, `Z` all have one).
**Two touching ones are a block.** Assert all of it:

```python
def test_the_font_survives_the_panel():
    for ch, glyph in FONT.items():
        w = len(glyph[0])
        assert len({len(r) for r in glyph}) == 1, f"{ch}: ragged rows"
        full = [i for i, r in enumerate(glyph) if r == "1" * w]
        assert not [i for i in full if i + 1 in full], f"{ch}: solid block"
        ink = sum(r.count("1") for r in glyph)
        assert ink / (w * len(glyph)) < 0.70, f"{ch}: too dense"
    items = [(c, g) for c, g in FONT.items() if c != " "]
    for (a, ga), (b, gb) in itertools.combinations(items, 2):
        if len(ga[0]) != len(gb[0]):
            continue                    # different widths can't be confused
        d = sum(1 for r1, r2 in zip(ga, gb) for c1, c2 in zip(r1, r2) if c1 != c2)
        assert d > 1, f"{a}/{b} differ by one pixel"
```

**Measure width per glyph, and prove the measurement matches the renderer.**
`text_width()` drives every layout decision you have — the scroll box, the
two-label row fit — so if it disagrees with what `_text()` draws, labels
overlap or scroll when they'd have fit. Render to a buffer and compare the
rightmost lit column against the measurement.

Also: a lone `S` beside a digit is the worst case any pixel font faces —
spell the unit (`5SEC`, not `5S`). And a missing glyph is *silently skipped*,
leaving a gap, so assert that every string your app can produce is drawable.

### 5b. Build vocabularies from the feed, not from what you expect

A mapping keyed on values you assumed rather than values you observed fails
**silently**, and it fails on the interesting cases.

Concrete: the DSN feed's `band` attribute was mapped for `S`, `X` and `Ka`.
The feed also emits **`K`** — which is JWST at 28 Mbit, the fastest link on the
network. It fell through to the default colour and to no narration at all. No
error, no log line, nothing on screen that looked wrong. It was found by
running `--dry-run` against the live feed for an unrelated reason.

Before writing a lookup keyed on a remote field:

```bash
curl -s <feed> | grep -o 'band="[^"]*"' | sort | uniq -c
```

Then pin what you found in a test — not the mapping's own keys, which is
circular, but the vocabulary the source actually publishes. And normalise:
`Ka`, `KA`, ` ka ` and `Ka-band` are one value, and only one of them will be
the one you happened to type.

**Validate before state, watermark or cache mutation.** Syntax is not semantic
validity: timestamps need magnitude and future-skew bounds; coordinates and
rates need finite/domain checks; JSON caches need a versioned shape and a
nonnegative age. Build a complete candidate, validate it, then swap it in
atomically. A rejected enrichment must leave the last good state intact.

**Unknown is a product state, not permission to invent a default.** If a
truth-bearing config source is missing or partial, omit the claim or show an
explicit unknown. Never turn missing longitude into zero, an unknown device
size into the common size, or repeated source records into a sum unless the
source contract actually establishes those meanings.

**Remote strings and collections get semantic budgets at ingestion.** Bound
identity length, record count and cache payload size before they can drive
frame counts, LCMs, filenames, logs or speech. Do not silently slice an
overlong identity; use a documented overflow/unsupported token or a stable,
explicit digest fallback, then test that work is bounded independently of
input length.

**Time-sensitive narration needs an expiry.** A static present-tense sentence
about a mission, outage, office-holder or other changing fact must carry its
primary source, review date and review-by date. After expiry, fail closed to a
phase-neutral identity/purpose sentence until the fact is revalidated.

The same rule covers the fields you *don't* use. A field parsed into every
record and read nowhere is dead weight that survives because deleting it feels
riskier than leaving it — `tz_offset_s` sat there for weeks after the globe
stopped needing it. Delete it; the test suite is what makes that safe.

### 6a. An icon next to text is read as text unless you stop it

A small glyph set beside a label does not get read as a picture by default —
it gets read as another character in the string. Two things fix it, and the
first matters more than the second:

**Give it its own hue.** Drawn in the label's colour, an icon joins the word.
Drawn in a colour of its own, it separates instantly. This is worth more than
any amount of redrawing: the same 6x6 antenna shape read as a picture in warm
ink and as a letter in the digits' blue, side by side, same pixels.

**Break the symmetry that letters have.** A cup on a *centred* stem is the
letter `Y` at every size that fits on this panel — no amount of detail in the
cup rescues it. Running the mast off-centre turns the identical pixel count
into an object. Related traps, all found by drawing the candidate next to real
text and looking: a ring beside digits reads as `0`, a shape with a flat top
and a diagonal reads as `2`, and anything with a centred vertical stroke and
two arms reads as `T` or `Y`.

The test is never the glyph alone. Render it **beside the actual text it will
live next to** and judge it there — an icon that looks fine in isolation is
the normal way this ships broken.

If the icon can carry a number you already have, let it. An antenna that leans
at the pass's real elevation costs the same pixels as one that doesn't, and
turns decoration into a readout. Keep the steps coarse — three positions, not
five — for the same reason texture needs 30% contrast.

### 6b. Two labels at opposite ends of a row is not a collision guarantee

Anchoring one label left and one right *looks* safe and isn't: it only holds
while both stay short. `18:43` beside `246KBPS` wants 61 px of a 56 px row and
the two overlapped outright on the panel — unreadable, and it shipped, because
every value tried during development happened to fit.

Fit the row explicitly. If both labels do not fit, give one a semantic box and
marquee its **complete** value, alternate the labels, or use a deliberately
defined abbreviation whose meaning survives. Never manufacture an
abbreviation by slicing arbitrary text: the generic `[:4]` fallback below was
how `UPLINK` became `UPLI` on the Distance view.

```python
def row_plan(left, right, room, gap=3):
    if text_width(left) + gap + text_width(right) <= room:
        return "static", left, right
    right_room = room - text_width(left) - gap
    if right_room >= 5:       # caller scrolls the full `right` through this box
        return "marquee", left, right, right_room
    return "alternate", left, right
```

A planned compact form such as `18KBPS` → `18K` can be valid when the unit is
already unambiguous in that exact view; `UPLINK` → `UPLI` is not. Test the
whole matrix of values your labels can actually produce, not the one on screen
while you were building.

### 6c. Containment is not completeness

A clip test that proves no pixel escaped its box can still approve an
amputated label. `NO LINK` once occupied 33 pixels but was drawn through a
31-pixel clip: every emitted pixel was correctly contained, while 40% of the
`K` disappeared. A call spy also passed because `_text("NO LINK", clip=...)`
really was called. The bug existed only in the final pixels.

For every non-scrolling label promised in full:

1. Render the intended label **without clipping** into an independent scratch
   image and measure its complete ink bounding box.
2. Prove that box fits its declared semantic region. Remember that this repo's
   `_text` clips use inclusive endpoints, while `PIL.Image.crop((x0, y0, x1,
   y1))` uses exclusive right/bottom endpoints. Centralise that conversion;
   do not sprinkle `+1` and magic edge coordinates through tests.
3. Compare the expected ink with the **final composed frame**, not an
   intermediate call or layer. Do this across every animation frame, page and
   freshness/status variant where the label should remain unchanged.
4. If the full ink cannot fit, explicitly marquee, alternate, use an approved
   abbreviation, or omit the field. Clipping is a containment tool, never a
   degradation policy.

Use layout constants for the box (`NETWORK_X0 - 1`), not a duplicated literal
(`39`). Include normal, empty, delayed/stale, maximum-value and long-label
fixtures in the matrix; edge-heavy glyphs such as `M`, `W`, `K` and
right-aligned suffixes expose off-by-one errors fastest.

For a marquee, go further than checking that x positions change. Compare each
viewport crop to an independently rendered full text strip, prove every source
column becomes visible during the cycle, and check the last→first seam. For an
animation, declare which semantic regions must be static and assert those
final-frame crops are invariant; separately assert that nominated motion
regions really change.

### 6d. The scene must survive a viewer who hasn't read your code

Every object in a scene needs a real-world referent and that referent's
physics. The failures here are not rendering bugs — the pixels land exactly
where the code puts them — they are *meaning* bugs, and no declared check
catches them because you'd have had to think of the absurdity to declare
against it. Two shipped the same day:

- **Floating trees.** Crowns with 1px trunks: the trunk vanished (law 5 —
  one-pixel features always do) and the crowns read as green blobs hovering
  in the sky. The renderer drew a trunk; the panel showed none; every
  "crowns are distinct" check passed.
- **The train-eating grain elevator.** A 3px tower existed so trains had
  something to vanish behind — scenery invented to solve the code's
  occlusion problem, not because a prairie horizon needed it. Viewers saw a
  magician's cabinet swallowing 25-car freights. The honest occluders were
  already in the frame: the screen edge and the HUD card.

Ask, while actually looking at the composed render: *what holds this up?
what could genuinely hide that? would a stranger name this object
correctly?* If an element exists because your code needed it rather than
because the place would have it, delete it and use what the scene already
owns — terrain, the frame boundary, the HUD card. The mechanical version of
this review is the `busybar-viz` skill's naive-viewer pass: hand the
previews to a reader with no context and treat everything they must be told
as a defect.

### 7. One .anim is one clock — don't encode a variable in its duration

Everything baked into a loop shares that loop's playback rate. If you set the
loop duration from some quantity (a distance, a speed, a queue depth), then
**every other animated thing inherits it**: text scrolls at whatever rate the
data dictates, a rotating globe spins faster when a nearer object is selected.

Keep the loop duration **fixed** and encode the variable as *spacing* or
*count* within the frames instead. A chain of evenly-spaced marks advancing
exactly one spacing per loop is seamless at any speed, and the spacing carries
the quantity:

```python
spacing = track_px * LOOP_S / crossing_s   # far = tight chain, near = one mark
# each mark's speed is spacing / LOOP_S == track_px / crossing_s, the truth
```

If something genuinely must move at its own independent rate, it belongs in a
device **text element** with `scroll_rate` (px/minute), not in your frames.

**Never take the LCM of source-driven clocks without a hard budget.** Three
ordinary coprime marquee periods can multiply into thousands of eagerly
allocated PIL frames. Validate source string/count bounds first, set maximum
frames/bytes/duration before allocation, and use independent modulo clocks or
bounded pages. If several baked marquees share one asset, each must complete
an integer number of its own cycles at or below the readability-speed ceiling;
one page's long label must not speed up or slow down an unrelated label.

**A cache key is a render contract.** Derive it from the immutable semantic
render plan, or test the invariant that equal keys produce byte-identical
frames. Include presence/absence flags even when rate, band or another scalar
is unknown; "no receive record" and "active receive with unknown rate" often
share the same scalar values but draw different pixels.

## Choosing a priority

| What | Priority |
|---|---|
| stub / poweroff | 0 |
| any standard built-in app (the idle floor) | 10 |
| skystrip (the ambient foreground) | 30 |
| an interrupting app that must beat the foreground | 40–60 |
| active BUSY/CUSTOM work session | 90 — **refuses everything else** |

Clear the floor of 10 and you're on screen. Beyond that the number only decides
who wins a tie against the *system* app, not against another user app — see law
#1. Nothing you can set punches through a focus session.

## Hardware input: wheel, buttons, switch

Input arrives on the same WebSocket as device status: `bb.stream_status_ws()`
(`apps/skystrip_app/input.py::listen_buttons` is the working reference). Traps, all
verified on-device:

- **proto3 omits zero values.** An absent field *is* the zero enum, so an empty
  `button_event {}` means `OK` + `PRESS`. Test for absence, not for a value.
- Enums may arrive as **names or ints** depending on decoding — handle both.
- **RELEASE events arrive too**, so a naive handler fires twice per press.
- **One felt click of the wheel is ONE count.** Verified twice, on hardware.
  This line used to say "accumulate raw counts into detents", which reads as
  an invitation to pick a divisor — a later app picked 4, so three of every
  four clicks silently did nothing and the dial felt broken. Keep the
  accumulator (it costs nothing and handles a burst), but the threshold is
  **1**. Before inventing any hardware constant, grep the other apps: this one
  was already measured and written down in `apps/skystrip_app/limits.py`
  (`ENC_COUNTS_PER_DETENT`).
- **Never re-render the scene per detent.** An `.anim` upload is ~80 kB and
  about a second round trip; at one per click the wheel feels dead and then
  lurches through a backlog. Use *reveal-on-stop*: draw an instant read-out
  with device Text/Rectangle elements while the wheel moves, and commit the
  real scene only once it has rested (~0.6 s). Both `apps/dsn_app/device/display.py::draw_picker`
  and `apps/skystrip_app/device/scrubber.py::draw_scrub_readout` are the reference.
- **Coalesce per message, not per event.** One websocket message can carry
  several updates; drawing inside that loop issues an HTTP POST per event.
  Collect, then draw once at the end of the message.
- While a read-out is up, **pause anything that moves the selection** (an
  auto-rotate timer will shift it out from under the user mid-turn), and tick
  the main loop faster (~0.15 s) so the commit doesn't lag a whole second
  behind the last click.
- The **START button is free to map** only while the slider is OFF — with a
  BUSY/CUSTOM session running, the firmware claims it (confirmed live).
- A clean websocket close ends the read loop **without raising**. Back off on a
  short-lived session or you get a reconnect hot loop.
- **External record order is not selection identity.** Reconcile an existing
  selection by exact key, then a unique semantic continuation, then a stable
  deterministic fallback. Property-test every permutation of an equivalent
  source snapshot; a feed reorder must not change what the user is watching.

## Audio

`.snd` is headerless raw PCM s16le mono 44100 Hz — about 88 kB per second, so
watch what you write to flash. `AudioPlayRequest` carries only `path` /
`stock_path`: **there is no loop flag and no scheduling**. Repeating sound means
re-triggering from the host (skystrip's siren re-fires every 9s for a 10s
clip — see SIREN_RETRIGGER_S and SIREN_SECONDS, which are the numbers that
matter rather than these), which makes host-side jitter audible — don't try to
build a tight sequencer out of it.

**Opposite device effects need one generation-owned sequencer.** A lock alone
does not make STOP/PLAY safe when the client retries transport failures: an
older STOP can still land after a newer PLAY. Capture the intent generation
before each await, serialize both operations through one I/O lock, and reject
obsolete generations before sending. A deterministic test should delay the
first STOP response until after the newer PLAY would otherwise be accepted.
Shutdown belongs to that same sequencer: invalidate PLAY generations, settle
or close any in-flight transport, then send the final STOP under the I/O lock.
Never bypass ordering with a direct cleanup STOP that an older PLAY can outlive.

**Speech costs about as long as the speech lasts.** Kokoro on a Pi 5 runs at
roughly **1× realtime** — measured: a 12-second line took 10.4 s warm, plus
~1.4 s to open the model. So synthesising on a keypress is not a slow path,
it is a broken one: the app has moved on to a different scene before the audio
starts and then narrates something no longer on screen. Two halves fix it:

- **Bake ahead.** Synthesise whatever is on screen *before* it is asked for,
  and cache it **on the device** — assets persist per `application_name`, so a
  hit costs nothing and survives a restart. Name the file after a hash of the
  line's own text, which turns the firmware's cache-by-path (normally the trap
  in law 3) into the mechanism: identical text is identical bytes, nothing is
  ever overwritten, so the 508 cannot fire.
- **Put the voice in the key, and in the filename.** Keyed on text alone,
  changing the voice keeps serving the old narrator's recordings for as long
  as the cache holds them — you switch voices and hear no difference. And a
  bare hash is unsweepable: make the narrator visible in the name
  (`voice_afnova_0f76f95857.snd`) so startup can reclaim another voice's files
  instead of stranding ~1.8 MB each on flash forever. Treat anything you
  cannot recognise as yours as reclaimable, or the *next* naming change
  strands a cache too.
- **Hold the display for the length of the narration**, then release it — but
  never release a lock the user set themselves.

**Long CPU work must not ride the default executor.** `asyncio.run()` joins
its threads on the way out, so a 20-second synth in flight makes the app
ignore SIGTERM until it finishes — long enough for barkeep to SIGKILL it,
which skips the handler that clears the panel and leaves the last frame stuck
there. Run it on a daemon thread and settle a future instead:

```python
def work():
    try:    value, error = synth_snd(text, VOICE), None
    except BaseException as exc: value, error = None, exc
    try:    loop.call_soon_threadsafe(_settle, fut, error, value)
    except RuntimeError: pass          # loop already closed: shutting down
threading.Thread(target=work, daemon=True).start()
```

Tests that send signals to a child must wait for an explicit readiness marker
written after handlers are installed. Process creation or a supervisor's
"running" status proves only that the child exists, not that SIGTERM cleanup is
ready to be exercised.

**Check that the parameter you passed is the one that gets used.** `synth_snd`
took a `voice` argument and then read `SKYSTRIP_VOICE` from the environment
instead, silently discarding it — so an app asking for `af_nova` was narrated
by another app's voice, and nothing failed. A passing call is not proof the
value arrived.

## Registering the app

barkeep is the control plane; an app that isn't in `apps.toml` cannot be run,
configured, or seen.

```toml
[myapp]
kind = "foreground"          # owns the display  |  "background" = draws on events
entrypoint = "apps/myapp.py"
description = "One line, shown on the card in the web UI"

[myapp.config.MYAPP_STATION]
description = "Shown under the field in the editor"
default = "9414290"
type = "number"              # text (default) | number | email
# choices = ["a", "b"]       # one-of picker
# type = "multiselect" + choices = [...]   # subset, stored comma-separated
```

Every config key becomes an environment variable in your process. Read them
with an `or`-default (`os.environ.get("MYAPP_TZ") or "UTC"`), never
`get(k, default)` — a key can arrive explicitly blank, and `float("")` at
import crash-loops the app with the display dark.

**Declare every ordinary key you read.** An undeclared key still works
(children inherit the environment) but is invisible and unsettable in the web
UI — normally a bug on a headless Pi. The narrow exception is a credential or
credential-bearing URL while Barkeep has no secret-field/redaction contract:
document it in `.env.example`, keep it only in owner-readable `.env`, scrub it
from offline workers, never log it, and explain the exception in `AGENTS.md`.
Do not expose a secret through `apps.toml`, because Barkeep's LAN config GET/UI
returns declared values verbatim. If the value is neither ordinary config nor
a genuine secret, make it a constant.

If `apps.toml` or `barkeep/` isn't in your tree, you're on a commit that
predates the control plane — check `git log --oneline -5` and rebase before
designing around a topology that no longer exists.

Given law #1, `kind = "background"` means *interrupts*, not *overlays*. A
background app should draw rarely, briefly, and let its elements time out.

## Workflow

1. `uv run scripts/new_app.py <name>` copies the config → payload → send
   template and registers the app in `apps.toml` in one collision-safe step.
   The result is a ~140-line **synchronous** hello-world with a `--dry-run`
   flag. It has no async loop and no signal handling, so copy those from
   `apps/skystrip_app/runtime.py` (`run()` and its `finally`) for anything long-lived.
2. `uv run apps/<name>.py --dry-run` before ever touching the device.
3. Give raster/native-asset visuals one pure, deterministic production seam.
   It returns ordinary PIL frames and must be used by the real asset path; app
   code never imports `busybar_viz`, and the seam never performs device or
   network I/O. Plan the app-specific adapter before writing it:

   ```bash
   uv run busybar-viz scaffold myapp \
     --renderer apps.myapp:render_visual --display front --json
   ```

   Review the output, rerun with `--write`, make the two printed explicit
   registry edits, then extend the generated adapter with deterministic
   controls, semantic inputs, regions, independent full-ink references, and
   meaningful checks. Do not create a visualizer-only facsimile of native
   firmware Text/Countdown elements.
4. Add the `apps.toml` entry and declare every ordinary config key. Document
   every key in `.env.example`; keep only genuine secrets out of `apps.toml`
   under the narrow redaction exception above.
5. Write tests for the host-side logic (`tests/`); the suite runs with no bar
   and no network. Pure functions — parsing, resolution, layout math — are
   where the bugs live. Render the registered scenario, inspect its native and
   gap-aware evidence, and compare the artifact with the prior accepted SHA:

   ```bash
   uv run busybar-viz run myapp/default --json
   uv run busybar-viz compare BEFORE_SHA AFTER_SHA --json
   ```

   A generated gap file is not proof it was inspected. Follow the
   `busybar-viz` skill and `docs/busybar-viz.md` when making visual claims.
6. Run it against the real bar and capture both displays with
   `busybar_dev.screen.save_screens(bb, ".")`. Label that evidence
   **framebuffer-captured**; separately inspect the physical panel before using
   **hardware-observed**. The framebuffer and the panel frequently disagree.
7. `./deploy/ship.sh` deploys committed work to the Pi and restarts barkeep.

**Stop the Pi's copy before testing yours** (`curl -X POST
http://<pi>:8080/api/foreground -H 'content-type: application/json'
-d '{"app": null}'`). Two writers fight, per law #1. Relatedly: your startup
sweep is app-scoped and cannot tell your instance from another's, so guard it
on one-shot paths — `if once: skip the sweep` — or a laptop test will delete
the assets the Pi's copy is playing.

**When no bar is attached** (it happens — USB unplugged, Wi-Fi dropped), state
exactly what the offline evidence established: renderer-verified pixels and,
only when actually inspected, a gap-previewed spacing simulation. Say plainly
that no framebuffer capture or physical-panel observation happened, list the
remaining device claims (real contrast, apparent size, animation feel, native
composition and on-device legibility), and leave runnable commands for the
live check. Never let "the tests pass" stand in for "it looks right on the
panel."

## Motion belongs to the device

Encode one `.anim` and let the firmware loop it (`busybar_dev/anim.py`,
`AnimationElement`, `loop=True`). A host-driven redraw loop burns the network,
stutters over Wi-Fi, and fights the priority model on every frame.

Because the device owns playback, **every periodic motion must complete a whole
number of cycles per loop**, or the seam visibly jumps. Frame rate is per-app,
not global: skystrip's ambient scenes run 40 frames at 5 fps, but its backroads
traffic needs 80 at 10 fps to read as motion. Faster costs render CPU and
upload size — spend it only where it shows.

## Common mistakes

| Mistake | What happens | Instead |
|---|---|---|
| "I'll draw over the other app at priority 40" | Your draw *replaces* theirs; theirs replaces yours a minute later | Own the display for a bounded window, then release |
| Recoloring a stable element id per frame | API 200, panel frozen on frame one | New element id per appearance change |
| `sky_a.anim` / `sky_b.anim` alternation | 508 after a restart — the firmware still holds one | Versioned filenames + reap |
| Versioned names but no startup sweep | Flash fills with orphans over months | Sweep `/ext/user_assets/<app>` before first draw |
| Guarding `display_draw` for 409 but not `audio_play` | Silent stall during focus sessions | One `_is_refusal` helper on every device call |
| `getattr(exc, "http_status", None) == 409` | Never matches; the quiet branch never runs | `exc.status_code` |
| Colors 10% apart encoding severity | Indistinguishable on the panel | ≥30% per channel, or change hue |
| Inventing font metrics to lay out text | Clipping or dead gutters | `condensed`, center, x=36, y=8 — screenshot anything else |
| Proving a clipped label stays inside its box | A suffix can vanish while the containment test passes | Render the full unclipped ink, prove it fits, then compare the final composed pixels in every frame |
| Slicing an arbitrary label to make a row fit | `UPLINK` becomes the meaningless `UPLI` | Marquee the complete label, alternate it, or use an explicit semantic abbreviation |
| Minting a new id but not retiring the old | Both stay on the panel; an older opaque card can resurface | Retire old ids and create the new bounded layer, preferably atomically in one accepted payload |
| Testing only the POST order of transient cards | New card appears first, then the old one resurfaces after its timeout | Advance through the newer timeout and assert every interrupted old id was retired |
| `timeout=1` used as a frame delay | Whole seconds — sub-second effects pile up | Frames in one `.anim`, or an explicit retire draw |
| Drawing a place name / title straight from a feed | `Zürich` 400s the whole draw (ASCII-only) | Transliterate before drawing |
| Yield on 409 that clears the pending change | App shows one thing, does another | Re-derive desired state when the yield lifts |
| Per-process counter in filenames | Restarts at 1 every launch; collides after a failed sweep | Timestamp **and** counter |
| Filled gradient background | Reads as a haze of dots on a spaced panel | Mostly-off: black, plus sparse bright shapes |
| A fixed-width pixel font | 3 wide: '0'=='O' and '5'=='S', byte-identical. 4 wide: M and W become filled rectangles | Proportional — 5 columns for M/W, 3 for I/1, 4 for the rest. Slashed zero. Assert no collisions (law #6) |
| Loop duration set from your data | Text scroll and every other motion inherit it | Fixed loop; encode the variable as spacing |
| Picking your own encoder-counts-per-detent | One count IS one detent; a divisor of 4 eats 3 of every 4 clicks | grep the other apps for the measured constant |
| Re-rendering the scene on every wheel detent | ~80 KB and ~1 s per upload; the dial feels dead | Instant text read-out, commit the scene on rest |
| Two labels anchored to opposite ends of a row | Not a collision guarantee — they overlapped on the panel | Plan the row; marquee, alternate, or use an explicit semantic abbreviation |
| 4-wide `M` and `W` | Two adjacent filled rows: a block, not a letter | 5 columns for those two; the font is proportional |
| Synthesising speech on a keypress | ~1x realtime; the scene has changed before it plays | Prepare ahead, cache on device by text+voice hash |
| Heavy CPU on the default executor | asyncio.run() joins it, so SIGTERM is ignored -> SIGKILL | Daemon thread + call_soon_threadsafe |
| Trusting that an argument you passed was used | `synth_snd` ignored its `voice` param for the env var | Assert the value arrives, not just that it ran |
| Copying the newest-looking pattern out of skystrip | It is thousands of lines and some of it is superseded | Check `git log -S` before copying an approach |

## Red flags — stop and re-read the laws

- You're about to write "draws on top of" or "overlays" about another app
- You're changing x/y/color on an id you already drew
- A mutable asset path could receive different bytes; use either a unique
  timestamp/counter or an immutable version+content digest
- You can't name where orphaned files get cleaned up
- You're catching an exception from a device call without asking "is this a 409?"
- You're about to claim it looks right without a screenshot
- You changed pixels and your evidence is an image you generated
  yourself — a scratch PNG has no SHA, so it cannot be gap-previewed
- A region carries text or a meaning-bearing colour and no
  `region.contrast_floor` check covers it
- Your preview draws pixels as solid adjacent squares (simulate the gaps)
- A static/status label has `clip=` but no full-ink final-frame regression
- Your label test spies on a draw call but never inspects the composed pixels
- Two glyphs in your font might be identical and you haven't asserted otherwise
- Your .anim's duration depends on the data it's displaying
- You invented a hardware constant instead of grepping for a measured one
- Anything interactive waits on an asset upload
- Two labels share a row and you only checked today's values
- An object exists in your scene because the code needed it (an occluder, a
  mask, an anchor), not because the place would have it
- You verified the properties you designed and called the picture verified

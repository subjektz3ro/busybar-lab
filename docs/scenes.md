# Adding a scene to skystrip — the checklist

Distilled from building house, skyline, and lakefront (and deleting
cyberpunk). Walk every line before calling a scene done.

## Wiring
- [ ] Add the name to `SCENES` (START-cycle order, `--scene` flag, and
      the Time Machine's scene-mismatch guard all follow automatically).
- [ ] Scene branch in the render pipeline draws AFTER sky/stars/sun/
      moon/clouds and BEFORE precipitation (rain falls in front of you).
- [ ] Scene accent in `_ambient_mood()` (top-strip color; USB topology).
- [ ] Fireflies/smoke-class layers: extend their scene gates if they
      belong in the new scene.

## Palette laws (the LED panel is the judge, not the preview)
- [ ] Brightness deltas under ~30% vanish on the panel — design detail
      at ≥30% contrast; previews flatter subtlety.
- [ ] Hue-separate adjacent regions (sky vs. structures vs. water);
      full-saturation colors beat tasteful muted ones under LED gamma.
- [ ] Daylight rule: foreground colors by day are SUNLIT MIDTONES,
      warm-leaning (walls ~(118,104,96) territory). A scene built at
      night will default too dark — always test `--at 10:30`.
- [ ] Night rule for lit scenes (cities): the median pixel should be
      lit. Dark-nature scenes earn darkness, but silhouettes must still
      separate from the sky.

## The ambient light model
- [ ] EVERY structure color routes through
      `_shade(_lerp_rgb(night, day, daylight), amb)` — golden hour
      warms, overcast flattens, storms gloom, all for free.
- [ ] Self-luminous pixels (windows, lamps, neon, fire, glows) are
      NEVER shaded — and they turn ON in storm-dark daytime.

## Motion (cozy pacing)
- [ ] One loop = 8s / 40 frames; every periodic motion wraps an INTEGER
      number of cycles so the loop is seamless.
- [ ] Nothing zips. Slow drifts, breathing lamplight, occasional
      one-frame accents.
- [ ] Every motion is gated on REAL data: wind sway needs ≥8 km/h (and
      leans downwind via wind_dir), fireflies need summer nights >15°C,
      smoke needs cold, fog needs humidity/visibility. No decoration
      that lies about conditions.

## Weather & celestial interplay
- [ ] Scenes render the fused live snapshot (station observations when
      available, otherwise validated model fields). Past precipitation remains
      observation-only; forecast frames are explicitly model output.
      Warnings/alerts are a separate overlay layer, never scene art.
- [ ] Check the scene under: storm, rain, snow, fog, deep cloud. Decide
      emissive gating deliberately (does a campfire survive rain?).
- [ ] Sun/moon arcs: does this scene need a custom arc (skyline rides
      low, lakefront hugs the water)? Moon stays BEHIND foreground art.
- [ ] Moonlight pass: silver rim + ground pool scaled by real lit
      fraction × clear sky; killed by cloud/storm.
- [ ] Status corner: white clock/temp marks the live view; amber is reserved
      for the Time Machine. It bakes on top at top-left, so keep key art out
      from under it or accept being overwritten.

## Seasons
- [ ] Month-driven variants: foliage green/autumn/bare-winter at
      minimum. Evergreens may opt out, deliberately.

## Test matrix (all `--preview`, no device needed)
- [ ] `--at 10:30` (full day) · `--at 19:45` (dusk) · `--at 23:00`
      (night) · `--at 05:30` (dawn)
- [ ] `--storm` · `--rain` · `--snow --temp -3 --month 1` (winter)
- [ ] `--wind 30 --winddir 270` (sway + lean) · `--month 10` (autumn)
- [ ] `--cloud 1.0` (no celestial bodies, flat light)
- [ ] Then ON-DEVICE: gamma and physical-panel review. Screenshots
      lie about subtlety; the panel is the arbiter.

## Firmware etiquette (usually free, verify anyway)
- [ ] Scene change triggers a timeline rebuild — scrub the Time Machine
      once after shipping.
- [ ] New assets (sprites) live in code or `apps/assets/`; anims upload
      under versioned filenames (path-cached firmware).

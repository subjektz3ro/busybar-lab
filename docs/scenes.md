# Skystrip scene checklist

Use this checklist when adding or modifying a Skystrip scene. It summarizes
requirements established by the house, skyline, and lakefront scenes.

## Wiring
- [ ] Add the name to `SCENES` (START-cycle order, `--scene` flag, and
      the Time Machine's scene-mismatch guard all follow automatically).
- [ ] Scene branch in the render pipeline draws AFTER sky/stars/sun/
      moon/clouds and BEFORE precipitation (rain falls in front of you).
- [ ] Scene accent in `_ambient_mood()` (top-strip color; USB topology).
- [ ] Fireflies/smoke-class layers: extend their scene gates if they
      belong in the new scene.

## Panel contrast and palette
- [ ] Brightness deltas under ~30% vanish on the panel — design detail
      at ≥30% contrast; previews flatter subtlety.
- [ ] Hue-separate adjacent regions (sky vs. structures vs. water). Muted
      differences may disappear under LED gamma.
- [ ] Daylight rule: foreground colors by day are SUNLIT MIDTONES,
      warm-leaning (walls ~(118,104,96) territory). A scene built at
      night will default too dark — always test `--at 10:30`.
- [ ] For lit city scenes, keep the median pixel illuminated. For dark natural
      scenes, maintain visible separation between silhouettes and sky.

## Ambient light
- [ ] Route structure colors through
      `_shade(_lerp_rgb(night, day, daylight), amb)` for consistent golden-hour,
      overcast, and storm adjustments.
- [ ] Do not shade self-luminous pixels such as windows, lamps, neon, fire, or
      glows. Enable them during dark storm daytime.

## Animation timing
- [ ] One loop = 8s / 40 frames; every periodic motion wraps an INTEGER
      number of cycles so the loop is seamless.
- [ ] Use slow drifts, gradual brightness changes, and occasional one-frame
      accents.
- [ ] Every motion is gated on REAL data: wind sway needs ≥8 km/h (and
      leans downwind via wind_dir), fireflies need summer nights >15°C,
      smoke needs cold, and fog needs humidity/visibility. Do not show
      weather-dependent motion when its condition is absent.

## Weather & celestial interplay
- [ ] Scenes render the fused live snapshot (station observations when
      available, otherwise validated model fields). Past precipitation remains
      observation-only; forecast frames are explicitly model output.
      Warnings/alerts are a separate overlay layer, never scene art.
- [ ] Test under storm, rain, snow, fog, and dense cloud. Define whether each
      emissive effect remains active during precipitation.
- [ ] Set scene-specific sun and moon arcs where required. The Moon stays
      behind foreground art.
- [ ] Moonlight pass: silver rim + ground pool scaled by real lit
      fraction × clear sky; killed by cloud/storm.
- [ ] Status corner: white clock/temp marks the live view; amber is reserved
      for the Time Machine. It bakes on top at top-left, so keep key art out
      from under it or accept being overwritten.

## Seasons
- [ ] Implement month-based green, autumn, and bare-winter foliage variants.
      Evergreen behavior may remain unchanged.

## Test matrix (all `--preview`, no device needed)
- [ ] `--at 10:30` (full day) · `--at 19:45` (dusk) · `--at 23:00`
      (night) · `--at 05:30` (dawn)
- [ ] `--storm` · `--rain` · `--snow --temp -3 --month 1` (winter)
- [ ] `--wind 30 --winddir 270` (sway + lean) · `--month 10` (autumn)
- [ ] `--cloud 1.0` (no celestial bodies, flat light)
- [ ] Inspect gamma and legibility on the physical panel. Preview images can
      overstate subtle contrast.

## Firmware validation
- [ ] Scene change triggers a timeline rebuild — scrub the Time Machine
      once after shipping.
- [ ] New assets (sprites) live in code or `apps/assets/`; anims upload
      under versioned filenames (path-cached firmware).

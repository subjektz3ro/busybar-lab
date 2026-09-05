# Known issues

## Firmware 1.2.3: Auto brightness can wash out dark scenes

A dark Skystrip scene was observed on the physical panel as a nearly uniform
gray field under **Auto** brightness on firmware **1.2.3**. Switching to fixed
50% restored contrast and scene detail in a controlled, reversible test. The
symptom was reported before the app refactor; neither a weather-feed change
nor a renderer change was needed to restore contrast.

This establishes a brightness-dependent hardware symptom and a working manual
mitigation on one device, not a proven firmware root cause or a universal
minimum brightness. A framebuffer capture or offline preview cannot establish
the physical panel's contrast. Firmware Auto and display-driver behavior remain
outside the renderer's pixel-baseline checks.

### Automatic mitigation in these apps

Skystrip and DSN now check brightness once at startup, including `--once`:

- Only an exact firmware version of `1.2.3` with brightness set to `auto`
  triggers a change: **fixed 35% by default**.
- Existing manual levels, other firmware versions and unknown version/mode
  values are left alone. Merely connecting, capturing a framebuffer, rendering
  a preview or running a dry run does not apply the workaround.
- Readback is verified with bounded polling because a successful setting
  request can precede the updated value. Failure or a five-second startup
  deadline warns and lets the app continue; shutdown cancels the operation.
- The setting is **device-wide and persistent**, including after app exit.
  It disables ambient-light adjustment, not animation or weather effects.
  It is not periodically enforced and does not change the renderer's pixels.

The default is a practical mitigation, not a firmware gamma repair. If a
particular panel still washes out at 35%, try a higher manual level; 50% was
the level used in the physical contrast-restoration test.

### Configuration and reversal

`BUSYBAR_AUTO_BRIGHTNESS_FALLBACK` is shared device configuration in `.env`,
not a per-app Barkeep editor setting:

```dotenv
BUSYBAR_AUTO_BRIGHTNESS_FALLBACK=35
```

Blank or unset means 35; an integer from 1 through 100 selects another fallback
level. Invalid values warn and leave the device unchanged. Editing the fallback
does not replace an already-manual level: change that level on the device, or
select Auto before the next app start to apply the new fallback.

To restore normal Auto operation, first set:

```dotenv
BUSYBAR_AUTO_BRIGHTNESS_FALLBACK=off
```

Restart Barkeep (or restart a directly launched app with the updated environment),
then select Auto in the device's brightness settings. Disabling the workaround
alone does not undo an earlier persistent manual setting. After a firmware
upgrade, restore Auto manually if desired; apps do not guess whether an existing
manual level was chosen by the operator or by this workaround.

Versioned upstream references: [firmware 1.2.3 release](https://github.com/busy-app/busybar-firmware/releases/tag/1.2.3)
and [brightness/Auto controls](https://github.com/busy-app/busybar-firmware/blob/1.2.3/applications/services/brightness_control/cli/cli_command_display.c).
The application workaround does not claim to repair the upstream Auto algorithm.

Regression coverage lives in `tests/test_brightness_workaround.py`: both real
app startup paths, exact version/mode gating, opt-out and invalid configuration,
manual-level preservation, delayed readback, lost responses, deadlines,
cancellation, and the installed client's HTTP request contract.

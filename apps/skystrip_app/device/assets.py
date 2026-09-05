"""Skystrip device / assets."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import re

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app.audio import report_assets as _audio_report_assets
from apps.skystrip_app.render import alerts as _render_alerts
from busybar_dev import anim
from busybar_dev.weather_alerts import Alert


async def ensure_alert_asset(
    bb,
    state: _model.SkyState,
    alert: Alert,
    generation: int,
) -> str | None:
    """Upload one immutable marquee and discard only definitely unused work."""
    key = _render_alerts._alert_asset_key(alert)
    if state.alert_asset_file is not None and (
        getattr(state, "alert_asset_key", None) in (None, key)
    ):
        # ``None`` supports deterministic fakes that inject a provisioned path.
        return state.alert_asset_file
    frames = _render_alerts.alert_animation_frames(alert)
    blob = anim.encode_anim(frames, fps=_limits.ALERT_ANIM_FPS)
    digest = hashlib.sha256(blob).hexdigest()[:16]
    filename = f"alert_{digest}.anim"
    try:
        if filename not in state.alert_files:
            await bb.assets_upload(_limits.APP_NAME, filename, blob)
        if (
            state.alert_generation != generation
            or state.visual_alert is None
            or _render_alerts._alert_asset_key(state.visual_alert) != key
        ):
            # Upload succeeded and no draw was attempted: definitely unused.
            with contextlib.suppress(Exception):
                await bb.storage_remove(
                    f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                )
            return None
        state.alert_asset_file = filename
        state.alert_asset_key = key
        if filename not in state.alert_files:
            state.alert_files.append(filename)
        while len(state.alert_files) > _limits.ALERT_ASSET_KEEP:
            stale = state.alert_files.pop(0)
            if stale == filename:
                state.alert_files.append(stale)
                break
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{stale}")
        return filename
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - an alert retries on its next tick
        _limits.logger.warning("alert marquee upload failed: %s", exc)
        return None


# Every versioned family this app writes. Anything missing here leaks
# permanently: a crossing whose cleanup is skipped (a 409, or SIGTERM during
# its sleep) leaves a train_ file no restart ever reclaims.
GENERATION_FILES = re.compile(
    r"^(tl_|rva_|report_|sky_|train_|traffic_|alert_|flash_|meteor_)"
    r".*\.(anim|snd)$"
)

# Names this app wrote under an OLDER scheme, which no pattern above matches
# and therefore nothing has ever reclaimed. Found resident on a live device:
#
#   siren.snd      2.65 MB  the unversioned siren, before content addressing
#   sky_a.png               the a/b alternation the skill names as a mistake
#   sky_b.png
#   sky_demo.png
#
# The versioned siren (siren_<digest>.snd) is deliberately NOT here: it is the
# live asset with its own retirement path in retire_siren_assets. This set is
# only for schemes we have stopped using, and it must stay a closed list —
# "anything I don't recognise" would eat the deterministic report cache.
LEGACY_ORPHANS = frozenset(
    {
        "siren.snd",
        "sky_a.png",
        "sky_b.png",
        "sky_demo.png",
        "tts.snd",
    }
)


async def sweep_stale_assets(bb) -> None:
    """Remove transient generations orphaned by previous instances.

    A crash can leave ~130-320 kB animations or legacy multi-megabyte report
    takes behind until this next startup sweep (213 orphans once accumulated
    before this existed). Deterministic text+voice reports are the durable
    cache and are bounded separately when the report worker adopts them. This
    runs before the instance draws anything, so nothing swept can be playing.
    """
    try:
        await bb.display_clear(application_name=_limits.APP_NAME)
        files = (await bb.storage_list(f"/ext/user_assets/{_limits.APP_NAME}")).list
        # Deterministic text+voice report paths are the durable cache. They are
        # adopted lazily by the report worker; only old timestamp generations
        # and the other transient asset families are restart-orphans.
        stale = [
            f.name
            for f in files
            if (
                GENERATION_FILES.match(f.name)
                and _audio_report_assets._report_file_identity(f.name) is None
            )
            or f.name in LEGACY_ORPHANS
        ]
        for name in stale:
            try:
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{name}")
            except Exception:  # noqa: BLE001 - reaping is best-effort
                pass
        if stale:
            _limits.logger.info("swept %d stale asset generations", len(stale))
    except Exception as exc:  # noqa: BLE001 - hygiene must never be fatal
        _limits.logger.warning("asset sweep failed: %s", exc)

"""Skystrip device / effects."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import random
from datetime import datetime, timezone

from astral.sun import elevation
from busylib import exceptions, types
from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather_state as _weather_state
from apps.skystrip_app.render import effects as _render_effects
from apps.skystrip_app.render import primitives as _render_primitives
from apps.skystrip_app.render import scene as _render_scene
from apps.skystrip_app.render import status as _render_status
from apps.skystrip_app.render import traffic as _render_traffic
from busybar_dev import anim
from busybar_dev.config import describe_exception
from busybar_dev.device import is_refusal as _is_refusal


async def flash(bb, state: _model.SkyState, dist_km: float) -> None:
    """Flash only the rendered sky backdrop, with one native top-LED pulse.

    Sub-second color changes on a stable Rectangle id are ignored by firmware.
    One short ``.anim`` makes every step real, while recomposing the scene with
    ``lightning=...`` leaves houses, trees, skyline, water, and status ink
    intact instead of washing the entire display white.
    """
    if (
        state.visual_alert is not None
        or state.scrub_slot is not None
        or state.current_scene_file is None
        or not _weather_state.weather_is_fresh(state)
    ):
        return
    scene = state.scene
    scene_file = state.current_scene_file
    intent = state.view_generation
    now = datetime.now(timezone.utc)
    phase0 = (asyncio.get_running_loop().time() % 8.0) / 8.0
    seed = int(asyncio.get_running_loop().time() // 600)
    segment = _render_effects.render_lightning_segment(
        now,
        state.weather,
        seed,
        phase0=phase0,
        scene=scene,
        dist_km=dist_km,
    )
    # Keep the encoder's established list input while the public segment uses
    # an immutable tuple to make its ordered frame contract explicit.
    blob = anim.encode_anim(list(segment.frames), fps=segment.fps)
    filename = f"flash_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
    await bb.assets_upload(_limits.APP_NAME, filename, blob)
    if (
        state.view_generation != intent
        or state.scene != scene
        or state.current_scene_file != scene_file
        or state.scrub_slot is not None
        or state.visual_alert is not None
        or not _weather_state.weather_is_fresh(state)
    ):
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
        return
    state.effect_generation += 1
    payload = types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        # Distant strikes stay in the rendered backdrop. Only a genuinely
        # nearby strike gets the conspicuous native top-LED notification.
        led_notification_color=segment.led_notification_color,
        elements=[
            types.AnimationElement(
                id=f"flash{state.effect_generation}",
                type="animation",
                path=filename,
                loop=False,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=segment.timeout_s,
            )
        ],
    )
    try:
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.current_scene_file != scene_file
                or state.scrub_slot is not None
                or state.visual_alert is not None
                or not _weather_state.weather_is_fresh(state)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                    )
                return
            await bb.display_draw(payload)
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            with contextlib.suppress(Exception):
                await bb.storage_remove(
                    f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                )
        raise
    await asyncio.sleep(segment.timeout_s + _limits.FLASH_ASSET_RETIRE_GRACE_S)
    with contextlib.suppress(Exception):
        await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
    _limits.logger.info("lightning backdrop: strike %.0f km away", dist_km)


async def meteor(bb, state: _model.SkyState) -> None:
    """One native baked shooting-star animation; geometry really advances."""
    if (
        state.visual_alert is not None
        or state.scrub_slot is not None
        or state.current_scene_file is None
        or not _weather_state.weather_is_fresh(state)
    ):
        return
    m_rng = random.Random()
    x = m_rng.randrange(6, 46)
    y = m_rng.randrange(0, 3)
    ddx = m_rng.choice((3, 3, -3))  # mostly left-to-right, 3:1 slope
    scene = state.scene
    intent = state.view_generation
    now = datetime.now(timezone.utc)
    phase0 = (asyncio.get_running_loop().time() % 8.0) / 8.0
    seed = int(asyncio.get_running_loop().time() // 600)
    frames: list[Image.Image] = []
    for index in range(8):
        image = _render_scene.render_scene(
            now,
            state.weather,
            seed,
            phase=(phase0 + index / 96.0) % 1.0,
            scene=scene,
        )
        pixels = _render_primitives._rgb_pixels(image)
        for tail, scale in enumerate((1.0, 0.55, 0.28)):
            tx = x + (index - tail) * ddx
            ty = y + index - tail
            if 0 <= tx < _limits.W and 0 <= ty < _limits.H:
                pixels[tx, ty] = _render_primitives._rgb_int(
                    channel * scale for channel in (242, 246, 255)
                )
        frames.append(image)
    blob = anim.encode_anim(frames, fps=12)
    filename = f"meteor_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
    await bb.assets_upload(_limits.APP_NAME, filename, blob)
    if (
        state.view_generation != intent
        or state.scene != scene
        or state.scrub_slot is not None
        or state.visual_alert is not None
        or not _weather_state.weather_is_fresh(state)
    ):
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
        return
    state.effect_generation += 1
    try:
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.scrub_slot is not None
                or state.visual_alert is not None
                or not _weather_state.weather_is_fresh(state)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                    )
                return
            await bb.display_draw(
                types.DisplayElements(
                    application_name=_limits.APP_NAME,
                    priority=_limits.PRIORITY,
                    elements=[
                        types.AnimationElement(
                            id=f"meteor{state.effect_generation}",
                            type="animation",
                            path=filename,
                            loop=False,
                            x=0,
                            y=0,
                            display=types.DisplayName.FRONT,
                            timeout=2,
                        )
                    ],
                )
            )
        await asyncio.sleep(2.05)
        with contextlib.suppress(Exception):
            await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{filename}")
        _limits.logger.info("meteor: a shooting star crossed the bar")
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):
            with contextlib.suppress(Exception):
                await bb.storage_remove(
                    f"/ext/user_assets/{_limits.APP_NAME}/{filename}"
                )


async def train_crossing(bb, state: _model.SkyState) -> None:
    """One freight, once — a device-played one-shot overlay on the full
    sky band, so it never repeats with the scene loop.

    The band spans the WHOLE width: with no corner card left to hide
    behind (the band once abutted an opaque card at x=19, and trains
    "emerged from the ether" there once the card was gone), the freight
    now enters at one true screen edge and leaves at the other. The
    status digits are repainted on top of every frame by the production
    painter, so the train passes behind the clock like anything behind a
    HUD — and the crossing waits for a window inside the current minute,
    so the baked clock can never go stale mid-crossing."""
    loop = asyncio.get_running_loop()
    sec = datetime.now().second
    if sec > 35:  # ~20s crossing + margin fits before :59
        await asyncio.sleep(60 - sec + 0.5)
    now = datetime.now(timezone.utc)
    scene_frames = await loop.run_in_executor(
        None,
        lambda: _render_scene.render_loop_frames(
            now, state.weather, seed=1, scene="backroads"
        ),
    )
    band = scene_frames[0].crop((0, 0, _limits.W, 6))
    # Which band pixels are foreground trees? Diff the same frame rendered
    # without the lane, rather than restating the lane's geometry here.
    bare = await loop.run_in_executor(
        None,
        lambda: _render_scene.render_scene(
            now, state.weather, 1, phase=0.0, scene="backroads", lane=False
        ),
    )
    foreground = frozenset(
        (x, y)
        for y in range(6)
        for x in range(_limits.W)
        if band.getpixel((x, y)) != bare.getpixel((x, y))
    )
    night = (elevation(_settings.OBSERVER, now) < 2) or state.weather.stormy
    frames = await loop.run_in_executor(
        None, _render_effects._freight_frames, band, night, random.Random(), foreground
    )
    for frame in frames:
        _render_status._bake_status(
            frame.load(), now, state.weather, 0.0, scene="backroads"
        )
    blob = anim.encode_anim(frames, fps=12)
    stamp = datetime.now().strftime("%H%M%S")
    fname = f"train_{stamp}.anim"
    dur = len(frames) / 12
    await bb.assets_upload(_limits.APP_NAME, fname, blob)
    try:
        await bb.display_draw(
            types.DisplayElements(
                application_name=_limits.APP_NAME,
                priority=_limits.PRIORITY,
                elements=[
                    types.AnimationElement(
                        id=f"trn{stamp}",
                        type="animation",
                        path=fname,
                        loop=False,
                        x=0,
                        y=0,
                        display=types.DisplayName.FRONT,
                        timeout=int(dur) + 2,
                    )
                ],
            )
        )
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):  # refused outright: the file was never opened
            try:
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{fname}")
            except Exception:  # noqa: BLE001
                pass
        raise
    _limits.logger.info("train: a train is crossing (%.0fs)", dur)
    await asyncio.sleep(dur + 4)
    try:
        await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{fname}")
    except Exception:  # noqa: BLE001
        pass


async def traffic_crossing(bb, state: _model.SkyState) -> None:
    """One episode of traffic, once — a device-played overlay on the three
    rows cars occupy, so nothing about it repeats with the scene loop."""
    loop = asyncio.get_running_loop()
    now = datetime.now(timezone.utc)
    local = now.astimezone(_settings.TZ)
    elev = elevation(_settings.OBSERVER, now)
    lights_on = elev < 2 or state.weather.stormy
    _, mean_vehicles = _render_traffic.traffic_density(local.hour)

    def _build():
        scene = _render_scene.render_scene(
            now, state.weather, 1, phase=0.0, scene="backroads"
        )
        bare = _render_scene.render_scene(
            now, state.weather, 1, phase=0.0, scene="backroads", lane=False
        )
        top, rows = _render_traffic.TRAFFIC_BAND_TOP, _render_traffic.TRAFFIC_BAND_ROWS
        band = scene.crop((0, top, _limits.W, top + rows))
        # The poplar trunks cross these rows; learn which pixels they own by
        # diffing the lane away, exactly as the freight overlay does.
        foreground = frozenset(
            (x, y)
            for y in range(rows)
            for x in range(_limits.W)
            if scene.getpixel((x, top + y)) != bare.getpixel((x, top + y))
        )
        # Its own entropy, every time: no seed, no bucket, no repetition.
        rng = random.Random()
        n = max(1, int(round(rng.gauss(mean_vehicles, 0.8))))
        plan = _render_traffic.plan_traffic(rng, local.hour, lights_on, n)
        # Same ambient the scene itself uses, so an overlay car is lit like
        # the road it drives on (render_scene collapses cloud under storm).
        cloud = 1.0 if state.weather.stormy else state.weather.cloud_frac
        amb = _render_primitives._ambient(elev, cloud, state.weather)
        frames = _render_traffic.traffic_episode_frames(
            band, plan, lights_on, amb, foreground
        )
        return frames, plan

    frames, plan = await loop.run_in_executor(None, _build)
    blob = anim.encode_anim(frames, fps=_render_traffic.TRAFFIC_FPS)
    stamp = datetime.now().strftime("%H%M%S")
    fname = f"traffic_{stamp}.anim"
    dur = len(frames) / _render_traffic.TRAFFIC_FPS
    await bb.assets_upload(_limits.APP_NAME, fname, blob)
    try:
        await bb.display_draw(
            types.DisplayElements(
                application_name=_limits.APP_NAME,
                priority=_limits.PRIORITY,
                elements=[
                    types.AnimationElement(
                        id=f"trf{stamp}",
                        type="animation",
                        path=fname,
                        loop=False,
                        x=0,
                        y=_render_traffic.TRAFFIC_BAND_TOP,
                        display=types.DisplayName.FRONT,
                        timeout=int(dur) + 2,
                    )
                ],
            )
        )
    except exceptions.BusyBarAPIError as exc:
        if _is_refusal(exc):  # refused outright: the file was never opened
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{fname}")
        raise
    _limits.logger.info(
        "traffic: %d vehicle(s) over %.0fs (%s)",
        len(plan),
        dur,
        ", ".join(v["kind"] for v in plan),
    )
    await asyncio.sleep(dur + 3)
    with contextlib.suppress(Exception):
        await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{fname}")


async def watch_traffic(bb, state: _model.SkyState) -> None:
    """Cars come when they come. The gap between episodes is drawn fresh
    each time from the hour's own density, so the road is never on a
    metronome — which is the whole point of taking traffic out of the loop.
    """
    while True:
        mean_gap, _ = _render_traffic.traffic_density(datetime.now(_settings.TZ).hour)
        await asyncio.sleep(random.expovariate(1.0 / mean_gap) + 4.0)
        if state.scene != "backroads" or state.weather.severe:
            continue
        if state.scrub_slot is not None:
            continue  # the Time Machine owns the road while scrubbing
        try:
            await traffic_crossing(bb, state)
        except exceptions.BusyBarAPIError:
            pass  # display owned elsewhere; the road waits
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
            _limits.logger.warning(
                "traffic episode failed: %s", describe_exception(exc)
            )


async def watch_trains(bb, state: _model.SkyState) -> None:
    """Every so often, if the farm road is on stage, a train rolls
    through — an event, not a loop."""
    while True:
        await asyncio.sleep(random.uniform(2.5 * 60, 6 * 60))
        if state.scene == "backroads" and not state.weather.severe:
            try:
                await train_crossing(bb, state)
            except exceptions.BusyBarAPIError:
                pass  # display owned elsewhere; the train waits
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
                # busylib raises BusyBarRequestError (a sibling class) for
                # transport trouble, and the encoder can raise anything. This
                # task used to die silently: no more freights until restart,
                # while barkeep still reported the app healthy.
                _limits.logger.warning("train crossing failed: %s", exc)


async def watch_meteors(bb, state: _model.SkyState) -> None:
    """Very infrequently, on a clear enough night, one shooting star."""
    while True:
        await asyncio.sleep(random.uniform(20 * 60, 60 * 60))
        wx = state.weather
        if (
            elevation(_settings.OBSERVER, datetime.now(timezone.utc)) < -8
            and wx.cloud_frac < 0.5
            and not (wx.rain or wx.snow or wx.stormy)
        ):
            try:
                await meteor(bb, state)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a watcher must not vanish
                _limits.logger.debug("meteor failed: %s", exc)

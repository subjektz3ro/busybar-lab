"""Skystrip device / scrubber."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
from datetime import datetime, timedelta, timezone

from busylib import types

from apps.skystrip_app import alerts as _alerts
from apps.skystrip_app import limits as _limits
from apps.skystrip_app import model as _model
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather_timeline as _weather_timeline
from apps.skystrip_app.device import report_status as _device_report_status
from apps.skystrip_app.render import scene as _render_scene
from busybar_dev import anim


def _timeline_payload(
    section: str, eid: str, filename: str, timeout: int = 60
) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=_limits.APP_NAME,
        priority=_limits.PRIORITY,
        elements=[
            types.AnimationElement(
                id=eid,
                type="animation",
                path=filename,
                section=section,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=timeout,
            )
        ],
    )


async def retire_reveal(bb, state: _model.SkyState) -> None:
    """Firmware law: an element id can never re-seek. Retiring = redrawing
    it with the SAME content and a 1s timeout (verified working)."""
    if state.last_reveal is None:
        return
    r = state.last_reveal
    async with state.display_lock:
        await bb.display_draw(
            types.DisplayElements(
                application_name=_limits.APP_NAME,
                priority=_limits.PRIORITY,
                elements=[
                    types.AnimationElement(
                        id=r["eid"],
                        type="animation",
                        path=r["fname"],
                        section=r.get("section"),
                        loop=True,
                        x=0,
                        y=0,
                        display=types.DisplayName.FRONT,
                        timeout=1,
                    )
                ],
            )
        )
    if state.last_reveal is r:
        state.last_reveal = None


async def animate_reveal(
    bb,
    state: _model.SkyState,
    slot: int,
    *,
    initial: bool = False,
    intent: int | None = None,
) -> None:
    """A revealed moment starts as a held frame; this swaps in the full
    animated loop for that moment once it's rendered — without ever
    blocking the wheel. Aborts silently if the user moved on."""
    if intent is None:
        intent = state.view_generation
    scene = state.scene
    try:
        meta = state.timeline_meta
        if meta is None:
            return
        t = meta["start"] + timedelta(seconds=_limits.TIMELINE_STEP_S * slot)
        loop = asyncio.get_running_loop()
        frames = await loop.run_in_executor(
            None,
            lambda: _render_scene.render_loop_frames(
                t.astimezone(timezone.utc),
                _weather_timeline.wx_at(state, t),
                7,
                scene=scene,
                scrubbed=True,
                n_frames=_limits.ANIM_FRAMES,
            ),
        )
        if (
            state.view_generation != intent
            or state.scene != scene
            or state.scrub_slot != slot
            or _alerts._unacknowledged_alert_active(state)
            or (not initial and not state.revealed)
        ):
            return  # the wheel moved on or a fresh alert took the display
        blob = anim.encode_anim(frames, fps=_limits.ANIM_FPS)
        fname = f"rva_{hashlib.sha256(blob).hexdigest()[:16]}.anim"
        await bb.assets_upload(_limits.APP_NAME, fname, blob)
        if (
            state.view_generation != intent
            or state.scene != scene
            or state.scrub_slot != slot
            or _alerts._unacknowledged_alert_active(state)
            or (not initial and not state.revealed)
        ):
            with contextlib.suppress(Exception):
                await bb.storage_remove(f"/ext/user_assets/{_limits.APP_NAME}/{fname}")
            return
        state.reveal_n += 1
        eid = f"rv{state.reveal_n}"
        prev = state.last_reveal
        elements: list = []
        if prev is not None:
            elements.append(
                types.AnimationElement(
                    id=prev["eid"],
                    type="animation",
                    path=prev["fname"],
                    section=prev.get("section"),
                    loop=True,
                    x=0,
                    y=0,
                    display=types.DisplayName.FRONT,
                    timeout=1,
                )
            )
        elements.append(
            types.AnimationElement(
                id=eid,
                type="animation",
                path=fname,
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=60,
            )
        )
        async with state.display_lock:
            if (
                state.view_generation != intent
                or state.scene != scene
                or state.scrub_slot != slot
                or _alerts._unacknowledged_alert_active(state)
                or (not initial and not state.revealed)
            ):
                with contextlib.suppress(Exception):
                    await bb.storage_remove(
                        f"/ext/user_assets/{_limits.APP_NAME}/{fname}"
                    )
                return
            await bb.display_draw(
                types.DisplayElements(
                    application_name=_limits.APP_NAME,
                    priority=_limits.PRIORITY,
                    elements=elements,
                )
            )
        state.last_reveal = {"eid": eid, "slot": slot, "fname": fname, "section": None}
        state.revealed = True
        state.reveal_pending = False
        state.anim_reveal_file = fname
        if fname not in state.anim_reveal_files:
            state.anim_reveal_files.append(fname)
        while len(state.anim_reveal_files) > 3:
            old_file = state.anim_reveal_files.pop(0)
            with contextlib.suppress(Exception):
                await bb.storage_remove(
                    f"/ext/user_assets/{_limits.APP_NAME}/{old_file}"
                )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        _limits.logger.debug("animated reveal skipped: %s", exc)
    finally:
        if state.view_generation == intent and state.scrub_slot == slot:
            state.reveal_pending = False


def _slot_label(meta: dict, slot: int) -> str:
    dt = meta["start"] + timedelta(seconds=_limits.TIMELINE_STEP_S * slot)
    today = datetime.now(_settings.TZ).date()
    prefix = "TMW " if dt.date() > today else ("YDA " if dt.date() < today else "")
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{prefix}{dt.hour % 12 or 12}:{dt.minute:02d} {ampm}"


def _readout_elements(generation: int, label: str, timeout: int) -> list:
    return [
        types.RectangleElement(
            id=f"ror{generation}",
            type="rectangle",
            x=8,
            y=3,
            width=56,
            height=11,
            fill="solid",
            fill_colors=["#000000C0"],
            border_width=0,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
        types.TextElement(
            id=f"rot{generation}",
            type="text",
            text=label,
            font="condensed",
            color="#FFD98CFF",
            align="center",
            x=36,
            y=8,
            display=types.DisplayName.FRONT,
            timeout=timeout,
        ),
    ]


def _retired_readout_elements(readout: dict | None) -> list:
    if readout is None:
        return []
    return _readout_elements(readout["generation"], readout["label"], 1)


async def draw_scrub_readout(
    bb, state: _model.SkyState, label: str, timeout: int = 3
) -> None:
    """The big instant time readout that rides the wheel. FIRMWARE LAW:
    element attributes are immutable after creation — every redraw here
    keeps IDENTICAL geometry; only the text string (and timeout) vary,
    the one mutation the firmware honors (the clock element proved it).
    There is no clear: the timeout dissolves it."""
    old_readout = state.last_readout
    old_reveal = state.last_reveal
    generation = state.readout_gen + 1
    report_statuses = _device_report_status._live_report_statuses(state)
    elements = [
        *_device_report_status._retired_report_status_elements(report_statuses),
        *_retired_readout_elements(old_readout),
    ]
    if old_reveal is not None:
        elements.append(
            types.AnimationElement(
                id=old_reveal["eid"],
                type="animation",
                path=old_reveal["fname"],
                section=old_reveal.get("section"),
                loop=True,
                x=0,
                y=0,
                display=types.DisplayName.FRONT,
                timeout=1,
            )
        )
    elements.extend(_readout_elements(generation, label, timeout))
    async with state.display_lock:
        # A CAP update can re-arm the warning after the wheel message was
        # decoded but before this draw wins the device lane. Never let that
        # stale readout cover the newly active alert card.
        if _alerts._unacknowledged_alert_active(state):
            return
        await bb.display_draw(
            types.DisplayElements(
                application_name=_limits.APP_NAME,
                priority=_limits.PRIORITY,
                elements=elements,
            )
        )
        _device_report_status._forget_report_statuses(state, report_statuses)
    state.readout_gen = generation
    state.last_readout = {
        "generation": generation,
        "label": label,
        "timeout": timeout,
    }
    if state.last_reveal is old_reveal:
        state.last_reveal = None


def _scrub_reveal_ready(state: _model.SkyState, now: float) -> bool:
    """Whether a rested wheel selection may take the front display.

    An acknowledged CAP alert remains selected so its red top-LED reminder
    can continue. Only the unacknowledged alert card owns the front display.
    """
    return (
        state.scrub_slot is not None
        and not state.revealed
        and not state.reveal_pending
        and not _alerts._unacknowledged_alert_active(state)
        and now - state.scrub_touched > _limits.REVEAL_REST_S
        and state.timeline_meta is not None
    )


def _half_hour_floor(dt: datetime) -> datetime:
    return dt.replace(minute=0 if dt.minute < 30 else 30, second=0, microsecond=0)


async def build_timeline(bb, state: _model.SkyState) -> None:
    """Pre-render the whole ±24h scrub range as one sectioned .anim so a
    wheel detent is a single draw call. Rebuilds half-hourly and whenever
    the scene changes."""
    while True:
        try:
            meta = state.timeline_meta
            if state.scrub_slot is not None:
                await asyncio.sleep(5)  # never swap files mid-scrub
                continue
            stale = (
                meta is None
                or meta["scene"] != state.scene
                or (datetime.now(_settings.TZ) - meta["built"]).total_seconds() > 1800
            )
            if stale and state.hourly:
                # Snapshot the scene: rendering 97 slots takes seconds and
                # yields below, so a START press lands mid-render. Sampling
                # state.scene per slot would bake two scenes into one file.
                scene = state.scene
                start = _half_hour_floor(datetime.now(_settings.TZ)) - timedelta(
                    hours=24
                )
                frames = []
                for i in range(_limits.TIMELINE_SLOTS):
                    t = start + timedelta(seconds=_limits.TIMELINE_STEP_S * i)
                    frames.append(
                        _render_scene.render_scene(
                            t.astimezone(timezone.utc),
                            _weather_timeline.wx_at(state, t),
                            7,
                            phase=0.0,
                            scene=scene,
                            scrubbed=True,
                        )
                    )
                    if i % 8 == 0:
                        await asyncio.sleep(0)  # stay cooperative
                if scene != state.scene:
                    # The scene changed while we rendered. Throw the work
                    # away rather than upload a file whose label would claim
                    # a scene it doesn't contain — the reveal guard trusts
                    # that label, and a lying one is what flashes the
                    # previous theme onto the bar mid-scrub.
                    _limits.logger.info(
                        "timeline: scene changed mid-render (%s -> %s), rebuilding",
                        scene,
                        state.scene,
                    )
                    continue
                # Each slot holds for 200 display-frames (200s at fps=1):
                # the firmware plays PAST a section's end rather than
                # holding, so the frame itself must outlast any park —
                # idle snap-home at 45s always fires first
                secs = [
                    (f"s{i:02d}", i * 200, i * 200 + 199)
                    for i in range(_limits.TIMELINE_SLOTS)
                ]
                blob = anim.encode_anim(
                    frames,
                    fps=1,
                    sections=secs,
                    durations=[200] * _limits.TIMELINE_SLOTS,
                )
                # Versioned name: the firmware caches anim files by path,
                # so overwriting one path serves stale generations
                fname = f"tl_{datetime.now().strftime('%H%M%S')}.anim"
                await bb.assets_upload(_limits.APP_NAME, fname, blob)
                state.timeline_meta = {
                    "start": start,
                    "scene": scene,
                    "built": datetime.now(_settings.TZ),
                    "file": fname,
                }
                # Retire one generation late: a reveal committed while this
                # rebuild was rendering still points at the previous file with
                # a 60s element timeout, and deleting it under the firmware
                # leaves a frozen past-time frame that "back to now" can't
                # clear. A rebuild cycle outlasts any element timeout.
                state.timeline_files.append(fname)
                while len(state.timeline_files) > 2:
                    stale_tl = state.timeline_files.pop(0)
                    try:
                        await bb.storage_remove(
                            f"/ext/user_assets/{_limits.APP_NAME}/{stale_tl}"
                        )
                    except Exception:  # noqa: BLE001
                        pass
                _limits.logger.info(
                    "timeline: %d slots ready (%.0f kB)",
                    _limits.TIMELINE_SLOTS,
                    len(blob) / 1024,
                )
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _limits.logger.warning("timeline build failed: %s", exc)
            await asyncio.sleep(60)

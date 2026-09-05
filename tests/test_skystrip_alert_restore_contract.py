"""Acknowledging an alert must never hand back a blank panel.

Refusing to redraw an expired live scene is deliberate — a plausible sky built
from a two-hour-old observation is a lie.  But the refusal has to leave
*something* on the display.  ``restore_current_view`` clears every same-app
element before it rebuilds, so a restore that declines to draw is not a no-op:
it is the panel going black, with no second chance.  The scene loop is gated on
the same ``weather_is_fresh`` predicate, so nothing else repaints it either.

These are host-only contracts against in-memory fakes.  No device, network, or
wall-clock sleep participates.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "apps"))
from apps.skystrip_app import limits as sky_limits
from apps.skystrip_app import model as sky_model
from apps.skystrip_app import weather as sky_weather
from apps.skystrip_app.device import alerts as sky_device_alerts
from apps.skystrip_app.device import display as sky_device_display


def _tornado_warning():
    from busybar_dev.weather_alerts import Alert

    now = datetime.now().astimezone()
    return Alert(
        identifier="urn:alert:tornado",
        references=(),
        event="Tornado Warning",
        headline="Tornado Warning",
        status="Actual",
        message_type="Alert",
        severity="Extreme",
        urgency="Immediate",
        certainty="Observed",
        effective=now - timedelta(minutes=1),
        onset=now - timedelta(minutes=1),
        expires=now + timedelta(minutes=20),
        ends=now + timedelta(minutes=20),
    )


class RecordingBar:
    """Record display traffic in order, so a clear-then-nothing is visible."""

    def __init__(self):
        self.operations: list[str] = []
        self.draws: list = []

    async def display_draw(self, payload):
        self.draws.append(payload)
        self.operations.append("DRAW")

    async def display_clear(self, *, application_name: str):
        self.operations.append("CLEAR")

    async def audio_stop(self):
        self.operations.append("STOP")


def _expire_the_live_weather(state) -> None:
    """A resident scene whose observation has aged past its lease."""
    state.current_scene_file = "sky_expired.anim"
    state.weather_ready.set()
    state.weather_updated_at = (
        asyncio.get_running_loop().time() - sky_weather.WEATHER_LEASE_S - 1
    )


def _drawn_element_ids(bb: RecordingBar) -> list[str]:
    return [element.id for payload in bb.draws for element in payload.elements]


@pytest.mark.asyncio
async def test_restoring_over_an_expired_scene_leaves_the_panel_lit():
    """The exact tornado-acknowledgement path: clear, then draw *something*."""
    state = sky_model.SkyState()
    _expire_the_live_weather(state)
    state.visual_alert = _tornado_warning()
    state.weather.severe = True
    state.weather.severe_event = "Tornado Warning"
    bb = RecordingBar()

    await sky_device_display.restore_current_view(bb, state)

    assert "CLEAR" in bb.operations, "the restore must remove the alert card"
    assert bb.operations.index("CLEAR") < len(bb.operations) - 1, (
        "the display was cleared and nothing was drawn after it: black screen"
    )
    assert _drawn_element_ids(bb), "no element survived the acknowledgement"


@pytest.mark.asyncio
async def test_acknowledging_a_live_warning_never_ends_in_a_dark_panel():
    """End-to-end through ``acknowledge_alert``, the button's real entry point."""
    state = sky_model.SkyState()
    _expire_the_live_weather(state)
    state.visual_alert = _tornado_warning()
    state.siren_alert = state.visual_alert
    state.weather.severe = True
    state.weather.severe_event = "Tornado Warning"
    bb = RecordingBar()

    handled = await sky_device_alerts.acknowledge_alert(bb, state, "test")

    assert handled
    assert state.alert_acked
    assert _drawn_element_ids(bb), (
        "acknowledged a still-active warning and the panel went dark"
    )


@pytest.mark.asyncio
async def test_a_restore_that_draws_nothing_is_not_reported_as_done():
    """If the panel really cannot be repainted, the retry must stay armed.

    ``alert_dismiss_pending`` is what makes ``severe_alarm`` try again on its
    next tick.  Clearing it after drawing nothing is what turns a transient
    blank into a permanent one.
    """
    state = sky_model.SkyState()
    state.current_scene_file = None
    state.weather_ready.clear()
    state.weather_updated_at = None
    state.visual_alert = _tornado_warning()
    state.alert_dismiss_pending = True
    bb = RecordingBar()

    await sky_device_display.restore_current_view(bb, state)

    if not _drawn_element_ids(bb):
        assert state.alert_dismiss_pending, (
            "nothing was drawn, yet the restore was marked complete"
        )


@pytest.mark.asyncio
async def test_the_stale_notice_is_retired_by_the_scene_that_replaces_it():
    """Elements accumulate by id — an un-retired card rides on top of the sky."""
    state = sky_model.SkyState()
    state.stale_notice_at = asyncio.get_running_loop().time()

    retired = sky_device_display._retired_stale_notice_elements(state)

    assert [element.id for element in retired] == ["wxstaleb", "wxstalet"]
    assert all(element.timeout == 1 for element in retired), (
        "retirement is a one-second lease on identical ids and geometry"
    )
    assert not sky_device_display._retired_stale_notice_elements(sky_model.SkyState()), (
        "nothing to retire when the notice was never shown"
    )


@pytest.mark.asyncio
async def test_the_stale_notice_never_covers_an_alert_or_time_machine():
    """A warning card and a scrubbed view both outrank the outage notice."""
    for field, value in (("visual_alert", _tornado_warning()), ("scrub_slot", 12)):
        state = sky_model.SkyState()
        setattr(state, field, value)
        bb = RecordingBar()

        assert not await sky_device_display.keep_stale_notice(bb, state)
        assert not bb.draws, f"the notice drew over {field}"


@pytest.mark.asyncio
async def test_a_booting_app_does_not_announce_an_outage_it_has_not_had():
    """Startup is briefly 'not fresh'. Crying wolf every boot teaches you to
    ignore the one time it matters."""
    state = sky_model.SkyState()
    bb = RecordingBar()

    assert not await sky_device_display.keep_stale_notice(bb, state)
    assert not bb.draws
    assert state.stale_since, "the outage clock must start on the first miss"

    state.stale_since -= sky_limits.STALE_NOTICE_GRACE_S
    assert await sky_device_display.keep_stale_notice(bb, state)
    assert _drawn_element_ids(bb) == ["wxstaleb", "wxstalet"]


@pytest.mark.asyncio
async def test_acknowledgement_shows_the_notice_without_waiting_out_the_grace():
    """The grace protects boot. At acknowledgement the alternative is black
    *right now*, so the notice is immediate."""
    state = sky_model.SkyState()
    _expire_the_live_weather(state)
    state.visual_alert = _tornado_warning()
    bb = RecordingBar()

    await sky_device_display.restore_current_view(bb, state)

    assert _drawn_element_ids(bb) == ["wxstaleb", "wxstalet"]
    assert state.stale_notice_at, "the notice must be claimed so a scene retires it"


@pytest.mark.asyncio
async def test_the_stale_notice_refreshes_on_its_own_cadence():
    """It carries a real lease; redrawing it every tick is pointless traffic."""
    state = sky_model.SkyState()
    state.stale_since = (
        asyncio.get_running_loop().time() - sky_limits.STALE_NOTICE_GRACE_S
    )
    bb = RecordingBar()

    assert await sky_device_display.keep_stale_notice(bb, state)
    assert _drawn_element_ids(bb) == ["wxstaleb", "wxstalet"]
    assert state.stale_notice_at

    assert not await sky_device_display.keep_stale_notice(bb, state)
    assert len(bb.draws) == 1, "redrew an unchanged card before its cadence"

    state.stale_notice_at -= sky_limits.STALE_REDRAW_S
    assert await sky_device_display.keep_stale_notice(bb, state)
    assert len(bb.draws) == 2, "the notice was allowed to lapse into black"


@pytest.mark.asyncio
async def test_the_notice_outlives_nothing_and_its_lease_beats_its_cadence():
    """A refresh that lands after the lease expired is a visible black gap."""
    assert sky_limits.STALE_REDRAW_S < sky_limits.STALE_ELEMENT_TIMEOUT_S
    assert sky_limits.STALE_ELEMENT_TIMEOUT_S <= sky_limits.ELEMENT_TIMEOUT_S


@pytest.mark.asyncio
async def test_acknowledgement_demands_a_fresh_scene_instead_of_the_cached_one():
    """The observed failure: ~24s of black after acknowledging a warning.

    ``restore_current_view`` re-creates "sky" pointing at the file the scene
    loop last uploaded — an asset the firmware cached BY PATH and held open
    across the clear.  The panel stayed dark until the next wall-clock minute
    pushed a new versioned filename.  ``scene_change`` wakes that loop inside a
    second, so the gap is bounded by the redraw, not by the clock.
    """
    state = sky_model.SkyState()
    state.current_scene_file = "sky_00001_1.anim"
    state.weather_ready.set()
    state.weather_updated_at = asyncio.get_running_loop().time()
    state.visual_alert = _tornado_warning()
    state.weather.severe = True
    bb = RecordingBar()

    await sky_device_alerts.acknowledge_alert(bb, state, "START")

    assert state.scene_change.is_set(), (
        "nothing asked for a fresh scene; the panel waits out the minute"
    )


@pytest.mark.asyncio
async def test_an_all_clear_also_repaints_without_waiting_out_the_minute():
    """Alert expiry runs the same restore and had the same black gap."""
    state = sky_model.SkyState()
    state.current_scene_file = "sky_00001_1.anim"
    state.weather_ready.set()
    state.weather_updated_at = asyncio.get_running_loop().time()
    state.alert_dismiss_pending = True
    bb = RecordingBar()

    await sky_device_display.restore_current_view(bb, state)

    assert state.scene_change.is_set()
    assert not state.alert_dismiss_pending


def test_the_notice_text_fits_the_condensed_budget():
    """~12 characters at 72px, centered — the busybar-app skill's law."""
    assert len(sky_limits.STALE_WEATHER_TEXT) <= 12

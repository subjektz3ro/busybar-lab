"""Shared weather, scene, alert and generation-bound report state.

One SkyState belongs to one runtime. Background tasks retain this same owner
so cancellation and stale-generation checks cover every effect.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from PIL import Image

from apps.skystrip_app import limits as _limits
from apps.skystrip_app import settings as _settings
from apps.skystrip_app import weather as _weather
from busybar_dev.weather_alerts import Alert


@dataclass(frozen=True)
class _FlashEvent:
    distance_km: float
    observed_at: float


@dataclass(frozen=True)
class ReportRequest:
    """One double-START intent, fenced from later views and alerts."""

    generation: int
    view_generation: int
    alert_generation: int
    text: str


@dataclass(frozen=True)
class ReportStatus:
    """One possibly committed native report card with a bounded lease."""

    request_generation: int
    element_generation: int
    label: str
    expires_at: float
    view_generation: int = 0
    alert_generation: int = 0
    terminal: bool = False


@dataclass
class SkyState:
    weather: _weather.WeatherState = field(default_factory=_weather.WeatherState)
    weather_ready: asyncio.Event = field(default_factory=asyncio.Event)
    weather_updated_at: float | None = None
    nws_point_covered: bool | None = None
    nws_point_checked: asyncio.Event = field(default_factory=asyncio.Event)
    # `None` means no echo only while radar_at is fresh; without a timestamp it
    # is unavailable/unknown and cannot declare the point dry.
    radar_dbz: float | None = None
    radar_at: float = 0.0  # loop-time of that sample, 0 = never
    radar_covered: bool | None = None  # official RainViewer coverage mask
    om_rain: bool | None = None  # Open-Meteo current precip at our coords
    om_at: float | None = None  # source time on the monotonic clock
    station_rain: bool | None = None  # optional NWS observation evidence
    station_at: float | None = None  # source time on the monotonic clock
    rain_known: bool = False  # at least one provider resolved rain
    rain_at: float | None = None  # source time of resolved last-good rain
    snow_at: float | None = None  # source time of falling-snow evidence
    thunder_at: float | None = None  # source time of thunder evidence
    snow_depth_at: float | None = None  # source time of modeled ground snow
    rain_src: str = ""  # which source last decided rain (for logs)
    flash_queue: asyncio.Queue = field(
        default_factory=lambda: asyncio.Queue(maxsize=_limits.FLASH_QUEUE_MAX)
    )
    scene_files: list = field(default_factory=list)  # live scene anim + prior
    scene_gen: int = 0
    timeline_files: list = field(default_factory=list)  # live timeline + prior
    last_pushed: tuple[bytes, str, str] | None = None  # (png, clock, color)
    last_drawn_at: float = 0.0
    # Monotonic time of the last "no live weather" notice; 0.0 means never.
    stale_notice_at: float = 0.0
    # When the current run of stale weather began; 0.0 means not stale.
    stale_since: float = 0.0
    scene_idx: int = 0
    scene_change: asyncio.Event = field(default_factory=asyncio.Event)
    current_scene_file: str | None = None
    current_scene_frames: tuple[Image.Image, ...] = ()
    view_generation: int = 0
    effect_generation: int = 0
    alert_acked: bool = False
    visual_alert: Alert | None = None
    siren_alert: Alert | None = None
    active_alerts: tuple[Alert, ...] = ()
    alert_generation: int = 0
    alert_changed: asyncio.Event = field(default_factory=asyncio.Event)
    alert_wake_generation: int = 0
    alert_asset_file: str | None = None
    alert_asset_key: str | None = None
    alert_files: list[str] = field(default_factory=list)
    alert_drawn_generation: int = -1
    alert_dismiss_pending: bool = False
    alert_known: bool = False
    siren_file: str | None = None
    siren_repair: int = 0
    siren_retire: set[str] = field(default_factory=set)
    siren_retire_after: dict[str, float] = field(default_factory=dict)
    siren_ambiguous: set[str] = field(default_factory=set)
    siren_asset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    siren_asset_changed: asyncio.Event = field(default_factory=asyncio.Event)
    display_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    audio_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    audio_generation: int = 0
    audio_owner: str | None = None
    audio_path: str | None = None
    audio_stop_pending: bool = False
    switch_position: str | None = None
    switch_generation: int = 0
    shutting_down: bool = False
    forecast: list | None = None  # first two NWS forecast periods
    hourly: list | None = None  # 72h of (local datetime, weather dict)
    obs_history: list | None = None  # 26h of (local datetime, observation)
    report_file: str | None = None  # pre-baked spoken report, on the bar
    report_text: str | None = None  # the words inside report_file
    report_generation: int = 0
    report_status_generation: int = 0
    report_request: ReportRequest | None = None
    report_statuses: list[ReportStatus] = field(default_factory=list)
    report_prepare_text: str | None = None
    report_prepare_task: asyncio.Task | None = None
    report_prepare_pending: str | None = None
    report_prepare_pending_priority: bool = False
    report_asset_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    report_files: list[str] = field(default_factory=list)
    report_retire: set[str] = field(default_factory=set)
    report_repairs: dict[str, int] = field(default_factory=dict)
    report_expected_sizes: dict[str, int] = field(default_factory=dict)
    scrub_touched: float = 0.0
    timeline_meta: dict | None = None  # start dt, scene, built dt
    scrub_slot: int | None = None  # where the wheel points, None = live
    revealed: bool = False  # forecast frame currently shown
    reveal_pending: bool = False  # one generation is rendering/drawing
    last_reveal: dict | None = None  # {eid, slot, fname, section} on screen
    reveal_n: int = 0
    readout_gen: int = 0  # bumped after each reveal: fresh ids
    last_readout: dict | None = None  # exact native ids/content for retirement
    anim_reveal_file: str | None = None  # last animated-reveal upload
    anim_reveal_files: list[str] = field(default_factory=list)
    enc_accum: int = 0  # raw encoder counts toward next detent
    detached_tasks: set[asyncio.Task] = field(default_factory=set)

    @property
    def scene(self) -> str:
        return _settings.ENABLED_SCENES[self.scene_idx % len(_settings.ENABLED_SCENES)]


def spawn_owned(state: SkyState, coro) -> asyncio.Task:
    """Track every detached coroutine so shutdown can cancel and settle it."""
    task = asyncio.create_task(coro)
    state.detached_tasks.add(task)
    task.add_done_callback(state.detached_tasks.discard)
    return task

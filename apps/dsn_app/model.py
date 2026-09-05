"""Shared DSN state, watch intent and generation-bound narration messages.

One instance belongs to one runtime. Providers, input and device operations
must not mirror its ownership counters into independently updated state.
"""

from __future__ import annotations

import asyncio
import time
from collections import OrderedDict
from dataclasses import dataclass, field

from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source


# --- data ------------------------------------------------------------------
@dataclass
class Watch:
    """One immutable, locally timed represented light crossing.

    The DSN contact may end while this local representation continues.
    `on_air` reports source visibility; it never moves the frozen deadline.
    """

    link: _source.Link
    started_at: float
    light_s: float
    deadline: float
    generation: int
    return_view: str
    live_key: str | None
    on_air: bool = True


@dataclass(frozen=True)
class NarrationRequest:
    """One explicit START intent, separate from background cache ordering."""

    generation: int
    key: str
    name: str | None
    view: str


@dataclass(frozen=True)
class NarrationNotice:
    """Terminal user feedback bound to the exact request that produced it."""

    generation: int
    key: str
    name: str | None
    view: str
    label: str


@dataclass
class State:
    links: list[_source.Link] = field(default_factory=list)
    ranges: dict[int, tuple[float, float]] = field(
        default_factory=dict
    )  # naif -> (km, at)
    range_retry_at: dict[int, float] = field(default_factory=dict)
    range_unavailable: set[int] = field(default_factory=set)
    focus: str | None = None  # user real-time lock; None = auto-rotate
    narration_focus: str | None = None  # orthogonal hold while audio plays
    completion_pending: str | None = None  # hold until arrival blink is accepted
    cursor: int = 0
    scene_files: list[str] = field(default_factory=list)
    scene_cache: OrderedDict[tuple, str] = field(default_factory=OrderedDict)
    scene_gen: int = 0
    enc_accum: int = 0
    names: dict[str, str] = field(default_factory=dict)
    dish_types: dict[str, str] = field(default_factory=dict)  # DSS43 -> 70M
    site_lons: dict[str, float] = field(default_factory=dict)  # Canberra -> 148.98
    dirty: asyncio.Event = field(default_factory=asyncio.Event)
    seen: dict[str, dict] = field(default_factory=dict)  # craft -> first/last/passes
    speech: dict[str, float] = field(default_factory=dict)  # filename -> seconds
    # A PLAY 404 means a resident path can be corrupt rather than absent.
    # Repairs get a new immutable generation; the base-name mapping lets a
    # later process rediscover the newest usable generation from storage.
    speech_repairs: dict[str, int] = field(default_factory=dict)
    speech_retire: set[str] = field(default_factory=set)
    speech_cache_ready: bool = False
    speaking: bool = False
    synth: asyncio.Lock = field(default_factory=asyncio.Lock)
    picking: bool = False  # wheel is being turned; the picker is up
    pick_at: float = 0.0  # loop time of the last detent
    manual_until: float = 0.0  # loop time; a deliberate selection wins
    realtime_since: float | None = None  # wall clock when the lock was taken
    watch: Watch | None = None
    rt_generation: int | None = None  # immutable countdown id for this watch
    rt_counter: int = 0
    rt_nonce: str = field(default_factory=lambda: f"{time.time_ns() & 0xFFFFFFFF:x}")
    led_blink: str | None = None  # colour for the NEXT draw, then cleared
    led_generation: int = 0
    countdown_up: bool = False  # a live countdown element is on screen
    countdown_id: str | None = None
    view: str = field(default_factory=lambda: _settings.DEFAULT_VIEW)
    view_before_lock: str | None = None
    feed_timestamp_ms: int | None = None
    feed_advanced_at: float | None = None
    feed_seeded: bool = False
    freshness: str = "offline"
    aim_trails: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    event_queue: list[dict] = field(default_factory=list)
    next_event_at: float = 0.0
    last_scene_signature: tuple | None = None
    last_scene_filename: str | None = None
    live_lease_up: bool = False
    last_live_lease_timestamp_ms: int | None = None
    narration_texts: dict[str, str] = field(default_factory=dict)
    narration_frozen_at: dict[str, int] = field(default_factory=dict)
    # text, distinct source snapshots observed, last NASA source timestamp
    narration_candidates: dict[str, tuple[str, int, int]] = field(default_factory=dict)
    narration_priority: str | None = None
    narration_request_counter: int = 0
    narration_request: NarrationRequest | None = None
    narration_notice: NarrationNotice | None = None
    narration_notice_retry_at: float = 0.0
    narration_notice_failures: int = 0
    interactive_draw: asyncio.Lock = field(default_factory=asyncio.Lock)
    interactive_layer: int = 0
    interactive_visible_until: float = 0.0
    active_event_label: str | None = None
    active_event_asset: str | None = None
    active_event_embedded_label: bool = False
    active_event_until: float = 0.0
    audio_stop_pending: bool = False
    audio_stop_retry_at: float = 0.0
    audio_generation: int = 0
    audio_stop_generation: int | None = None
    audio_io: asyncio.Lock = field(default_factory=asyncio.Lock)
    speech_tasks: set[asyncio.Task] = field(default_factory=set)
    ok_down_at: float | None = None
    ok_hold_fired: bool = False
    ok_hold_task: asyncio.Task | None = None
    status_up: bool = False
    completion_link: _source.Link | None = None
    completion_generation: int | None = None
    narration_revision: int = 0
    narration_changed: asyncio.Event = field(default_factory=asyncio.Event)
    narration_return_view: str | None = None
    heartbeat_id: str | None = None
    heartbeat_y: int | None = None
    heartbeat_generation: int = 0
    heartbeat_pending_timestamp_ms: int | None = None
    heartbeat_pending_id: str | None = None
    heartbeat_pending_y: int | None = None
    # A transport failure can lose the response after the device committed a
    # draw. Keep every such id until one accepted payload retires it.
    heartbeat_uncertain: dict[str, int] = field(default_factory=dict)
    heartbeat_uncertain_until: dict[str, float] = field(default_factory=dict)
    event_assets: dict[str, str] = field(default_factory=dict)
    event_warm_task: asyncio.Task | None = None
    network_page: int = 0
    network_page_pending: bool = False
    network_warm_task: asyncio.Task | None = None
    network_warm_signature: tuple | None = None
    # ``inf`` means a wheel-rest Focus Lens is waiting for its first accepted
    # draw.  Once accepted it becomes a loop-time deadline, guaranteeing one
    # complete native marquee without making Focus an ambient carousel.
    network_focus_until: float = 0.0
    network_focus_key: str | None = None
    # Focus is one deliberate semantic-zoom snapshot. NASA's independent
    # native heartbeat can still advance during it, but changing telemetry
    # cannot restart the full-name marquee and consume the user's dwell.
    network_focus_links: tuple[_source.Link, ...] = ()
    network_focus_names: dict[str, str] = field(default_factory=dict)
    network_focus_trails: dict[str, list[tuple[int, int]]] = field(default_factory=dict)

    def current(self) -> _source.Link | None:
        if self.watch is not None and self.realtime_since is not None:
            return self.watch.link
        if self.completion_link is not None and self.completion_pending:
            return self.completion_link
        if not self.links:
            return None
        held = self.focus or self.narration_focus or self.completion_pending
        if held:
            for link in self.links:
                if link.key == held:
                    return link
            return None  # never put another craft on its timer
        return self.links[self.cursor % len(self.links)]


def request_led(state: State, colour: str) -> None:
    state.led_generation += 1
    state.led_blink = colour


def clear_led(state: State, colour: str | None = None) -> None:
    if colour is None or state.led_blink == colour:
        state.led_generation += 1
        state.led_blink = None


def note_narration_change(state: State) -> None:
    """Wake an accepted PLAY so stale dish/craft audio cannot continue."""
    state.narration_revision += 1
    state.narration_changed.set()
    state.narration_changed = asyncio.Event()

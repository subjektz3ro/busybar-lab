"""DSN history."""

from __future__ import annotations

import json
import math

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import settings as _settings
from apps.dsn_app import source as _source

# --- what the network was doing while you were not looking -----------------
# Every poll used to overwrite the last snapshot and discard it, so the app
# was structurally unable to say "Voyager was tracked for six hours today" or
# "first Psyche pass this week". It is an append-only log of TRANSITIONS, not
# of polls: a poll every 30 seconds would be 2,880 identical lines a day, and
# the interesting thing is the change.
# Bounded by BYTES, because that is what protects an SD card, and trimmed
# back to a line count comfortably under it so the trim is rare. Sizing these
# close together is the trap: 5000 lines of ~100 bytes is ~500 KB, so a 512 KB
# ceiling would rewrite the whole file on nearly every append forever.
HISTORY_MAX_BYTES = 1024 * 1024  # ceiling: about a month of ordinary traffic

HISTORY_MAX_LINES = 4000  # ~400 KB, so ~60% headroom after a trim


def link_events(
    before: list[_source.Link], after: list[_source.Link], now: float
) -> list[dict]:
    """What changed between two snapshots. Pure, so it can be tested.

    Three kinds of thing are worth remembering: a craft appearing on a dish,
    that pass ending, and the special modes changing mid-pass — an array
    forming, a DDOR fix starting. Everything else is the same pass continuing.
    """
    was = {l.key: l for l in before}
    now_by_key = {l.key: l for l in after}
    events: list[dict] = []

    for key, link in now_by_key.items():
        flags = sorted(
            f
            for f, on in (
                ("arrayed", link.arrayed),
                ("mspa", link.mspa),
                ("ddor", link.ddor),
            )
            if on
        )
        if key not in was:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "appear",
                    "dish": link.dish,
                    "craft": link.craft,
                    "band": link.band,
                    "bps": link.down_bps,
                    "flags": flags,
                }
            )
        else:
            old = was[key]
            before_flags = sorted(
                f
                for f, on in (
                    ("arrayed", old.arrayed),
                    ("mspa", old.mspa),
                    ("ddor", old.ddor),
                )
                if on
            )
            if before_flags != flags:
                events.append(
                    {
                        "t": round(now, 1),
                        "event": "flags",
                        "dish": link.dish,
                        "craft": link.craft,
                        "flags": flags,
                    }
                )

    for key, old in was.items():
        if key not in now_by_key:
            events.append(
                {
                    "t": round(now, 1),
                    "event": "vanish",
                    "dish": old.dish,
                    "craft": old.craft,
                }
            )
    return events


def append_history(events: list[dict]) -> None:
    """Append, and keep the file bounded. Never fatal: losing history is not
    a reason to stop showing the sky."""
    if not events:
        return
    try:
        _settings.HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _settings.HISTORY_PATH.open("a") as fh:
            for event in events:
                fh.write(json.dumps(event) + "\n")
        if _settings.HISTORY_PATH.stat().st_size > HISTORY_MAX_BYTES:
            kept = _settings.HISTORY_PATH.read_text().splitlines()[-HISTORY_MAX_LINES:]
            _settings.HISTORY_PATH.write_text("\n".join(kept) + "\n")
    except OSError as exc:
        _limits.logger.debug("history append failed: %s", exc)


def _history_observation(event: object) -> tuple[str, float] | None:
    """Validate one persisted arrival without trusting JSON's shape or numbers."""
    if not isinstance(event, dict) or event.get("event") != "appear":
        return None
    craft, timestamp = event.get("craft"), event.get("t")
    if not isinstance(craft, str) or not craft:
        return None
    if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float, str)):
        return None
    try:
        when = float(timestamp)
    except (ValueError, OverflowError):
        return None
    if not math.isfinite(when) or when < 0:
        return None
    return craft.lower(), when


def load_history(state: _model.State) -> None:
    """Rebuild 'have I seen this craft before' from the log.

    A corrupt or half-written line is skipped rather than fatal — this file is
    appended to by a process that can be SIGKILLed mid-write.
    """
    state.seen = {}
    try:
        content = _settings.HISTORY_PATH.read_bytes()
    except OSError:
        return
    for line in content.splitlines():
        try:
            event = json.loads(line)
        except (ValueError, UnicodeError):
            continue
        observation = _history_observation(event)
        if observation is None:
            continue
        craft, when = observation
        record = state.seen.setdefault(
            craft, {"first": when, "last": when, "passes": 0}
        )
        record["first"] = min(record["first"], when)
        record["last"] = max(record["last"], when)
        record["passes"] += 1
    if state.seen:
        _limits.logger.info(
            "history: %d craft seen before, %d passes",
            len(state.seen),
            sum(r["passes"] for r in state.seen.values()),
        )


def note_seen(state: _model.State, events: list[dict]) -> None:
    """Keep the in-memory view current without re-reading the file."""
    for event in events:
        if event["event"] != "appear":
            continue
        craft = event["craft"].lower()
        record = state.seen.setdefault(
            craft, {"first": event["t"], "last": event["t"], "passes": 0}
        )
        record["last"] = event["t"]
        record["passes"] += 1

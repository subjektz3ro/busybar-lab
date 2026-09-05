"""DSN audio / policy."""

from __future__ import annotations

from dataclasses import replace

from apps.dsn_app import limits as _limits
from apps.dsn_app import model as _model
from apps.dsn_app import source as _source
from apps.dsn_app.audio import words as _audio_words


def narration_ready(state: _model.State, link: _source.Link) -> bool:
    """Is this line worth baking yet?

    spoken() silently drops whole sentences when its inputs have not arrived:
    an empty name table gives the bare feed code instead of the full name, and
    an unresolved range removes BOTH the distance and the light-time
    sentences. prebake starts two seconds after launch, while fetch_names is
    still in flight and Horizons has not answered, so without this gate it
    caches a description of a spacecraft the app barely knows anything about
    -- and the content hash then keeps that stub forever. Measured on device:
    complete lines run 15-25 seconds, the stubs 1.4 to 4.6.
    """
    if not state.names:
        return False  # no full name yet
    if (
        (link.range_km is None or link.range_km <= 0)
        and link.naif
        and link.naif not in state.range_unavailable
    ):
        return False  # Horizons may still answer
    return True


def observe_narration(state: _model.State, link: _source.Link) -> str | None:
    """Freeze one stable script for the lifetime of a dish/craft pass.

    Live telemetry still drives the pixels. Speech deliberately samples it
    only after two identical coarse scripts so a one-dB or rounding-boundary
    wobble cannot keep the Pi synthesising forever.
    """
    if not narration_ready(state, link):
        return None
    # The background worker wakes every two seconds, but NASA's snapshot does
    # not. Counting worker ticks would always freeze the very first eligible
    # feed sample; require two distinct source timestamps instead.
    observation = state.feed_timestamp_ms
    if observation is None:
        return None
    text = _audio_words.spoken(link, state.names, state.dish_types)
    frozen = state.narration_texts.get(link.key)
    # A pass gets one stable script.  Telemetry may keep changing after the
    # first two-source-snapshot freeze, but allowing a second pair to replace
    # it would churn TTS and its on-device cache for the entire pass.
    if frozen is not None:
        return frozen
    previous, count, last_observation = state.narration_candidates.get(
        link.key, ("", 0, -1)
    )
    if previous != text:
        count = 1
    elif observation != last_observation:
        count += 1
    state.narration_candidates[link.key] = (text, count, observation)
    if count < 2:
        return frozen
    state.narration_texts[link.key] = text
    state.narration_frozen_at[link.key] = observation
    state.narration_candidates.pop(link.key, None)
    return text


def request_narration(
    state: _model.State, link: _source.Link, name: str | None
) -> _model.NarrationRequest:
    """Remember one cold START without confusing it with bake priority.

    The exact generation prevents an old worker completing after the user has
    moved from producing a stale READY/ERROR notice. Repeated START presses on
    the same unresolved line reuse the request and do not duplicate work.
    """
    current = state.narration_request
    if (
        current is not None
        and current.key == link.key
        and current.name == name
        and current.view == state.view
    ):
        state.narration_priority = link.key
        return current
    state.narration_request_counter += 1
    request = _model.NarrationRequest(
        state.narration_request_counter, link.key, name, state.view
    )
    state.narration_request = request
    state.narration_notice = None
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0
    state.narration_priority = link.key
    return request


def bind_narration_request(
    state: _model.State, key: str, name: str
) -> _model.NarrationRequest | None:
    """Bind a waiting START intent to the stable script chosen by prebake."""
    request = state.narration_request
    if request is None or request.key != key:
        return None
    if request.name != name:
        request = replace(request, name=name)
        state.narration_request = request
    return request


def clear_narration_request(state: _model.State) -> None:
    """Invalidate UI intent; useful cache work may still finish silently."""
    state.narration_request_counter += 1
    state.narration_request = None
    state.narration_notice = None
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0


def finish_narration_request(
    state: _model.State, request: _model.NarrationRequest | None, label: str
) -> bool:
    """Queue terminal feedback only for the exact still-current START."""
    if request is None or state.narration_request != request:
        return False
    if label not in {_limits.NARRATION_READY, _limits.NARRATION_ERROR}:
        raise ValueError(f"invalid narration notice: {label}")
    state.narration_request = None
    state.narration_notice = _model.NarrationNotice(
        request.generation, request.key, request.name, request.view, label
    )
    state.narration_notice_retry_at = 0.0
    state.narration_notice_failures = 0
    state.dirty.set()  # wake the scheduler; no scene upload
    return True


def narration_notice_backoff_s(failures: int) -> float:
    """Back off a refused toast without losing its exact user intent."""
    return 2.0 if failures <= 0 else min(30.0, 2.0**failures)

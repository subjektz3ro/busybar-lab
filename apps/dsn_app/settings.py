"""Process configuration, applied explicitly at startup; safe defaults on import.

Consumers retain this module, not copies of its mutable settings. The app has
one runtime per process; configure before starting its tasks.
"""

from __future__ import annotations

import os

from apps.dsn_app import config as _config
from apps.dsn_app import limits as _limits
from busybar_dev import load_env

POLL_S = _config.DEFAULT_DSN_CONFIG.poll_s

ROTATE_S = _config.DEFAULT_DSN_CONFIG.rotate_s

VOICE = _config.DEFAULT_DSN_CONFIG.voice

DEFAULT_VIEW = _config.DEFAULT_DSN_CONFIG.default_view

DSN_NETWORK_STYLE = _config.DEFAULT_DSN_CONFIG.network_style

FEED_DELAYED_S = max(POLL_S * 2.5, 15.0)

FEED_STALE_S = max(POLL_S * 5.0, 35.0)

# A FRESH claim is a short native-element lease, separate from the animation.
# NASA's advancing source timestamp moves a two-LED native dash without touching
# (and therefore restarting) the native carrier loop. If this process dies,
# the live claim expires soon after a running process would draw FEED DELAY.
LIVE_LEASE_TIMEOUT_S = max(30, int(FEED_DELAYED_S + POLL_S))

# Plain imports use documented public-safe defaults. Parsing owner environment,
# resolving custom cache paths and emitting configuration warnings happen only
# through configure_runtime(), immediately before the CLI starts work.
RUNTIME_CONFIG = _config.DEFAULT_DSN_CONFIG

MANAGED_CACHE_ROOT = _config.DEFAULT_DSN_CONFIG.managed_cache_root

CACHE_DIR = _config.DEFAULT_DSN_CONFIG.cache_dir

RANGE_CACHE = CACHE_DIR / "dsn_ranges.json"

HISTORY_PATH = CACHE_DIR / "dsn_history.jsonl"


def apply_runtime_config(config: _config.DsnConfig) -> None:
    """Apply one validated immutable configuration before starting app tasks."""
    global CACHE_DIR, DEFAULT_VIEW, DSN_NETWORK_STYLE, FEED_DELAYED_S
    global FEED_STALE_S, HISTORY_PATH, LIVE_LEASE_TIMEOUT_S
    global MANAGED_CACHE_ROOT, POLL_S, RANGE_CACHE, ROTATE_S, RUNTIME_CONFIG
    global VOICE

    RUNTIME_CONFIG = config
    POLL_S = config.poll_s
    ROTATE_S = config.rotate_s
    VOICE = config.voice
    DEFAULT_VIEW = config.default_view
    DSN_NETWORK_STYLE = config.network_style
    MANAGED_CACHE_ROOT = config.managed_cache_root
    CACHE_DIR = config.cache_dir
    RANGE_CACHE = CACHE_DIR / "dsn_ranges.json"
    HISTORY_PATH = CACHE_DIR / "dsn_history.jsonl"
    FEED_DELAYED_S = max(POLL_S * 2.5, 15.0)
    FEED_STALE_S = max(POLL_S * 5.0, 35.0)
    LIVE_LEASE_TIMEOUT_S = max(30, int(FEED_DELAYED_S + POLL_S))
    for warning in config.warnings:
        _limits.logger.warning("%s", warning)


def configure_runtime() -> _config.DsnConfig:
    """Load owner dotenv values, validate the resulting environment and apply it."""
    load_env()
    config = _config.parse_runtime_config(os.environ)
    apply_runtime_config(config)
    return config

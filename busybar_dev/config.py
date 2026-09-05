"""Shared environment parsing and log-safe error reporting.

There were three implementations of "read KEY=value lines", and they differed
in exactly the two places that matter:

    apps/skystrip.py    stripped surrounding quotes,  kept blank values
    busybar_dev         did NOT strip quotes,         DROPPED blank values
    barkeep/configstore did NOT strip quotes,         kept blank values

That is not cosmetic. barkeep calls the middle one at startup, folds the result
into its own environment, and hands that to every child it spawns. So a
perfectly ordinary quoted `.env`:

    SKYSTRIP_LAT="51.5074"

put the literal string `"51.5074"` into the daemon's environment; the child's own
parser would have stripped the quotes, but it uses `setdefault`, so it could
not correct a value that was already set. `float('"51.5074"')` then raised at
import — a crash loop with the display dark, which is precisely the failure the
comment above `_coordinate` claims to have designed away. Running the same app
by hand with the same `.env` worked, which is what made it expensive to find.

Two behaviours are deliberate here:

**Quotes.** Hand-edited files follow the usual dotenv convention, so matched
surrounding quotes are stripped. Machine-written files (`config/<app>.env`,
which barkeep's UI writes verbatim) are read literally — quoting there would
break the round-trip between what an operator types and what the editor shows
them back. Same code, one named flag, rather than two implementations that
drift.

**Blank values.** `KEY=` is kept, everywhere. It is the documented way to say
"explicitly blank" (an anonymous NWS contact, an auto-discovered station) as
opposed to "not set". Dropping it was the old busybar_dev behaviour and made
that distinction unrepresentable for anything launched through barkeep. This is
safe to change because every reader uses an `or`-default, which the busybar-app
skill requires precisely so that blank and missing behave alike.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

log = logging.getLogger(__name__)

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"

# Anything readable by group or other.
_SHARED_MODE_BITS = 0o077
_warned_paths: set[str] = set()


def _unquote(value: str) -> str:
    """Strip one matched pair of surrounding quotes, and nothing else."""
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def is_single_line(value: str) -> bool:
    """Match every separator accepted by str.splitlines(), plus env's NUL ban.

    Checking only CR/LF lets a Unicode line separator forge another key when
    a machine-written override is read back through parse_env_text().
    """
    return not any(char in value for char in "\n\r\v\f\x1c\x1d\x1e\x85\u2028\u2029\0")


def parse_env_text(text: str, *, strip_quotes: bool = False) -> dict[str, str]:
    """Parse `KEY=value` lines. Later duplicates win, blanks are preserved."""
    values: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not key:
            continue
        values[key] = _unquote(value) if strip_quotes else value
    return values


def read_env_file(path: Path, *, strip_quotes: bool = False) -> dict[str, str]:
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text(), strip_quotes=strip_quotes)


def secret_file_warning(path: Path) -> str:
    """A warning if a secret-bearing file is readable beyond its owner.

    `.env` holds BUSYBAR_TOKEN and the coordinates. barkeep's own config store
    writes `config/<app>.env` through mkstemp and so gets 0600 for free; the
    hand-written `.env` comes from deploy/install.sh under the default umask,
    and was found at 0644 on a live host — the less sensitive file was the
    better protected one.
    """
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        return ""
    if not mode & _SHARED_MODE_BITS:
        return ""
    return (f"{path} is mode {mode:03o} and holds BUSYBAR_TOKEN — every local "
            f"account can read it. Fix with: chmod 600 {path}")


def load_env(path: Path | None = None) -> dict[str, str]:
    """Fold the repo-root `.env` into `os.environ`. Existing vars win.

    Lives here, not just in apps, so every `connect()` caller — installer
    checks, one-off scripts, tests — sees the same bar config, parsed the same
    way.
    """
    env_path = ENV_PATH if path is None else path
    if not env_path.is_file():
        return {}
    # Warn, never chmod: the file is the operator's, and silently widening or
    # narrowing someone's permissions is not this function's business.
    warning = secret_file_warning(env_path)
    if warning and str(env_path) not in _warned_paths:
        _warned_paths.add(str(env_path))
        log.warning("%s", warning)
    loaded = read_env_file(env_path, strip_quotes=True)
    for key, value in loaded.items():
        os.environ.setdefault(key, value)
    return loaded


# --- keeping coordinates out of logs ---------------------------------------

# A decimal-degree pair, in a URL query or a path segment. Deliberately loose:
# it is a backstop, not the primary defence.
_COORD_PAIR = re.compile(
    r"(-?\d{1,3}\.\d{3,})\s*(?:,|%2C|&[a-z_]*=|/)\s*(-?\d{1,3}\.\d{3,})",
    re.IGNORECASE)


def redact_coordinates(text: str) -> str:
    """Replace decimal-degree pairs with a placeholder."""
    return _COORD_PAIR.sub("<lat>,<lon>", text)


def describe_exception(exc: BaseException) -> str:
    """A log-safe rendering of a request failure.

    httpx puts the full request URL in HTTPStatusError.__str__, and every
    weather call this repo makes carries the coordinates in its query or path:

        Client error '404 Not Found' for url
        'https://api.weather.gov/alerts/active?point=51.5074%2C-0.1278'

    Those lines reach the app's stdout, barkeep's 1000-line ring,
    logs/<app>.log and journald. A Barkeep operator can expose that log endpoint
    to the LAN, so coordinates must not reach any of those sinks.

    So: the exception type and, where there is one, the status code. Never the
    exception's own message.
    """
    name = type(exc).__name__
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status is not None:
        return f"{name} (HTTP {status})"
    request = getattr(exc, "request", None)
    if request is not None:
        # Transport failures carry a URL but no response. The host is safe to
        # name — it is a public API — but the query string is not.
        url = getattr(request, "url", None)
        return f"{name} on {getattr(url, 'host', '?')}"
    detail = redact_coordinates(str(exc))
    return f"{name}: {detail}" if detail else name


class CoordinateRedactingFilter(logging.Filter):
    """Last-resort scrub, so the next code path that formats a URL is covered
    by construction rather than by review."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - never break logging
            return True
        scrubbed = redact_coordinates(message)
        if scrubbed != message:
            record.msg, record.args = scrubbed, ()
        return True

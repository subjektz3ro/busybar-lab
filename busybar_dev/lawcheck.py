"""Static checks for draw-request laws not enforced by busylib's models.

busylib's pydantic models already bound priorities and coordinates. They do
not enforce the firmware's application/element identifier patterns or text
contract: text is non-empty printable ASCII (`^[\\x20-\\x7E]+$`, API 25.0.0),
and one `Zürich` from a live feed rejects the *entire* draw with HTTP 400. The
classic failure is an app that works all week and blanks the moment a feed
serves an accented name. Duplicate ids are different: the request may be
accepted, but one element silently loses its identity. Checking the complete
built payload catches both classes in `--dry-run`, before a device sees it.

Findings are strings, not exceptions: a dry run should report every problem
at once, and callers decide whether findings are fatal.
"""

from __future__ import annotations

import re

from busylib import types

_PRINTABLE_ASCII = re.compile(r"[\x20-\x7E]+\Z")
_DEVICE_IDENTIFIER = re.compile(r"[a-zA-Z0-9._-]+\Z")


def _describe_bad_text(value: str) -> str:
    if not value:
        return "is empty"
    bad = sorted({char for char in value if not _PRINTABLE_ASCII.fullmatch(char)})
    shown = ", ".join(repr(char) for char in bad[:5])
    return f"contains non-printable-ASCII {shown}"


def check_application_name(application_name: str) -> list[str]:
    """Return a finding when an app name would be rejected by the API."""

    if _DEVICE_IDENTIFIER.fullmatch(application_name):
        return []
    return [
        f"application_name {application_name!r} does not match the device "
        "pattern ^[a-zA-Z0-9._-]+$"
    ]


def check_display_elements(payload: types.DisplayElements) -> list[str]:
    """Return every rejection or silent identity-loss finding in a payload."""

    findings = check_application_name(payload.application_name)
    seen_ids: set[str] = set()
    for element in payload.elements:
        identity = getattr(element, "id", None)
        if not isinstance(identity, str) or not _DEVICE_IDENTIFIER.fullmatch(identity):
            findings.append(
                f"element id {identity!r} does not match the device pattern "
                "^[a-zA-Z0-9._-]+$"
            )
        if identity in seen_ids:
            findings.append(
                f"element id {identity!r} appears twice in one payload; the "
                "device treats ids as identities, so one of these is lost"
            )
        if isinstance(identity, str):
            seen_ids.add(identity)
        text = getattr(element, "text", None)
        if isinstance(text, str) and not _PRINTABLE_ASCII.fullmatch(text):
            findings.append(
                f"element {identity!r} text {_describe_bad_text(text)}; the "
                "API requires one or more printable ASCII characters and "
                "invalid text rejects the whole draw with HTTP 400 — "
                "transliterate before drawing"
            )
    return findings

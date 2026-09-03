"""Smoke test: draw centered text on the front display, screenshot it, clear.

    uv run apps/hello.py                     # draw HELLO, save proof PNGs, keep it up
    uv run apps/hello.py --text "BRB 5 MIN"  # any text; long text scrolls
    uv run apps/hello.py --clear             # take it back down
    uv run apps/hello.py --say "hello world" # also speak through the bar
    uv run apps/hello.py --dry-run            # build the request, touch no device
"""

from __future__ import annotations

import argparse
import logging

from busylib import types

from busybar_dev import connect
from busybar_dev.screen import save_screens
from busybar_dev.device import is_refusal
from busybar_dev.tts import say_on_bar

APP_NAME = "hello"
logger = logging.getLogger(__name__)


def build_payload(text: str) -> types.DisplayElements:
    element = types.TextElement(
        id="hello",
        type="text",
        text=text,
        font="condensed",
        align="center",
        x=36,
        y=8,
        display=types.DisplayName.FRONT,
    )
    if len(text) > 12:  # roughly wider than the 72 px panel in condensed
        element.scroll_rate = 1400  # pixels per MINUTE
    return types.DisplayElements(application_name=APP_NAME, elements=[element])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--text", default="HELLO")
    parser.add_argument("--say", metavar="TEXT", help="also speak TEXT out loud")
    parser.add_argument("--clear", action="store_true")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report the requested operation without touching the device",
    )
    parser.add_argument("--shots", default="scratch", help="directory for proof PNGs")
    args = parser.parse_args()

    if args.dry_run:
        if args.clear:
            logger.info("Dry run: would clear application %r.", APP_NAME)
            return
        payload = build_payload(args.text)
        logger.info("Dry run payload: %s", payload.model_dump(exclude_none=True))
        if args.say:
            logger.info("Dry run speech: %r", args.say)
        return

    try:
        bar = connect()
    except ConnectionError as exc:
        # This is the README's first command; a stranger with no bar plugged
        # in yet deserves the message, not the traceback around it.
        raise SystemExit(
            f"{exc}\nNothing was drawn. --dry-run exercises the payload "
            "offline; apps/hello.md covers connection setup.") from None
    with bar as bb:
        version = bb.version()
        logger.info("Connected: API %s", version.api_semver)
        if args.clear:
            bb.display_clear(application_name=APP_NAME)
            logger.info("Cleared.")
            return
        try:
            bb.display_draw(build_payload(args.text))
        except Exception as exc:
            # AGENTS.md names this command as the stack smoke test, so it is
            # exactly what gets run while a focus session is up. Yield, do not
            # traceback.
            if not is_refusal(exc):
                raise
            logger.info("A BUSY/CUSTOM session owns the display; nothing drawn.")
            return
        front, back = save_screens(bb, args.shots)
        logger.info("Drawn %r — proof: %s, %s", args.text, front, back)
        if args.say:
            try:
                say_on_bar(bb, args.say, app_name=APP_NAME)
            except Exception as exc:
                # audio_play refuses during a session the same way a draw does.
                if not is_refusal(exc):
                    raise
                logger.info("A BUSY/CUSTOM session owns audio; nothing spoken.")
                return
            logger.info("Spoke: %r", args.say)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()

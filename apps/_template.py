"""Template for a new BUSY Bar app — generate a registered copy, then edit.

    uv run scripts/new_app.py my_idea
    uv run apps/my_idea.py --dry-run
    uv run apps/my_idea.py --clear

Shape follows docs/busylib/AGENTS.md: config -> payload as data -> send.

If the app renders raster frames (rather than native text elements like this
template), also expose one pure zero-argument seam so agents can see it
offline, and register it as data in apps.toml:

    def render_visual():
        return {"front": (frames, fps)}   # deterministic; no I/O, no clock

    # apps.toml:  [my_idea.viz]
    #             renderer = "apps.my_idea:render_visual"

That registers `my_idea/default`; CI separately runs its declared audits and
checks its accepted pixel baseline for drift. See docs/busybar-viz.md.
"""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass

from busylib import types

from busybar_dev import connect
from busybar_dev.device import is_refusal
from busybar_dev.lawcheck import check_application_name, check_display_elements

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class Config:
    app_name: str
    text: str
    priority: int
    dry_run: bool
    clear: bool = False


def parse_args() -> Config:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-name", default="my-idea")
    parser.add_argument("--text", default="HELLO")
    # Idle built-in apps draw at ~10; an active BUSY/CUSTOM session at 90.
    parser.add_argument("--priority", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--clear", action="store_true",
        help="remove this app's draw and exit",
    )
    args = parser.parse_args()
    return Config(args.app_name, args.text, args.priority, args.dry_run, args.clear)


def build_payload(config: Config) -> types.DisplayElements:
    return types.DisplayElements(
        application_name=config.app_name,
        priority=config.priority,
        elements=[
            types.TextElement(
                id="status",
                type="text",
                text=config.text,
                font="condensed",
                align="center",
                x=36,
                y=8,
                display=types.DisplayName.FRONT,
            )
        ],
    )


def run(config: Config) -> None:
    if config.clear:
        findings = check_application_name(config.app_name)
        for finding in findings:
            logger.warning("law check: %s", finding)
        if config.dry_run:
            logger.info("Dry run: would clear application %r.", config.app_name)
            if findings:
                raise SystemExit(1)
            return
        if findings:
            raise SystemExit("refusing an operation that violates device laws")
        with connect() as bb:
            bb.version()
            try:
                bb.display_clear(application_name=config.app_name)
            except Exception as exc:
                # Clear may be refused by the same BUSY/CUSTOM session that
                # refuses draws. It is cleanup, so yield without a traceback.
                if not is_refusal(exc):
                    raise
                logger.info(
                    "A BUSY/CUSTOM session owns the display; nothing cleared."
                )
                return
            logger.info("Cleared application %r.", config.app_name)
        return

    payload = build_payload(config)
    # Some device laws reject the whole request; duplicate element identities
    # instead lose content silently. Catch both before hardware sees them.
    findings = check_display_elements(payload)
    for finding in findings:
        logger.warning("law check: %s", finding)
    if config.dry_run:
        logger.info("Dry run payload: %s", payload.model_dump(exclude_none=True))
        if findings:
            raise SystemExit(1)
        return
    if findings:
        raise SystemExit("refusing a payload that violates device draw laws")
    with connect() as bb:
        bb.version()
        try:
            bb.display_draw(payload)
        except Exception as exc:
            # 409 is normal operation, not a failure: an active BUSY/CUSTOM
            # session refuses every outside draw, at any priority. Yield
            # quietly — a traceback here is what greets you the first time you
            # test during a focus session, and this file is the one every new
            # app is copied from.
            if not is_refusal(exc):
                raise
            logger.info("A BUSY/CUSTOM session owns the display; nothing drawn.")
            return
        logger.info("Drawn. Rerun this app with --clear to remove it.")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    run(parse_args())

"""Skystrip launcher and offline renderer compatibility entry points."""

import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.skystrip_app.render.effects import (  # noqa: E402
    render_lightning_segment as render_lightning_segment,
)
from apps.skystrip_app.render.scene import (  # noqa: E402
    render_loop_frames as render_loop_frames,
)
from apps.skystrip_app.render.scene import (
    render_scene as render_scene,
)
from apps.skystrip_app.render.scene import (
    render_sky as render_sky,
)
from apps.skystrip_app.weather import WeatherState as WeatherState  # noqa: E402


def main() -> None:
    """Keep provider and device imports off the offline renderer path."""
    from apps.skystrip_app.cli import main as run_cli

    run_cli()


if __name__ == "__main__":
    main()

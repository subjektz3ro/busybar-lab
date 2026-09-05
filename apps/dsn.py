"""DSN launcher and stable offline renderers; implementation is in dsn_app/."""

import sys
from pathlib import Path

# Direct file execution puts apps/, not the checkout root, on sys.path.
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from apps.dsn_app.render.examples import (  # noqa: E402
    render_distance_visual as render_distance_visual,
)
from apps.dsn_app.render.examples import (
    render_instrument_visual as render_instrument_visual,
)
from apps.dsn_app.render.examples import (
    render_visual as render_visual,
)


def main() -> None:
    """Keep network, audio and runtime imports off the offline renderer path."""
    from apps.dsn_app.cli import main as run_cli

    run_cli()


if __name__ == "__main__":
    main()

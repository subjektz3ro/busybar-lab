"""One-shot TTS worker used to release neural-model memory after a bake."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .tts import synth_snd


def render(text: str, voice: str, output: Path) -> None:
    """Render one line to a private raw-PCM file without using stdout."""
    output.write_bytes(synth_snd(text, voice))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--voice", required=True)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    render(sys.stdin.read(), args.voice, args.output)


if __name__ == "__main__":
    main()

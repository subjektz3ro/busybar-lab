"""Deterministic visual evidence for BUSY Bar applications."""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version

__all__ = ["__version__"]

try:
    __version__ = version("busybar-lab")
except PackageNotFoundError:  # source tree used without installing the project
    __version__ = "0+unknown"

"""All supported launch modes dispatch exactly once to the same CLI owner."""

import importlib
import runpy
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
@pytest.mark.parametrize("mode", ["script", "module", "package"])
def test_launch_modes_dispatch_once_without_starting_real_work(app, mode, monkeypatch):
    cli = importlib.import_module(f"apps.{app}_app.cli")
    main = Mock()
    monkeypatch.setattr(cli, "main", main)
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(sys, "argv", [f"apps/{app}.py", "--once"])
    if mode == "script":
        runpy.run_path(str(ROOT / f"apps/{app}.py"), run_name="__main__")
    else:
        module = f"apps.{app}" + ("_app" if mode == "package" else "")
        monkeypatch.delitem(sys.modules, module, raising=False)
        runpy.run_module(module, run_name="__main__")
    main.assert_called_once_with()


@pytest.mark.parametrize("app", ["dsn", "skystrip"])
def test_importing_a_launcher_does_not_start_the_cli(app, monkeypatch):
    cli = importlib.import_module(f"apps.{app}_app.cli")
    main = Mock()
    monkeypatch.setattr(cli, "main", main)
    monkeypatch.delitem(sys.modules, f"apps.{app}", raising=False)
    launcher = importlib.import_module(f"apps.{app}")
    main.assert_not_called()
    launcher.main()
    main.assert_called_once_with()

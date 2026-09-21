"""Runtime paths shared by source and frozen builds."""
from __future__ import annotations

import sys
from pathlib import Path


def application_dir() -> Path:
    """Directory containing the source tree or the packaged executable."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def data_dir() -> Path:
    """Persistent service data beside the program/executable."""
    return application_dir() / "data"


def bundled_web_dir() -> Path:
    """Web assets, including PyInstaller's extracted one-file bundle."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    return Path(bundle_root) / "web" if bundle_root else application_dir() / "web"


def runtime_command() -> list[str]:
    """Command used by autostart and the settings page."""
    if getattr(sys, "frozen", False):
        return [str(Path(sys.executable).resolve())]
    return [sys.executable, str((application_dir() / "research_server.py").resolve())]

from __future__ import annotations

import sys
from pathlib import Path

APP_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_VALUE_NAME = "G2GPriceTracker"


def startup_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{Path(sys.executable).resolve()}"'
    launcher = Path(__file__).resolve().parents[2] / "launcher.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    executable = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{executable.resolve()}" "{launcher.resolve()}"'


def set_startup_enabled(enabled: bool) -> None:
    if sys.platform != "win32":
        return
    import winreg

    with winreg.OpenKey(
        winreg.HKEY_CURRENT_USER,
        APP_RUN_KEY,
        0,
        winreg.KEY_SET_VALUE,
    ) as key:
        if enabled:
            winreg.SetValueEx(key, APP_VALUE_NAME, 0, winreg.REG_SZ, startup_command())
        else:
            try:
                winreg.DeleteValue(key, APP_VALUE_NAME)
            except FileNotFoundError:
                pass

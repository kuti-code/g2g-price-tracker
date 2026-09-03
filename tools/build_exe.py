from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def build_command(project_root: Path) -> list[str]:
    asset_dir = project_root / "assets"
    data_separator = os.pathsep
    return [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--onefile",
        "--windowed",
        "--distpath",
        str(project_root),
        "--workpath",
        str(project_root / "build"),
        "--specpath",
        str(project_root / "build"),
        "--name",
        "G2GPriceTracker",
        "--paths",
        str(project_root / "src"),
        "--collect-all",
        "pystray",
        "--icon",
        str(asset_dir / "g2g-price-tracker.ico"),
        "--add-data",
        f"{asset_dir / 'g2g-price-tracker.png'}{data_separator}assets",
        "--add-data",
        f"{asset_dir / 'g2g-price-tracker.ico'}{data_separator}assets",
        str(project_root / "launcher.py"),
    ]


def main() -> None:
    project_root = Path(__file__).resolve().parents[1]
    subprocess.run(build_command(project_root), cwd=project_root, check=True)


if __name__ == "__main__":
    main()

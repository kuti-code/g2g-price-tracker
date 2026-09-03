from __future__ import annotations

import sys
from pathlib import Path


def resource_path(*parts: str) -> Path:
    """Resolve bundled assets in development and PyInstaller one-file builds."""
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        return Path(bundle_root).joinpath(*parts)

    development_path = Path(__file__).resolve().parents[2].joinpath(*parts)
    if development_path.exists():
        return development_path
    return Path(__file__).resolve().parent.joinpath(*parts)

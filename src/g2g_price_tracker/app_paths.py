import os
import sys
from dataclasses import dataclass
from pathlib import Path

APP_DIRECTORY = "G2GPriceTracker"
PUBLISHER_DIRECTORY = "kuti-code"


@dataclass(frozen=True, slots=True)
class AppPaths:
    root: Path
    database: Path
    settings: Path
    telegram_secret: Path
    log: Path

    @classmethod
    def default(cls) -> "AppPaths":
        if sys.platform == "win32":
            base = Path(os.getenv("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
        else:
            base = Path(os.getenv("XDG_DATA_HOME", Path.home() / ".local" / "share"))

        root = base / PUBLISHER_DIRECTORY / APP_DIRECTORY
        return cls(
            root=root,
            database=root / "data" / "prices.db",
            settings=root / "settings.json",
            telegram_secret=root / "telegram-token.dat",
            log=root / "logs" / "tracker.log",
        )

    def ensure(self) -> None:
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.log.parent.mkdir(parents=True, exist_ok=True)

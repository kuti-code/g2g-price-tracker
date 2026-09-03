import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .config import DEFAULT_G2G_URL, DEFAULT_INTERVAL_MINUTES, DEFAULT_SELLER


@dataclass(slots=True)
class AppSettings:
    seller: str = DEFAULT_SELLER
    interval_minutes: int = DEFAULT_INTERVAL_MINUTES
    source_url: str = DEFAULT_G2G_URL
    price_alert_enabled: bool = False
    price_alert_threshold: str = ""
    sound_alert_enabled: bool = True
    sound_duration_seconds: int = 10
    sound_until_dismissed: bool = False
    telegram_enabled: bool = False
    telegram_chat_id: str = ""
    alert_once_until_recovery: bool = True
    start_with_windows: bool = False
    minimize_to_tray_on_close: bool = True
    theme: str = "light"

    @classmethod
    def load(cls, path: str | Path) -> "AppSettings":
        settings_path = Path(path)
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            return cls(
                seller=str(data.get("seller", DEFAULT_SELLER)).strip() or DEFAULT_SELLER,
                interval_minutes=max(1, int(data.get("interval_minutes", 10))),
                source_url=str(data.get("source_url", DEFAULT_G2G_URL)).strip() or DEFAULT_G2G_URL,
                price_alert_enabled=bool(data.get("price_alert_enabled", False)),
                price_alert_threshold=str(data.get("price_alert_threshold", "")).strip(),
                sound_alert_enabled=bool(data.get("sound_alert_enabled", True)),
                sound_duration_seconds=max(1, int(data.get("sound_duration_seconds", 10))),
                sound_until_dismissed=bool(data.get("sound_until_dismissed", False)),
                telegram_enabled=bool(data.get("telegram_enabled", False)),
                telegram_chat_id=str(data.get("telegram_chat_id", "")).strip(),
                alert_once_until_recovery=bool(data.get("alert_once_until_recovery", True)),
                start_with_windows=bool(data.get("start_with_windows", False)),
                minimize_to_tray_on_close=bool(data.get("minimize_to_tray_on_close", True)),
                theme="dark" if str(data.get("theme", "light")).lower() == "dark" else "light",
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return cls()

    def save(self, path: str | Path) -> None:
        settings_path = Path(path)
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = settings_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(asdict(self), indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(settings_path)

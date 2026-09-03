from __future__ import annotations

import json
import sys
import threading
from decimal import Decimal
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def should_trigger_price_alert(
    price: Decimal,
    threshold: Decimal,
    *,
    armed: bool,
    once_until_recovery: bool,
) -> bool:
    return price < threshold and (armed or not once_until_recovery)


def send_telegram_message(
    bot_token: str,
    chat_id: str,
    text: str,
    *,
    timeout: float = 15.0,
) -> None:
    if not bot_token.strip() or not chat_id.strip():
        raise ValueError("Telegram bot token and chat ID are required.")
    payload = urlencode({"chat_id": chat_id.strip(), "text": text}).encode("utf-8")
    request = Request(
        f"https://api.telegram.org/bot{bot_token.strip()}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Telegram rejected the request ({exc.code}): {detail}") from exc
    except (URLError, TimeoutError) as exc:
        raise RuntimeError(f"Telegram could not be reached: {exc}") from exc
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Telegram returned an invalid response.") from exc
    if not result.get("ok"):
        raise RuntimeError(str(result.get("description") or "Telegram notification failed."))


class SoundAlarm:
    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def is_active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self, duration_seconds: int | None) -> None:
        self.stop()
        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            args=(duration_seconds, self._stop_event),
            daemon=True,
            name="price-alert-sound",
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()

    @staticmethod
    def _run(duration_seconds: int | None, stop_event: threading.Event) -> None:
        if sys.platform != "win32":
            return
        import winsound

        if duration_seconds is not None:
            remaining = max(1, duration_seconds) * 1000
        else:
            remaining = None

        while not stop_event.is_set() and (remaining is None or remaining > 0):
            winsound.Beep(880, 260)
            if stop_event.wait(0.14):
                break
            winsound.Beep(660, 260)
            if stop_event.wait(0.34):
                break
            if remaining is not None:
                remaining -= 1000

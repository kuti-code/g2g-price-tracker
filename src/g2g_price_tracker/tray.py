from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PIL import Image, ImageDraw


class TrayController:
    def __init__(
        self,
        *,
        open_app: Callable[[], None],
        check_now: Callable[[], None],
        pause_tracking: Callable[[], None],
        show_last_price: Callable[[], None],
        exit_app: Callable[[], None],
        icon_path: str | Path | None = None,
    ) -> None:
        self.callbacks = (open_app, check_now, pause_tracking, show_last_price, exit_app)
        self.icon_path = Path(icon_path) if icon_path else None
        self.icon = None

    def _image(self) -> Image.Image:
        if self.icon_path and self.icon_path.is_file():
            with Image.open(self.icon_path) as source:
                return source.convert("RGBA").resize((64, 64), Image.Resampling.LANCZOS)
        image = Image.new("RGBA", (64, 64), "#0f766e")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((5, 5, 59, 59), radius=13, outline="#ffffff", width=3)
        draw.line((15, 44, 27, 31, 37, 36, 50, 18), fill="#ffffff", width=5)
        return image

    def start(self) -> None:
        if self.icon is not None:
            return
        import pystray

        open_app, check_now, pause, last_price, exit_app = self.callbacks
        self.icon = pystray.Icon(
            "G2GPriceTracker",
            self._image(),
            "G2G Price Tracker",
            menu=pystray.Menu(
                pystray.MenuItem("Open", lambda _icon, _item: open_app(), default=True),
                pystray.MenuItem("Check now", lambda _icon, _item: check_now()),
                pystray.MenuItem("Pause tracking", lambda _icon, _item: pause()),
                pystray.MenuItem("Last price", lambda _icon, _item: last_price()),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Exit", lambda _icon, _item: exit_app()),
            ),
        )
        self.icon.run_detached()

    def notify(self, message: str, title: str = "G2G Price Tracker") -> None:
        if self.icon is not None:
            self.icon.notify(message, title)

    def stop(self) -> None:
        if self.icon is not None:
            self.icon.stop()
            self.icon = None

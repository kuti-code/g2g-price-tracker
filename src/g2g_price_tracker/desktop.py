from __future__ import annotations

import ctypes
import math
import os
import queue
import random
import sys
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse

from .alerts import SoundAlarm, send_telegram_message, should_trigger_price_alert
from .app_paths import AppPaths
from .database import TABLE_ROW_LIMIT, PriceRepository
from .errors import PriceNotFoundError
from .exporting import export_price_history_xlsx
from .logging_setup import configure_logging
from .models import build_target_key
from .resources import resource_path
from .scraper import CollectionError, TrackerConfig, collect_price
from .secret_store import SecretStorageError, load_secret, save_secret
from .settings import AppSettings
from .startup import set_startup_enabled
from .tray import TrayController


@dataclass(frozen=True, slots=True)
class ThemePalette:
    background: str
    panel: str
    panel_alt: str
    input_background: str
    text: str
    muted: str
    accent: str
    market_low: str
    market_average: str
    grid: str
    border: str
    selection: str
    secondary: str
    secondary_active: str
    start_ready: str
    start_ready_active: str
    start_running: str
    start_running_active: str
    stop: str
    stop_active: str
    reset: str
    reset_active: str


LIGHT_THEME = ThemePalette(
    background="#F3F5F7",
    panel="#FFFFFF",
    panel_alt="#E8EDF2",
    input_background="#FFFFFF",
    text="#18212F",
    muted="#667085",
    accent="#0F766E",
    market_low="#D96B66",
    market_average="#D5A514",
    grid="#E4E7EC",
    border="#D0D5DD",
    selection="#CFF3EC",
    secondary="#E8EDF2",
    secondary_active="#DCE3EA",
    start_ready="#A8D8C6",
    start_ready_active="#92CBB5",
    start_running="#F4D58D",
    start_running_active="#E9C46F",
    stop="#F2AAA6",
    stop_active="#E9928D",
    reset="#F8D7DA",
    reset_active="#EFC2C6",
)

DARK_THEME = ThemePalette(
    background="#0B1120",
    panel="#111A2B",
    panel_alt="#18243A",
    input_background="#0D1628",
    text="#E7EDF7",
    muted="#93A4BC",
    accent="#32D0BA",
    market_low="#FB7185",
    market_average="#FBBF24",
    grid="#29364D",
    border="#2A3953",
    selection="#164E63",
    secondary="#1C2A40",
    secondary_active="#263A55",
    start_ready="#164E45",
    start_ready_active="#1F6F62",
    start_running="#5A4515",
    start_running_active="#71581A",
    stop="#542A36",
    stop_active="#713642",
    reset="#422733",
    reset_active="#5B3341",
)

THEMES = {"light": LIGHT_THEME, "dark": DARK_THEME}

WINDOW_WIDTH = 1600
WINDOW_HEIGHT = 965
WINDOW_MARGIN = 6
WINDOW_VERTICAL_MARGIN = 10
MIN_WINDOW_WIDTH = 1000
MIN_WINDOW_HEIGHT = 640
CHART_PANE_MIN_WIDTH = 420
TABLE_PANE_MIN_WIDTH = 480
ANALYTICS_SASH_WIDTH = 7
CHART_FRAME_INTERVAL_MS = 33
WINDOW_LAYOUT_IDLE_MS = 160
SCROLL_REGION_UPDATE_DELAY_MS = 120
WM_SETREDRAW = 0x000B
RDW_INVALIDATE = 0x0001
RDW_ERASE = 0x0004
RDW_ALLCHILDREN = 0x0080
RDW_FRAME = 0x0400
WINDOWS_APP_USER_MODEL_ID = "kuti-code.G2GPriceTracker"
WINDOWS_SINGLE_INSTANCE_MUTEX = r"Local\kuti-code.G2GPriceTracker"
ALREADY_RUNNING_MESSAGE = "G2G Price Tracker is already running."


def initial_window_bounds(
    work_left: int,
    work_top: int,
    work_right: int,
    work_bottom: int,
) -> tuple[int, int, int, int]:
    """Fit and position the first window inside the taskbar-safe work area."""
    work_width = max(1, work_right - work_left)
    work_height = max(1, work_bottom - work_top)
    width = min(WINDOW_WIDTH, max(1, work_width - (2 * WINDOW_MARGIN)))
    height = min(WINDOW_HEIGHT, max(1, work_height - (2 * WINDOW_VERTICAL_MARGIN)))
    x = work_left + max(WINDOW_MARGIN, (work_width - width) // 2)
    # Leave the same small grab area above and below the initial window.
    y = work_top + WINDOW_VERTICAL_MARGIN
    return x, y, width, height


def _layout_without_focus(layout: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Return a ttk layout with visual focus rings removed but spacing preserved."""
    cleaned: list[tuple[str, dict]] = []
    for element, raw_options in layout:
        options = dict(raw_options)
        children = _layout_without_focus(options.pop("children", []))
        if element.lower().endswith(".focus"):
            cleaned.extend(children)
            continue
        if children:
            options["children"] = children
        cleaned.append((element, options))
    return cleaned


def _windows_colorref(hex_color: str) -> int:
    """Convert #RRGGBB into the BGR COLORREF value expected by DWM."""
    value = hex_color.lstrip("#")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return red | (green << 8) | (blue << 16)


def analytics_sash_limits(total_width: int) -> tuple[int, int]:
    """Keep both analytics panes usable while the sash is dragged."""
    minimum = CHART_PANE_MIN_WIDTH
    maximum = max(minimum, total_width - TABLE_PANE_MIN_WIDTH - ANALYTICS_SASH_WIDTH)
    return minimum, maximum


def clipped_capture_bbox(
    x: int,
    y: int,
    width: int,
    height: int,
    clip_bounds: tuple[int, int, int, int],
) -> tuple[int, int, int, int]:
    """Clip a widget capture to the visible taskbar-safe desktop work area."""
    clip_left, clip_top, clip_right, clip_bottom = clip_bounds
    left = max(x, clip_left)
    top = max(y, clip_top)
    right = min(x + width, clip_right)
    bottom = min(y + height, clip_bottom)
    if right - left < 2 or bottom - top < 2:
        raise RuntimeError("The requested area is not visible enough to capture.")
    return left, top, right, bottom


class SingleInstanceGuard:
    """Keep one Windows process alive by owning a named system mutex."""

    ERROR_ALREADY_EXISTS = 183

    def __init__(self) -> None:
        self._handle = None

    def acquire(self) -> bool:
        if sys.platform != "win32":
            return True
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.CreateMutexW.argtypes = [
                ctypes.c_void_p,
                wintypes.BOOL,
                wintypes.LPCWSTR,
            ]
            kernel32.CreateMutexW.restype = wintypes.HANDLE
            handle = kernel32.CreateMutexW(None, True, WINDOWS_SINGLE_INSTANCE_MUTEX)
            if not handle:
                return True
            if kernel32.GetLastError() == self.ERROR_ALREADY_EXISTS:
                kernel32.CloseHandle(handle)
                return False
            self._handle = handle
            return True
        except (AttributeError, OSError):
            return True

    def release(self) -> None:
        if sys.platform != "win32" or self._handle is None:
            return
        try:
            kernel32 = ctypes.windll.kernel32
            kernel32.ReleaseMutex(self._handle)
            kernel32.CloseHandle(self._handle)
        except (AttributeError, OSError):
            pass
        finally:
            self._handle = None


def _show_already_running_message() -> None:
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.MessageBoxW(
            None,
            ALREADY_RUNNING_MESSAGE,
            "G2G Price Tracker",
            0x00000040,
        )
    except (AttributeError, OSError):
        pass


CHART_RENDER_LIMIT = 300
CHART_RANGES = ("Last 24 Hours", "Last 7 Days", "Last 30 Days", "Last 100 Checks", "All Time")
RETRY_DELAYS = (5.0, 15.0, 45.0)


def _price_text(value: Decimal | float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(currency, f"{currency} ")
    return f"{symbol}{float(value):.6f}".rstrip("0").rstrip(".")


def _local_time(iso_value: str) -> datetime:
    return datetime.fromisoformat(iso_value).astimezone()


def _downsample_rows(rows: list[object], limit: int = CHART_RENDER_LIMIT) -> list[object]:
    if len(rows) <= limit:
        return rows
    indexes = {round(index * (len(rows) - 1) / (limit - 1)) for index in range(limit)}
    return [rows[index] for index in sorted(indexes)]


def retry_delay(error: CollectionError, retry_index: int, *, jitter: float = 0.0) -> float:
    if error.retry_after is not None:
        return max(0.0, error.retry_after) + jitter
    return RETRY_DELAYS[min(retry_index, len(RETRY_DELAYS) - 1)] + jitter


@lru_cache(maxsize=12)
def _chart_font(size: int, bold: bool = False):
    """Load a native chart font once, with a bundled Pillow fallback."""
    from PIL import ImageFont

    windows_fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    candidates = [
        windows_fonts / ("seguisb.ttf" if bold else "segoeui.ttf"),
        "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf",
    ]
    for candidate in candidates:
        try:
            return ImageFont.truetype(str(candidate), size)
        except OSError:
            continue
    return ImageFont.load_default()


def render_price_chart_image(
    points: list[tuple[datetime, float, float | None, float | None]],
    currency: str,
    palette: ThemePalette,
    width: int,
    height: int,
):
    """Rasterize the complete chart so Tk only repaints one canvas item."""
    from PIL import Image, ImageDraw

    width = max(width, 500)
    height = max(height, 230)
    image = Image.new("RGB", (width, height), palette.panel)
    draw = ImageDraw.Draw(image)
    label_font = _chart_font(12)
    axis_font = _chart_font(11)
    empty_font = _chart_font(15)
    left, right, top, bottom = 78, 24, 38, 52
    plot_width = width - left - right
    plot_height = height - top - bottom

    if not points:
        draw.text(
            (width / 2, height / 2),
            "The chart will appear after the first price check.",
            fill=palette.muted,
            font=empty_font,
            anchor="mm",
        )
        return image

    values = [point[1] for point in points]
    values.extend(point[2] for point in points if point[2] is not None)
    values.extend(point[3] for point in points if point[3] is not None)
    minimum, maximum = min(values), max(values)
    spread = maximum - minimum
    padding = spread * 0.12 if spread else max(abs(maximum) * 0.05, 0.0001)
    y_min, y_max = minimum - padding, maximum + padding

    for index in range(5):
        fraction = index / 4
        y = top + plot_height * fraction
        value = y_max - (y_max - y_min) * fraction
        draw.line((left, y, width - right, y), fill=palette.grid, width=1)
        draw.text(
            (left - 10, y),
            _price_text(value, currency),
            fill=palette.muted,
            font=label_font,
            anchor="rm",
        )

    point_count = len(points)

    def x_position(index: int) -> float:
        return left + (plot_width / max(1, point_count - 1)) * index

    def y_position(value: float) -> float:
        return top + (y_max - value) / (y_max - y_min) * plot_height

    def draw_series(coordinates: list[tuple[float, float]], color: str) -> None:
        if len(coordinates) >= 2:
            draw.line(coordinates, fill=color, width=3, joint="curve")
        elif coordinates:
            x, y = coordinates[0]
            draw.line((x - 1, y, x + 1, y), fill=color, width=3)

    seller_coordinates: list[tuple[float, float]] = []
    market_coordinates: list[tuple[float, float]] = []
    average_coordinates: list[tuple[float, float]] = []
    for index, (_, seller_value, market_value, average_value) in enumerate(points):
        x = x_position(index)
        seller_coordinates.append((x, y_position(seller_value)))
        if market_value is not None:
            market_coordinates.append((x, y_position(market_value)))
        if average_value is not None:
            average_coordinates.append((x, y_position(average_value)))

    draw_series(seller_coordinates, palette.accent)
    draw_series(market_coordinates, palette.market_low)
    draw_series(average_coordinates, palette.market_average)

    legend_x = max(left, width - right - 525)
    legend_items = (
        (0, 31, "Selected Seller", palette.accent),
        (155, 186, "Market Lowest", palette.market_low),
        (300, 331, "Market Average (Lowest 5)", palette.market_average),
    )
    for line_offset, text_offset, label, color in legend_items:
        draw.line(
            (legend_x + line_offset, 17, legend_x + line_offset + 24, 17),
            fill=color,
            width=3,
        )
        draw.text(
            (legend_x + text_offset, 17),
            label,
            fill=palette.text,
            font=label_font,
            anchor="lm",
        )

    marker_step = max(1, math.ceil(point_count / 7))
    for index, (observed_at, seller_value, market_value, average_value) in enumerate(points):
        x, y = x_position(index), y_position(seller_value)
        if point_count <= 30 or index in {0, point_count - 1}:
            draw.ellipse((x - 3, y - 3, x + 3, y + 3), fill=palette.accent)
            if market_value is not None:
                market_y = y_position(market_value)
                draw.ellipse(
                    (x - 3, market_y - 3, x + 3, market_y + 3),
                    fill=palette.market_low,
                )
            if average_value is not None:
                average_y = y_position(average_value)
                draw.ellipse(
                    (x - 3, average_y - 3, x + 3, average_y + 3),
                    fill=palette.market_average,
                )
        if index % marker_step == 0 or index == point_count - 1:
            draw.multiline_text(
                (x, height - 39),
                observed_at.strftime("%d.%m\n%H:%M"),
                fill=palette.muted,
                font=axis_font,
                anchor="ma",
                align="center",
                spacing=0,
            )
    return image


@dataclass(frozen=True)
class ChartScene:
    """Data-dependent work, prepared once; resizing only projects coordinates."""

    series: tuple[tuple[tuple[float, float], ...], ...] = ((), (), ())
    y_labels: tuple[str, ...] = ()
    time_labels: tuple[tuple[float, str], ...] = ()


def prepare_chart_scene(points, currency: str) -> ChartScene:
    if not points:
        return ChartScene()
    values = [value for point in points for value in point[1:] if value is not None]
    minimum, maximum = min(values), max(values)
    spread = maximum - minimum
    padding = spread * 0.12 if spread else max(abs(maximum) * 0.05, 0.0001)
    y_min, y_max = minimum - padding, maximum + padding
    denominator = max(1, len(points) - 1)
    series = tuple(
        tuple(
            (index / denominator, (y_max - point[column]) / (y_max - y_min))
            for index, point in enumerate(points)
            if point[column] is not None
        )
        for column in (1, 2, 3)
    )
    label_count = min(8, len(points))
    indexes = sorted(
        {round(index * denominator / max(1, label_count - 1)) for index in range(label_count)}
    )
    return ChartScene(
        series=series,
        y_labels=tuple(
            _price_text(y_max - (y_max - y_min) * index / 4, currency) for index in range(5)
        ),
        time_labels=tuple(
            (index / denominator, points[index][0].strftime("%d.%m\n%H:%M")) for index in indexes
        ),
    )


def project_chart_series(series, bounds) -> tuple[float, ...]:
    left, top, width, height = bounds
    coordinates = tuple(value for x, y in series for value in (left + x * width, top + y * height))
    # A Canvas line needs at least two points, even for a single price check.
    return coordinates * 2 if len(coordinates) == 2 else coordinates


def chart_layout(width: int, height: int, legend_widths):
    """Fit the actual viewport, including the narrow side of the splitter."""
    width, height = max(1, width), max(1, height)
    left, right, bottom = min(78, width // 3), 20, min(48, height // 3)
    positions = []
    x, y = 8, 16
    for item_width in legend_widths:
        if x > 8 and x + item_width > width - 8:
            x, y = 8, y + 20
        positions.append((x, y))
        x += item_width + 18
    top = min(y + 20, max(1, height - bottom - 15))
    return (left, top, max(1, width - left - right), max(1, height - top - bottom)), positions


class PriceChart(tk.Canvas):
    """Retained vector chart: 34 items, three polylines, no resize bitmaps."""

    def __init__(self, master: tk.Misc, palette: ThemePalette) -> None:
        from tkinter import font as tkfont

        self.palette = palette
        super().__init__(
            master,
            background=palette.panel,
            highlightthickness=0,
            borderwidth=0,
            width=1,
            height=280,
        )
        self._scene = ChartScene()
        self._frame_id: str | None = None
        self._last_rendered_size: tuple[int, int] | None = None
        self._render_dirty = True
        self.render_count = 0
        self.last_render_ms = 0.0
        self._label_font = tkfont.Font(self, family="Segoe UI", size=-12)
        self._axis_font = tkfont.Font(self, family="Segoe UI", size=-11)
        self._empty_item = self.create_text(
            0,
            0,
            text="The chart will appear after the first price check.",
            font=("Segoe UI", -15),
            justify="center",
        )
        self._grid_items = [self.create_line(0, 0, 0, 0) for _ in range(5)]
        self._y_items = [
            self.create_text(0, 0, anchor="e", font=self._label_font) for _ in range(5)
        ]
        labels = ("Selected Seller", "Market Lowest", "Market Average (Lowest 5)")
        self._legend_widths = tuple(31 + self._label_font.measure(label) for label in labels)
        self._legend_lines = [self.create_line(0, 0, 0, 0, width=3) for _ in labels]
        self._legend_text = [
            self.create_text(0, 0, text=label, anchor="w", font=self._label_font)
            for label in labels
        ]
        self._series_items = [self.create_line(0, 0, 0, 0, width=2, smooth=False) for _ in labels]
        self._time_items = [
            self.create_text(0, 0, anchor="n", justify="center", font=self._axis_font)
            for _ in range(8)
        ]
        self._marker_items = [
            [self.create_oval(0, 0, 0, 0, outline="") for _ in range(2)] for _ in labels
        ]
        self.set_palette(palette)
        self.bind("<Configure>", self._request_frame)
        self.bind("<Map>", self._request_frame)
        self._update_scene_items()

    def _request_frame(self, _event=None) -> None:
        # Coalesce to the newest size at <=30fps. Never cancel/restart per pixel,
        # wait for a quiet period, or hide the last valid chart during a drag.
        if self._frame_id is None and self.winfo_ismapped():
            self._frame_id = self.after(CHART_FRAME_INTERVAL_MS, self._flush_frame)

    def _flush_frame(self) -> None:
        self._frame_id = None
        if self.winfo_ismapped():
            self.redraw()

    def set_palette(self, palette: ThemePalette) -> None:
        self.palette = palette
        self.configure(background=palette.panel)
        for item in self._grid_items:
            self.itemconfigure(item, fill=palette.grid)
        for item in self._y_items + self._time_items + [self._empty_item]:
            self.itemconfigure(item, fill=palette.muted)
        for item in self._legend_text:
            self.itemconfigure(item, fill=palette.text)
        for index, color in enumerate((palette.accent, palette.market_low, palette.market_average)):
            for item in (
                self._legend_lines[index],
                self._series_items[index],
                *self._marker_items[index],
            ):
                self.itemconfigure(item, fill=color)
        self._render_dirty = True
        self._request_frame()

    def set_data(self, rows: list[object]) -> None:
        points = []
        for row in _downsample_rows(rows):
            market_value = row["market_lowest_price"]
            average_value = row["market_average_price"]
            points.append(
                (
                    _local_time(row["observed_at"]),
                    float(row["unit_price"]),
                    float(market_value) if market_value is not None else None,
                    float(average_value) if average_value is not None else None,
                )
            )
        self._scene = prepare_chart_scene(points, rows[-1]["currency"] if rows else "USD")
        self._update_scene_items()
        self._render_dirty = True
        self._request_frame()

    def _update_scene_items(self) -> None:
        populated = bool(self._scene.y_labels)
        state = "normal" if populated else "hidden"
        self.itemconfigure(self._empty_item, state="hidden" if populated else "normal")
        for item in self._grid_items + self._legend_lines + self._legend_text:
            self.itemconfigure(item, state=state)
        for index, item in enumerate(self._y_items):
            self.itemconfigure(
                item, state=state, text=self._scene.y_labels[index] if populated else ""
            )
        for index, item in enumerate(self._time_items):
            labels = self._scene.time_labels
            self.itemconfigure(
                item, state="hidden", text=labels[index][1] if index < len(labels) else ""
            )
        for index, series in enumerate(self._scene.series):
            self.itemconfigure(self._series_items[index], state="normal" if series else "hidden")
            for marker_index, item in enumerate(self._marker_items[index]):
                visible = len(series) > marker_index
                self.itemconfigure(item, state="normal" if visible else "hidden")

    def redraw(self) -> None:
        width, height = self.winfo_width(), self.winfo_height()
        if width < 2 or height < 2:
            return
        size = (width, height)
        if not self._render_dirty and size == self._last_rendered_size:
            return
        started = time.perf_counter()
        bounds, legends = chart_layout(width, height, self._legend_widths)
        left, top, plot_width, plot_height = bounds
        self.coords(self._empty_item, width / 2, height / 2)
        self.itemconfigure(self._empty_item, width=max(1, width - 30))
        for index in range(5):
            y = top + plot_height * index / 4
            self.coords(self._grid_items[index], left, y, left + plot_width, y)
            self.coords(self._y_items[index], left - 8, y)
        for index, (x, y) in enumerate(legends):
            self.coords(self._legend_lines[index], x, y, x + 24, y)
            self.coords(self._legend_text[index], x + 31, y)
        for index, series in enumerate(self._scene.series):
            if not series:
                continue
            self.coords(self._series_items[index], *project_chart_series(series, bounds))
            for item, (x, y) in zip(self._marker_items[index], (series[0], series[-1])):
                x, y = left + x * plot_width, top + y * plot_height
                self.coords(item, x - 3, y - 3, x + 3, y + 3)
        labels = self._scene.time_labels
        step = max(1, math.ceil((len(labels) - 1) / max(1, plot_width // 95)))
        for index, (fraction, _) in enumerate(labels):
            self.coords(
                self._time_items[index], left + fraction * plot_width, top + plot_height + 12
            )
            self.itemconfigure(
                self._time_items[index],
                state="normal" if index % step == 0 or index == len(labels) - 1 else "hidden",
            )
        self._last_rendered_size = size
        self._render_dirty = False
        self.render_count += 1
        self.last_render_ms = (time.perf_counter() - started) * 1000

    def destroy(self) -> None:
        if self._frame_id is not None:
            self.after_cancel(self._frame_id)
            self._frame_id = None
        super().destroy()


class ScrollableFrame(ttk.Frame):
    """A theme-aware vertical viewport used when a tab is shorter than its content."""

    def __init__(self, master: tk.Misc, palette: ThemePalette) -> None:
        super().__init__(master, style="App.TFrame")
        self.palette = palette
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        self.canvas = tk.Canvas(
            self,
            background=palette.background,
            highlightthickness=0,
            borderwidth=0,
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            style="App.Vertical.TScrollbar",
        )
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.content = ttk.Frame(self.canvas, style="App.TFrame")
        self._content_window = self.canvas.create_window(
            (0, 0),
            window=self.content,
            anchor="nw",
        )
        self._scroll_region_update_id: str | None = None
        self._scrollbar_visible = True
        self.content.bind("<Configure>", self._schedule_scroll_region_update)
        self.canvas.bind("<Configure>", self._resize_content)
        self.bind("<Enter>", self._bind_wheel)
        self.bind("<Leave>", self._unbind_wheel)
        self.after_idle(self._update_scroll_region)

    def _resize_content(self, event) -> None:
        if not self.winfo_ismapped():
            return
        self.canvas.itemconfigure(self._content_window, width=event.width)
        self._schedule_scroll_region_update()

    def _schedule_scroll_region_update(self, _event=None) -> None:
        if not self.winfo_ismapped():
            return
        if self._scroll_region_update_id is not None:
            self.after_cancel(self._scroll_region_update_id)
        self._scroll_region_update_id = self.after(
            SCROLL_REGION_UPDATE_DELAY_MS,
            self._update_scroll_region,
        )

    def _update_scroll_region(self, _event=None) -> None:
        self._scroll_region_update_id = None
        if not self.winfo_ismapped():
            return
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        needs_scrollbar = self.content.winfo_reqheight() > self.canvas.winfo_height()
        if needs_scrollbar and not self._scrollbar_visible:
            self.scrollbar.grid()
            self._scrollbar_visible = True
        elif not needs_scrollbar and self._scrollbar_visible:
            self.scrollbar.grid_remove()
            self._scrollbar_visible = False
            self.canvas.yview_moveto(0)

    def _bind_wheel(self, _event=None) -> None:
        self.bind_all("<MouseWheel>", self._on_mousewheel)

    def _unbind_wheel(self, _event=None) -> None:
        try:
            containing = self.winfo_containing(self.winfo_pointerx(), self.winfo_pointery())
        except tk.TclError:
            containing = None
        widget = containing
        while widget is not None:
            if widget is self:
                return
            widget = getattr(widget, "master", None)
        self.unbind_all("<MouseWheel>")

    def _on_mousewheel(self, event) -> None:
        if self.content.winfo_reqheight() > self.canvas.winfo_height():
            self.canvas.yview_scroll(int(-event.delta / 120), "units")

    def set_palette(self, palette: ThemePalette) -> None:
        self.palette = palette
        self.canvas.configure(background=palette.background)

    def destroy(self) -> None:
        self.unbind_all("<MouseWheel>")
        if self._scroll_region_update_id is not None:
            self.after_cancel(self._scroll_region_update_id)
            self._scroll_region_update_id = None
        super().destroy()


class TrackerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        # Do not expose an unthemed native frame while saved settings are loading.
        # This prevents the white title-bar flash when the persisted theme is Dark.
        self.withdraw()
        self.title("G2G Price Tracker")
        self._place_initial_window()
        self._set_window_icon()

        self.paths = AppPaths.default()
        self.paths.ensure()
        self.logger = configure_logging(self.paths.log)
        self.logger.info("Startup: initializing application")
        self.settings = AppSettings.load(self.paths.settings)
        self.palette = THEMES[self.settings.theme]
        self.configure(background=self.palette.background)
        self.repository = PriceRepository(self.paths.database)
        self.repository.initialize()

        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.collect_lock = threading.Lock()
        self.scheduler_thread: threading.Thread | None = None
        self.is_running = False
        self.next_collection_at: float | None = None
        self.alert_armed = True
        self.sound_alarm = SoundAlarm()
        self.consecutive_failures = 0
        self.last_observation = None
        self._colored_button_roles: dict[tk.Button, str] = {}
        self._scrollable_frames: list[ScrollableFrame] = []
        self._switch_image_sets: list[tuple[tk.PhotoImage, ...]] = []
        self._switch_style_generation = 0
        self._table_values: dict[str, tuple[str, ...]] = {}
        self._displayed_target_key: str | None = None
        self._history_refresh_id: str | None = None
        self._pending_window_size: tuple[int, int] | None = None
        self._committed_window_size: tuple[int, int] | None = None
        self._window_layout_id: str | None = None
        self._window_redraw_suspended = False
        self._window_configure_tag = f"G2GTopLevel{id(self)}"
        self._settings_built = False
        self._diagnostics_built = False
        try:
            self.telegram_token = load_secret(self.paths.telegram_secret)
        except SecretStorageError:
            self.telegram_token = ""

        self.seller_var = tk.StringVar(value=self.settings.seller)
        self.interval_var = tk.StringVar(value=str(self.settings.interval_minutes))
        self.url_var = tk.StringVar(value=self.settings.source_url)
        self.status_var = tk.StringVar(value="Ready")
        self.market_status_var = tk.StringVar(value="")
        self.next_var = tk.StringVar(value="Automatic tracking is off")
        self.latest_var = tk.StringVar(value="—")
        self.change_var = tk.StringVar(value="—")
        self.low_var = tk.StringVar(value="—")
        self.market_low_var = tk.StringVar(value="—")
        self.high_var = tk.StringVar(value="—")
        self.all_average_var = tk.StringVar(value="—")
        self.market_average_var = tk.StringVar(value="—")
        self.count_var = tk.StringVar(value="0")
        self.chart_subtitle_var = tk.StringVar(value="No price checks for this target")
        self.listing_var = tk.StringVar(value="—")
        self.chart_range_var = tk.StringVar(value="Last 100 Checks")
        self.health_var = tk.StringVar(value="Waiting for first check")
        self.last_success_var = tk.StringVar(value="—")
        self.request_duration_var = tk.StringVar(value="—")
        self.failure_count_var = tk.StringVar(value="0")
        self.last_error_var = tk.StringVar(value="—")
        self.alert_enabled_var = tk.BooleanVar(value=self.settings.price_alert_enabled)
        self.alert_threshold_var = tk.StringVar(value=self.settings.price_alert_threshold)
        self.sound_enabled_var = tk.BooleanVar(value=self.settings.sound_alert_enabled)
        self.sound_duration_var = tk.StringVar(value=str(self.settings.sound_duration_seconds))
        self.sound_until_dismissed_var = tk.BooleanVar(value=self.settings.sound_until_dismissed)
        self.telegram_enabled_var = tk.BooleanVar(value=self.settings.telegram_enabled)
        self.telegram_chat_id_var = tk.StringVar(value=self.settings.telegram_chat_id)
        self.telegram_token_var = tk.StringVar(value=self.telegram_token)
        self.alert_once_var = tk.BooleanVar(value=self.settings.alert_once_until_recovery)
        self.start_with_windows_var = tk.BooleanVar(value=self.settings.start_with_windows)
        self.minimize_to_tray_var = tk.BooleanVar(value=self.settings.minimize_to_tray_on_close)
        self.theme_var = tk.StringVar(value=self.settings.theme.capitalize())

        self.tray = TrayController(
            open_app=lambda: self.after(0, self._restore_from_tray),
            check_now=lambda: self.after(0, self._collect_once),
            pause_tracking=lambda: self.after(0, self._stop),
            show_last_price=lambda: self.after(0, self._show_last_price),
            exit_app=lambda: self.after(0, self._exit_app),
            icon_path=resource_path("assets", "g2g-price-tracker.ico"),
        )

        self.logger.info("Startup: configuring styles")
        self._configure_styles()
        self.logger.info("Startup: building interface")
        self._build_ui()
        self.logger.info("Startup: interface built")
        self.update_idletasks()
        self._pending_window_size = (self.winfo_width(), self.winfo_height())
        self._commit_window_layout()
        # A normal toplevel binding is inherited by every descendant widget.
        # This private bindtag exists only on the root, so child Configure
        # storms never cross the Python boundary during an outer resize.
        self.bindtags((self._window_configure_tag, *self.bindtags()))
        self.bind_class(self._window_configure_tag, "<Configure>", self._on_window_configure)
        self._set_windows_title_bar_theme(self.settings.theme == "dark")
        self._set_windows_native_icons()
        self.deiconify()
        self.after_idle(self._set_windows_native_icons)
        self._refresh_history()
        self.logger.info("Startup: application ready")
        self.after(200, self._process_events)
        self.after(1000, self._update_countdown)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _set_window_icon(self) -> None:
        try:
            if sys.platform == "win32":
                self.iconbitmap(default=str(resource_path("assets", "g2g-price-tracker.ico")))
                self._window_icon = None
                return
            self._window_icon = tk.PhotoImage(
                file=str(resource_path("assets", "g2g-price-tracker.png"))
            )
            self.iconphoto(True, self._window_icon)
        except (OSError, tk.TclError):
            self._window_icon = None

    def _set_windows_native_icons(self) -> None:
        """Set both native Windows icon slots used by title bars and the taskbar."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            user32.LoadImageW.argtypes = [
                wintypes.HINSTANCE,
                wintypes.LPCWSTR,
                wintypes.UINT,
                ctypes.c_int,
                ctypes.c_int,
                wintypes.UINT,
            ]
            user32.LoadImageW.restype = wintypes.HANDLE
            user32.SendMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.SendMessageW.restype = ctypes.c_ssize_t
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            icon_path = str(resource_path("assets", "g2g-price-tracker.ico"))
            big_icon = user32.LoadImageW(
                None,
                icon_path,
                1,
                user32.GetSystemMetrics(11),
                user32.GetSystemMetrics(12),
                0x0010,
            )
            small_icon = user32.LoadImageW(
                None,
                icon_path,
                1,
                user32.GetSystemMetrics(49),
                user32.GetSystemMetrics(50),
                0x0010,
            )
            window_handle = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            if big_icon:
                user32.SendMessageW(window_handle, 0x0080, 1, big_icon)
            if small_icon:
                user32.SendMessageW(window_handle, 0x0080, 0, small_icon)
            self._native_icon_handles = (big_icon, small_icon)
        except (AttributeError, OSError, tk.TclError):
            self._native_icon_handles = ()

    def _set_windows_title_bar_theme(self, dark: bool) -> None:
        """Match the native Windows title bar to the selected application theme."""
        if sys.platform != "win32":
            return
        try:
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            window_handle = user32.GetAncestor(self.winfo_id(), 2) or self.winfo_id()
            enabled = ctypes.c_int(1 if dark else 0)
            dwmapi = ctypes.windll.dwmapi
            dwmapi.DwmSetWindowAttribute.argtypes = [
                wintypes.HWND,
                wintypes.DWORD,
                ctypes.c_void_p,
                wintypes.DWORD,
            ]
            dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
            result = dwmapi.DwmSetWindowAttribute(
                window_handle,
                20,
                ctypes.byref(enabled),
                ctypes.sizeof(enabled),
            )
            if result != 0:
                dwmapi.DwmSetWindowAttribute(
                    window_handle,
                    19,
                    ctypes.byref(enabled),
                    ctypes.sizeof(enabled),
                )
            palette = DARK_THEME if dark else LIGHT_THEME
            # Windows 11 supports explicit border/caption/text colors. Older
            # versions simply reject these optional attributes and keep the
            # immersive-dark-mode result above.
            for attribute, color in (
                (34, palette.border),
                (35, palette.background),
                (36, palette.text),
            ):
                color_value = ctypes.c_uint(_windows_colorref(color))
                dwmapi.DwmSetWindowAttribute(
                    window_handle,
                    attribute,
                    ctypes.byref(color_value),
                    ctypes.sizeof(color_value),
                )
        except (AttributeError, OSError, tk.TclError):
            pass

    def _place_initial_window(self) -> None:
        left, top, right, bottom = self._work_area()
        x, y, width, height = initial_window_bounds(left, top, right, bottom)
        self.geometry(f"{width}x{height}+{x}+{y}")
        self.minsize(min(MIN_WINDOW_WIDTH, width), min(MIN_WINDOW_HEIGHT, height))

    def _work_area(self) -> tuple[int, int, int, int]:
        if sys.platform == "win32":
            rect = wintypes.RECT()
            try:
                success = ctypes.windll.user32.SystemParametersInfoW(
                    0x0030, 0, ctypes.byref(rect), 0
                )
                if success:
                    return rect.left, rect.top, rect.right, rect.bottom
            except (AttributeError, OSError):
                pass
        return 0, 0, self.winfo_screenwidth(), self.winfo_screenheight()

    def _create_switch_element(self, style: ttk.Style) -> str:
        """Create the same clear toggle geometry for both color palettes."""
        from PIL import Image, ImageDraw, ImageTk

        dark = self.settings.theme == "dark"
        scale = 4
        width, height = 44, 20

        def switch_image(
            track: str,
            knob: str,
            outline: str,
            *,
            selected: bool,
        ) -> tk.PhotoImage:
            image = Image.new("RGBA", (width * scale, height * scale), (0, 0, 0, 0))
            draw = ImageDraw.Draw(image)
            track_box = (1 * scale, 1 * scale, 37 * scale, 19 * scale)
            draw.rounded_rectangle(
                track_box,
                radius=9 * scale,
                fill=track,
                outline=outline,
                width=1 * scale,
            )
            knob_left = 19 if selected else 3
            draw.ellipse(
                (
                    knob_left * scale,
                    3 * scale,
                    (knob_left + 14) * scale,
                    17 * scale,
                ),
                fill=knob,
            )
            image = image.resize((width, height), Image.Resampling.LANCZOS)
            return ImageTk.PhotoImage(image, master=self)

        if dark:
            switch_colors = (
                ("#33445E", "#E7EDF7", "#6F829D", False),
                (DARK_THEME.accent, "#FFFFFF", DARK_THEME.accent, True),
                ("#202C40", "#6F7D91", "#39485E", False),
                ("#235E58", "#AAB7C8", "#39766F", True),
            )
        else:
            switch_colors = (
                ("#CBD4DF", "#FFFFFF", "#8D9CAF", False),
                (LIGHT_THEME.accent, "#FFFFFF", LIGHT_THEME.accent, True),
                ("#E3E7EC", "#B1BBC8", "#C6CDD6", False),
                ("#9BC2BD", "#EEF4F3", "#82ADA8", True),
            )
        images = tuple(
            switch_image(track, knob, outline, selected=selected)
            for track, knob, outline, selected in switch_colors
        )
        self._switch_image_sets.append(images)
        self._switch_style_generation += 1
        element_name = f"AppSwitch{self._switch_style_generation}.indicator"
        off_image, on_image, disabled_off_image, disabled_on_image = images
        style.element_create(
            element_name,
            "image",
            off_image,
            ("disabled", "selected", disabled_on_image),
            ("disabled", disabled_off_image),
            ("selected", on_image),
            sticky="w",
        )
        return element_name

    def _configure_styles(self) -> None:
        palette = self.palette
        style = ttk.Style(self)
        themes = style.theme_names()
        # One drawing engine keeps every widget's geometry identical across themes.
        if "clam" in themes:
            style.theme_use("clam")

        button_layout = _layout_without_focus(style.layout("TButton"))
        if button_layout:
            style.layout("Secondary.TButton", button_layout)
        switch_element = self._create_switch_element(style)
        style.layout(
            "Panel.TCheckbutton",
            [
                (
                    "Checkbutton.padding",
                    {
                        "sticky": "nswe",
                        "children": [
                            (switch_element, {"side": "left", "sticky": "w"}),
                            (
                                "Checkbutton.label",
                                {"side": "left", "sticky": "nswe"},
                            ),
                        ],
                    },
                )
            ],
        )
        notebook_tab_layout = _layout_without_focus(style.layout("TNotebook.Tab"))
        if notebook_tab_layout:
            style.layout("App.TNotebook.Tab", notebook_tab_layout)

        style.configure(".", background=palette.background, foreground=palette.text)
        style.configure("App.TFrame", background=palette.background)
        style.configure("Panel.TFrame", background=palette.panel)
        style.configure(
            "Title.TLabel",
            background=palette.background,
            foreground=palette.text,
            font=("Segoe UI", 20, "bold"),
        )
        style.configure(
            "Subtitle.TLabel",
            background=palette.background,
            foreground=palette.muted,
            font=("Segoe UI", 10),
        )
        style.configure(
            "PanelTitle.TLabel",
            background=palette.panel,
            foreground=palette.text,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "PanelText.TLabel",
            background=palette.panel,
            foreground=palette.muted,
            font=("Segoe UI", 9),
        )
        style.configure(
            "MetricName.TLabel",
            background=palette.panel,
            foreground=palette.muted,
            font=("Segoe UI", 9),
        )
        style.configure(
            "MetricValue.TLabel",
            background=palette.panel,
            foreground=palette.text,
            font=("Segoe UI", 15, "bold"),
        )
        style.configure(
            "Secondary.TButton",
            font=("Segoe UI", 10),
            padding=(12, 8),
            background=palette.secondary,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            relief="flat",
        )
        style.map(
            "Secondary.TButton",
            background=[("active", palette.secondary_active), ("pressed", palette.selection)],
            foreground=[("disabled", palette.muted)],
        )
        style.configure(
            "Panel.TCheckbutton",
            background=palette.panel,
            foreground=palette.text,
            font=("Segoe UI", 10),
            padding=(0, 2),
        )
        style.map(
            "Panel.TCheckbutton",
            background=[("active", palette.panel), ("disabled", palette.panel)],
            foreground=[("disabled", palette.muted)],
        )
        style.configure(
            "Status.TLabel",
            background=palette.panel,
            foreground=palette.accent,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "MarketStatus.TLabel",
            background=palette.panel,
            foreground=palette.market_low,
            font=("Segoe UI", 9, "bold"),
        )
        style.configure(
            "Countdown.TLabel",
            background=palette.panel,
            foreground=palette.accent,
            font=("Segoe UI", 11, "bold"),
        )
        style.configure(
            "TEntry",
            fieldbackground=palette.input_background,
            foreground=palette.text,
            insertcolor=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            relief="flat",
            borderwidth=1,
        )
        style.configure(
            "TSpinbox",
            fieldbackground=palette.input_background,
            foreground=palette.text,
            insertcolor=palette.text,
            arrowcolor=palette.muted,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            relief="flat",
            borderwidth=1,
        )
        style.configure(
            "TCombobox",
            fieldbackground=palette.input_background,
            background=palette.panel_alt,
            foreground=palette.text,
            arrowcolor=palette.muted,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            selectbackground=palette.selection,
            selectforeground=palette.text,
            relief="flat",
            borderwidth=1,
        )
        for widget_style in ("TEntry", "TSpinbox", "TCombobox"):
            style.map(
                widget_style,
                fieldbackground=[
                    ("readonly", palette.input_background),
                    ("disabled", palette.panel_alt),
                ],
                foreground=[("disabled", palette.muted), ("readonly", palette.text)],
            )
        style.configure(
            "App.TNotebook",
            background=palette.background,
            bordercolor=palette.background,
            lightcolor=palette.background,
            darkcolor=palette.background,
            relief="flat",
            borderwidth=0,
            tabmargins=(0, 0, 0, 0),
        )
        style.configure(
            "App.TNotebook.Tab",
            background=palette.panel_alt,
            foreground=palette.muted,
            padding=(16, 8),
            expand=(0, 0, 0, 0),
            focuscolor=palette.panel_alt,
            lightcolor=palette.panel_alt,
            darkcolor=palette.panel_alt,
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "App.TNotebook.Tab",
            background=[("selected", palette.panel), ("active", palette.secondary_active)],
            foreground=[("selected", palette.accent), ("active", palette.text)],
            padding=[("selected", (16, 8)), ("active", (16, 8))],
            expand=[("selected", (0, 0, 0, 0)), ("active", (0, 0, 0, 0))],
            lightcolor=[("selected", palette.panel), ("active", palette.secondary_active)],
            darkcolor=[("selected", palette.panel), ("active", palette.secondary_active)],
        )
        style.configure(
            "Treeview",
            rowheight=28,
            font=("Segoe UI", 9),
            background=palette.panel,
            fieldbackground=palette.panel,
            foreground=palette.text,
            bordercolor=palette.border,
            lightcolor=palette.border,
            darkcolor=palette.border,
            relief="flat",
            borderwidth=0,
        )
        style.map(
            "Treeview",
            background=[("selected", palette.selection)],
            foreground=[("selected", palette.text)],
        )
        style.configure(
            "Treeview.Heading",
            font=("Segoe UI", 9, "bold"),
            background=palette.panel_alt,
            foreground=palette.text,
            bordercolor=palette.border,
            relief="flat",
        )
        style.map("Treeview.Heading", background=[("active", palette.secondary_active)])
        for scrollbar_style in (
            "App.Vertical.TScrollbar",
            "App.Horizontal.TScrollbar",
        ):
            style.configure(
                scrollbar_style,
                background="#607894" if self.settings.theme == "dark" else palette.panel_alt,
                troughcolor=(
                    "#08101E" if self.settings.theme == "dark" else palette.input_background
                ),
                arrowcolor=palette.text if self.settings.theme == "dark" else palette.muted,
                bordercolor="#354A67" if self.settings.theme == "dark" else palette.border,
                lightcolor="#607894" if self.settings.theme == "dark" else palette.panel_alt,
                darkcolor="#607894" if self.settings.theme == "dark" else palette.panel_alt,
                relief="flat",
                borderwidth=1,
                width=16,
            )
            style.map(
                scrollbar_style,
                background=[
                    (
                        "active",
                        ("#8298B5" if self.settings.theme == "dark" else palette.secondary_active),
                    ),
                    (
                        "pressed",
                        palette.accent if self.settings.theme == "dark" else palette.selection,
                    ),
                ],
                arrowcolor=[("active", palette.text)],
            )

    def _colored_button(
        self,
        master: tk.Misc,
        *,
        text: str,
        command,
        color_role: str,
    ) -> tk.Button:
        background = getattr(self.palette, color_role)
        active_background = getattr(self.palette, f"{color_role}_active")
        button = tk.Button(
            master,
            text=text,
            command=command,
            background=background,
            activebackground=active_background,
            foreground=self.palette.text,
            activeforeground=self.palette.text,
            disabledforeground=self.palette.muted,
            font=("Segoe UI", 10, "bold"),
            relief="flat",
            borderwidth=0,
            padx=14,
            pady=7,
            cursor="hand2",
            takefocus=False,
            highlightthickness=0,
        )
        self._colored_button_roles[button] = color_role
        return button

    def _set_colored_button_role(self, button: tk.Button, color_role: str) -> None:
        self._colored_button_roles[button] = color_role
        button.configure(
            background=getattr(self.palette, color_role),
            activebackground=getattr(self.palette, f"{color_role}_active"),
        )

    def _apply_theme(self, theme_name: str) -> None:
        normalized = "dark" if theme_name.strip().lower() == "dark" else "light"
        self.settings = replace(self.settings, theme=normalized)
        self.palette = THEMES[normalized]
        self.theme_var.set(normalized.capitalize())
        self.configure(background=self.palette.background)
        self._configure_styles()
        for button, color_role in self._colored_button_roles.items():
            self._set_colored_button_role(button, color_role)
            button.configure(
                foreground=self.palette.text,
                activeforeground=self.palette.text,
                disabledforeground=self.palette.muted,
            )
        if hasattr(self, "chart"):
            self.chart.set_palette(self.palette)
        if hasattr(self, "analytics_splitter"):
            self.analytics_splitter.configure(
                background=self.palette.background,
                proxybackground=self.palette.accent,
            )
        for scrollable_frame in getattr(self, "_scrollable_frames", ()):
            scrollable_frame.set_palette(self.palette)
        self._set_windows_title_bar_theme(normalized == "dark")
        self.after_idle(lambda: self._set_windows_title_bar_theme(normalized == "dark"))

    def _on_theme_selected(self, _event=None) -> None:
        self._apply_theme(self.theme_var.get())
        if hasattr(self, "settings_status_var"):
            self.settings_status_var.set("Theme preview applied — save settings to keep it")
        if hasattr(self, "theme_combo"):
            self.after_idle(lambda: self._clear_combobox_selection(self.theme_combo))

    def _on_window_configure(self, event) -> None:
        size = (event.width, event.height)
        if size == self._pending_window_size:
            return
        self._pending_window_size = size
        self._set_windows_client_redraw(False)
        if self._window_layout_id is not None:
            self.after_cancel(self._window_layout_id)
        self._window_layout_id = self.after(WINDOW_LAYOUT_IDLE_MS, self._commit_window_layout)

    def _commit_window_layout(self) -> None:
        """Resize the complete widget tree once, after the outer drag settles."""
        self._window_layout_id = None
        size = self._pending_window_size
        if size is None or size == self._committed_window_size:
            self._set_windows_client_redraw(True)
            return
        width, height = (max(1, int(value)) for value in size)
        try:
            self.notebook.place_configure(
                x=0,
                y=0,
                width=width,
                height=height,
                relwidth=0,
                relheight=0,
            )
            # Complete the single final geometry pass while Windows is still
            # holding client-area painting. Intermediate Tk/GDI frames never
            # reach DWM, but every widget has its final dimensions before the
            # client is invalidated once below.
            self.update_idletasks()
            if hasattr(self, "chart"):
                self.chart.redraw()
            self._committed_window_size = (width, height)
        finally:
            self._set_windows_client_redraw(True)

    def _set_windows_client_redraw(self, enabled: bool) -> None:
        """Suspend expensive client painting during a native Windows resize."""
        suspended = not enabled
        if sys.platform != "win32" or suspended == self._window_redraw_suspended:
            return
        try:
            user32 = ctypes.windll.user32
            user32.SendMessageW.argtypes = [
                wintypes.HWND,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            user32.SendMessageW.restype = ctypes.c_ssize_t
            user32.RedrawWindow.argtypes = [
                wintypes.HWND,
                ctypes.c_void_p,
                wintypes.HRGN,
                wintypes.UINT,
            ]
            user32.RedrawWindow.restype = wintypes.BOOL
            client_handle = self.winfo_id()
            if not client_handle:
                return
            user32.SendMessageW(client_handle, WM_SETREDRAW, int(enabled), 0)
            self._window_redraw_suspended = suspended
            if enabled:
                user32.RedrawWindow(
                    client_handle,
                    None,
                    None,
                    RDW_INVALIDATE | RDW_ERASE | RDW_FRAME | RDW_ALLCHILDREN,
                )
        except (AttributeError, OSError, tk.TclError):
            # The deferred-layout optimization remains active if the optional
            # native repaint optimization is unavailable on a Windows build.
            self._window_redraw_suspended = False

    def _on_chart_range_selected(self, _event=None) -> None:
        # Range changes redraw the chart only; the table and its scroll stay put.
        self.chart.set_data(self._chart_rows_for_current_range())
        if hasattr(self, "chart_range_combo"):
            self.after_idle(lambda: self._clear_combobox_selection(self.chart_range_combo))

    def _clear_combobox_selection(self, combo: ttk.Combobox) -> None:
        try:
            combo.selection_clear()
        except tk.TclError:
            pass
        self.focus_set()

    def _set_initial_analytics_split(self) -> None:
        if not hasattr(self, "analytics_splitter"):
            return
        width = self.analytics_splitter.winfo_width()
        if width <= 1:
            self.after(50, self._set_initial_analytics_split)
            return
        minimum, maximum = analytics_sash_limits(width)
        position = max(minimum, min(round(width * 0.6), maximum))
        self.analytics_splitter.sash_place(0, position, 0)

    def _set_status(self, text: str, market_text: str = "") -> None:
        self.status_var.set(text)
        self.market_status_var.set(market_text)

    def _build_ui(self) -> None:
        notebook = ttk.Notebook(self, style="App.TNotebook", takefocus=False)
        # Relative sizing is used only for the first hidden layout pass. It is
        # converted to explicit dimensions before the window is shown, keeping
        # the whole widget tree stable throughout a native border drag.
        notebook.place(x=0, y=0, relwidth=1, relheight=1)
        tracker_tab = ttk.Frame(notebook, style="App.TFrame")
        settings_tab = ttk.Frame(notebook, style="App.TFrame")
        diagnostics_tab = ttk.Frame(notebook, style="App.TFrame")
        notebook.add(tracker_tab, text="Tracker")
        notebook.add(settings_tab, text="Settings")
        notebook.add(diagnostics_tab, text="Diagnostics")
        self.notebook = notebook
        self._settings_tab = settings_tab
        self._diagnostics_tab = diagnostics_tab
        notebook.bind("<<NotebookTabChanged>>", self._on_notebook_tab_changed)

        outer = ttk.Frame(tracker_tab, style="App.TFrame", padding=(20, 14))
        outer.pack(fill="both", expand=True)

        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 12))
        title_row = ttk.Frame(header, style="App.TFrame")
        title_row.pack(fill="x")
        ttk.Label(title_row, text="G2G Price Tracker", style="Title.TLabel").pack(side="left")
        ttk.Label(
            title_row,
            text="by: kuti-code",
            style="Subtitle.TLabel",
        ).pack(side="right", anchor="e", pady=(8, 0))
        ttk.Label(
            header,
            text=(
                "Track any seller/market price from a supported G2G category, store its history, "
                "and visualize the trend."
            ),
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 0))

        controls = ttk.Frame(outer, style="Panel.TFrame", padding=14)
        controls.pack(fill="x", pady=(0, 10))
        controls.columnconfigure(0, weight=1)
        controls.columnconfigure(1, weight=0)

        fields = ttk.Frame(controls, style="Panel.TFrame")
        fields.grid(row=0, column=0, sticky="nsew")
        top_fields = ttk.Frame(fields, style="Panel.TFrame")
        top_fields.pack(fill="x", pady=(0, 10))

        seller_field = ttk.Frame(top_fields, style="Panel.TFrame")
        seller_field.pack(side="left")
        ttk.Label(seller_field, text="Seller name", style="PanelTitle.TLabel").pack(anchor="w")
        self.seller_entry = ttk.Entry(
            seller_field,
            width=34,
            textvariable=self.seller_var,
            font=("Segoe UI", 10),
        )
        self.seller_entry.pack(anchor="w", pady=(5, 0))
        self.seller_entry.bind("<FocusOut>", self._schedule_history_refresh)
        self.seller_entry.bind("<Return>", self._schedule_history_refresh)

        interval_field = ttk.Frame(top_fields, style="Panel.TFrame")
        interval_field.pack(side="left", padx=(18, 0))
        ttk.Label(
            interval_field,
            text="Tracking interval",
            style="PanelTitle.TLabel",
        ).pack(anchor="w")
        interval_row = ttk.Frame(interval_field, style="Panel.TFrame")
        interval_row.pack(anchor="w", pady=(5, 0))
        self.interval_spinbox = ttk.Spinbox(
            interval_row,
            from_=1,
            to=1440,
            width=7,
            textvariable=self.interval_var,
            font=("Segoe UI", 10),
        )
        self.interval_spinbox.pack(side="left")
        ttk.Label(interval_row, text="minutes", style="PanelText.TLabel").pack(
            side="left", padx=(8, 0)
        )

        ttk.Label(fields, text="G2G source URL", style="PanelTitle.TLabel").pack(anchor="w")
        self.url_entry = ttk.Entry(fields, textvariable=self.url_var, font=("Segoe UI", 9))
        self.url_entry.pack(fill="x", pady=(5, 0))
        self.url_entry.bind("<FocusOut>", self._schedule_history_refresh)
        self.url_entry.bind("<Return>", self._schedule_history_refresh)

        actions = ttk.Frame(controls, style="Panel.TFrame")
        actions.grid(row=0, column=1, sticky="e", padx=(22, 0))
        actions.columnconfigure(0, weight=1)
        actions.columnconfigure(1, weight=1)
        self.start_button = self._colored_button(
            actions,
            text="Start tracking",
            command=self._start,
            color_role="start_ready",
        )
        self.start_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = self._colored_button(
            actions,
            text="Stop",
            command=self._stop,
            color_role="stop",
        )
        self.stop_button.configure(state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        self.once_button = ttk.Button(
            actions,
            text="Check now",
            style="Secondary.TButton",
            command=self._collect_once,
            takefocus=False,
        )
        self.once_button.grid(row=1, column=0, sticky="ew", padx=(0, 5), pady=(6, 0))
        self.export_button = ttk.Button(
            actions,
            text="Export data",
            style="Secondary.TButton",
            command=self._export_data,
            takefocus=False,
        )
        self.export_button.grid(row=1, column=1, sticky="ew", padx=(5, 0), pady=(6, 0))
        self.reset_button = self._colored_button(
            actions,
            text="Reset history",
            command=self._reset_history,
            color_role="reset",
        )
        self.reset_button.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))

        status_row = ttk.Frame(outer, style="Panel.TFrame", padding=(14, 8))
        status_row.pack(fill="x", pady=(0, 10))
        ttk.Label(status_row, textvariable=self.status_var, style="Status.TLabel").pack(side="left")
        ttk.Label(
            status_row,
            textvariable=self.market_status_var,
            style="MarketStatus.TLabel",
        ).pack(side="left", padx=(14, 0))
        ttk.Label(status_row, textvariable=self.next_var, style="Countdown.TLabel").pack(
            side="right"
        )

        self.analytics_frame = ttk.Frame(outer, style="App.TFrame")
        self.analytics_frame.pack(fill="both", expand=True)

        metrics = ttk.Frame(self.analytics_frame, style="App.TFrame")
        metrics.pack(fill="x", pady=(0, 10))
        metric_definitions = (
            ("Seller Latest", self.latest_var),
            ("Seller Change", self.change_var),
            ("Seller Lowest", self.low_var),
            ("Seller Highest", self.high_var),
            ("Seller Average", self.all_average_var),
            ("Market Lowest", self.market_low_var),
            ("Market Average (Lowest 5)", self.market_average_var),
            ("Price checks", self.count_var),
        )
        for index, (label, variable) in enumerate(metric_definitions):
            metrics.columnconfigure(index, weight=1)
            card = ttk.Frame(metrics, style="Panel.TFrame", padding=(11, 9))
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 6, 0))
            ttk.Label(card, text=label, style="MetricName.TLabel").pack(anchor="w")
            ttk.Label(card, textvariable=variable, style="MetricValue.TLabel").pack(
                anchor="w", pady=(4, 0)
            )

        content = tk.PanedWindow(
            self.analytics_frame,
            orient="horizontal",
            background=self.palette.background,
            borderwidth=0,
            sashwidth=ANALYTICS_SASH_WIDTH,
            sashrelief="flat",
            opaqueresize=False,
            proxybackground=self.palette.accent,
            proxyborderwidth=0,
            proxyrelief="flat",
        )
        content.pack(fill="both", expand=True)
        self.analytics_splitter = content

        chart_panel = ttk.Frame(content, style="Panel.TFrame", padding=13)
        self.chart_panel = chart_panel
        table_panel = ttk.Frame(content, style="Panel.TFrame", padding=13)
        # The splitter owns pane sizes; text/table requests must not feed back
        # into its geometry when column widths or labels change.
        chart_panel.pack_propagate(False)
        table_panel.pack_propagate(False)
        content.add(chart_panel, minsize=CHART_PANE_MIN_WIDTH, stretch="always")
        content.add(table_panel, minsize=TABLE_PANE_MIN_WIDTH, stretch="always")
        self.after_idle(self._set_initial_analytics_split)

        chart_header = ttk.Frame(chart_panel, style="Panel.TFrame")
        chart_header.pack(fill="x")
        ttk.Label(chart_header, text="Price history", style="PanelTitle.TLabel").pack(side="left")
        ttk.Label(chart_header, textvariable=self.listing_var, style="PanelTitle.TLabel").pack(
            side="right"
        )
        chart_meta = ttk.Frame(chart_panel, style="Panel.TFrame")
        chart_meta.pack(fill="x", pady=(2, 8))
        ttk.Label(chart_meta, textvariable=self.chart_subtitle_var, style="PanelText.TLabel").pack(
            side="left"
        )
        self.chart_range_combo = ttk.Combobox(
            chart_meta,
            state="readonly",
            takefocus=False,
            width=17,
            values=CHART_RANGES,
            textvariable=self.chart_range_var,
        )
        self.chart_range_combo.pack(side="right")
        self.chart_range_combo.bind("<<ComboboxSelected>>", self._on_chart_range_selected)
        self.chart = PriceChart(chart_panel, self.palette)
        self.chart.pack(fill="both", expand=True)

        ttk.Label(table_panel, text="Recent price checks", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 8)
        )
        table_container = ttk.Frame(table_panel, style="Panel.TFrame")
        table_container.pack(fill="both", expand=True)
        table_container.columnconfigure(0, weight=1)
        table_container.rowconfigure(0, weight=1)
        table_container.grid_propagate(False)
        self.table = ttk.Treeview(
            table_container,
            columns=("time", "price", "market", "average"),
            show="headings",
        )
        self.table.heading("time", text="Date")
        self.table.heading("price", text="Seller")
        self.table.heading("market", text="Market Lowest")
        self.table.heading("average", text="Market Average (Lowest 5)")
        self.table.column("time", width=140, minwidth=140, anchor="center", stretch=False)
        self.table.column("price", width=90, minwidth=90, anchor="e", stretch=False)
        self.table.column("market", width=120, minwidth=120, anchor="e", stretch=False)
        self.table.column("average", width=160, minwidth=160, anchor="e", stretch=True)
        vertical_scrollbar = ttk.Scrollbar(
            table_container,
            orient="vertical",
            command=self.table.yview,
            style="App.Vertical.TScrollbar",
        )
        horizontal_scrollbar = ttk.Scrollbar(
            table_container,
            orient="horizontal",
            command=self.table.xview,
            style="App.Horizontal.TScrollbar",
        )
        self.table.configure(
            yscrollcommand=vertical_scrollbar.set,
            xscrollcommand=horizontal_scrollbar.set,
        )
        self.table.grid(row=0, column=0, sticky="nsew")
        vertical_scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal_scrollbar.grid(row=1, column=0, sticky="ew")

    def _on_notebook_tab_changed(self, _event=None) -> None:
        try:
            selected = str(self.notebook.select())
        except tk.TclError:
            return
        if selected == str(self._settings_tab):
            self._ensure_settings_tab()
        elif selected == str(self._diagnostics_tab):
            self._ensure_diagnostics_tab()
        elif hasattr(self, "chart"):
            self.chart._request_frame()

    def _ensure_settings_tab(self) -> None:
        if self._settings_built:
            return
        settings_scroll = ScrollableFrame(self._settings_tab, self.palette)
        settings_scroll.pack(fill="both", expand=True)
        self._scrollable_frames.append(settings_scroll)
        self._build_settings_ui(settings_scroll.content)
        self._settings_built = True

    def _ensure_diagnostics_tab(self) -> None:
        if self._diagnostics_built:
            return
        diagnostics_scroll = ScrollableFrame(self._diagnostics_tab, self.palette)
        diagnostics_scroll.pack(fill="both", expand=True)
        self._scrollable_frames.append(diagnostics_scroll)
        self._build_diagnostics_ui(diagnostics_scroll.content)
        self._diagnostics_built = True

    def _build_settings_ui(self, parent: tk.Misc) -> None:
        outer = ttk.Frame(parent, style="App.TFrame", padding=(24, 18))
        outer.pack(fill="both", expand=True)
        header = ttk.Frame(outer, style="App.TFrame")
        header.pack(fill="x", pady=(0, 14))
        ttk.Label(header, text="Application settings", style="Title.TLabel").pack(side="left")
        ttk.Label(
            outer,
            text=(
                "Configure appearance, optional price alerts, notifications, startup, and "
                "background window behavior."
            ),
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(0, 14))

        appearance_panel = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        appearance_panel.pack(fill="x", pady=(0, 14))
        appearance_text = ttk.Frame(appearance_panel, style="Panel.TFrame")
        appearance_text.pack(side="left", fill="x", expand=True)
        ttk.Label(appearance_text, text="Appearance", style="PanelTitle.TLabel").pack(anchor="w")
        ttk.Label(
            appearance_text,
            text="Light is the default. Dark uses a modern deep-navy palette and applies instantly.",
            style="PanelText.TLabel",
        ).pack(anchor="w", pady=(5, 0))
        self.theme_combo = ttk.Combobox(
            appearance_panel,
            state="readonly",
            takefocus=False,
            width=12,
            values=("Light", "Dark"),
            textvariable=self.theme_var,
            font=("Segoe UI", 10),
        )
        self.theme_combo.pack(side="right", padx=(18, 0))
        self.theme_combo.bind("<<ComboboxSelected>>", self._on_theme_selected)

        panels = ttk.Frame(outer, style="App.TFrame")
        panels.pack(fill="x")
        panels.columnconfigure(0, weight=1)
        panels.columnconfigure(1, weight=1)

        threshold_panel = ttk.Frame(panels, style="Panel.TFrame", padding=16)
        threshold_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ttk.Label(threshold_panel, text="Price threshold", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 9)
        )
        ttk.Checkbutton(
            threshold_panel,
            text="Enable below-price alert",
            variable=self.alert_enabled_var,
            command=self._sync_alert_controls,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).pack(anchor="w")
        threshold_row = ttk.Frame(threshold_panel, style="Panel.TFrame")
        threshold_row.pack(fill="x", pady=(12, 8))
        ttk.Label(
            threshold_row,
            text="Alert when price is below",
            style="PanelText.TLabel",
        ).pack(side="left")
        self.alert_threshold_entry = ttk.Entry(
            threshold_row,
            width=14,
            textvariable=self.alert_threshold_var,
            font=("Segoe UI", 10),
        )
        self.alert_threshold_entry.pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            threshold_panel,
            text="Alert once, then re-arm after the price recovers",
            variable=self.alert_once_var,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).pack(anchor="w", pady=(4, 0))
        ttk.Label(
            threshold_panel,
            text="This avoids repeated alerts while the price remains below the threshold.",
            style="PanelText.TLabel",
        ).pack(anchor="w", pady=(5, 0))

        sound_panel = ttk.Frame(panels, style="Panel.TFrame", padding=16)
        sound_panel.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ttk.Label(sound_panel, text="Sound", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 9)
        )
        ttk.Checkbutton(
            sound_panel,
            text="Play an alert sound",
            variable=self.sound_enabled_var,
            command=self._sync_alert_controls,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).pack(anchor="w")
        duration_row = ttk.Frame(sound_panel, style="Panel.TFrame")
        duration_row.pack(fill="x", pady=(12, 8))
        ttk.Label(duration_row, text="Duration", style="PanelText.TLabel").pack(side="left")
        self.sound_duration_spinbox = ttk.Spinbox(
            duration_row,
            from_=1,
            to=3600,
            width=8,
            textvariable=self.sound_duration_var,
            font=("Segoe UI", 10),
        )
        self.sound_duration_spinbox.pack(side="left", padx=(10, 6))
        ttk.Label(duration_row, text="seconds", style="PanelText.TLabel").pack(side="left")
        self.sound_until_check = ttk.Checkbutton(
            sound_panel,
            text="Keep playing until the alert is dismissed",
            variable=self.sound_until_dismissed_var,
            command=self._sync_alert_controls,
            style="Panel.TCheckbutton",
            takefocus=False,
        )
        self.sound_until_check.pack(anchor="w", pady=(2, 8))
        sound_actions = ttk.Frame(sound_panel, style="Panel.TFrame")
        sound_actions.pack(anchor="w")
        ttk.Button(
            sound_actions,
            text="Test sound",
            style="Secondary.TButton",
            command=self._test_sound,
            takefocus=False,
        ).pack(side="left")
        ttk.Button(
            sound_actions,
            text="Stop sound",
            style="Secondary.TButton",
            command=self.sound_alarm.stop,
            takefocus=False,
        ).pack(side="left", padx=(8, 0))

        telegram_panel = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        telegram_panel.pack(fill="x", pady=(14, 0))
        ttk.Label(telegram_panel, text="Telegram", style="PanelTitle.TLabel").grid(
            row=0, column=0, sticky="w", pady=(0, 9)
        )
        ttk.Checkbutton(
            telegram_panel,
            text="Send Telegram notifications",
            variable=self.telegram_enabled_var,
            command=self._sync_alert_controls,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).grid(row=1, column=0, columnspan=4, sticky="w")
        ttk.Label(telegram_panel, text="Bot token", style="PanelText.TLabel").grid(
            row=2, column=0, sticky="w", pady=(12, 0)
        )
        self.telegram_token_entry = ttk.Entry(
            telegram_panel,
            width=44,
            textvariable=self.telegram_token_var,
            show="•",
            font=("Segoe UI", 10),
        )
        self.telegram_token_entry.grid(row=3, column=0, sticky="w", pady=(5, 0))
        ttk.Label(telegram_panel, text="Chat ID", style="PanelText.TLabel").grid(
            row=2, column=1, sticky="w", padx=(18, 0), pady=(12, 0)
        )
        self.telegram_chat_entry = ttk.Entry(
            telegram_panel,
            width=24,
            textvariable=self.telegram_chat_id_var,
            font=("Segoe UI", 10),
        )
        self.telegram_chat_entry.grid(row=3, column=1, sticky="w", padx=(18, 0), pady=(5, 0))
        self.telegram_test_button = ttk.Button(
            telegram_panel,
            text="Test Telegram",
            style="Secondary.TButton",
            command=self._test_telegram,
            takefocus=False,
        )
        self.telegram_test_button.grid(row=3, column=2, sticky="w", padx=(18, 0), pady=(5, 0))
        ttk.Label(
            telegram_panel,
            text=(
                "The bot token is encrypted with Windows Data Protection API and can only be "
                "decrypted by your Windows account."
            ),
            style="PanelText.TLabel",
        ).grid(row=4, column=0, columnspan=4, sticky="w", pady=(9, 0))

        system_panel = ttk.Frame(outer, style="Panel.TFrame", padding=16)
        system_panel.pack(fill="x", pady=(14, 0))
        ttk.Label(system_panel, text="Background operation", style="PanelTitle.TLabel").pack(
            anchor="w", pady=(0, 9)
        )
        ttk.Checkbutton(
            system_panel,
            text="Run G2G Price Tracker when Windows starts",
            variable=self.start_with_windows_var,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).pack(anchor="w")
        ttk.Checkbutton(
            system_panel,
            text="Minimize to the system tray when the window is closed",
            variable=self.minimize_to_tray_var,
            style="Panel.TCheckbutton",
            takefocus=False,
        ).pack(anchor="w", pady=(8, 0))
        ttk.Label(
            system_panel,
            text=(
                "When enabled, the X button hides the app without interrupting tracking. "
                "Disable it to make the X button exit immediately."
            ),
            style="PanelText.TLabel",
        ).pack(anchor="w", pady=(6, 0))

        footer = ttk.Frame(outer, style="App.TFrame")
        footer.pack(fill="x", pady=(16, 0))
        self.settings_status_var = tk.StringVar(value="Settings are loaded")
        ttk.Label(footer, textvariable=self.settings_status_var, style="Subtitle.TLabel").pack(
            side="left"
        )
        self._colored_button(
            footer,
            text="Save settings",
            command=self._save_settings,
            color_role="start_ready",
        ).pack(side="right")
        self._sync_alert_controls()

    def _build_diagnostics_ui(self, parent: tk.Misc) -> None:
        outer = ttk.Frame(parent, style="App.TFrame", padding=(24, 18))
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="Diagnostics", style="Title.TLabel").pack(anchor="w")
        ttk.Label(
            outer,
            text="Connection health, retry state, and local troubleshooting details.",
            style="Subtitle.TLabel",
        ).pack(anchor="w", pady=(4, 16))

        panel = ttk.Frame(outer, style="Panel.TFrame", padding=18)
        panel.pack(fill="x")
        diagnostics = (
            ("Health", self.health_var),
            ("Last successful check", self.last_success_var),
            ("Last request duration", self.request_duration_var),
            ("Consecutive failures", self.failure_count_var),
            ("Last error", self.last_error_var),
        )
        for row_index, (label, variable) in enumerate(diagnostics):
            ttk.Label(panel, text=label, style="PanelTitle.TLabel").grid(
                row=row_index, column=0, sticky="nw", pady=8
            )
            ttk.Label(
                panel,
                textvariable=variable,
                style="PanelText.TLabel",
                wraplength=850,
                justify="left",
            ).grid(row=row_index, column=1, sticky="w", padx=(28, 0), pady=8)

        actions = ttk.Frame(outer, style="App.TFrame")
        actions.pack(fill="x", pady=(16, 0))
        ttk.Button(
            actions,
            text="Open log folder",
            style="Secondary.TButton",
            command=self._open_log_folder,
            takefocus=False,
        ).pack(side="left")
        ttk.Label(
            actions,
            text=str(self.paths.log),
            style="Subtitle.TLabel",
        ).pack(side="left", padx=(14, 0))

    def _sync_alert_controls(self) -> None:
        alert_state = "normal" if self.alert_enabled_var.get() else "disabled"
        sound_state = (
            "normal"
            if self.alert_enabled_var.get() and self.sound_enabled_var.get()
            else "disabled"
        )
        telegram_state = (
            "normal"
            if self.alert_enabled_var.get() and self.telegram_enabled_var.get()
            else "disabled"
        )
        self.alert_threshold_entry.configure(state=alert_state)
        self.sound_duration_spinbox.configure(
            state="disabled" if self.sound_until_dismissed_var.get() else sound_state
        )
        self.sound_until_check.configure(state=sound_state)
        self.telegram_token_entry.configure(state=telegram_state)
        self.telegram_chat_entry.configure(state=telegram_state)
        self.telegram_test_button.configure(state=telegram_state)

    def _save_settings(self, *, show_confirmation: bool = True) -> bool:
        threshold_text = self.alert_threshold_var.get().strip()
        if self.alert_enabled_var.get():
            try:
                threshold = Decimal(threshold_text)
            except InvalidOperation:
                messagebox.showwarning(
                    "Invalid alert threshold",
                    "Enter a valid price such as 0.025.",
                    parent=self,
                )
                return False
            if not threshold.is_finite() or threshold <= 0:
                messagebox.showwarning(
                    "Invalid alert threshold",
                    "The alert threshold must be greater than zero.",
                    parent=self,
                )
                return False

        try:
            duration = int(self.sound_duration_var.get())
        except ValueError:
            duration = 0
        if not 1 <= duration <= 3600:
            messagebox.showwarning(
                "Invalid sound duration",
                "Sound duration must be between 1 and 3600 seconds.",
                parent=self,
            )
            return False

        token = self.telegram_token_var.get().strip()
        chat_id = self.telegram_chat_id_var.get().strip()
        if self.telegram_enabled_var.get() and (not token or not chat_id):
            messagebox.showwarning(
                "Incomplete Telegram settings",
                "Enter both a bot token and a chat ID, or disable Telegram notifications.",
                parent=self,
            )
            return False

        updated = replace(
            self.settings,
            price_alert_enabled=self.alert_enabled_var.get(),
            price_alert_threshold=threshold_text,
            sound_alert_enabled=self.sound_enabled_var.get(),
            sound_duration_seconds=duration,
            sound_until_dismissed=self.sound_until_dismissed_var.get(),
            telegram_enabled=self.telegram_enabled_var.get(),
            telegram_chat_id=chat_id,
            alert_once_until_recovery=self.alert_once_var.get(),
            start_with_windows=self.start_with_windows_var.get(),
            minimize_to_tray_on_close=self.minimize_to_tray_var.get(),
            theme="dark" if self.theme_var.get().lower() == "dark" else "light",
        )
        try:
            set_startup_enabled(updated.start_with_windows)
            save_secret(self.paths.telegram_secret, token)
            updated.save(self.paths.settings)
        except (OSError, SecretStorageError) as exc:
            messagebox.showerror("Could not save settings", str(exc), parent=self)
            return False
        self.settings = updated
        self._apply_theme(updated.theme)
        self.telegram_token = token
        self.alert_armed = True
        self.settings_status_var.set("Settings saved")
        if show_confirmation:
            messagebox.showinfo("Settings saved", "Application settings were saved.", parent=self)
        return True

    def _test_sound(self) -> None:
        duration = None if self.sound_until_dismissed_var.get() else 3
        self.sound_alarm.start(duration)
        if duration is None:
            messagebox.showinfo(
                "Sound test",
                "The sound will stop when you close this message.",
                parent=self,
            )
            self.sound_alarm.stop()

    def _test_telegram(self) -> None:
        token = self.telegram_token_var.get().strip()
        chat_id = self.telegram_chat_id_var.get().strip()
        if not token or not chat_id:
            messagebox.showwarning(
                "Incomplete Telegram settings",
                "Enter both a bot token and a chat ID first.",
                parent=self,
            )
            return
        self.telegram_test_button.configure(state="disabled")
        self.settings_status_var.set("Sending Telegram test...")
        threading.Thread(
            target=self._send_telegram_worker,
            args=(token, chat_id, "G2G Price Tracker test notification ✓", True),
            daemon=True,
            name="telegram-test",
        ).start()

    def _current_settings(self) -> AppSettings:
        seller = self.seller_var.get().strip()
        if not seller:
            raise ValueError("Seller name cannot be empty.")
        try:
            interval = int(self.interval_var.get())
        except ValueError as exc:
            raise ValueError("Tracking interval must be a whole number.") from exc
        if not 1 <= interval <= 1440:
            raise ValueError("Tracking interval must be between 1 and 1440 minutes.")

        source_url = self.url_var.get().strip()
        parsed = urlparse(source_url)
        hostname = (parsed.hostname or "").lower()
        if parsed.scheme != "https" or not (hostname == "g2g.com" or hostname.endswith(".g2g.com")):
            raise ValueError("The source must be a valid HTTPS G2G URL.")
        return replace(
            self.settings,
            seller=seller,
            interval_minutes=interval,
            source_url=source_url,
        )

    @staticmethod
    def _tracker_config(settings: AppSettings) -> TrackerConfig:
        return TrackerConfig(url=settings.source_url, seller=settings.seller)

    def _selected_target_key(self) -> str | None:
        seller = self.seller_var.get().strip()
        source_url = self.url_var.get().strip()
        if not seller or not source_url:
            return None
        return build_target_key(seller, source_url)

    def _start(self) -> None:
        try:
            settings = self._current_settings()
        except ValueError as exc:
            messagebox.showwarning("Invalid settings", str(exc), parent=self)
            return

        settings.save(self.paths.settings)
        self.settings = settings
        self.stop_event.clear()
        self.is_running = True
        self._set_controls_running(True)
        self._set_status(f"Preparing the first price check for {settings.seller}...")
        self.next_var.set("New check in: now")
        self.scheduler_thread = threading.Thread(
            target=self._scheduler_loop,
            args=(settings,),
            daemon=True,
            name="price-scheduler",
        )
        self.scheduler_thread.start()
        self.logger.info(
            "Tracking started for seller=%s interval=%sm",
            settings.seller,
            settings.interval_minutes,
        )

    def _stop(self) -> None:
        self.stop_event.set()
        self.is_running = False
        self.next_collection_at = None
        self._set_controls_running(False)
        self._set_status("Stop requested. An active price check may still finish.")
        self.next_var.set("Automatic tracking is off")
        self.logger.info("Tracking stop requested")

    def _collect_once(self) -> None:
        try:
            settings = self._current_settings()
        except ValueError as exc:
            messagebox.showwarning("Invalid settings", str(exc), parent=self)
            return
        settings.save(self.paths.settings)
        threading.Thread(
            target=self._single_collection,
            args=(settings,),
            daemon=True,
            name="manual-collection",
        ).start()

    def _scheduler_loop(self, settings: AppSettings) -> None:
        while not self.stop_event.is_set():
            self._single_collection(settings)
            if self.stop_event.is_set():
                break
            due = time.time() + settings.interval_minutes * 60
            self.events.put(("next_due", due))
            if self.stop_event.wait(settings.interval_minutes * 60):
                break
        self.events.put(("scheduler_stopped", None))

    def _single_collection(self, settings: AppSettings) -> None:
        if not self.collect_lock.acquire(blocking=False):
            self.events.put(("busy", None))
            return
        self.events.put(("collecting", settings.seller))
        started = time.monotonic()
        try:
            retry_index = 0
            while True:
                try:
                    observation = collect_price(self._tracker_config(settings))
                    break
                except CollectionError as exc:
                    if not exc.retryable or retry_index >= len(RETRY_DELAYS):
                        raise
                    delay = retry_delay(exc, retry_index, jitter=random.uniform(0, 1.5))
                    retry_index += 1
                    self.events.put(("retrying", (delay, str(exc))))
                    logger = getattr(self, "logger", None)
                    if logger:
                        logger.warning(
                            "Temporary collection error; retry %s in %.1fs: %s",
                            retry_index,
                            delay,
                            exc,
                        )
                    if self.stop_event.wait(delay):
                        raise CollectionError("The retry was cancelled because tracking stopped.")
            row_id = self.repository.add(observation)
            duration = time.monotonic() - started
            self.events.put(("success", (row_id, observation, duration)))
            logger = getattr(self, "logger", None)
            if logger:
                logger.info(
                    "Price saved seller=%s seller_price=%s market_low=%s market_average=%s duration=%.2fs",
                    observation.seller,
                    observation.unit_price,
                    observation.market_lowest_price,
                    observation.market_average_price,
                    duration,
                )
        except PriceNotFoundError as exc:
            self.stop_event.set()
            self.events.put(("seller_not_found", str(exc)))
            logger = getattr(self, "logger", None)
            if logger:
                logger.error("Seller not found: %s", exc)
        except (CollectionError, OSError, ValueError) as exc:
            duration = time.monotonic() - started
            self.events.put(("error", (str(exc), duration)))
            logger = getattr(self, "logger", None)
            if logger:
                logger.error("Price check failed after %.2fs: %s", duration, exc)
        except Exception as exc:
            duration = time.monotonic() - started
            self.events.put(("error", (f"Unexpected error: {exc}", duration)))
            logger = getattr(self, "logger", None)
            if logger:
                logger.exception("Unexpected price check error")
        finally:
            self.collect_lock.release()

    def _process_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break

            if event == "collecting":
                self.once_button.configure(state="disabled")
                self._set_status(f"Checking G2G: {payload}")
            elif event == "success":
                _row_id, observation, duration = payload
                self.last_observation = observation
                self.consecutive_failures = 0
                self.health_var.set("Healthy")
                self.last_success_var.set(datetime.now().astimezone().strftime("%d.%m.%Y %H:%M:%S"))
                self.request_duration_var.set(f"{duration:.2f} seconds")
                self.failure_count_var.set("0")
                self.last_error_var.set("—")
                market_text = (
                    _price_text(observation.market_lowest_price, observation.currency)
                    if observation.market_lowest_price is not None
                    else "—"
                )
                self._set_status(
                    (
                        f"Saved {observation.seller} · "
                        f"{_price_text(observation.unit_price, observation.currency)}"
                    ),
                    (
                        f"Market Lowest ({observation.market_lowest_seller or 'Unknown seller'})"
                        f" · {market_text}"
                    ),
                )
                self.once_button.configure(state="normal")
                self._refresh_history(observation.target_key)
                self._evaluate_price_alert(observation)
            elif event == "error":
                error_text, duration = payload
                self.consecutive_failures += 1
                self.health_var.set("Degraded")
                self.request_duration_var.set(f"{duration:.2f} seconds")
                self.failure_count_var.set(str(self.consecutive_failures))
                self.last_error_var.set(error_text)
                self._set_status("Price check failed")
                self.once_button.configure(state="normal")
                messagebox.showerror(
                    "Could not collect price",
                    f"{error_text}\n\nCheck the seller name and filters in the G2G source URL.",
                    parent=self,
                )
            elif event == "seller_not_found":
                self.consecutive_failures += 1
                self.health_var.set("Configuration error")
                self.failure_count_var.set(str(self.consecutive_failures))
                self.last_error_var.set(str(payload))
                was_running = self.is_running
                self.stop_event.set()
                self.is_running = False
                self.next_collection_at = None
                self._set_controls_running(False)
                self.once_button.configure(state="normal")
                self._set_status(
                    "Seller not found — tracking stopped" if was_running else "Seller not found"
                )
                self.next_var.set("Automatic tracking is off")
                suffix = "\n\nAutomatic tracking has been stopped." if was_running else ""
                messagebox.showerror(
                    "Seller not found",
                    f"{payload}{suffix}",
                    parent=self,
                )
            elif event == "busy":
                self._set_status("A price check is already in progress.")
            elif event == "retrying":
                delay, error_text = payload
                self.health_var.set("Retrying temporary failure")
                self.last_error_var.set(error_text)
                self._set_status(f"Temporary error — retrying in {delay:.0f} seconds")
            elif event == "next_due":
                self.next_collection_at = float(payload)
            elif event == "telegram_test_success":
                if hasattr(self, "telegram_test_button"):
                    self.telegram_test_button.configure(state="normal")
                self.settings_status_var.set("Telegram test delivered")
                messagebox.showinfo(
                    "Telegram test",
                    "The test notification was delivered.",
                    parent=self,
                )
            elif event == "telegram_test_error":
                if hasattr(self, "telegram_test_button"):
                    self.telegram_test_button.configure(state="normal")
                self.settings_status_var.set("Telegram test failed")
                messagebox.showerror("Telegram test failed", str(payload), parent=self)
            elif event == "telegram_alert_error":
                self.settings_status_var.set("Telegram alert failed")
                messagebox.showerror("Telegram alert failed", str(payload), parent=self)
            elif (
                event == "scheduler_stopped"
                and self.stop_event.is_set()
                and self.status_var.get() != "Seller not found — tracking stopped"
            ):
                self._set_status("Tracking stopped")

        self.after(200, self._process_events)

    def _evaluate_price_alert(self, observation: object) -> None:
        settings = self.settings
        if not settings.price_alert_enabled or not settings.price_alert_threshold:
            return
        try:
            threshold = Decimal(settings.price_alert_threshold)
        except InvalidOperation:
            return
        price = observation.unit_price
        if price >= threshold:
            self.alert_armed = True
            return
        if not should_trigger_price_alert(
            price,
            threshold,
            armed=self.alert_armed,
            once_until_recovery=settings.alert_once_until_recovery,
        ):
            return
        if settings.alert_once_until_recovery:
            self.alert_armed = False

        if settings.telegram_enabled and self.telegram_token and settings.telegram_chat_id:
            message = (
                "G2G Price Alert\n"
                f"Seller: {observation.seller}\n"
                f"Listing: {observation.listing_title}\n"
                f"Current price: {_price_text(price, observation.currency)}\n"
                f"Threshold: {_price_text(threshold, observation.currency)}\n"
                f"Source: {observation.source_url}"
            )
            threading.Thread(
                target=self._send_telegram_worker,
                args=(
                    self.telegram_token,
                    settings.telegram_chat_id,
                    message,
                    False,
                ),
                daemon=True,
                name="telegram-price-alert",
            ).start()

        if settings.sound_alert_enabled:
            duration = None if settings.sound_until_dismissed else settings.sound_duration_seconds
            self.sound_alarm.start(duration)
        if self.state() == "withdrawn":
            self._restore_from_tray()
        messagebox.showwarning(
            "Price alert",
            (
                f"{observation.seller} is now {_price_text(price, observation.currency)}.\n"
                f"Your alert threshold is {_price_text(threshold, observation.currency)}."
            ),
            parent=self,
        )
        if settings.sound_until_dismissed:
            self.sound_alarm.stop()

    def _send_telegram_worker(
        self,
        token: str,
        chat_id: str,
        text: str,
        is_test: bool,
    ) -> None:
        try:
            send_telegram_message(token, chat_id, text)
        except (RuntimeError, ValueError) as exc:
            self.events.put(
                ("telegram_test_error" if is_test else "telegram_alert_error", str(exc))
            )
        else:
            if is_test:
                self.events.put(("telegram_test_success", None))

    def _update_countdown(self) -> None:
        if self.is_running and self.next_collection_at:
            remaining = max(0, int(self.next_collection_at - time.time()))
            minutes, seconds = divmod(remaining, 60)
            self.next_var.set(f"New check in: {minutes:02d}:{seconds:02d}")
        self.after(1000, self._update_countdown)

    def _set_controls_running(self, running: bool) -> None:
        input_state = "disabled" if running else "normal"
        self.seller_entry.configure(state=input_state)
        self.interval_spinbox.configure(state=input_state)
        self.url_entry.configure(state=input_state)
        self.start_button.configure(
            state="disabled" if running else "normal",
            text="Tracking..." if running else "Start tracking",
        )
        self._set_colored_button_role(
            self.start_button,
            "start_running" if running else "start_ready",
        )
        self.stop_button.configure(state="normal" if running else "disabled")
        self.reset_button.configure(state="disabled" if running else "normal")

    def _update_recent_table(self, rows: list[object]) -> None:
        """Update rows by database identity instead of destroying the whole table."""
        values_by_id = {}
        for row in rows[:TABLE_ROW_LIMIT]:
            observed_at = _local_time(row["observed_at"]).strftime("%d.%m.%Y %H:%M")
            market_price = row["market_lowest_price"]
            market_average = row["market_average_price"]
            values_by_id[f"price-{row['id']}"] = (
                observed_at,
                _price_text(Decimal(row["unit_price"]), row["currency"]),
                (
                    _price_text(Decimal(market_price), row["currency"])
                    if market_price is not None
                    else "—"
                ),
                (
                    _price_text(Decimal(market_average), row["currency"])
                    if market_average is not None
                    else "—"
                ),
            )
        if values_by_id == self._table_values and tuple(values_by_id) == tuple(self._table_values):
            return
        previous_view = self.table.yview()
        removed = self._table_values.keys() - values_by_id.keys()
        if removed:
            self.table.delete(*removed)
        for item_id, values in values_by_id.items():
            if item_id not in self._table_values:
                self.table.insert("", "end", iid=item_id, values=values)
            elif values != self._table_values[item_id]:
                self.table.item(item_id, values=values)
        self.table.set_children("", *values_by_id)
        if previous_view and previous_view[0] > 0:
            self.table.yview_moveto(previous_view[0])
        self._table_values = values_by_id

    def _schedule_history_refresh(self, _event=None) -> None:
        if self._history_refresh_id is not None:
            self.after_cancel(self._history_refresh_id)
        self._history_refresh_id = self.after(180, self._refresh_history_if_target_changed)

    def _refresh_history_if_target_changed(self) -> None:
        self._history_refresh_id = None
        selected = self._selected_target_key()
        if selected == self._displayed_target_key:
            return
        self._refresh_history(selected)

    def _chart_rows_for_current_range(self, target_key: str | None = None) -> list[object]:
        selected = target_key if target_key is not None else self._selected_target_key()
        if not selected:
            return []
        return self.repository.rows_for_chart(selected, self.chart_range_var.get())

    def _clear_metrics(self) -> None:
        self.latest_var.set("—")
        self.change_var.set("—")
        self.low_var.set("—")
        self.market_low_var.set("—")
        self.high_var.set("—")
        self.all_average_var.set("—")
        self.market_average_var.set("—")
        self.count_var.set("0")
        self.chart_subtitle_var.set("No price checks for this target")
        self.listing_var.set("—")

    def _apply_summary(self, summary: dict[str, object]) -> None:
        statistics = summary["statistics"]
        currency = str(summary["currency"])
        self.latest_var.set(_price_text(statistics.latest, currency))
        self.low_var.set(_price_text(statistics.lowest, currency))
        latest_market_price = summary["market_lowest_price"]
        self.market_low_var.set(
            _price_text(Decimal(str(latest_market_price)), currency)
            if latest_market_price is not None
            else "—"
        )
        self.high_var.set(_price_text(statistics.highest, currency))
        self.all_average_var.set(_price_text(statistics.all_time_average, currency))
        latest_market_average = summary["market_average_price"]
        self.market_average_var.set(
            _price_text(Decimal(str(latest_market_average)), currency)
            if latest_market_average is not None
            else "—"
        )
        self.count_var.set(str(summary["count"]))
        market_seller = summary["market_lowest_seller"] or "market leader"
        self.chart_subtitle_var.set(f"Selected Seller vs. Market Lowest ({market_seller})")
        self.listing_var.set(str(summary["listing_title"]))
        if statistics.latest_change_percent is None:
            self.change_var.set("—")
        else:
            self.change_var.set(f"{statistics.latest_change_percent:+.2f}%")

    def _refresh_history(self, target_key: str | None = None) -> None:
        selected_target = target_key or self._selected_target_key()
        self._displayed_target_key = selected_target
        if not selected_target:
            self.chart.set_data([])
            self._update_recent_table([])
            self._clear_metrics()
            return

        summary = self.repository.target_summary(selected_target)
        table_rows = self.repository.recent_rows(selected_target)
        chart_rows = self.repository.rows_for_chart(selected_target, self.chart_range_var.get())
        self.chart.set_data(chart_rows)
        self._update_recent_table(table_rows)
        if summary is None:
            self._clear_metrics()
            return
        self._apply_summary(summary)

    def _export_data(self) -> None:
        target_key = self._selected_target_key()
        seller = self.seller_var.get().strip()
        if not target_key:
            messagebox.showwarning(
                "Invalid target",
                "Enter a seller name and G2G source URL first.",
                parent=self,
            )
            return
        rows = self.repository.rows(target_key=target_key)
        if not rows:
            messagebox.showinfo(
                "Nothing to export",
                "There are no saved price checks for this target.",
                parent=self,
            )
            return
        safe_seller = "".join(
            character for character in seller if character.isalnum() or character in "-_"
        )
        output_directory = filedialog.askdirectory(
            parent=self,
            title="Export price history",
        )
        if not output_directory:
            return
        stamp = datetime.now(UTC).astimezone().strftime("%Y%m%d-%H%M%S")
        base_name = f"{safe_seller or 'seller'}-g2g-prices-{stamp}"
        directory = Path(output_directory)
        image_path = directory / f"{base_name}.png"
        excel_path = directory / f"{base_name}.xlsx"
        temporary_image = image_path.with_suffix(".png.tmp")
        temporary_excel = excel_path.with_suffix(".xlsx.tmp")
        temporary_chart = directory / f".{base_name}-chart.png"

        try:
            from PIL import ImageGrab

            self.update_idletasks()
            # Flush a pending 30fps chart frame before either export capture.
            self.chart.redraw()
            self.update_idletasks()
            x = self.analytics_frame.winfo_rootx()
            y = self.analytics_frame.winfo_rooty()
            width = self.analytics_frame.winfo_width()
            height = self.analytics_frame.winfo_height()
            if width < 2 or height < 2:
                raise RuntimeError("The analytics area is not ready for capture.")
            work_area = self._work_area()
            screenshot = ImageGrab.grab(
                bbox=clipped_capture_bbox(x, y, width, height, work_area),
                all_screens=True,
            )
            screenshot.save(temporary_image, format="PNG")
            chart_x = self.chart_panel.winfo_rootx()
            chart_y = self.chart_panel.winfo_rooty()
            chart_width = self.chart_panel.winfo_width()
            chart_height = self.chart_panel.winfo_height()
            chart_capture = ImageGrab.grab(
                bbox=clipped_capture_bbox(
                    chart_x,
                    chart_y,
                    chart_width,
                    chart_height,
                    work_area,
                ),
                all_screens=True,
            )
            chart_capture.save(temporary_chart, format="PNG")
            export_price_history_xlsx(
                temporary_excel,
                rows,
                chart_image_path=temporary_chart,
            )
            temporary_image.replace(image_path)
            temporary_excel.replace(excel_path)
            temporary_chart.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - report export failures in the UI.
            for partial in (
                temporary_image,
                temporary_excel,
                temporary_chart,
                image_path,
                excel_path,
            ):
                try:
                    partial.unlink()
                except FileNotFoundError:
                    pass
            messagebox.showerror(
                "Export failed",
                f"The PNG and Excel files could not be created together.\n\n{exc}",
                parent=self,
            )
            return
        messagebox.showinfo(
            "Export complete",
            f"Two files were created:\n\n{image_path}\n{excel_path}",
            parent=self,
        )

    def _reset_history(self) -> None:
        if self.collect_lock.locked():
            messagebox.showwarning(
                "Price check in progress",
                "Wait for the active price check to finish before resetting history.",
                parent=self,
            )
            return
        target_key = self._selected_target_key()
        seller = self.seller_var.get().strip()
        if not target_key:
            messagebox.showwarning(
                "Invalid target",
                "Enter a seller name and G2G source URL first.",
                parent=self,
            )
            return
        count = self.repository.count(target_key)
        if count == 0:
            messagebox.showinfo(
                "Nothing to reset",
                "There are no saved price checks for this target.",
                parent=self,
            )
            return
        confirmed = messagebox.askyesno(
            "Reset price history?",
            (
                f"Delete all {count} saved price checks for '{seller}' and this G2G source?\n\n"
                "This action cannot be undone."
            ),
            icon="warning",
            parent=self,
        )
        if not confirmed:
            return
        deleted = self.repository.delete_target(target_key)
        self._refresh_history(target_key)
        self._set_status(f"Deleted {deleted} price checks for {seller}")

    def _open_log_folder(self) -> None:
        try:
            if sys.platform == "win32":
                os.startfile(self.paths.log.parent)  # type: ignore[attr-defined]
            else:
                import subprocess

                subprocess.Popen(["xdg-open", str(self.paths.log.parent)])
        except OSError as exc:
            messagebox.showerror("Could not open logs", str(exc), parent=self)

    def _restore_from_tray(self) -> None:
        self._set_windows_client_redraw(True)
        self.deiconify()
        self.state("normal")
        self.lift()
        self.focus_force()

    def _hide_to_tray(self) -> None:
        try:
            self.tray.start()
        except Exception as exc:  # noqa: BLE001 - tray backends vary by Windows setup.
            messagebox.showerror("System tray unavailable", str(exc), parent=self)
            return
        self.withdraw()
        self.tray.notify("The app is still running in the background.")

    def _show_last_price(self) -> None:
        observation = self.last_observation
        if observation is None:
            self.tray.notify("No successful price check is available yet.")
            return
        message = (
            f"{observation.seller}: {_price_text(observation.unit_price, observation.currency)}\n"
            "Market Lowest: "
            f"{_price_text(observation.market_lowest_price, observation.currency) if observation.market_lowest_price is not None else '—'}"
        )
        self.tray.notify(message)

    def _exit_app(self) -> None:
        self._set_windows_client_redraw(True)
        self.stop_event.set()
        self.sound_alarm.stop()
        self.tray.stop()
        self.destroy()

    def _on_close(self) -> None:
        if self.settings.minimize_to_tray_on_close:
            self._hide_to_tray()
        else:
            self._exit_app()


def _enable_windows_dpi_awareness() -> None:
    """Prevent Windows from bitmap-scaling Tk and making text look blurry."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except (AttributeError, OSError):
                pass


def _set_windows_app_user_model_id() -> None:
    """Give taskbar grouping and notifications the same stable app identity."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(WINDOWS_APP_USER_MODEL_ID)
    except (AttributeError, OSError):
        pass


def main() -> None:
    _enable_windows_dpi_awareness()
    _set_windows_app_user_model_id()
    instance_guard = SingleInstanceGuard()
    if not instance_guard.acquire():
        _show_already_running_message()
        return
    try:
        app = TrackerApp()
        app.mainloop()
    finally:
        instance_guard.release()


if __name__ == "__main__":
    main()

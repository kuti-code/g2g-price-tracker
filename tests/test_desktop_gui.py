"""Real Tk regressions; run on Windows or under an X display, never mock widgets."""

import math
import os
import sys
import tempfile
import time
import tkinter as tk
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from g2g_price_tracker.app_paths import AppPaths
from g2g_price_tracker.desktop import (
    DARK_THEME,
    LIGHT_THEME,
    PriceChart,
    TrackerApp,
)


def sample_rows(count: int = 5000) -> list[dict]:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    return [
        {
            "id": index + 1,
            "observed_at": (start + timedelta(minutes=index)).isoformat(),
            "unit_price": str(0.01 + math.sin(index / 13) * 0.001),
            "market_lowest_price": "0.0085",
            "market_average_price": "0.009",
            "market_lowest_seller": "Synthetic seller",
            "currency": "USD",
            "listing_title": "Synthetic category > Price",
        }
        for index in range(count)
    ]


@unittest.skipUnless(sys.platform == "win32" or os.environ.get("DISPLAY"), "No desktop display")
class RealTkResizeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="g2g-gui-test-")
        root = Path(self.directory.name)
        paths = AppPaths(
            root,
            root / "data/prices.db",
            root / "settings.json",
            root / "secret.dat",
            root / "logs/tracker.log",
        )
        with patch.object(AppPaths, "default", return_value=paths):
            self.app = TrackerApp()
        self.settle()

    def tearDown(self):
        self.app.destroy()
        for handler in self.app.logger.handlers[:]:
            handler.close()
            self.app.logger.removeHandler(handler)
        self.directory.cleanup()

    def settle(self):
        # Deferred top-level layout (160 ms) plus the chart's next 33 ms frame.
        deadline = time.monotonic() + 0.28
        while time.monotonic() < deadline:
            self.app.update()
            time.sleep(0.001)

    def test_scene_item_count_stays_bounded_across_data_resize_and_theme(self):
        chart = self.app.chart
        for count in (1, 100, 5000):
            chart.set_data(sample_rows(count))
            for width, height in ((1600, 965), (1000, 640), (1200, 760)):
                self.app.geometry(f"{width}x{height}")
                self.settle()
                self.assertEqual(len(chart.find_all()), 34)
                self.assertEqual(
                    chart._last_rendered_size, (chart.winfo_width(), chart.winfo_height())
                )
                self.assertTrue(all(chart.type(item) != "image" for item in chart.find_all()))
                self.assertEqual(chart.itemcget(chart._series_items[0], "state"), "normal")
        for palette in (DARK_THEME, LIGHT_THEME):
            chart.set_palette(palette)
            self.settle()
            self.assertEqual(len(chart.find_all()), 34)

    def test_native_proxy_does_not_resize_panes_until_mouse_release(self):
        split = self.app.analytics_splitter
        self.assertFalse(split.tk.getboolean(split.cget("opaqueresize")))
        self.assertEqual(int(split.panecget(split.panes()[0], "minsize")), 420)
        self.assertEqual(int(split.panecget(split.panes()[1], "minsize")), 480)
        x, y = split.sash_coord(0)
        original = self.app.chart_panel.winfo_width()
        split.event_generate("<ButtonPress-1>", x=x + 2, y=y + 30)
        split.event_generate("<B1-Motion>", x=x - 80, y=y + 30, state=256)
        self.app.update()
        self.assertEqual(self.app.chart_panel.winfo_width(), original)
        split.event_generate("<ButtonRelease-1>", x=x - 80, y=y + 30)
        self.settle()
        self.assertLess(self.app.chart_panel.winfo_width(), original)

    def test_return_to_tab_uses_actual_chart_size(self):
        notebook = self.app.winfo_children()[0]
        self.app.chart.set_data(sample_rows(100))
        self.settle()
        notebook.select(1)
        self.app.geometry("1100x700")
        self.settle()
        notebook.select(0)
        self.settle()
        chart = self.app.chart
        self.assertTrue(chart.winfo_ismapped())
        self.assertEqual(chart._last_rendered_size, (chart.winfo_width(), chart.winfo_height()))

    def test_incremental_table_preserves_selected_row_and_filter_does_not_rebuild(self):
        rows = list(reversed(sample_rows(100)))
        self.app._update_recent_table(rows)
        self.app.table.selection_set("price-50")
        self.app._update_recent_table(list(reversed(sample_rows(101))))
        self.assertEqual(self.app.table.selection(), ("price-50",))
        children = self.app.table.get_children()
        self.app.chart_range_var.set("All Time")
        self.app._on_chart_range_selected()
        self.assertEqual(self.app.table.get_children(), children)

    def test_destroy_chart_cancels_pending_frame(self):
        root = tk.Toplevel(self.app)
        chart = PriceChart(root, DARK_THEME)
        chart.pack(fill="both", expand=True)
        self.app.update()
        chart._request_frame()
        callback = chart._frame_id
        chart.destroy()
        if callback:
            self.assertNotIn(callback, self.app.tk.splitlist(self.app.tk.call("after", "info")))
        root.destroy()

    def test_outer_resize_defers_the_complete_widget_tree_layout(self):
        notebook = self.app.notebook
        committed = (notebook.winfo_width(), notebook.winfo_height())
        target = (max(1000, self.app.winfo_width() - 120), max(640, self.app.winfo_height() - 80))

        self.app.geometry(f"{target[0]}x{target[1]}")
        self.app.update()

        self.assertEqual((notebook.winfo_width(), notebook.winfo_height()), committed)
        self.settle()
        self.assertEqual((notebook.winfo_width(), notebook.winfo_height()), target)

    def test_root_configure_handler_is_not_inherited_by_children(self):
        self.assertIn(self.app._window_configure_tag, self.app.bindtags())
        self.assertNotIn(self.app._window_configure_tag, self.app.chart.bindtags())


if __name__ == "__main__":
    unittest.main()

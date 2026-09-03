import queue
import threading
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock, call, patch

from g2g_price_tracker import desktop
from g2g_price_tracker.desktop import (
    CHART_PANE_MIN_WIDTH,
    DARK_THEME,
    TABLE_PANE_MIN_WIDTH,
    ChartScene,
    PriceChart,
    ScrollableFrame,
    SingleInstanceGuard,
    TrackerApp,
    _layout_without_focus,
    _windows_colorref,
    analytics_sash_limits,
    chart_layout,
    clipped_capture_bbox,
    initial_window_bounds,
    prepare_chart_scene,
    project_chart_series,
    render_price_chart_image,
)
from g2g_price_tracker.errors import PriceNotFoundError
from g2g_price_tracker.settings import AppSettings


class TrackingFailureTests(unittest.TestCase):
    @patch("g2g_price_tracker.desktop.collect_price")
    def test_missing_seller_stops_scheduler_and_emits_fatal_event(self, collect_price) -> None:
        collect_price.side_effect = PriceNotFoundError("Seller was not found")
        app = SimpleNamespace(
            collect_lock=threading.Lock(),
            events=queue.Queue(),
            stop_event=threading.Event(),
            repository=Mock(),
            _tracker_config=TrackerApp._tracker_config,
        )
        settings = AppSettings(
            seller="MissingSeller",
            interval_minutes=10,
            source_url="https://www.g2g.com/categories/example/offer/group",
        )

        TrackerApp._single_collection(app, settings)

        self.assertTrue(app.stop_event.is_set())
        self.assertEqual(app.events.get_nowait(), ("collecting", "MissingSeller"))
        self.assertEqual(
            app.events.get_nowait(),
            ("seller_not_found", "Seller was not found"),
        )
        app.repository.add.assert_not_called()


class ChartResizeTests(unittest.TestCase):
    def test_ten_thousand_resize_events_schedule_only_one_frame(self) -> None:
        chart = SimpleNamespace(
            _frame_id=None,
            winfo_ismapped=Mock(return_value=True),
            after=Mock(return_value="frame"),
            _flush_frame=Mock(),
        )
        for _ in range(10_000):
            PriceChart._request_frame(chart)
        chart.after.assert_called_once_with(33, chart._flush_frame)

    def test_unmapped_chart_waits_for_map_event(self) -> None:
        chart = SimpleNamespace(
            _frame_id=None,
            winfo_ismapped=Mock(return_value=False),
            after=Mock(),
        )
        PriceChart._request_frame(chart)
        chart.after.assert_not_called()

    def test_all_time_resize_only_updates_retained_coordinates(self) -> None:
        now = datetime(2026, 8, 25, 12, tzinfo=UTC)
        points = [
            (now + timedelta(minutes=index), 0.01 + index / 1_000_000, 0.009, 0.0095)
            for index in range(300)
        ]
        chart = SimpleNamespace(
            _scene=prepare_chart_scene(points, "USD"),
            _last_rendered_size=None,
            _render_dirty=True,
            _legend_widths=(130, 130, 200),
            _empty_item=1,
            _grid_items=list(range(2, 7)),
            _y_items=list(range(7, 12)),
            _legend_lines=[12, 13, 14],
            _legend_text=[15, 16, 17],
            _series_items=[18, 19, 20],
            _time_items=list(range(21, 29)),
            _marker_items=[[29, 30], [31, 32], [33, 34]],
            render_count=0,
            winfo_width=Mock(return_value=900),
            winfo_height=Mock(return_value=420),
            coords=Mock(),
            itemconfigure=Mock(),
            delete=Mock(),
            create_image=Mock(),
        )
        with patch("g2g_price_tracker.desktop.prepare_chart_scene") as prepare:
            PriceChart.redraw(chart)
            prepare.assert_not_called()
        chart.create_image.assert_not_called()
        chart.delete.assert_not_called()
        self.assertEqual(chart.coords.call_count, 34)
        self.assertEqual(chart.render_count, 1)
        self.assertEqual(chart._last_rendered_size, (900, 420))
        self.assertFalse(chart._render_dirty)
        # An identical size/data frame performs no additional coordinate updates.
        PriceChart.redraw(chart)
        self.assertEqual(chart.coords.call_count, 34)


class TopLevelLayoutTests(unittest.TestCase):
    def test_resize_events_only_replace_one_deferred_commit(self) -> None:
        app = SimpleNamespace(
            _pending_window_size=(1000, 640),
            _window_layout_id="old-layout",
            after_cancel=Mock(),
            after=Mock(return_value="new-layout"),
            _commit_window_layout=Mock(),
            _set_windows_client_redraw=Mock(),
        )
        event = SimpleNamespace(width=1200, height=760)

        TrackerApp._on_window_configure(app, event)

        app.after_cancel.assert_called_once_with("old-layout")
        app.after.assert_called_once_with(160, app._commit_window_layout)
        app._set_windows_client_redraw.assert_called_once_with(False)
        self.assertEqual(app._pending_window_size, (1200, 760))
        self.assertEqual(app._window_layout_id, "new-layout")

    def test_position_only_configure_does_not_schedule_layout(self) -> None:
        app = SimpleNamespace(
            _pending_window_size=(1200, 760),
            _window_layout_id=None,
            after_cancel=Mock(),
            after=Mock(),
        )

        TrackerApp._on_window_configure(app, SimpleNamespace(width=1200, height=760))

        app.after.assert_not_called()
        app.after_cancel.assert_not_called()

    def test_commit_changes_only_the_top_level_notebook_geometry(self) -> None:
        app = SimpleNamespace(
            _window_layout_id="pending",
            _pending_window_size=(1200, 760),
            _committed_window_size=(1000, 640),
            notebook=Mock(),
            update_idletasks=Mock(),
            _set_windows_client_redraw=Mock(),
        )

        TrackerApp._commit_window_layout(app)

        app.notebook.place_configure.assert_called_once_with(
            x=0,
            y=0,
            width=1200,
            height=760,
            relwidth=0,
            relheight=0,
        )
        self.assertEqual(app._committed_window_size, (1200, 760))
        self.assertIsNone(app._window_layout_id)
        app.update_idletasks.assert_called_once_with()
        app._set_windows_client_redraw.assert_called_once_with(True)

    def test_commit_restores_redraw_when_layout_fails(self) -> None:
        app = SimpleNamespace(
            _window_layout_id="pending",
            _pending_window_size=(1200, 760),
            _committed_window_size=(1000, 640),
            notebook=Mock(),
            update_idletasks=Mock(side_effect=RuntimeError("layout failed")),
            _set_windows_client_redraw=Mock(),
        )

        with self.assertRaisesRegex(RuntimeError, "layout failed"):
            TrackerApp._commit_window_layout(app)

        app._set_windows_client_redraw.assert_called_once_with(True)

    def test_raster_chart_keeps_requested_canvas_size(self) -> None:
        image = render_price_chart_image([], "USD", DARK_THEME, 900, 420)

        self.assertEqual(image.size, (900, 420))


class WindowsClientRedrawTests(unittest.TestCase):
    def test_suspend_and_resume_use_only_the_tk_client_handle(self) -> None:
        user32 = SimpleNamespace(SendMessageW=Mock(), RedrawWindow=Mock(return_value=1))
        app = SimpleNamespace(
            _window_redraw_suspended=False,
            winfo_id=Mock(return_value=1234),
        )

        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(
                desktop.ctypes,
                "windll",
                SimpleNamespace(user32=user32),
                create=True,
            ),
        ):
            TrackerApp._set_windows_client_redraw(app, False)
            TrackerApp._set_windows_client_redraw(app, True)

        self.assertEqual(
            user32.SendMessageW.call_args_list,
            [
                call(1234, desktop.WM_SETREDRAW, 0, 0),
                call(1234, desktop.WM_SETREDRAW, 1, 0),
            ],
        )
        user32.RedrawWindow.assert_called_once_with(
            1234,
            None,
            None,
            desktop.RDW_INVALIDATE
            | desktop.RDW_ERASE
            | desktop.RDW_FRAME
            | desktop.RDW_ALLCHILDREN,
        )
        self.assertFalse(app._window_redraw_suspended)

    def test_repeated_suspend_is_a_noop(self) -> None:
        app = SimpleNamespace(_window_redraw_suspended=True)

        with patch.object(desktop.sys, "platform", "win32"):
            TrackerApp._set_windows_client_redraw(app, False)

        self.assertTrue(app._window_redraw_suspended)


class ChartSceneTests(unittest.TestCase):
    def test_empty_and_single_point_scenes(self) -> None:
        self.assertEqual(prepare_chart_scene([], "USD"), ChartScene())
        scene = prepare_chart_scene([(datetime.now(UTC), 0.01, None, None)], "USD")
        self.assertEqual(len(scene.y_labels), 5)
        self.assertEqual(len(project_chart_series(scene.series[0], (78, 30, 500, 200))), 4)
        self.assertEqual(scene.series[1:], ((), ()))

    def test_narrow_short_layout_uses_actual_viewport(self) -> None:
        bounds, positions = chart_layout(394, 150, (140, 130, 210))
        left, top, width, height = bounds
        self.assertLessEqual(left + width, 394)
        self.assertLessEqual(top + height, 150)
        self.assertGreater(positions[2][1], positions[0][1])


class HistoryViewTests(unittest.TestCase):
    def test_chart_filter_does_not_rebuild_table(self) -> None:
        app = SimpleNamespace(
            chart=Mock(),
            chart_range_combo=Mock(),
            after_idle=Mock(),
            table=Mock(),
            _chart_rows_for_current_range=Mock(return_value=[]),
        )
        TrackerApp._on_chart_range_selected(app)
        app.chart.set_data.assert_called_once_with([])
        app._chart_rows_for_current_range.assert_called_once_with()
        self.assertEqual(app.table.mock_calls, [])

    def test_unchanged_table_does_not_issue_tcl_commands(self) -> None:
        app = SimpleNamespace(_table_values={}, table=Mock())
        TrackerApp._update_recent_table(app, [])
        self.assertEqual(app.table.mock_calls, [])


class ScrollableFrameResizeTests(unittest.TestCase):
    def test_scroll_region_updates_are_debounced(self) -> None:
        frame = SimpleNamespace(
            _scroll_region_update_id="pending-update",
            winfo_ismapped=Mock(return_value=True),
            after_cancel=Mock(),
            after=Mock(return_value="next-update"),
            _update_scroll_region=Mock(),
        )

        ScrollableFrame._schedule_scroll_region_update(frame)

        frame.after_cancel.assert_called_once_with("pending-update")
        frame.after.assert_called_once_with(120, frame._update_scroll_region)
        self.assertEqual(frame._scroll_region_update_id, "next-update")


class ClosingBehaviorTests(unittest.TestCase):
    def test_close_minimizes_without_prompt_when_enabled(self) -> None:
        app = SimpleNamespace(
            settings=AppSettings(minimize_to_tray_on_close=True),
            _hide_to_tray=Mock(),
            _exit_app=Mock(),
        )

        TrackerApp._on_close(app)

        app._hide_to_tray.assert_called_once_with()
        app._exit_app.assert_not_called()

    def test_close_exits_without_prompt_when_disabled(self) -> None:
        app = SimpleNamespace(
            settings=AppSettings(minimize_to_tray_on_close=False),
            _hide_to_tray=Mock(),
            _exit_app=Mock(),
        )

        TrackerApp._on_close(app)

        app._exit_app.assert_called_once_with()
        app._hide_to_tray.assert_not_called()


class InitialWindowPlacementTests(unittest.TestCase):
    def test_user_selected_window_height_is_kept_inside_the_work_area(self) -> None:
        x, y, width, height = initial_window_bounds(0, 0, 1920, 1032)

        self.assertEqual((width, height), (1600, 965))
        self.assertEqual(x, 160)
        self.assertEqual(y, 10)
        self.assertEqual(1032 - (y + height), 57)

    def test_window_shrinks_when_the_work_area_is_too_short(self) -> None:
        x, y, width, height = initial_window_bounds(0, 0, 1366, 728)

        self.assertEqual((x, y), (6, 10))
        self.assertEqual((width, height), (1354, 708))


class CaptureBoundsTests(unittest.TestCase):
    def test_capture_is_clipped_before_the_taskbar(self) -> None:
        bbox = clipped_capture_bbox(182, 426, 1556, 606, (0, 0, 1920, 1022))

        self.assertEqual(bbox, (182, 426, 1738, 1022))

    def test_capture_inside_work_area_keeps_its_exact_dimensions(self) -> None:
        bbox = clipped_capture_bbox(100, 200, 800, 500, (0, 0, 1920, 1040))

        self.assertEqual(bbox, (100, 200, 900, 700))


class ThemeStyleHelpersTests(unittest.TestCase):
    def test_focus_elements_are_removed_without_losing_their_children(self) -> None:
        layout = [
            (
                "Button.border",
                {
                    "children": [
                        (
                            "Button.focus",
                            {"children": [("Button.label", {"sticky": "nswe"})]},
                        )
                    ]
                },
            )
        ]

        cleaned = _layout_without_focus(layout)

        self.assertEqual(
            cleaned,
            [
                (
                    "Button.border",
                    {"children": [("Button.label", {"sticky": "nswe"})]},
                )
            ],
        )

    def test_windows_colorref_uses_bgr_byte_order(self) -> None:
        self.assertEqual(_windows_colorref("#123456"), 0x563412)


class SingleInstanceTests(unittest.TestCase):
    def test_existing_windows_instance_is_rejected(self) -> None:
        kernel32 = Mock()
        kernel32.CreateMutexW.return_value = 123
        kernel32.GetLastError.return_value = SingleInstanceGuard.ERROR_ALREADY_EXISTS
        guard = SingleInstanceGuard()

        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(
                desktop.ctypes,
                "windll",
                SimpleNamespace(kernel32=kernel32),
                create=True,
            ),
        ):
            acquired = guard.acquire()

        self.assertFalse(acquired)
        kernel32.CloseHandle.assert_called_once_with(123)
        self.assertIsNone(guard._handle)

    def test_owned_windows_mutex_is_released(self) -> None:
        kernel32 = Mock()
        kernel32.CreateMutexW.return_value = 456
        kernel32.GetLastError.return_value = 0
        guard = SingleInstanceGuard()

        with (
            patch.object(desktop.sys, "platform", "win32"),
            patch.object(
                desktop.ctypes,
                "windll",
                SimpleNamespace(kernel32=kernel32),
                create=True,
            ),
        ):
            self.assertTrue(guard.acquire())
            guard.release()

        kernel32.ReleaseMutex.assert_called_once_with(456)
        kernel32.CloseHandle.assert_called_once_with(456)
        self.assertIsNone(guard._handle)


class AnalyticsSplitterTests(unittest.TestCase):
    def test_sash_limits_preserve_both_pane_minimums(self) -> None:
        minimum, maximum = analytics_sash_limits(1000)

        self.assertEqual(minimum, CHART_PANE_MIN_WIDTH)
        self.assertEqual(maximum, 1000 - TABLE_PANE_MIN_WIDTH - 7)

    def test_initial_split_preserves_both_pane_minimums(self) -> None:
        splitter = Mock()
        splitter.winfo_width.return_value = 1000
        app = SimpleNamespace(analytics_splitter=splitter, after=Mock())

        TrackerApp._set_initial_analytics_split(app)

        expected_position = 1000 - TABLE_PANE_MIN_WIDTH - 7
        self.assertGreaterEqual(expected_position, CHART_PANE_MIN_WIDTH)
        splitter.sash_place.assert_called_once_with(0, expected_position, 0)

    def test_combobox_text_selection_is_cleared(self) -> None:
        combo = Mock()
        app = SimpleNamespace(focus_set=Mock())

        TrackerApp._clear_combobox_selection(app, combo)

        combo.selection_clear.assert_called_once_with()
        app.focus_set.assert_called_once_with()


class AppearanceTests(unittest.TestCase):
    def test_dark_theme_is_applied_without_changing_other_settings(self) -> None:
        scrollable_frame = Mock()
        app = SimpleNamespace(
            settings=AppSettings(seller="ExampleSeller", theme="light"),
            palette=None,
            theme_var=Mock(),
            configure=Mock(),
            _configure_styles=Mock(),
            _colored_button_roles={},
            _scrollable_frames=[scrollable_frame],
            _set_windows_title_bar_theme=Mock(),
            after_idle=Mock(),
        )

        TrackerApp._apply_theme(app, "Dark")

        self.assertEqual(app.settings.seller, "ExampleSeller")
        self.assertEqual(app.settings.theme, "dark")
        self.assertEqual(app.palette, DARK_THEME)
        app.configure.assert_called_once_with(background=DARK_THEME.background)
        app._configure_styles.assert_called_once_with()
        scrollable_frame.set_palette.assert_called_once_with(DARK_THEME)
        app._set_windows_title_bar_theme.assert_called_once_with(True)

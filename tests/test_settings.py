import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from g2g_price_tracker.settings import AppSettings


class SettingsTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            settings = AppSettings(
                seller="ExampleSeller",
                interval_minutes=10,
                source_url="https://www.g2g.com/categories/poe-currency/offer/group",
                price_alert_enabled=True,
                price_alert_threshold="0.025",
                sound_alert_enabled=True,
                sound_duration_seconds=15,
                sound_until_dismissed=False,
                telegram_enabled=True,
                telegram_chat_id="123456",
                alert_once_until_recovery=True,
                start_with_windows=True,
                minimize_to_tray_on_close=False,
                theme="dark",
            )
            settings.save(path)
            self.assertEqual(AppSettings.load(path), settings)

    def test_invalid_file_uses_defaults(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text("not-json", encoding="utf-8")
            loaded = AppSettings.load(path)
            self.assertTrue(loaded.seller)
            self.assertGreaterEqual(loaded.interval_minutes, 1)

    def test_interval_is_clamped_to_one_minute(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "seller": "ExampleSeller",
                        "interval_minutes": 0,
                        "source_url": "https://www.g2g.com/example",
                    }
                ),
                encoding="utf-8",
            )
            self.assertEqual(AppSettings.load(path).interval_minutes, 1)

    def test_old_settings_default_to_minimize_on_close(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "seller": "ExampleSeller",
                        "interval_minutes": 10,
                        "source_url": "https://www.g2g.com/example",
                    }
                ),
                encoding="utf-8",
            )

            self.assertTrue(AppSettings.load(path).minimize_to_tray_on_close)
            self.assertEqual(AppSettings.load(path).theme, "light")

    def test_unknown_theme_falls_back_to_light(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.json"
            path.write_text(json.dumps({"theme": "neon"}), encoding="utf-8")

            self.assertEqual(AppSettings.load(path).theme, "light")

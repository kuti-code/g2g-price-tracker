import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from g2g_price_tracker.app_paths import AppPaths


class AppPathsTests(unittest.TestCase):
    def test_windows_data_directory_uses_project_publisher_name(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            local_app_data = Path(temporary_directory)
            with (
                patch("g2g_price_tracker.app_paths.sys.platform", "win32"),
                patch.dict(os.environ, {"LOCALAPPDATA": str(local_app_data)}),
            ):
                paths = AppPaths.default()

        self.assertEqual(paths.root, local_app_data / "kuti-code" / "G2GPriceTracker")
        self.assertEqual(paths.database, paths.root / "data" / "prices.db")


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path

from PIL import Image

from g2g_price_tracker.tray import TrayController
from tools.build_exe import build_command


class BuildConfigurationTests(unittest.TestCase):
    def test_build_uses_icon_and_bundles_runtime_assets(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        command = build_command(project_root)

        self.assertIn("--icon", command)
        self.assertIn(str(project_root / "assets" / "g2g-price-tracker.ico"), command)
        self.assertEqual(command[command.index("--distpath") + 1], str(project_root))
        self.assertEqual(command.count("--add-data"), 2)
        self.assertEqual(command[-1], str(project_root / "launcher.py"))

    def test_windows_installer_builds_root_executable_and_desktop_shortcut(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        script = (project_root / "setup.cmd").read_text(encoding="utf-8")

        self.assertIn('move /y "dist\\G2GPriceTracker.exe" "G2GPriceTracker.exe"', script)
        self.assertIn("G2G Price Tracker.lnk", script)
        self.assertIn('start "" "%G2G_EXE_PATH%"', script)
        self.assertNotIn("ExecutionPolicy Bypass", script)

    def test_release_publishes_checksum_with_scoped_write_permission(self) -> None:
        project_root = Path(__file__).resolve().parents[1]
        workflow = (project_root / ".github" / "workflows" / "release.yml").read_text(
            encoding="utf-8"
        )

        self.assertIn("G2GPriceTracker.exe.sha256", workflow)
        self.assertIn("publish-release:", workflow)
        self.assertIn("contents: read", workflow)
        self.assertIn("contents: write", workflow)
        self.assertIn("RELEASE_TAG: ${{ github.ref_name }}", workflow)

    def test_project_exposes_only_one_windows_command_script(self) -> None:
        project_root = Path(__file__).resolve().parents[1]

        self.assertEqual(
            [path.name for path in project_root.glob("*.cmd")],
            ["setup.cmd"],
        )

    def test_icon_assets_include_window_and_executable_formats(self) -> None:
        asset_dir = Path(__file__).resolve().parents[1] / "assets"
        with Image.open(asset_dir / "g2g-price-tracker.png") as png:
            self.assertEqual(png.size, (1024, 1024))
            self.assertEqual(png.mode, "RGBA")
        with Image.open(asset_dir / "g2g-price-tracker.ico") as icon:
            self.assertIn((256, 256), icon.info["sizes"])
            self.assertIn((16, 16), icon.info["sizes"])

    def test_system_tray_uses_the_project_icon(self) -> None:
        icon_path = Path(__file__).resolve().parents[1] / "assets" / "g2g-price-tracker.ico"
        controller = TrayController(
            open_app=lambda: None,
            check_now=lambda: None,
            pause_tracking=lambda: None,
            show_last_price=lambda: None,
            exit_app=lambda: None,
            icon_path=icon_path,
        )

        tray_image = controller._image()

        self.assertEqual(tray_image.size, (64, 64))
        self.assertEqual(tray_image.mode, "RGBA")

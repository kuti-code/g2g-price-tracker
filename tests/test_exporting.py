import unittest
import zipfile
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image
from test_database import _observation

from g2g_price_tracker.database import PriceRepository
from g2g_price_tracker.exporting import export_price_history_xlsx, fit_image_within


class ImageSizingTests(unittest.TestCase):
    def test_fit_preserves_wide_image_aspect_ratio(self) -> None:
        self.assertEqual(fit_image_within(1556, 606, 680, 300), (680, 265))

    def test_fit_does_not_enlarge_small_images(self) -> None:
        self.assertEqual(fit_image_within(320, 180, 680, 300), (320, 180))


class ExportTests(unittest.TestCase):
    def test_creates_valid_xlsx_with_summary_and_price_checks(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = PriceRepository(root / "prices.db")
            url = "https://www.g2g.com/categories/example/offer/group"
            repository.add(_observation("ExampleSeller", url, "0.032"))
            repository.add(_observation("ExampleSeller", url, "0.028"))

            chart = root / "chart.png"
            Image.new("RGB", (640, 320), "white").save(chart)
            output = export_price_history_xlsx(
                root / "prices.xlsx", repository.rows(), chart_image_path=chart
            )

            self.assertTrue(output.is_file())
            with zipfile.ZipFile(output) as archive:
                self.assertIsNone(archive.testzip())
                workbook_xml = archive.read("xl/workbook.xml").decode("utf-8")
                summary_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
                self.assertIn("Summary", workbook_xml)
                self.assertIn("Price Checks", workbook_xml)
                self.assertIn("AVERAGE", summary_xml)
                self.assertIn("Market Lowest Price", shared_strings)
                self.assertIn("Market Average (Lowest 5)", shared_strings)
                self.assertIn("by: kuti-code", shared_strings)
                self.assertIn("xl/media/image1.png", archive.namelist())
                self.assertIn("xl/media/image2.png", archive.namelist())
                drawing_xml = archive.read("xl/drawings/drawing1.xml").decode("utf-8")
                self.assertIn('cx="5715000"', drawing_xml)
                self.assertIn('cy="2857500"', drawing_xml)
                self.assertIn('cx="304800"', drawing_xml)
                self.assertIn('cy="304800"', drawing_xml)
                styles_xml = archive.read("xl/styles.xml").decode("utf-8")
                self.assertIn('indent="5"', styles_xml)

    def test_external_text_is_never_exported_as_an_excel_formula(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = PriceRepository(root / "prices.db")
            observation = _observation(
                '=HYPERLINK("https://example.invalid","click")',
                "https://www.g2g.com/categories/example/offer/group",
                "0.028",
            )
            repository.add(observation)

            output = export_price_history_xlsx(root / "prices.xlsx", repository.rows())

            with zipfile.ZipFile(output) as archive:
                summary_xml = archive.read("xl/worksheets/sheet1.xml").decode("utf-8")
                checks_xml = archive.read("xl/worksheets/sheet2.xml").decode("utf-8")
                shared_strings = archive.read("xl/sharedStrings.xml").decode("utf-8")
                self.assertNotIn("HYPERLINK", summary_xml)
                self.assertNotIn("HYPERLINK", checks_xml)
                self.assertIn("HYPERLINK", shared_strings)

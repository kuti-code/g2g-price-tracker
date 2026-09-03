import sqlite3
import unittest
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from g2g_price_tracker.database import PriceRepository
from g2g_price_tracker.models import PriceObservation, build_target_key


def _observation(seller: str, source_url: str, price: str) -> PriceObservation:
    return PriceObservation(
        target_key=build_target_key(seller, source_url),
        seller=seller,
        listing_title="Example listing",
        category="example-category",
        unit_price=Decimal(price),
        currency="USD",
        source_url=source_url,
        observed_at=datetime(2026, 8, 18, tzinfo=UTC),
        market_lowest_price=Decimal("0.024"),
        market_lowest_seller="LowestSeller",
        market_average_price=Decimal("0.026"),
    )


class DatabaseTests(unittest.TestCase):
    def test_round_trip(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = PriceRepository(root / "prices.db")
            observation = _observation(
                "ExampleSeller",
                "https://www.g2g.com/categories/example/offer/group?fa=one",
                "0.032",
            )
            repository.add(observation)

            rows = repository.rows()

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["unit_price"], "0.032")
            self.assertEqual(rows[0]["market_lowest_price"], "0.024")
            self.assertEqual(rows[0]["market_lowest_seller"], "LowestSeller")
            self.assertEqual(rows[0]["market_average_price"], "0.026")

    def test_migrates_an_existing_database_without_deleting_history(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "prices.db"
            with sqlite3.connect(path) as connection:
                connection.execute(
                    """
                    CREATE TABLE price_observations (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        target_key TEXT NOT NULL,
                        seller TEXT NOT NULL,
                        listing_title TEXT NOT NULL,
                        category TEXT NOT NULL,
                        unit_price TEXT NOT NULL,
                        currency TEXT NOT NULL,
                        source_url TEXT NOT NULL,
                        observed_at TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    INSERT INTO price_observations
                    (target_key, seller, listing_title, category, unit_price, currency,
                     source_url, observed_at)
                    VALUES ('old', 'Seller', 'Listing', 'category', '0.030', 'USD',
                            'https://www.g2g.com/categories/example/offer/group',
                            '2026-08-18T00:00:00+00:00')
                    """
                )

            repository = PriceRepository(path)
            repository.initialize()
            rows = repository.rows()

            self.assertEqual(len(rows), 1)
            self.assertIsNone(rows[0]["market_lowest_price"])
            self.assertIsNone(rows[0]["market_average_price"])

    def test_isolates_history_by_seller_and_source_url(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            repository = PriceRepository(root / "prices.db")
            url_a = "https://www.g2g.com/categories/game-a/offer/group"
            url_b = "https://www.g2g.com/categories/game-b/offer/group"
            observations = (
                _observation("ExampleSeller", url_a, "0.032"),
                _observation("OtherSeller", url_a, "0.029"),
                _observation("ExampleSeller", url_b, "1.500"),
            )
            for observation in observations:
                repository.add(observation)

            target_key = build_target_key("exampleseller", url_a)
            rows = repository.rows(target_key=target_key)

            self.assertEqual([row["unit_price"] for row in rows], ["0.032"])

    def test_deletes_only_the_selected_target(self) -> None:
        with TemporaryDirectory() as directory:
            repository = PriceRepository(Path(directory) / "prices.db")
            url = "https://www.g2g.com/categories/example/offer/group"
            first = _observation("ExampleSeller", url, "0.032")
            second = _observation("OtherSeller", url, "0.029")
            repository.add(first)
            repository.add(second)

            deleted = repository.delete_target(first.target_key)

            self.assertEqual(deleted, 1)
            self.assertEqual(repository.rows(target_key=first.target_key), [])
            self.assertEqual(len(repository.rows(target_key=second.target_key)), 1)

    def test_summary_and_chart_queries_do_not_load_full_history(self) -> None:
        with TemporaryDirectory() as directory:
            repository = PriceRepository(Path(directory) / "prices.db")
            url = "https://www.g2g.com/categories/example/offer/group"
            key = build_target_key("ExampleSeller", url)
            for index in range(5):
                observation = PriceObservation(
                    target_key=key,
                    seller="ExampleSeller",
                    listing_title="Example listing",
                    category="example-category",
                    unit_price=Decimal(str(0.030 + index / 1000)),
                    currency="USD",
                    source_url=url,
                    observed_at=datetime(2026, 8, 18, hour=index, tzinfo=UTC),
                    market_lowest_price=Decimal("0.024"),
                    market_lowest_seller="LowestSeller",
                    market_average_price=Decimal("0.026"),
                )
                repository.add(observation)

            summary = repository.target_summary(key)
            recent = repository.recent_rows(key, limit=2)
            chart_rows = repository.rows_for_chart(key, "Last 100 Checks")

            self.assertEqual(summary["count"], 5)
            self.assertEqual(str(summary["statistics"].latest), "0.034")
            self.assertEqual([row["unit_price"] for row in recent], ["0.034", "0.033"])
            self.assertEqual(len(chart_rows), 5)
            self.assertEqual(repository.count(key), 5)

import unittest
from decimal import Decimal

from g2g_price_tracker.models import build_target_key, calculate_price_statistics


class TargetIdentityTests(unittest.TestCase):
    def test_seller_case_and_query_order_do_not_create_duplicate_targets(self) -> None:
        first = build_target_key(
            "ExampleSeller",
            "https://www.g2g.com/categories/example/offer/group?sort=lowest&fa=one",
        )
        second = build_target_key(
            "exampleseller",
            "https://WWW.G2G.COM/categories/example/offer/group?fa=one&sort=lowest",
        )

        self.assertEqual(first, second)

    def test_different_source_url_creates_a_different_target(self) -> None:
        first = build_target_key(
            "ExampleSeller", "https://www.g2g.com/categories/game-a/offer/group"
        )
        second = build_target_key(
            "ExampleSeller", "https://www.g2g.com/categories/game-b/offer/group"
        )

        self.assertNotEqual(first, second)


class PriceStatisticsTests(unittest.TestCase):
    def test_all_time_average_uses_all_500_price_checks(self) -> None:
        prices = [Decimal(index) for index in range(1, 501)]

        statistics = calculate_price_statistics(prices)

        self.assertEqual(statistics.all_time_average, Decimal("250.5"))
        self.assertEqual(statistics.latest, Decimal(500))

    def test_latest_change_uses_the_previous_price_check(self) -> None:
        statistics = calculate_price_statistics([Decimal(10), Decimal(12)])

        self.assertEqual(statistics.latest_change_percent, Decimal(20))

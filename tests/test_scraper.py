import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from urllib.error import HTTPError

from g2g_price_tracker.config import DEFAULT_G2G_URL
from g2g_price_tracker.errors import PriceNotFoundError
from g2g_price_tracker.scraper import (
    CollectionError,
    TrackerConfig,
    _find_exact_seller_offer,
    _request_json,
    _search_parameters,
    collect_price,
)


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class G2GApiTests(unittest.TestCase):
    def test_translates_category_url_to_lightweight_api_parameters(self) -> None:
        parameters = _search_parameters(DEFAULT_G2G_URL, "ExampleSeller")

        self.assertEqual(parameters["seo_term"], "poe-currency")
        self.assertIn("lgc_19398_tier_42692", parameters["filter_attr"])
        self.assertEqual(parameters["sort"], "lowest_price")
        self.assertEqual(parameters["seller"], "ExampleSeller")
        self.assertEqual(parameters["currency"], "USD")
        self.assertEqual(parameters["country"], "TR")

    def test_rejects_non_g2g_source_url(self) -> None:
        with self.assertRaises(ValueError):
            _search_parameters("https://example.com/categories/poe-currency", "ExampleSeller")

    def test_market_request_keeps_filters_without_a_seller(self) -> None:
        parameters = _search_parameters(DEFAULT_G2G_URL, None)

        self.assertNotIn("seller", parameters)
        self.assertEqual(parameters["sort"], "lowest_price")
        self.assertEqual(parameters["page_size"], 1)

    def test_finds_exact_seller_case_insensitively(self) -> None:
        data = {
            "code": 2000,
            "messages": [],
            "payload": {
                "results": [
                    {"username": "AnotherSeller", "display_price": "0.01"},
                    {"username": "ExampleSeller", "display_price": "0.025"},
                ]
            },
        }

        offer = _find_exact_seller_offer(data, "exampleseller")

        self.assertEqual(offer["display_price"], "0.025")

    def test_missing_seller_has_clear_error(self) -> None:
        data = {"code": 2000, "messages": [], "payload": {"results": []}}

        with self.assertRaises(PriceNotFoundError):
            _find_exact_seller_offer(data, "MissingSeller")

    @patch("g2g_price_tracker.scraper.urlopen")
    def test_collects_price_without_browser(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = [
            _FakeResponse(
                {
                    "code": 2000,
                    "messages": [],
                    "payload": {
                        "results": [
                            {
                                "username": "ExampleSeller",
                                "title": "[PC] Example Game > Example Currency",
                                "seo_term": "example-currency",
                                "display_price": "0.025",
                                "display_currency": "USD",
                            }
                        ]
                    },
                }
            ),
            _FakeResponse(
                {
                    "code": 2000,
                    "messages": [],
                    "payload": {
                        "results": [
                            {
                                "username": "CheapestSeller",
                                "display_price": "0.0235",
                                "display_currency": "USD",
                            },
                            {"username": "Two", "display_price": "0.0240"},
                            {"username": "Three", "display_price": "0.0245"},
                            {"username": "Four", "display_price": "0.0250"},
                            {"username": "Five", "display_price": "0.0255"},
                        ]
                    },
                }
            ),
        ]

        observation = collect_price(TrackerConfig(DEFAULT_G2G_URL, "ExampleSeller"))

        self.assertEqual(observation.unit_price, Decimal("0.025"))
        self.assertEqual(observation.currency, "USD")
        self.assertEqual(observation.listing_title, "[PC] Example Game > Example Currency")
        self.assertEqual(observation.category, "example-currency")
        self.assertEqual(observation.market_lowest_price, Decimal("0.0235"))
        self.assertEqual(observation.market_lowest_seller, "CheapestSeller")
        self.assertEqual(observation.market_average_price, Decimal("0.0245"))
        seller_request = mocked_urlopen.call_args_list[0].args[0]
        market_request = mocked_urlopen.call_args_list[1].args[0]
        self.assertIn("/v3/offer/search?", seller_request.full_url)
        self.assertIn("seller=ExampleSeller", seller_request.full_url)
        self.assertNotIn("seller=", market_request.full_url)
        self.assertIn("sort=lowest_price", market_request.full_url)
        self.assertIn("page_size=5", market_request.full_url)

    @patch("g2g_price_tracker.scraper.urlopen")
    def test_429_has_actionable_error(self, mocked_urlopen) -> None:
        mocked_urlopen.side_effect = HTTPError(
            "https://sls.g2g.com/v3/offer/search", 429, "Too Many Requests", {}, None
        )

        with self.assertRaisesRegex(CollectionError, "Increase the tracking interval") as raised:
            _request_json("https://sls.g2g.com/v3/offer/search")
        self.assertTrue(raised.exception.retryable)
        self.assertEqual(raised.exception.retry_after, 60.0)

import json
import unittest
from decimal import Decimal
from unittest.mock import patch

from g2g_price_tracker.alerts import send_telegram_message, should_trigger_price_alert


class _Response:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self) -> bytes:
        return json.dumps({"ok": True}).encode("utf-8")


class AlertTests(unittest.TestCase):
    def test_below_threshold_triggers_only_when_armed(self) -> None:
        self.assertTrue(
            should_trigger_price_alert(
                Decimal("0.024"),
                Decimal("0.025"),
                armed=True,
                once_until_recovery=True,
            )
        )
        self.assertFalse(
            should_trigger_price_alert(
                Decimal("0.024"),
                Decimal("0.025"),
                armed=False,
                once_until_recovery=True,
            )
        )
        self.assertFalse(
            should_trigger_price_alert(
                Decimal("0.025"),
                Decimal("0.025"),
                armed=True,
                once_until_recovery=True,
            )
        )

    @patch("g2g_price_tracker.alerts.urlopen", return_value=_Response())
    def test_telegram_request_uses_official_https_endpoint(self, urlopen) -> None:
        send_telegram_message("123:abc", "987", "Test")

        request = urlopen.call_args.args[0]
        self.assertEqual(
            request.full_url,
            "https://api.telegram.org/bot123:abc/sendMessage",
        )
        self.assertIn(b"chat_id=987", request.data)

import unittest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import kraken_order_audit


class KrakenOrderAuditTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)
        self.exchange = MagicMock()
        self.exchange.fetch_open_orders.return_value = []
        self.exchange.fetch_closed_orders.return_value = []

    def test_clean_audit_is_safe_and_queries_only_read_endpoints(self):
        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertTrue(result["safe_to_initialize_empty_execution_state"])
        self.assertEqual(result["unresolved_bot_orders"], 0)
        self.assertEqual(result["same_day_closed_bot_orders"], 0)
        self.assertEqual(self.exchange.fetch_open_orders.call_count, 4)
        self.assertEqual(self.exchange.fetch_closed_orders.call_count, 4)
        self.assertFalse(
            any(
                call.startswith("create") or "order" in call.lower() and call.startswith("add")
                for call in dir(self.exchange)
            )
        )

    def test_open_bot_order_blocks_empty_state_and_only_exposes_suffix(self):
        self.exchange.fetch_open_orders.side_effect = [
            [
                {
                    "id": "sensitive-prefix-ABC123",
                    "clientOrderId": "dca-1234567890abcd",
                }
            ],
            [],
            [],
            [],
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertFalse(result["safe_to_initialize_empty_execution_state"])
        self.assertEqual(result["unresolved_bot_orders"], 1)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["unresolved_order_id_suffixes"],
            ["ABC123"],
        )
        self.assertNotIn("sensitive-prefix", str(result))

    def test_same_bangkok_day_closed_bot_order_blocks_empty_state(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        previous_day_ms = int(
            datetime(2026, 8, 4, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        self.exchange.fetch_closed_orders.side_effect = [
            [
                {
                    "id": "closed-today-654321",
                    "clientOrderId": "dca-1234567890abcd",
                    "timestamp": same_day_ms,
                },
                {
                    "id": "closed-old-111111",
                    "clientOrderId": "dca-aaaaaaaaaaaaaa",
                    "timestamp": previous_day_ms,
                },
            ],
            [],
            [],
            [],
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 1)
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_non_bot_orders_are_ignored(self):
        self.exchange.fetch_open_orders.side_effect = [
            [{"id": "manual-order", "clientOrderId": "manual-123"}],
            [],
            [],
            [],
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertTrue(result["safe_to_initialize_empty_execution_state"])

    def test_main_fails_closed_without_leaking_exception_message(self):
        with (
            patch.object(
                kraken_order_audit,
                "audit_orders",
                side_effect=RuntimeError("secret-provider-payload"),
            ),
            patch("builtins.print") as output,
        ):
            status = kraken_order_audit.main()

        self.assertEqual(status, 1)
        self.assertNotIn("secret-provider-payload", output.call_args.args[0])


if __name__ == "__main__":
    unittest.main()

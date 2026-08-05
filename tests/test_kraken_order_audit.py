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
        self.assertEqual(self.exchange.fetch_closed_orders.call_count, 1)
        self.exchange.fetch_closed_orders.assert_called_once_with(
            None,
            limit=kraken_order_audit.CLOSED_ORDER_PAGE_SIZE,
            params={
                "ofs": 0,
                "start": unittest.mock.ANY,
                "closetime": "close",
            },
        )
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
                    "symbol": "BTC/GBP",
                },
                {
                    "id": "closed-old-111111",
                    "clientOrderId": "dca-aaaaaaaaaaaaaa",
                    "timestamp": previous_day_ms,
                    "symbol": "BTC/GBP",
                },
            ]
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 1)
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_cross_midnight_close_times_count_as_same_bangkok_day(self):
        opened_before_midnight = datetime(2026, 8, 4, 16, 59, tzinfo=timezone.utc)
        closed_after_midnight = datetime(2026, 8, 4, 17, 1, tzinfo=timezone.utc)
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": "closed-after-midnight-100001",
                "clientOrderId": "dca-11111111111111",
                "timestamp": int(opened_before_midnight.timestamp() * 1000),
                "lastUpdateTimestamp": int(closed_after_midnight.timestamp() * 1000),
                "symbol": "BTC/GBP",
            },
            {
                "id": "closed-after-midnight-100002",
                "clientOrderId": "dca-22222222222222",
                "timestamp": int(opened_before_midnight.timestamp() * 1000),
                "lastUpdateTimestamp": None,
                "info": {"closetm": closed_after_midnight.timestamp()},
                "symbol": "ETH/GBP",
            },
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 2)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["same_day_closed_count"], 1
        )
        self.assertEqual(
            result["markets"]["ETH_GBP"]["same_day_closed_count"], 1
        )
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

    def test_closed_bot_order_with_missing_timestamp_fails_closed(self):
        self.exchange.fetch_closed_orders.side_effect = [
            [
                {
                    "id": "closed-unknown-777777",
                    "clientOrderId": "dca-1234567890abcd",
                    "timestamp": None,
                    "symbol": "BTC/GBP",
                }
            ]
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["unknown_timestamp_closed_bot_orders"], 1)
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_closed_orders_paginate_past_fifty_and_deduplicate_ids(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        first_bot = {
            "id": "closed-page-one-000001",
            "clientOrderId": "dca-11111111111111",
            "timestamp": same_day_ms,
            "symbol": "BTC/GBP",
        }
        first_page = [first_bot]
        first_page.extend(
            {
                "id": f"manual-page-one-{index:06d}",
                "clientOrderId": f"manual-{index}",
                "timestamp": same_day_ms,
                "symbol": "BTC/GBP",
            }
            for index in range(49)
        )
        second_page = [
            dict(first_bot),
            {
                "id": "closed-page-two-000002",
                "clientOrderId": "dca-22222222222222",
                "timestamp": same_day_ms,
                "symbol": "ETH/GBP",
            },
        ]
        self.exchange.fetch_closed_orders.side_effect = [first_page, second_page]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 2)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["same_day_closed_count"], 1
        )
        self.assertEqual(
            result["markets"]["ETH_GBP"]["same_day_closed_count"], 1
        )
        self.assertEqual(self.exchange.fetch_closed_orders.call_count, 2)
        self.assertEqual(
            self.exchange.fetch_closed_orders.call_args_list[1].kwargs["params"],
            {
                "ofs": 50,
                "start": unittest.mock.ANY,
                "closetime": "close",
            },
        )

    def test_repeated_closed_order_page_fails_closed_on_no_progress(self):
        page = [
            {
                "id": f"closed-repeat-{index:06d}",
                "clientOrderId": f"manual-{index}",
                "timestamp": 1,
                "symbol": "BTC/GBP",
            }
            for index in range(kraken_order_audit.CLOSED_ORDER_PAGE_SIZE)
        ]
        self.exchange.fetch_closed_orders.side_effect = [page, list(page)]

        with self.assertRaisesRegex(RuntimeError, "made no progress"):
            kraken_order_audit.audit_orders(self.exchange, now=self.now)

    def test_closed_order_without_id_fails_closed(self):
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": None,
                "clientOrderId": "dca-1234567890abcd",
                "timestamp": 1,
                "symbol": "BTC/GBP",
            }
        ]

        with self.assertRaisesRegex(RuntimeError, "without an ID"):
            kraken_order_audit.audit_orders(self.exchange, now=self.now)

    def test_excessive_closed_order_pages_fail_closed(self):
        pages = []
        for page_number in range(2):
            pages.append(
                [
                    {
                        "id": f"closed-{page_number}-{index:06d}",
                        "clientOrderId": f"manual-{page_number}-{index}",
                        "timestamp": 1,
                        "symbol": "BTC/GBP",
                    }
                    for index in range(kraken_order_audit.CLOSED_ORDER_PAGE_SIZE)
                ]
            )
        self.exchange.fetch_closed_orders.side_effect = pages

        with (
            patch.object(kraken_order_audit, "MAX_CLOSED_ORDER_PAGES", 2),
            self.assertRaisesRegex(RuntimeError, "exceeded its safety limit"),
        ):
            kraken_order_audit.audit_orders(self.exchange, now=self.now)

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

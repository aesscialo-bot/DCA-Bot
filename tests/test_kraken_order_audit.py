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

    def _hype_source_flow(
        self,
        *,
        trade_date="2026-08-05",
        timestamp=None,
        last_update_timestamp=None,
    ):
        timestamp = timestamp or int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        common = {
            "timestamp": timestamp,
            "lastUpdateTimestamp": last_update_timestamp,
            "status": "closed",
            "filled": "10",
            "cost": "12.5",
        }
        return [
            {
                **common,
                "id": "hype-funding-500001",
                "clientOrderId": kraken_order_audit.build_client_order_id(
                    "HYPE_USD", trade_date, purpose="funding"
                ),
                "symbol": "GBP/USD",
            },
            {
                **common,
                "id": "hype-buy-500002",
                "clientOrderId": kraken_order_audit.build_client_order_id(
                    "HYPE_USD", trade_date, purpose="buy"
                ),
                "symbol": "HYPE/USD",
            },
        ]

    def test_active_targets_are_all_direct_gbp_and_legacy_markets_remain_audited(self):
        self.assertEqual(
            kraken_order_audit.TARGETS,
            ("BTC_GBP", "ETH_GBP", "SOL_GBP"),
        )
        self.assertEqual(
            kraken_order_audit.LEGACY_AUDIT_MARKETS,
            ("HYPE_USD", "GBP_USD"),
        )
        self.assertEqual(
            tuple(kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE.routes),
            ("BTC_GBP", "HYPE_USD", "SOL_GBP"),
        )

    def test_clean_audit_is_safe_and_queries_only_read_endpoints(self):
        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertTrue(result["safe_to_initialize_empty_execution_state"])
        self.assertEqual(result["audit_profile"], "ACTIVE_ETH_GBP")
        self.assertEqual(result["unresolved_bot_orders"], 0)
        self.assertEqual(result["same_day_closed_bot_orders"], 0)
        self.assertTrue(result["flow_integrity_ok"])
        self.exchange.fetch_open_orders.assert_called_once_with(None)
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
        self.assertEqual(
            [call[0] for call in self.exchange.method_calls],
            ["fetch_closed_orders", "fetch_open_orders"],
        )

    def test_open_bot_order_blocks_empty_state_and_only_exposes_suffix(self):
        self.exchange.fetch_open_orders.return_value = [
            {
                "id": "sensitive-prefix-ABC123",
                "clientOrderId": "dca-1234567890abcd",
                "symbol": "BTC/GBP",
            }
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
                "symbol": "HYPE/USD",
            },
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 2)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["same_day_closed_count"], 1
        )
        self.assertEqual(
            result["markets"]["HYPE_USD"]["same_day_closed_count"], 1
        )
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_non_bot_orders_are_ignored(self):
        self.exchange.fetch_open_orders.return_value = [
            {
                "id": "manual-order",
                "clientOrderId": "manual-123",
                "symbol": "BTC/GBP",
            }
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertTrue(result["safe_to_initialize_empty_execution_state"])

    def test_complete_direct_order_is_one_flow_not_a_duplicate(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        crypto_client_id = kraken_order_audit.build_client_order_id(
            "BTC_GBP", "2026-08-05", purpose="buy"
        )
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": "crypto-leg-100002",
                "clientOrderId": crypto_client_id,
                "timestamp": same_day_ms,
                "symbol": "BTC/GBP",
            },
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 1)
        self.assertEqual(result["same_day_completed_dca_flows"], 1)
        self.assertEqual(result["same_day_incomplete_dca_flows"], 0)
        self.assertEqual(result["same_day_duplicate_order_legs"], 0)
        self.assertEqual(result["same_day_mismatched_order_legs"], 0)
        self.assertTrue(result["flow_integrity_ok"])
        self.assertEqual(result["markets"]["GBP_USD"]["same_day_closed_count"], 0)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["same_day_closed_count"], 1
        )
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_legacy_funding_leg_is_mismatched_and_fails_closed(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        funding_client_id = kraken_order_audit.build_client_order_id(
            "HYPE_USD", "2026-08-05", purpose="funding"
        )
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": "orphan-funding-200001",
                "clientOrderId": funding_client_id,
                "timestamp": same_day_ms,
                "symbol": "GBP/USD",
            }
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_completed_dca_flows"], 0)
        self.assertEqual(result["same_day_incomplete_dca_flows"], 0)
        self.assertEqual(result["same_day_mismatched_order_legs"], 1)
        self.assertFalse(result["flow_integrity_ok"])
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_default_profile_rejects_complete_hype_source_flow(self):
        self.exchange.fetch_closed_orders.return_value = self._hype_source_flow()

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["audit_profile"], "ACTIVE_ETH_GBP")
        self.assertEqual(result["same_day_mismatched_order_legs"], 2)
        self.assertFalse(result["flow_integrity_ok"])

    def test_migration_source_profile_accepts_exact_filled_hype_flow(self):
        self.exchange.fetch_closed_orders.return_value = self._hype_source_flow()

        result = kraken_order_audit.audit_orders(
            self.exchange,
            now=self.now,
            profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
        )

        self.assertEqual(result["audit_profile"], "HYPE_TO_ETH_SOURCE")
        self.assertEqual(result["same_day_completed_dca_flows"], 1)
        self.assertEqual(result["same_day_incomplete_dca_flows"], 0)
        self.assertEqual(result["same_day_duplicate_order_legs"], 0)
        self.assertEqual(result["same_day_mismatched_order_legs"], 0)
        self.assertEqual(result["same_day_invalid_terminal_order_legs"], 0)
        self.assertEqual(
            result["same_day_completed_dca_flows_by_target"]["HYPE_USD"], 1
        )
        self.assertTrue(result["flow_integrity_ok"])
        self.assertTrue(result["safe_for_target_migration"])
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_migration_source_profile_blocks_missing_or_duplicate_hype_legs(self):
        funding, buy = self._hype_source_flow()
        duplicate_funding = dict(funding, id="hype-funding-500003")
        duplicate_buy = dict(buy, id="hype-buy-500004")
        for orders in (
            [funding],
            [buy],
            [funding, duplicate_funding, buy],
            [funding, buy, duplicate_buy],
        ):
            with self.subTest(order_count=len(orders)):
                self.exchange.fetch_closed_orders.return_value = orders
                result = kraken_order_audit.audit_orders(
                    self.exchange,
                    now=self.now,
                    profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
                )
                self.assertFalse(result["flow_integrity_ok"])

    def test_migration_source_profile_blocks_wrong_or_active_eth_legs(self):
        funding, buy = self._hype_source_flow()
        wrong_symbol = dict(buy, symbol="ETH/GBP")
        wrong_id = dict(buy, clientOrderId="dca-1234567890abcd")
        eth = dict(
            buy,
            clientOrderId=kraken_order_audit.build_client_order_id(
                "ETH_GBP", "2026-08-05", purpose="buy"
            ),
            symbol="ETH/GBP",
        )
        for bad_leg in (wrong_symbol, wrong_id, eth):
            with self.subTest(symbol=bad_leg["symbol"]):
                self.exchange.fetch_closed_orders.return_value = [funding, bad_leg]
                result = kraken_order_audit.audit_orders(
                    self.exchange,
                    now=self.now,
                    profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
                )
                self.assertFalse(result["flow_integrity_ok"])
                self.assertGreater(result["same_day_mismatched_order_legs"], 0)

    def test_migration_source_profile_requires_successful_positive_fills(self):
        for field, value in (
            ("status", "canceled"),
            ("status", "expired"),
            ("filled", 0),
            ("filled", float("nan")),
            ("filled", float("inf")),
            ("filled", "invalid"),
            ("filled", True),
            ("cost", 0),
            ("cost", -1),
            ("cost", float("-inf")),
            ("cost", None),
        ):
            with self.subTest(field=field, value=value):
                orders = self._hype_source_flow()
                orders[1][field] = value
                self.exchange.fetch_closed_orders.return_value = orders
                result = kraken_order_audit.audit_orders(
                    self.exchange,
                    now=self.now,
                    profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
                )
                self.assertFalse(result["flow_integrity_ok"])
                self.assertEqual(
                    result["same_day_invalid_terminal_order_legs"], 1
                )

        for missing in ("status", "filled", "cost"):
            with self.subTest(missing=missing):
                orders = self._hype_source_flow()
                orders[0].pop(missing)
                self.exchange.fetch_closed_orders.return_value = orders
                result = kraken_order_audit.audit_orders(
                    self.exchange,
                    now=self.now,
                    profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
                )
                self.assertFalse(result["flow_integrity_ok"])

    def test_migration_source_profile_accepts_direct_btc_or_sol_flow(self):
        timestamp = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        for target, symbol in (("BTC_GBP", "BTC/GBP"), ("SOL_GBP", "SOL/GBP")):
            with self.subTest(target=target):
                self.exchange.fetch_closed_orders.return_value = [
                    {
                        "id": f"{target}-500005",
                        "clientOrderId": kraken_order_audit.build_client_order_id(
                            target, "2026-08-05", purpose="buy"
                        ),
                        "timestamp": timestamp,
                        "symbol": symbol,
                        "status": "closed",
                        "filled": "0.01",
                        "cost": "12.5",
                    }
                ]
                result = kraken_order_audit.audit_orders(
                    self.exchange,
                    now=self.now,
                    profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
                )
                self.assertTrue(result["flow_integrity_ok"])
                self.assertEqual(result["same_day_completed_dca_flows"], 1)

    def test_migration_source_profile_blocks_matching_open_legs(self):
        orders = self._hype_source_flow()
        for order in orders:
            order["status"] = "open"
        self.exchange.fetch_open_orders.return_value = orders

        result = kraken_order_audit.audit_orders(
            self.exchange,
            now=self.now,
            profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
        )

        self.assertEqual(result["unresolved_bot_orders"], 2)
        self.assertEqual(result["unresolved_invalid_terminal_order_legs"], 2)
        self.assertFalse(result["flow_integrity_ok"])
        self.assertFalse(result["safe_for_target_migration"])

    def test_migration_source_profile_blocks_unknown_timestamps(self):
        orders = self._hype_source_flow()
        for order in orders:
            order["timestamp"] = None

        self.exchange.fetch_closed_orders.return_value = orders
        result = kraken_order_audit.audit_orders(
            self.exchange,
            now=self.now,
            profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
        )

        self.assertEqual(result["unknown_timestamp_closed_bot_orders"], 2)
        self.assertFalse(result["safe_for_target_migration"])
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

    def test_migration_source_profile_accepts_complete_cross_midnight_flow(self):
        opened = datetime(2026, 8, 4, 16, 59, tzinfo=timezone.utc)
        closed = datetime(2026, 8, 4, 17, 1, tzinfo=timezone.utc)
        self.exchange.fetch_closed_orders.return_value = self._hype_source_flow(
            trade_date="2026-08-04",
            timestamp=int(opened.timestamp() * 1000),
            last_update_timestamp=int(closed.timestamp() * 1000),
        )

        result = kraken_order_audit.audit_orders(
            self.exchange,
            now=self.now,
            profile=kraken_order_audit.HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
        )

        self.assertTrue(result["flow_integrity_ok"])
        self.assertEqual(result["same_day_completed_dca_flows"], 1)
        self.assertEqual(
            result["same_day_completed_dca_flow_dates_by_target"]["HYPE_USD"],
            ["2026-08-04"],
        )

    def test_only_fixed_internal_audit_profiles_are_accepted(self):
        custom = kraken_order_audit.AuditProfile(
            "CUSTOM", {"BTC_GBP": "DIRECT_GBP"}
        )
        with self.assertRaisesRegex(ValueError, "unsupported"):
            kraken_order_audit.audit_orders(
                self.exchange, now=self.now, profile=custom
            )

    def test_duplicate_crypto_leg_is_not_counted_as_a_complete_flow(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        funding_client_id = kraken_order_audit.build_client_order_id(
            "SOL_GBP", "2026-08-05", purpose="funding"
        )
        crypto_client_id = kraken_order_audit.build_client_order_id(
            "SOL_GBP", "2026-08-05", purpose="buy"
        )
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": "funding-leg-300001",
                "clientOrderId": funding_client_id,
                "timestamp": same_day_ms,
                "symbol": "GBP/USD",
            },
            {
                "id": "crypto-leg-300002",
                "clientOrderId": crypto_client_id,
                "timestamp": same_day_ms,
                "symbol": "SOL/GBP",
            },
            {
                "id": "crypto-leg-300003",
                "clientOrderId": crypto_client_id,
                "timestamp": same_day_ms,
                "symbol": "SOL/GBP",
            },
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_completed_dca_flows"], 0)
        self.assertEqual(result["same_day_incomplete_dca_flows"], 1)
        self.assertEqual(result["same_day_duplicate_order_legs"], 1)
        self.assertFalse(result["flow_integrity_ok"])

    def test_expected_client_id_on_wrong_market_is_mismatched(self):
        same_day_ms = int(
            datetime(2026, 8, 5, 1, 0, tzinfo=timezone.utc).timestamp() * 1000
        )
        crypto_client_id = kraken_order_audit.build_client_order_id(
            "BTC_GBP", "2026-08-05", purpose="buy"
        )
        self.exchange.fetch_closed_orders.return_value = [
            {
                "id": "wrong-market-400001",
                "clientOrderId": crypto_client_id,
                "timestamp": same_day_ms,
                "symbol": "GBP/USD",
            }
        ]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_mismatched_order_legs"], 1)
        self.assertFalse(result["flow_integrity_ok"])
        self.assertFalse(result["safe_to_initialize_empty_execution_state"])

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
                "symbol": "HYPE/USD",
            },
        ]
        self.exchange.fetch_closed_orders.side_effect = [first_page, second_page]

        result = kraken_order_audit.audit_orders(self.exchange, now=self.now)

        self.assertEqual(result["same_day_closed_bot_orders"], 2)
        self.assertEqual(
            result["markets"]["BTC_GBP"]["same_day_closed_count"], 1
        )
        self.assertEqual(
            result["markets"]["HYPE_USD"]["same_day_closed_count"], 1
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

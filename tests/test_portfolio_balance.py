import unittest
from datetime import datetime
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import portfolio_balance


class PortfolioConfigurationTests(unittest.TestCase):
    def test_extracts_unique_gbp_markets(self):
        target_map = {
            "BTC_GBP": {"BUY_ENABLED": False},
            "ETH_GBP": {"BUY_ENABLED": True},
        }

        self.assertEqual(
            portfolio_balance.extract_gbp_symbols(target_map),
            ["BTC/GBP", "ETH/GBP"],
        )

    def test_rejects_non_gbp_market(self):
        with self.assertRaisesRegex(ValueError, "expected a BASE_GBP"):
            portfolio_balance.extract_gbp_symbols({"BTC_EUR": {}})
        with self.assertRaisesRegex(ValueError, "expected a BASE_GBP"):
            portfolio_balance.extract_gbp_symbols({"btc_gbp": {}})

    def test_monthly_window_uses_latest_completed_fifth(self):
        timezone = ZoneInfo("Asia/Bangkok")

        before_cutoff = datetime(2026, 8, 4, 12, tzinfo=timezone)
        start, end = portfolio_balance.monthly_reporting_window(before_cutoff)
        self.assertEqual(start, datetime(2026, 6, 5, 7, tzinfo=timezone))
        self.assertEqual(end, datetime(2026, 7, 5, 7, tzinfo=timezone))

        after_cutoff = datetime(2026, 8, 5, 8, tzinfo=timezone)
        start, end = portfolio_balance.monthly_reporting_window(after_cutoff)
        self.assertEqual(start, datetime(2026, 7, 5, 7, tzinfo=timezone))
        self.assertEqual(end, datetime(2026, 8, 5, 7, tzinfo=timezone))


class KrakenPortfolioDataTests(unittest.TestCase):
    def test_uses_total_balance_and_kraken_ticker(self):
        exchange = MagicMock()
        exchange.fetch_balance.return_value = {
            "total": {"BTC": 0.1, "GBP": 125.5},
            "BTC": {"free": 0.09, "total": 0.1},
        }
        exchange.fetch_ticker.return_value = {"last": 50_000}

        balances = portfolio_balance.get_portfolio_balances(exchange, ["BTC/GBP"])
        prices = portfolio_balance.get_gbp_prices(exchange, ["BTC/GBP"])

        self.assertEqual(balances, {"BTC": 0.1, "GBP": 125.5})
        self.assertEqual(prices, {"BTC/GBP": 50_000})
        exchange.fetch_ticker.assert_called_once_with("BTC/GBP")

    def test_paginates_and_normalises_only_gbp_buy_fills(self):
        exchange = MagicMock()
        exchange.fetch_my_trades.side_effect = [
            [
                {
                    "id": "buy-1",
                    "order": "order-1",
                    "symbol": "BTC/GBP",
                    "timestamp": 110_000,
                    "side": "buy",
                    "amount": 0.01,
                    "price": 50_000,
                    "cost": 500,
                },
                {
                    "id": "sell-1",
                    "symbol": "BTC/GBP",
                    "timestamp": 120_000,
                    "side": "sell",
                    "amount": 0.01,
                    "price": 51_000,
                    "cost": 510,
                },
            ],
            [
                {
                    "id": "buy-2",
                    "order": "order-2",
                    "symbol": "BTC/GBP",
                    "timestamp": 150_000,
                    "side": "buy",
                    "amount": 0.02,
                    "price": 52_000,
                    "cost": 1_040,
                }
            ],
        ]

        with patch.object(portfolio_balance, "TRADE_PAGE_SIZE", 2):
            history = portfolio_balance.aggregate_buy_trades(
                exchange,
                ["BTC/GBP"],
                start_ts=100,
                end_ts=200,
            )

        self.assertEqual([trade["trade_id"] for trade in history["BTC"]], ["buy-2", "buy-1"])
        self.assertEqual(history["BTC"][0]["amount_gbp"], 1_040)
        self.assertEqual(history["BTC"][0]["rate_gbp"], 52_000)
        self.assertEqual(exchange.fetch_my_trades.call_count, 2)
        self.assertEqual(
            exchange.fetch_my_trades.call_args_list[0].kwargs,
            {
                "since": 100_000,
                "limit": 2,
                "params": {"ofs": 0, "end": 200, "limit": 2},
            },
        )
        self.assertEqual(
            exchange.fetch_my_trades.call_args_list[1].kwargs["params"]["ofs"],
            2,
        )
        self.assertIsNone(exchange.fetch_my_trades.call_args_list[0].args[0])

    def test_target_trade_is_found_after_pages_of_other_markets(self):
        exchange = MagicMock()
        other_trade = {
            "symbol": "ETH/GBP",
            "timestamp": 110_000,
            "side": "buy",
            "amount": 0.1,
            "price": 2_000,
            "cost": 200,
        }
        exchange.fetch_my_trades.side_effect = [
            [
                {**other_trade, "id": "eth-1"},
                {**other_trade, "id": "eth-2", "timestamp": 120_000},
            ],
            [
                {
                    "id": "btc-1",
                    "order": "order-btc-1",
                    "symbol": "BTC/GBP",
                    "timestamp": 150_000,
                    "side": "buy",
                    "amount": 0.001,
                    "price": 50_000,
                    "cost": 50,
                }
            ],
        ]

        with patch.object(portfolio_balance, "TRADE_PAGE_SIZE", 2):
            history = portfolio_balance.aggregate_buy_trades(
                exchange, ["BTC/GBP"], start_ts=100, end_ts=200
            )

        self.assertEqual([trade["trade_id"] for trade in history["BTC"]], ["btc-1"])
        self.assertNotIn("ETH", history)
        self.assertEqual(exchange.fetch_my_trades.call_count, 2)


class PortfolioReportTests(unittest.TestCase):
    def setUp(self):
        self.exchange = MagicMock()
        self.exchange.fetch_balance.return_value = {"total": {"BTC": 0.1, "GBP": 125}}
        self.exchange.fetch_ticker.return_value = {"last": 50_000}

    def test_short_report_is_gbp_only_and_skips_trade_history(self):
        report = portfolio_balance.build_portfolio_report(
            self.exchange,
            ["BTC/GBP"],
            short_report=True,
        )

        self.assertIn("CONFIGURED KRAKEN HOLDINGS (GBP)", report)
        self.assertIn("Price: £50,000.00", report)
        self.assertIn("£5,000.00", report)
        self.assertIn("GBP Cash", report)
        self.assertIn("£125.00", report)
        self.assertIn("£5,125.00", report)
        self.assertIn("Other Kraken assets", report)
        self.assertNotIn("BUY HISTORY", report)
        self.assertNotIn("USD", report)
        self.assertNotIn("$", report)
        self.exchange.fetch_my_trades.assert_not_called()

    def test_full_report_includes_fifth_to_fifth_gbp_buy_history(self):
        timezone = ZoneInfo("Asia/Bangkok")
        window = (
            datetime(2026, 7, 5, 7, tzinfo=timezone),
            datetime(2026, 8, 5, 7, tzinfo=timezone),
        )
        history = {
            "BTC": [
                {
                    "trade_id": "trade-1",
                    "order_id": "order-1",
                    "amount_crypto": 0.001,
                    "amount_gbp": 50.0,
                    "fee_gbp": 0.13,
                    "rate_gbp": 50_000.0,
                    "timestamp": datetime(2026, 7, 10, 12, tzinfo=timezone).timestamp(),
                }
            ]
        }

        with (
            patch.object(portfolio_balance, "monthly_reporting_window", return_value=window),
            patch.object(portfolio_balance, "aggregate_buy_trades", return_value=history) as aggregate,
        ):
            report = portfolio_balance.build_portfolio_report(
                self.exchange,
                ["BTC/GBP"],
                short_report=False,
            )

        self.assertIn("KRAKEN BUY HISTORY (05 Jul 2026 → 05 Aug 2026)", report)
        self.assertIn("£50.00 cost", report)
        self.assertIn("£0.13 fees", report)
        self.assertIn("at £50,000.00", report)
        self.assertIn("order `order-1`", report)
        aggregate.assert_called_once_with(
            self.exchange,
            ["BTC/GBP"],
            int(window[0].timestamp()),
            int(window[1].timestamp()),
        )

    def test_discord_split_never_exceeds_embed_description_limit(self):
        message = "\n".join(["x" * 1000] * 10)
        chunks = portfolio_balance.split_discord_message(message)

        self.assertGreater(len(chunks), 1)
        self.assertTrue(
            all(len(chunk) <= portfolio_balance.DISCORD_DESCRIPTION_LIMIT for chunk in chunks)
        )
        self.assertEqual("".join(chunks).replace("\n", ""), message.replace("\n", ""))


if __name__ == "__main__":
    unittest.main()

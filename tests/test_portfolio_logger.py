import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import requests

import portfolio_logger


class MockResponse:
    def __init__(self, status_code=200, body=None, text=None):
        self.status_code = status_code
        self._body = body or {}
        self.text = text if text is not None else str(self._body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")


def resolved_activity_response(symbol="BTCUSD", data_source="YAHOO"):
    return MockResponse(
        status_code=201,
        body={
            "activities": [
                {
                    "error": None,
                    "SymbolProfile": {
                        "dataSource": data_source,
                        "symbol": symbol,
                        "name": symbol,
                    },
                }
            ]
        },
    )


class GhostfolioGbpTests(unittest.TestCase):
    def setUp(self):
        timestamp = datetime(2026, 8, 4, 10, 30, tzinfo=timezone.utc).timestamp()
        self.trade = {
            "ts": timestamp,
            "amount_crypto": 0.0002,
            "amount_gbp": 10.0,
            "cost_gbp": 9.95,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.05,
            "fee_details": [
                {
                    "currency": "GBP",
                    "amount": 0.05,
                    "gbp_equivalent": 0.05,
                }
            ],
            "gbp_price_per_unit": 50000.0,
            "order_id": "KRAKEN-123",
        }

    def test_resolves_kraken_gbp_pair_to_usd_provider_profile(self):
        resolution = portfolio_logger.resolve_ghostfolio_asset("BTC", "BTC/GBP")

        self.assertEqual(resolution["dataSource"], "YAHOO")
        self.assertEqual(resolution["symbol"], "BTCUSD")
        self.assertFalse(resolution["usedExplicitMapping"])

    def test_explicit_provider_override_is_preserved_for_gbp_pair(self):
        resolution = portfolio_logger.resolve_ghostfolio_asset("HYPE", "HYPE/GBP")

        self.assertEqual(resolution["dataSource"], "COINGECKO")
        self.assertEqual(resolution["symbol"], "hyperliquid")
        self.assertTrue(resolution["usedExplicitMapping"])

    def test_non_gbp_exchange_pair_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Expected Kraken GBP pair BTC/GBP"):
            portfolio_logger.resolve_ghostfolio_asset("BTC", "BTC/USD")

    def test_ambiguous_asset_without_override_fails_closed(self):
        with patch.dict(
            portfolio_logger.SYMBOL_DATASOURCE_OVERRIDES, {}, clear=True
        ):
            with self.assertRaisesRegex(ValueError, "Ambiguous Ghostfolio asset"):
                portfolio_logger.resolve_ghostfolio_asset("HYPE", "HYPE/GBP")

    def test_activity_keeps_gbp_spend_and_converts_only_provider_price(self):
        activity = portfolio_logger.build_ghostfolio_activity(
            self.trade,
            "BTC",
            "account-id",
            exchange_pair="BTC/GBP",
            gbp_usd_rate=1.25,
        )

        self.assertEqual(activity["accountId"], "account-id")
        self.assertEqual(activity["currency"], "USD")
        self.assertEqual(activity["dataSource"], "YAHOO")
        self.assertEqual(activity["symbol"], "BTCUSD")
        self.assertEqual(activity["date"], "2026-08-04T10:30:00.000Z")
        self.assertEqual(activity["quantity"], 0.0002)
        self.assertEqual(activity["unitPrice"], 62500.0)
        self.assertEqual(
            activity["comment"],
            "GBP 10.00 total on Kraken "
            "(cost 9.95 + GBP debit fee 0.05; fee equivalent 0.05) | "
            "order KRAKEN-123",
        )
        self.assertEqual(activity["fee"], 0.0625)
        self.assertEqual(
            set(activity),
            {
                "accountId",
                "comment",
                "currency",
                "dataSource",
                "date",
                "fee",
                "quantity",
                "symbol",
                "type",
                "unitPrice",
            },
        )

    def test_base_asset_fee_is_reported_without_adding_to_gbp_cash_debit(self):
        trade = {
            **self.trade,
            "amount_gbp": 9.95,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.0,
            "fee_details": [
                {
                    "currency": "BTC",
                    "amount": 0.000001,
                    "gbp_equivalent": 0.05,
                }
            ],
        }

        activity = portfolio_logger.build_ghostfolio_activity(
            trade,
            "BTC",
            "account-id",
            exchange_pair="BTC/GBP",
            gbp_usd_rate=1.25,
        )

        self.assertEqual(activity["quantity"], 0.0002)
        self.assertEqual(activity["fee"], 0.0625)
        self.assertIn("GBP 9.95 total", activity["comment"])
        self.assertIn("GBP debit fee 0.00", activity["comment"])
        self.assertIn("fee equivalent 0.05", activity["comment"])

    def test_activity_rejects_invalid_trade_values(self):
        for field in (
            "amount_crypto",
            "amount_gbp",
            "cost_gbp",
            "gbp_price_per_unit",
        ):
            with self.subTest(field=field):
                invalid_trade = {**self.trade, field: 0}
                with self.assertRaisesRegex(ValueError, "positive finite"):
                    portfolio_logger.build_ghostfolio_activity(
                        invalid_trade,
                        "BTC",
                        "account-id",
                        exchange_pair="BTC/GBP",
                        gbp_usd_rate=1.25,
                    )

    def test_activity_rejects_invalid_conversion_rate(self):
        with self.assertRaisesRegex(ValueError, "GBP-to-USD rate"):
            portfolio_logger.build_ghostfolio_activity(
                self.trade,
                "BTC",
                "account-id",
                exchange_pair="BTC/GBP",
                gbp_usd_rate=0,
            )

    def test_account_mapping_prefers_symbol_then_default(self):
        mapping = {"BTC": "btc-account", "DEFAULT": "default-account"}

        self.assertEqual(
            portfolio_logger.get_account_id("btc", mapping), "btc-account"
        )
        self.assertEqual(
            portfolio_logger.get_account_id("DOGE", mapping), "default-account"
        )
        self.assertIsNone(portfolio_logger.get_account_id("BTC", {}))

    def test_primary_gbp_usd_rate_uses_timeout_and_status_check(self):
        with patch.object(
            portfolio_logger.requests,
            "get",
            return_value=MockResponse(body={"rates": {"USD": 1.27}}),
        ) as get:
            rate = portfolio_logger.get_gbp_usd_rate()

        self.assertEqual(rate, 1.27)
        get.assert_called_once_with(
            "https://api.frankfurter.app/latest?from=GBP&to=USD",
            timeout=portfolio_logger.FX_LOOKUP_TIMEOUT_SECONDS,
        )

    def test_historical_gbp_usd_rate_uses_trade_date(self):
        trade_timestamp = datetime(
            2020, 1, 2, 23, 30, tzinfo=timezone.utc
        ).timestamp()
        with patch.object(
            portfolio_logger.requests,
            "get",
            return_value=MockResponse(body={"rates": {"USD": 1.31}}),
        ) as get:
            rate = portfolio_logger.get_gbp_usd_rate(trade_timestamp)

        self.assertEqual(rate, 1.31)
        get.assert_called_once_with(
            "https://api.frankfurter.app/2020-01-02?from=GBP&to=USD",
            timeout=portfolio_logger.FX_LOOKUP_TIMEOUT_SECONDS,
        )

    def test_gbp_usd_rate_falls_back_to_second_source(self):
        with patch.object(
            portfolio_logger.requests,
            "get",
            side_effect=[
                MockResponse(status_code=503),
                MockResponse(body={"rates": {"USD": 1.26}}),
            ],
        ) as get:
            rate = portfolio_logger.get_gbp_usd_rate()

        self.assertEqual(rate, 1.26)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(
            get.call_args_list[1].args[0],
            "https://open.er-api.com/v6/latest/GBP",
        )

    def test_gbp_usd_rate_returns_none_when_sources_fail(self):
        with patch.object(
            portfolio_logger.requests,
            "get",
            side_effect=[requests.Timeout("one"), requests.ConnectionError("two")],
        ):
            rate = portfolio_logger.get_gbp_usd_rate()

        self.assertIsNone(rate)

    def test_asset_roi_uses_usd_provider_profile_for_gbp_pair(self):
        response = MockResponse(
            body={
                "holdings": [
                    {
                        "dataSource": "YAHOO",
                        "symbol": "BTCUSD",
                        "investment": 100.0,
                        "netPerformanceWithCurrencyEffect": 15.0,
                    }
                ]
            }
        )

        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger.requests, "get", return_value=response) as get,
        ):
            roi = portfolio_logger.get_asset_roi_percent(
                "BTC", "btc-account", exchange_pair="BTC/GBP"
            )

        self.assertEqual(roi, 15.0)
        self.assertEqual(
            get.call_args.kwargs["params"],
            {
                "accounts": "btc-account",
                "dataSource": "YAHOO",
                "range": "max",
                "symbol": "BTCUSD",
            },
        )

    def test_asset_roi_rejects_non_positive_investment(self):
        response = MockResponse(
            body={
                "holdings": [
                    {
                        "dataSource": "YAHOO",
                        "symbol": "BTCUSD",
                        "investment": 0,
                        "netPerformanceWithCurrencyEffect": 1,
                    }
                ]
            }
        )
        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger.requests, "get", return_value=response),
        ):
            roi = portfolio_logger.get_asset_roi_percent(
                "BTC", "btc-account", exchange_pair="BTC/GBP"
            )

        self.assertIsNone(roi)

    def test_missing_token_skips_logging_before_conversion_or_import(self):
        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", None),
            patch.object(portfolio_logger, "get_gbp_usd_rate") as fx,
            patch.object(portfolio_logger.requests, "post") as post,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertFalse(saved)
        fx.assert_not_called()
        post.assert_not_called()

    def test_successful_log_converts_price_inside_ghostfolio_boundary(self):
        dry_run = resolved_activity_response()
        imported = resolved_activity_response()

        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger, "get_gbp_usd_rate", return_value=1.25) as fx,
            patch.object(
                portfolio_logger.requests,
                "post",
                side_effect=[dry_run, imported],
            ) as post,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertTrue(saved)
        fx.assert_called_once_with(self.trade["ts"])
        self.assertEqual(post.call_count, 2)
        activity = post.call_args_list[0].kwargs["json"]["activities"][0]
        self.assertEqual(activity["unitPrice"], 62500.0)
        self.assertEqual(activity["currency"], "USD")
        self.assertIn("GBP 10.00", activity["comment"])
        self.assertIn("?dryRun=true", post.call_args_list[0].args[0])
        self.assertNotIn("?dryRun=true", post.call_args_list[1].args[0])

    def test_conversion_outage_skips_optional_import(self):
        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger, "get_gbp_usd_rate", return_value=None),
            patch.object(portfolio_logger.requests, "post") as post,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertFalse(saved)
        post.assert_not_called()

    def test_duplicate_dry_run_is_idempotent_success(self):
        duplicate = MockResponse(
            status_code=201,
            body={"activities": [{"error": {"code": "IS_DUPLICATE"}}]},
        )
        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger, "get_gbp_usd_rate", return_value=1.25),
            patch.object(
                portfolio_logger.requests, "post", return_value=duplicate
            ) as post,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertTrue(saved)
        self.assertEqual(post.call_count, 1)

    def test_retryable_provider_failure_retries_then_saves(self):
        temporary_failure = MockResponse(
            status_code=400,
            body={
                "message": [
                    'activities.0.symbol ("BTCUSD") is not valid for the '
                    'specified data source ("YAHOO")'
                ]
            },
        )
        valid = resolved_activity_response()

        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger, "get_gbp_usd_rate", return_value=1.25),
            patch.object(
                portfolio_logger.requests,
                "post",
                side_effect=[temporary_failure, valid, valid],
            ) as post,
            patch.object(portfolio_logger.time, "sleep") as sleep,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertTrue(saved)
        self.assertEqual(post.call_count, 3)
        sleep.assert_called_once_with(portfolio_logger.IMPORT_RETRY_DELAY_SECONDS)

    def test_resolution_mismatch_fails_without_real_import(self):
        mismatch = resolved_activity_response(symbol="ETHUSD")

        with (
            patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"),
            patch.object(
                portfolio_logger, "authenticate_ghostfolio", return_value="jwt"
            ),
            patch.object(portfolio_logger, "get_gbp_usd_rate", return_value=1.25),
            patch.object(
                portfolio_logger.requests, "post", return_value=mismatch
            ) as post,
        ):
            saved = portfolio_logger.log_to_ghostfolio(
                self.trade, "BTC", "account-id", exchange_pair="BTC/GBP"
            )

        self.assertFalse(saved)
        self.assertEqual(post.call_count, 1)

    def test_response_body_redacts_access_and_bearer_tokens(self):
        response = MockResponse(
            text="access-token and Bearer bearer-token must not be printed"
        )
        with patch.object(portfolio_logger, "GHOSTFOLIO_TOKEN", "access-token"):
            body = portfolio_logger._safe_response_body(
                response, ["Bearer bearer-token"]
            )

        self.assertNotIn("access-token", body)
        self.assertNotIn("bearer-token", body)
        self.assertEqual(body.count("[redacted]"), 2)


if __name__ == "__main__":
    unittest.main()

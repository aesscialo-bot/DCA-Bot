import unittest
from unittest.mock import MagicMock, patch

import kraken_client


class KrakenClientTests(unittest.TestCase):
    def test_symbol_uses_configured_gbp_quote(self):
        self.assertEqual(kraken_client.to_kraken_symbol("BTC_THB"), "BTC/GBP")

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_thb_quote_rate", return_value=0.022)
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_market_buy_by_cost_returns_normalized_fill(
        self, get_exchange, _get_fx, _sleep
    ):
        exchange = MagicMock()
        exchange.markets = {"BTC/GBP": {}}
        exchange.has = {"createMarketBuyOrderWithCost": True}
        exchange.market.return_value = {
            "limits": {"cost": {"min": 0.43}, "amount": {"min": 0.00005}}
        }
        exchange.fetch_ticker.return_value = {"ask": 50_000}
        exchange.cost_to_precision.return_value = "5.50"
        exchange.create_market_buy_order_with_cost.return_value = {
            "id": "kraken-order"
        }
        exchange.fetch_order.return_value = {
            "filled": 0.000109,
            "cost": 5.45,
            "timestamp": 1_700_000_000_000,
            "status": "closed",
        }
        get_exchange.return_value = exchange

        result = kraken_client.place_market_buy("BTC_THB", 250)

        exchange.create_market_buy_order_with_cost.assert_called_once_with(
            "BTC/GBP", 5.5
        )
        self.assertEqual(result["pair"], "BTC/GBP")
        self.assertEqual(result["order_id"], "kraken-order")
        self.assertAlmostEqual(result["spent_thb"], 5.45 / 0.022)

    @patch.object(kraken_client, "get_thb_quote_rate", return_value=0.022)
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_rejects_budget_below_market_minimum(self, get_exchange, _get_fx):
        exchange = MagicMock()
        exchange.markets = {"BTC/GBP": {}}
        exchange.market.return_value = {
            "limits": {"cost": {"min": 0.43}, "amount": {"min": 0.00005}}
        }
        exchange.fetch_ticker.return_value = {"ask": 50_000}
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(ValueError, "below Kraken's current minimum"):
            kraken_client.place_market_buy("BTC_THB", 100)


if __name__ == "__main__":
    unittest.main()

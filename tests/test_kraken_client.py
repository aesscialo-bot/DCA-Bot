import unittest
from unittest.mock import MagicMock, patch

import kraken_client


def configured_exchange():
    exchange = MagicMock()
    exchange.markets = {"BTC/GBP": {}}
    exchange.has = {"createMarketBuyOrderWithCost": True}
    exchange.market.return_value = {
        "limits": {"cost": {"min": 0.43}, "amount": {"min": 0.00005}}
    }
    exchange.fetch_ticker.return_value = {"ask": 50_000}
    exchange.cost_to_precision.return_value = "5.50"
    exchange.fetch_open_orders.return_value = []
    exchange.fetch_closed_orders.return_value = []
    return exchange


def terminal_fill(
    *,
    order_id="kraken-order",
    client_order_id="dca-1234567890abcd",
    status="closed",
):
    return {
        "id": order_id,
        "clientOrderId": client_order_id,
        "symbol": "BTC/GBP",
        "side": "buy",
        "status": status,
        "filled": 0.000109,
        "cost": 5.45,
        "fee": {"cost": 0.02, "currency": "GBP"},
        "timestamp": 1_700_000_000_000,
    }


class KrakenClientTests(unittest.TestCase):
    def test_symbol_accepts_only_gbp_pairs(self):
        self.assertEqual(kraken_client.to_kraken_symbol("BTC_GBP"), "BTC/GBP")
        self.assertEqual(kraken_client.to_kraken_symbol("eth/gbp"), "ETH/GBP")

        for foreign_pair in ("BTC_EUR", "BTC/USD", "BTC_JPY"):
            with self.subTest(pair=foreign_pair):
                with self.assertRaisesRegex(ValueError, "Only GBP Kraken pairs"):
                    kraken_client.to_kraken_symbol(foreign_pair)

    def test_client_order_id_is_deterministic_unique_and_kraken_sized(self):
        first = kraken_client.build_client_order_id("BTC_GBP", "2026-08-04")
        repeated = kraken_client.build_client_order_id("btc/gbp", "2026-08-04")
        next_day = kraken_client.build_client_order_id("BTC_GBP", "2026-08-05")
        other_market = kraken_client.build_client_order_id("ETH_GBP", "2026-08-04")

        self.assertEqual(first, repeated)
        self.assertNotEqual(first, next_day)
        self.assertNotEqual(first, other_market)
        self.assertRegex(first, r"^dca-[0-9a-f]{14}$")
        self.assertLessEqual(len(first), 18)

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_market_buy_uses_direct_gbp_cost_and_waits_for_terminal_fill(
        self, get_exchange, sleep
    ):
        exchange = configured_exchange()
        exchange.create_market_buy_order_with_cost.return_value = {
            "id": "kraken-order",
            "status": "open",
        }
        exchange.fetch_order.side_effect = [
            {
                "id": "kraken-order",
                "status": "open",
                "filled": 0.00005,
                "cost": 2.50,
            },
            terminal_fill(),
        ]
        get_exchange.return_value = exchange

        with patch.object(
            kraken_client,
            "_submission_deadline",
            return_value="2026-08-04T12:00:15.000Z",
        ):
            result = kraken_client.place_market_buy("BTC_GBP", 5.50)

        exchange.fetch_open_orders.assert_called_once_with(
            "BTC/GBP", params={"cl_ord_id": result["client_order_id"]}
        )
        exchange.fetch_closed_orders.assert_called_once_with(
            "BTC/GBP", params={"cl_ord_id": result["client_order_id"]}
        )
        exchange.cost_to_precision.assert_called_once_with("BTC/GBP", 5.50)
        create_args = exchange.create_market_buy_order_with_cost.call_args.args
        self.assertEqual(create_args[:2], ("BTC/GBP", 5.50))
        self.assertEqual(
            create_args[2],
            {
                "clientOrderId": result["client_order_id"],
                "deadline": "2026-08-04T12:00:15.000Z",
                "oflags": "fciq",
            },
        )
        self.assertEqual(exchange.fetch_order.call_count, 2)
        sleep.assert_called_once_with(1)

        self.assertEqual(result["pair"], "BTC/GBP")
        self.assertEqual(result["quote_currency"], "GBP")
        self.assertEqual(result["order_id"], "kraken-order")
        self.assertEqual(result["cost_gbp"], 5.45)
        self.assertEqual(result["fee_gbp"], 0.02)
        self.assertEqual(result["gbp_fee_debit"], 0.02)
        self.assertEqual(
            result["fee_details"],
            [{"currency": "GBP", "amount": 0.02, "gbp_equivalent": 0.02}],
        )
        self.assertEqual(result["spent_gbp"], 5.47)
        self.assertEqual(result["received"], 0.000109)
        self.assertAlmostEqual(
            result["market_gbp_price_per_unit"], 5.45 / 0.000109
        )
        self.assertAlmostEqual(
            result["effective_gbp_price_per_unit"], 5.47 / 0.000109
        )
        self.assertEqual(
            result["gbp_price_per_unit"], result["market_gbp_price_per_unit"]
        )

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_reconciles_closed_order_before_creating_another(self, get_exchange):
        exchange = configured_exchange()
        client_order_id = "dca-1234567890abcd"
        exchange.fetch_closed_orders.return_value = [
            terminal_fill(client_order_id=client_order_id)
        ]
        get_exchange.return_value = exchange

        with patch.object(
            kraken_client, "build_client_order_id", return_value=client_order_id
        ):
            result = kraken_client.place_market_buy("BTC_GBP", 5.50)

        self.assertEqual(result["order_id"], "kraken-order")
        exchange.create_market_buy_order_with_cost.assert_not_called()
        exchange.fetch_order.assert_not_called()
        exchange.fetch_ticker.assert_not_called()

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_reconciles_open_order_and_polls_it_instead_of_creating(
        self, get_exchange, sleep
    ):
        exchange = configured_exchange()
        client_order_id = "dca-1234567890abcd"
        exchange.fetch_open_orders.return_value = [
            {
                "id": "existing-order",
                "clientOrderId": client_order_id,
                "symbol": "BTC/GBP",
                "side": "buy",
                "status": "open",
                "filled": 0.00005,
                "cost": 2.50,
            }
        ]
        exchange.fetch_order.return_value = terminal_fill(
            order_id="existing-order", client_order_id=client_order_id
        )
        get_exchange.return_value = exchange

        with patch.object(
            kraken_client, "build_client_order_id", return_value=client_order_id
        ):
            result = kraken_client.place_market_buy("BTC_GBP", 5.50)

        self.assertEqual(result["order_id"], "existing-order")
        exchange.create_market_buy_order_with_cost.assert_not_called()
        exchange.fetch_order.assert_called_once_with("existing-order", "BTC/GBP")
        sleep.assert_not_called()

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_ambiguous_submission_is_reconciled_without_second_create(
        self, get_exchange, sleep
    ):
        exchange = configured_exchange()
        client_order_id = "dca-1234567890abcd"
        recovered = terminal_fill(client_order_id=client_order_id)
        exchange.fetch_open_orders.side_effect = [[], []]
        exchange.fetch_closed_orders.side_effect = [[], [recovered]]
        exchange.create_market_buy_order_with_cost.side_effect = TimeoutError(
            "response lost"
        )
        get_exchange.return_value = exchange

        with patch.object(
            kraken_client, "build_client_order_id", return_value=client_order_id
        ):
            result = kraken_client.place_market_buy("BTC_GBP", 5.50)

        self.assertEqual(result["order_id"], "kraken-order")
        exchange.create_market_buy_order_with_cost.assert_called_once()
        self.assertEqual(exchange.fetch_open_orders.call_count, 2)
        self.assertEqual(exchange.fetch_closed_orders.call_count, 2)
        sleep.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_rejects_gbp_amount_below_live_market_minimum(self, get_exchange):
        exchange = configured_exchange()
        exchange.market.return_value = {
            "limits": {"cost": {"min": 0.43}, "amount": {"min": 0.0002}}
        }
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenPreSubmissionError,
            "below Kraken's current minimum",
        ):
            kraken_client.place_market_buy("BTC_GBP", 5.00)

        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_rejects_amount_outside_bot_guardrails_before_authentication(self):
        for amount in (4.99, 1000.01):
            with self.subTest(amount=amount):
                with self.assertRaisesRegex(
                    kraken_client.KrakenPreSubmissionError,
                    "between 5 and 1000",
                ):
                    kraken_client.place_market_buy("BTC_GBP", amount)

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_fails_closed_if_quote_cost_market_buy_is_unavailable(
        self, get_exchange
    ):
        exchange = configured_exchange()
        exchange.has = {"createMarketBuyOrderWithCost": False}
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(RuntimeError, "quote-cost market buys"):
            kraken_client.place_market_buy("BTC_GBP", 5.50)

        exchange.create_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_open_partial_fill_never_counts_as_terminal(self, get_exchange, sleep):
        exchange = configured_exchange()
        exchange.create_market_buy_order_with_cost.return_value = {
            "id": "pending-order",
            "status": "open",
        }
        exchange.fetch_order.return_value = {
            "id": "pending-order",
            "status": "open",
            "filled": 0.00005,
            "cost": 2.50,
        }
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown, "remains non-terminal"
        ):
            kraken_client.place_market_buy("BTC_GBP", 5.50)

        self.assertEqual(
            exchange.fetch_order.call_count,
            len(kraken_client.ORDER_POLL_DELAYS_SECONDS) + 1,
        )
        self.assertEqual(sleep.call_count, len(kraken_client.ORDER_POLL_DELAYS_SECONDS))

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_terminal_zero_fill_fails_without_creating_another(self, get_exchange):
        exchange = configured_exchange()
        client_order_id = "dca-1234567890abcd"
        exchange.fetch_closed_orders.return_value = [
            {
                "id": "canceled-order",
                "clientOrderId": client_order_id,
                "symbol": "BTC/GBP",
                "side": "buy",
                "status": "canceled",
                "filled": 0,
                "cost": 0,
                "fee": {"cost": 0, "currency": "GBP"},
            }
        ]
        get_exchange.return_value = exchange

        with (
            patch.object(
                kraken_client, "build_client_order_id", return_value=client_order_id
            ),
            self.assertRaisesRegex(
                kraken_client.KrakenOrderNoFill, "without a confirmed fill"
            ),
        ):
            kraken_client.place_market_buy("BTC_GBP", 5.50)

        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_durable_intent_with_no_matching_order_never_creates(self, get_exchange):
        exchange = configured_exchange()
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown, "durable order intent"
        ):
            kraken_client.place_market_buy(
                "BTC_GBP",
                5.50,
                client_order_id="dca-1234567890abcd",
                reconcile_only=True,
            )

        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_durable_intent_lookup_failure_is_unknown_not_safe_to_clear(
        self, get_exchange
    ):
        exchange = configured_exchange()
        exchange.fetch_open_orders.side_effect = TimeoutError("Kraken unavailable")
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown,
            "reconciliation lookup failed",
        ):
            kraken_client.place_market_buy(
                "BTC_GBP",
                5.50,
                client_order_id="dca-1234567890abcd",
                reconcile_only=True,
            )

        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_durable_intent_setup_failure_is_unknown_not_safe_to_clear(
        self, get_exchange
    ):
        exchange = configured_exchange()
        exchange.load_markets.side_effect = TimeoutError("market load unavailable")
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown,
            "setup failed before AddOrder",
        ):
            kraken_client.place_market_buy(
                "BTC_GBP",
                5.50,
                client_order_id="dca-1234567890abcd",
                reconcile_only=True,
            )

        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_base_asset_fee_is_distinct_from_gbp_cash_debit(self):
        order = terminal_fill()
        order["fee"] = {"cost": 0.000001, "currency": "BTC"}

        result = kraken_client._normalise_terminal_fill(
            order, "BTC/GBP", "dca-1234567890abcd"
        )

        self.assertEqual(result["spent_gbp"], result["cost_gbp"])
        self.assertEqual(result["gbp_fee_debit"], 0)
        self.assertGreater(result["fee_gbp"], 0)
        self.assertEqual(result["fee_details"][0]["currency"], "BTC")

    def test_missing_fee_currency_fails_closed(self):
        order = terminal_fill()
        order["fee"] = {"cost": 0.02}

        with self.assertRaisesRegex(RuntimeError, "without its currency"):
            kraken_client._normalise_terminal_fill(
                order, "BTC/GBP", "dca-1234567890abcd"
            )

    def test_closed_order_with_missing_or_zero_fill_data_stays_unknown(self):
        for order in (
            {"id": "closed-order", "status": "closed"},
            {
                "id": "closed-order",
                "status": "closed",
                "filled": 0,
                "cost": 0,
            },
        ):
            with self.subTest(order=order):
                with self.assertRaises(kraken_client.KrakenOrderStateUnknown):
                    kraken_client._normalise_terminal_fill(
                        order, "BTC/GBP", "dca-1234567890abcd"
                    )

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_final_rule_check_runs_immediately_before_create(self, get_exchange):
        exchange = configured_exchange()
        events = []
        exchange.create_market_buy_order_with_cost.side_effect = (
            lambda *_args, **_kwargs: events.append("create") or terminal_fill()
        )
        get_exchange.return_value = exchange

        with patch.object(
            kraken_client,
            "_submission_deadline",
            side_effect=lambda: events.append("deadline")
            or "2026-08-04T12:00:15.000Z",
        ):
            result = kraken_client.place_market_buy(
                "BTC_GBP",
                5.50,
                client_order_id="dca-1234567890abcd",
                pre_submit_check=lambda: events.append("check"),
            )

        self.assertEqual(events, ["check", "deadline", "create"])
        self.assertEqual(result["order_id"], "kraken-order")

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_failed_final_rule_check_never_creates_order(self, get_exchange):
        exchange = configured_exchange()
        get_exchange.return_value = exchange

        def reject_stale_rules():
            raise RuntimeError("rules changed")

        with self.assertRaisesRegex(RuntimeError, "rules changed"):
            kraken_client.place_market_buy(
                "BTC_GBP",
                5.50,
                client_order_id="dca-1234567890abcd",
                pre_submit_check=reject_stale_rules,
            )

        exchange.create_market_buy_order_with_cost.assert_not_called()


if __name__ == "__main__":
    unittest.main()

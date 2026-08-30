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
        "lastUpdateTimestamp": 1_700_000_001_000,
    }


BUY_ID = "dca-1234567890abcd"
FUNDING_ID = "dca-fedcba09876543"


def configured_usd_exchange(target="BTC/USD"):
    exchange = MagicMock()
    exchange.markets = {target: {}, "GBP/USD": {}}
    exchange.has = {"createMarketBuyOrderWithCost": True}
    markets = {
        target: {
            "limits": {"cost": {"min": 0.5}, "amount": {"min": 0.00005}}
        },
        "GBP/USD": {
            "limits": {"cost": {"min": 1.0}, "amount": {"min": 1.0}}
        },
    }
    exchange.market.side_effect = lambda symbol: markets[symbol]
    exchange.fetch_ticker.side_effect = lambda symbol: (
        {"ask": 50_000.0} if symbol == target else
        {"bid": 1.25, "last": 1.255, "ask": 1.26}
    )
    exchange.fetch_balance.return_value = {"free": {"GBP": 100.0, "USD": 999.0}}
    exchange.amount_to_precision.side_effect = lambda _symbol, value: str(value)
    exchange.cost_to_precision.side_effect = lambda _symbol, value: str(value)
    exchange.fetch_open_orders.return_value = []
    exchange.fetch_closed_orders.return_value = []
    return exchange


def funding_fill(
    *,
    order_id="funding-order",
    client_order_id=FUNDING_ID,
    filled=10.0,
    cost=12.5,
    fee=0.03,
):
    return {
        "id": order_id,
        "clientOrderId": client_order_id,
        "symbol": "GBP/USD",
        "side": "sell",
        "status": "closed",
        "filled": filled,
        "cost": cost,
        # CCXT's unified fee agrees here, while native fields are authoritative.
        "fee": {"cost": fee, "currency": "USD"},
        "info": {"fee": str(fee), "oflags": "fciq"},
        "lastUpdateTimestamp": 1_700_000_001_000,
    }


def usd_buy_fill(
    *,
    target="BTC/USD",
    order_id="target-order",
    client_order_id=BUY_ID,
    filled=0.00025,
    cost=12.46,
    fee=0.03,
):
    return {
        "id": order_id,
        "clientOrderId": client_order_id,
        "symbol": target,
        "side": "buy",
        "status": "closed",
        "filled": filled,
        "cost": cost,
        # Reproduces CCXT's fcib currency-label issue; native fee is quote-valued.
        "fee": {"cost": fee, "currency": target.split("/")[0]},
        "info": {"fee": str(fee), "oflags": "fcib"},
        "lastUpdateTimestamp": 1_700_000_002_000,
    }


class KrakenClientTests(unittest.TestCase):
    def test_only_exact_native_rejection_responses_are_safe_no_fill(self):
        import ccxt
        accepted = ccxt.InsufficientFunds('kraken {"error":["EOrder:Insufficient funds"]}')
        self.assertEqual(kraken_client._definite_submission_rejection(accepted), "EOrder:Insufficient funds")
        for error in (
            TimeoutError(str(accepted)),
            ccxt.RequestTimeout(str(accepted)),
            ccxt.InvalidOrder('kraken {"error":["EOrder:Duplicate client order id"]}'),
            ccxt.ExchangeError('kraken {"error":["EOrder:Insufficient funds","EService:Unavailable"]}'),
            ccxt.ExchangeError('kraken {"error":["EOrder:Insufficient funds"],"result":{"txid":["O-1"]}}'),
            ccxt.InsufficientFunds("insufficient funds"),
        ):
            with self.subTest(error=error):
                self.assertIsNone(kraken_client._definite_submission_rejection(error))

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_explicit_rejection_after_empty_reconciliation_does_not_lock_forever(self, get_exchange, sleep):
        import ccxt
        exchange = configured_exchange()
        get_exchange.return_value = exchange
        exchange.create_market_buy_order_with_cost.side_effect = ccxt.InsufficientFunds(
            'kraken {"error":["EOrder:Insufficient funds"]}'
        )
        with self.assertRaisesRegex(kraken_client.KrakenOrderNoFill, "rejected.*Insufficient funds"):
            kraken_client.place_market_buy("BTC_GBP", 5.50)
        exchange.create_market_buy_order_with_cost.assert_called_once()
        self.assertGreater(exchange.fetch_closed_orders.call_count, 1)

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_order_evidence_takes_precedence_over_rejection_message(self, get_exchange):
        import ccxt
        exchange = configured_exchange()
        get_exchange.return_value = exchange
        exchange.create_market_buy_order_with_cost.side_effect = ccxt.InsufficientFunds(
            'kraken {"error":["EOrder:Insufficient funds"]}'
        )
        exchange.fetch_closed_orders.side_effect = [[], [terminal_fill()]]
        result = kraken_client.place_market_buy(
            "BTC_GBP", 5.50, client_order_id="dca-1234567890abcd"
        )
        self.assertEqual(result["order_id"], "kraken-order")
        exchange.create_market_buy_order_with_cost.assert_called_once()

    @patch.object(kraken_client.time, "sleep")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_rejection_with_failed_reconciliation_remains_unknown(self, get_exchange, sleep):
        import ccxt
        exchange = configured_exchange()
        get_exchange.return_value = exchange
        exchange.create_market_buy_order_with_cost.side_effect = ccxt.InsufficientFunds(
            'kraken {"error":["EOrder:Insufficient funds"]}'
        )
        exchange.fetch_open_orders.side_effect = [[], TimeoutError(), TimeoutError(), TimeoutError(), TimeoutError()]
        with self.assertRaises(kraken_client.KrakenOrderStateUnknown):
            kraken_client.place_market_buy("BTC_GBP", 5.50)

    def test_market_minimum_helper_is_read_only_and_uses_larger_limit(self):
        exchange = configured_exchange()

        result = kraken_client.get_market_minimum_gbp(
            "BTC_GBP", exchange=exchange
        )

        self.assertEqual(result["pair"], "BTC/GBP")
        self.assertEqual(result["minimum_cost_gbp"], 0.43)
        self.assertEqual(result["effective_minimum_gbp"], 2.5)
        exchange.load_markets.assert_called_once_with()
        exchange.fetch_ticker.assert_called_once_with("BTC/GBP")
        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_market_minimum_does_not_need_ticker_for_cost_only_market(self):
        exchange = configured_exchange()
        exchange.market.return_value = {
            "limits": {"cost": {"min": 5.0}, "amount": {"min": None}}
        }

        result = kraken_client.get_market_minimum_gbp(
            "BTC_GBP", exchange=exchange
        )

        self.assertEqual(result["effective_minimum_gbp"], 5.0)
        exchange.fetch_ticker.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_symbol_accepts_only_supported_gbp_and_usd_pairs(self):
        self.assertEqual(kraken_client.to_kraken_symbol("BTC_GBP"), "BTC/GBP")
        self.assertEqual(kraken_client.to_kraken_symbol("eth/gbp"), "ETH/GBP")
        # Historical incident recovery still needs generic USD-pair support.
        self.assertEqual(kraken_client.to_kraken_symbol("HYPE_USD"), "HYPE/USD")
        self.assertEqual(kraken_client.to_kraken_symbol("sol/usd"), "SOL/USD")

        for foreign_pair in ("BTC_EUR", "BTC_JPY", "BTC_GBPT"):
            with self.subTest(pair=foreign_pair):
                with self.assertRaisesRegex(ValueError, "Only GBP or USD Kraken pairs"):
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

        funding = kraken_client.build_client_order_id(
            "BTC_GBP", "2026-08-04", purpose="funding"
        )
        self.assertNotEqual(first, funding)
        self.assertEqual(
            funding,
            kraken_client.build_client_order_id(
                "btc/gbp", "2026-08-04", purpose="funding"
            ),
        )
        with self.assertRaisesRegex(ValueError, "purpose"):
            kraken_client.build_client_order_id(
                "BTC_GBP", "2026-08-04", purpose="other"
            )

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
            {
                **terminal_fill(),
                "fee": {"cost": 0.000001, "currency": "BTC"},
            },
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
                "oflags": "fcib",
            },
        )
        self.assertEqual(exchange.fetch_order.call_count, 2)
        sleep.assert_called_once_with(1)

        self.assertEqual(result["pair"], "BTC/GBP")
        self.assertEqual(result["quote_currency"], "GBP")
        self.assertEqual(result["order_id"], "kraken-order")
        self.assertEqual(result["cost_gbp"], 5.45)
        self.assertAlmostEqual(result["fee_gbp"], 0.05)
        self.assertEqual(result["gbp_fee_debit"], 0)
        self.assertEqual(result["fee_details"][0]["currency"], "BTC")
        self.assertEqual(result["fee_details"][0]["amount"], 0.000001)
        self.assertAlmostEqual(result["fee_details"][0]["gbp_equivalent"], 0.05)
        self.assertEqual(result["spent_gbp"], 5.45)
        self.assertLessEqual(result["spent_gbp"], 5.50)
        self.assertAlmostEqual(result["received"], 0.000108)
        self.assertAlmostEqual(
            result["market_gbp_price_per_unit"], 5.45 / 0.000109
        )
        self.assertAlmostEqual(
            result["effective_gbp_price_per_unit"], 5.45 / 0.000108
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

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_cost_precision_cannot_round_above_the_gbp_budget(self, get_exchange):
        exchange = configured_exchange()
        exchange.cost_to_precision.return_value = "5.51"
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenPreSubmissionError,
            "above the configured budget",
        ):
            kraken_client.place_market_buy("BTC_GBP", 5.50)

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

    def test_native_fcib_quote_fee_corrects_ccxt_base_currency_mislabel(self):
        order = terminal_fill()
        order["fee"] = {"cost": 0.02, "currency": "BTC"}
        order["info"] = {"fee": "0.02", "oflags": "viqc,fcib"}

        result = kraken_client._normalise_terminal_fill(
            order, "BTC/GBP", "dca-1234567890abcd"
        )

        market_price = order["cost"] / order["filled"]
        expected_base_fee = 0.02 / market_price
        self.assertAlmostEqual(
            result["received"], order["filled"] - expected_base_fee
        )
        self.assertAlmostEqual(result["fee_gbp"], 0.02)
        self.assertEqual(result["fee_details"][0]["currency"], "BTC")
        self.assertAlmostEqual(
            result["fee_details"][0]["amount"], expected_base_fee
        )
        self.assertEqual(result["gbp_fee_debit"], 0)
        self.assertEqual(result["spent_gbp"], order["cost"])

    def test_native_fciq_fee_remains_a_quote_currency_debit(self):
        order = terminal_fill()
        order["fee"] = {"cost": 0.02, "currency": "GBP"}
        order["info"] = {"fee": "0.02", "oflags": "viqc,fciq"}

        result = kraken_client._normalise_terminal_fill(
            order, "BTC/GBP", "dca-1234567890abcd"
        )

        self.assertEqual(result["received"], order["filled"])
        self.assertEqual(result["gbp_fee_debit"], 0.02)
        self.assertEqual(result["spent_gbp"], order["cost"] + 0.02)

    def test_native_conflicting_fee_flags_fail_closed(self):
        order = terminal_fill()
        order["info"] = {"fee": "0.02", "oflags": "fcib,fciq"}

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown, "mutually exclusive"
        ):
            kraken_client._normalise_terminal_fill(
                order, "BTC/GBP", "dca-1234567890abcd"
            )

    def test_terminal_timestamp_prefers_fill_close_time_over_open_time(self):
        order = terminal_fill()
        order["timestamp"] = 1_786_035_540_000
        order["lastUpdateTimestamp"] = 1_786_035_660_000

        result = kraken_client._normalise_terminal_fill(
            order, "BTC/GBP", "dca-1234567890abcd"
        )

        self.assertEqual(result["timestamp"], 1_786_035_660)

    def test_terminal_fill_without_any_timestamp_fails_closed(self):
        order = terminal_fill()
        order.pop("timestamp")
        order.pop("lastUpdateTimestamp")

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown, "no terminal timestamp"
        ):
            kraken_client._normalise_terminal_fill(
                order, "BTC/GBP", "dca-1234567890abcd"
            )

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

    def test_usd_market_minimum_is_converted_to_gbp_with_funding_buffer(self):
        exchange = configured_usd_exchange()

        result = kraken_client.get_market_minimum_gbp(
            "BTC_USD", exchange=exchange
        )

        self.assertEqual(result["pair"], "BTC/USD")
        self.assertEqual(result["quote_currency"], "USD")
        self.assertEqual(result["effective_minimum_quote"], 2.5)
        self.assertEqual(result["gbp_usd_rate"], 1.25)
        self.assertEqual(result["funding_minimum_gbp"], 1.0)
        self.assertAlmostEqual(
            result["effective_minimum_gbp"], 2.5 / (1.25 * 0.99)
        )
        exchange.create_market_sell_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_direct_gbp_buy_rejects_usd_target(self, get_exchange):
        exchange = configured_usd_exchange()
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenPreSubmissionError,
            "place_gbp_funded_market_buy",
        ):
            kraken_client.place_market_buy("BTC_USD", 10)

        exchange.fetch_open_orders.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "_submission_deadline", return_value="deadline")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_gbp_funded_buy_executes_exact_two_leg_flow(
        self, get_exchange, _deadline
    ):
        exchange = configured_usd_exchange()
        exchange.create_market_sell_order.return_value = funding_fill()
        exchange.create_market_buy_order_with_cost.return_value = usd_buy_fill()
        get_exchange.return_value = exchange
        final_check = MagicMock()

        result = kraken_client.place_gbp_funded_market_buy(
            "BTC_USD",
            10,
            BUY_ID,
            FUNDING_ID,
            pre_submit_check=final_check,
        )

        exchange.create_market_sell_order.assert_called_once_with(
            "GBP/USD",
            10.0,
            {
                "clientOrderId": FUNDING_ID,
                "deadline": "deadline",
                "oflags": "fciq",
            },
        )
        exchange.create_market_buy_order_with_cost.assert_called_once_with(
            "BTC/USD",
            12.47,
            {
                "clientOrderId": BUY_ID,
                "deadline": "deadline",
                "oflags": "fcib",
            },
        )
        self.assertEqual(final_check.call_count, 2)
        self.assertEqual(result["funding_order_id"], "funding-order")
        self.assertEqual(result["order_id"], "target-order")
        self.assertEqual(result["spent_gbp"], 10)
        self.assertAlmostEqual(result["cost_gbp"], 12.46 / 1.25)
        self.assertEqual(result["gbp_fee_debit"], 0)
        self.assertEqual(result["funded_usd"], 12.47)
        self.assertEqual(result["usd_received"], 12.47)
        self.assertEqual(result["cost_usd"], 12.46)
        self.assertEqual(result["funding_fee_usd"], 0.03)
        self.assertEqual(result["crypto_fee_usd"], 0.03)
        self.assertEqual(result["fee_usd"], 0.03)
        self.assertEqual(result["combined_fee_usd"], 0.06)
        self.assertEqual(result["usd_fee_debit"], 0)
        self.assertAlmostEqual(result["unused_usd"], 0.01)
        self.assertEqual(result["gbp_usd_rate"], 1.25)
        self.assertAlmostEqual(result["fee_gbp"], 0.06 / 1.25)
        self.assertEqual(
            {detail["leg"] for detail in result["fee_details"]},
            {"funding", "buy"},
        )
        self.assertLess(result["received"], 0.00025)

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_gbp_funded_buy_checks_free_gbp_before_funding(self, get_exchange):
        exchange = configured_usd_exchange()
        exchange.fetch_balance.return_value = {"free": {"GBP": 9.99}}
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenPreSubmissionError, "Insufficient free GBP"
        ):
            kraken_client.place_gbp_funded_market_buy(
                "BTC_USD", 10, BUY_ID, FUNDING_ID
            )

        exchange.create_market_sell_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_gbp_funded_buy_requires_exact_gbp_precision(self, get_exchange):
        exchange = configured_usd_exchange()
        exchange.amount_to_precision.side_effect = None
        exchange.amount_to_precision.return_value = "9.99"
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenPreSubmissionError,
            "cannot represent the exact",
        ):
            kraken_client.place_gbp_funded_market_buy(
                "BTC_USD", 10, BUY_ID, FUNDING_ID
            )

        exchange.create_market_sell_order.assert_not_called()

    @patch.object(kraken_client, "get_market_minimum_gbp")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_net_funding_below_fresh_target_minimum_never_buys(
        self, get_exchange, get_minimum
    ):
        exchange = configured_usd_exchange()
        exchange.create_market_sell_order.return_value = funding_fill()
        get_exchange.return_value = exchange
        get_minimum.side_effect = [
            {"effective_minimum_gbp": 1.0, "effective_minimum_quote": 0.5},
            {"effective_minimum_gbp": 20.0, "effective_minimum_quote": 20.0},
        ]

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown,
            "funding is complete.*below Kraken's current",
        ):
            kraken_client.place_gbp_funded_market_buy(
                "BTC_USD", 10, BUY_ID, FUNDING_ID
            )

        exchange.create_market_sell_order.assert_called_once()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_reconcile_only_never_submits_missing_crypto_leg(self, get_exchange):
        exchange = configured_usd_exchange()
        exchange.fetch_closed_orders.side_effect = lambda symbol, params: (
            [funding_fill()] if symbol == "GBP/USD" else []
        )
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown,
            "funding order is confirmed.*will not submit",
        ):
            kraken_client.place_gbp_funded_market_buy(
                "BTC_USD",
                10,
                BUY_ID,
                FUNDING_ID,
                reconcile_only=True,
            )

        exchange.create_market_sell_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_reconciles_both_closed_legs_without_duplicate(self, get_exchange):
        exchange = configured_usd_exchange()
        exchange.fetch_closed_orders.side_effect = lambda symbol, params: (
            [funding_fill()] if symbol == "GBP/USD" else [usd_buy_fill()]
        )
        get_exchange.return_value = exchange

        result = kraken_client.place_gbp_funded_market_buy(
            "BTC_USD",
            10,
            BUY_ID,
            FUNDING_ID,
            reconcile_only=True,
        )

        self.assertEqual(result["order_id"], "target-order")
        exchange.fetch_balance.assert_not_called()
        exchange.create_market_sell_order.assert_not_called()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    @patch.object(kraken_client, "_submission_deadline", return_value="deadline")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_ambiguous_funding_submission_reconciles_without_duplicate_sell(
        self, get_exchange, _deadline
    ):
        exchange = configured_usd_exchange()
        funding_closed_calls = 0

        def closed_orders(symbol, params):
            nonlocal funding_closed_calls
            if symbol != "GBP/USD":
                return []
            funding_closed_calls += 1
            return [] if funding_closed_calls == 1 else [funding_fill()]

        exchange.fetch_closed_orders.side_effect = closed_orders
        exchange.create_market_sell_order.side_effect = TimeoutError("lost")
        exchange.create_market_buy_order_with_cost.return_value = usd_buy_fill()
        get_exchange.return_value = exchange

        result = kraken_client.place_gbp_funded_market_buy(
            "BTC_USD", 10, BUY_ID, FUNDING_ID
        )

        self.assertEqual(result["order_id"], "target-order")
        exchange.create_market_sell_order.assert_called_once()
        exchange.create_market_buy_order_with_cost.assert_called_once()

    @patch.object(kraken_client, "_submission_deadline", return_value="deadline")
    @patch.object(kraken_client, "get_kraken_exchange")
    def test_ambiguous_crypto_submission_reconciles_without_second_buy(
        self, get_exchange, _deadline
    ):
        exchange = configured_usd_exchange()
        target_closed_calls = 0

        def closed_orders(symbol, params):
            nonlocal target_closed_calls
            if symbol == "GBP/USD":
                return []
            target_closed_calls += 1
            return [usd_buy_fill()] if target_closed_calls >= 3 else []

        exchange.fetch_closed_orders.side_effect = closed_orders
        exchange.create_market_sell_order.return_value = funding_fill()
        exchange.create_market_buy_order_with_cost.side_effect = TimeoutError("lost")
        get_exchange.return_value = exchange

        result = kraken_client.place_gbp_funded_market_buy(
            "BTC_USD", 10, BUY_ID, FUNDING_ID
        )

        self.assertEqual(result["order_id"], "target-order")
        exchange.create_market_buy_order_with_cost.assert_called_once()

    @patch.object(kraken_client, "get_kraken_exchange")
    def test_nonexact_funding_fill_retains_intent_and_never_buys(self, get_exchange):
        exchange = configured_usd_exchange()
        exchange.create_market_sell_order.return_value = funding_fill(filled=9.99)
        get_exchange.return_value = exchange

        with self.assertRaisesRegex(
            kraken_client.KrakenOrderStateUnknown, "exact configured GBP budget"
        ):
            kraken_client.place_gbp_funded_market_buy(
                "BTC_USD", 10, BUY_ID, FUNDING_ID
            )

        exchange.create_market_sell_order.assert_called_once()
        exchange.create_market_buy_order_with_cost.assert_not_called()

    def test_usd_fee_normalisation_uses_pair_quote_currency(self):
        sell = kraken_client._normalise_terminal_fill(
            funding_fill(),
            "GBP/USD",
            FUNDING_ID,
            expected_side="sell",
        )
        buy = kraken_client._normalise_terminal_fill(
            usd_buy_fill(),
            "BTC/GBP",
            BUY_ID,
        )

        self.assertEqual(sell["received"], 12.47)
        self.assertEqual(sell["spent"], 10)
        self.assertEqual(sell["quote_fee_debit"], 0.03)
        self.assertEqual(sell["fee_details"][0]["currency"], "USD")
        self.assertEqual(buy["quote_fee_debit"], 0)
        self.assertEqual(buy["fee_quote"], 0.03)
        self.assertEqual(buy["fee_details"][0]["currency"], "BTC")


if __name__ == "__main__":
    unittest.main()

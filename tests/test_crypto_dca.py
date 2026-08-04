import json
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import crypto_dca
import portfolio_logger


def target_rule(*, amount=25, enabled=True, target_time="07:00", dynamic=None):
    rule = {
        "TIME": target_time,
        "AMOUNT_GBP": amount,
        "BUY_ENABLED": enabled,
    }
    if dynamic is not None:
        rule["DYNAMIC_DCA"] = dynamic
    return rule


def pending_intent(*, trade_date="2026-08-04", amount=25):
    return {
        "client_order_id": "dca-1234567890abcd",
        "trade_date": trade_date,
        "amount_gbp": float(amount),
    }


class DcaConfigurationTests(unittest.TestCase):
    def test_reads_explicit_gbp_rules_and_separate_execution_state(self):
        target_map = {"BTC_GBP": target_rule()}
        state = {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-03"}}

        config = crypto_dca.get_config_for_symbol("BTC_GBP", target_map, state)

        self.assertEqual(config["AMOUNT_GBP"], 25.0)
        self.assertEqual(config["LAST_BUY_DATE"], "2026-08-03")
        self.assertEqual(config["KEY"], "BTC_GBP")
        self.assertTrue(config["BUY_ENABLED"])

    def test_rejects_non_gbp_pair_key(self):
        with self.assertRaisesRegex(ValueError, "Kraken GBP key"):
            crypto_dca.get_config_for_symbol(
                "BTC_EUR", {"BTC_EUR": target_rule(enabled=False)}
            )

    def test_rejects_unsupported_amount_field(self):
        target_map = {
            "BTC_GBP": {
                "TIME": "07:00",
                "AMOUNT": 25,
                "BUY_ENABLED": False,
            }
        }
        with self.assertRaisesRegex(ValueError, "unsupported AMOUNT"):
            crypto_dca.get_config_for_symbol("BTC_GBP", target_map)

    def test_rejects_execution_fields_in_rules(self):
        for field in ("LAST_BUY_DATE", "PENDING_ORDER"):
            with self.subTest(field=field):
                rules = {"BTC_GBP": {**target_rule(), field: {}}}
                with self.assertRaisesRegex(ValueError, "execution-only"):
                    crypto_dca.get_config_for_symbol("BTC_GBP", rules)

    def test_disabled_target_may_use_zero_until_user_sets_budget(self):
        config = crypto_dca.get_config_for_symbol(
            "BTC_GBP", {"BTC_GBP": target_rule(amount=0, enabled=False)}
        )
        self.assertEqual(config["AMOUNT_GBP"], 0)

    def test_enabled_target_requires_positive_gbp_amount(self):
        with self.assertRaisesRegex(ValueError, "between GBP 5 and GBP 1000"):
            crypto_dca.get_config_for_symbol(
                "BTC_GBP", {"BTC_GBP": target_rule(amount=0)}
            )

    def test_rejects_numeric_string_amount(self):
        with self.assertRaisesRegex(ValueError, "JSON number"):
            crypto_dca.get_config_for_symbol(
                "BTC_GBP", {"BTC_GBP": target_rule(amount="25")}
            )

    def test_rejects_missing_or_invalid_time(self):
        missing = {"AMOUNT_GBP": 25, "BUY_ENABLED": True}
        with self.assertRaisesRegex(ValueError, "must define TIME"):
            crypto_dca.get_config_for_symbol("BTC_GBP", {"BTC_GBP": missing})
        with self.assertRaisesRegex(ValueError, "24-hour HH:MM"):
            crypto_dca.get_config_for_symbol(
                "BTC_GBP", {"BTC_GBP": target_rule(target_time="25:00")}
            )

    def test_rejects_enabled_amount_above_hard_limit(self):
        with self.assertRaisesRegex(ValueError, "GBP 1000"):
            crypto_dca.get_config_for_symbol(
                "BTC_GBP", {"BTC_GBP": target_rule(amount=1000.01)}
            )

    def test_rejects_invalid_last_buy_date_in_execution_state(self):
        state = {"BTC_GBP": {"LAST_BUY_DATE": "2026-02-30"}}
        with self.assertRaisesRegex(ValueError, "valid YYYY-MM-DD"):
            crypto_dca.get_config_for_symbol(
                "BTC_GBP", {"BTC_GBP": target_rule()}, state
            )


class DcaGhostfolioBoundaryTests(unittest.TestCase):
    def test_roi_at_threshold_uses_half_configured_gbp_amount(self):
        with (
            patch.object(
                crypto_dca, "get_ghostfolio_account_id", return_value="btc-account"
            ),
            patch.object(portfolio_logger, "get_asset_roi_percent", return_value=-2.0),
        ):
            decision = crypto_dca.determine_dynamic_dca_decision(
                "BTC_GBP",
                80,
                {
                    "ENABLED": True,
                    "THRESHOLD_PERCENT": -2,
                    "REDUCED_MULTIPLIER": 0.5,
                },
            )

        self.assertEqual(decision["amount_gbp"], 40)
        self.assertEqual(decision["multiplier"], 0.5)
        self.assertEqual(decision["roi_percent"], -2.0)
        self.assertIn("Half buy", decision["reason"])

    def test_roi_below_threshold_uses_full_configured_gbp_amount(self):
        with (
            patch.object(
                crypto_dca, "get_ghostfolio_account_id", return_value="btc-account"
            ),
            patch.object(
                portfolio_logger, "get_asset_roi_percent", return_value=-15.43
            ),
        ):
            decision = crypto_dca.determine_dynamic_dca_decision(
                "BTC_GBP", 80, {"ENABLED": True}
            )

        self.assertEqual(decision["amount_gbp"], 80)
        self.assertEqual(decision["multiplier"], 1.0)
        self.assertEqual(decision["roi_percent"], -15.43)
        self.assertIn("below -2.00%", decision["reason"])

    def test_unavailable_roi_uses_full_configured_gbp_amount(self):
        with (
            patch.object(
                crypto_dca, "get_ghostfolio_account_id", return_value="btc-account"
            ),
            patch.object(portfolio_logger, "get_asset_roi_percent", return_value=None),
        ):
            decision = crypto_dca.determine_dynamic_dca_decision(
                "BTC_GBP", 80, {"ENABLED": True}
            )

        self.assertEqual(decision["amount_gbp"], 80)
        self.assertEqual(decision["multiplier"], 1.0)
        self.assertIsNone(decision["roi_percent"])
        self.assertIn("ROI is unavailable", decision["reason"])

    def test_main_executes_the_gbp_dynamic_dca_decision(self):
        decision = {
            "amount_gbp": 40,
            "multiplier": 0.5,
            "roi_percent": 1.25,
            "reason": "Half buy (x0.5): asset ROI is above threshold.",
        }
        target_map = {
            "BTC_GBP": target_rule(
                amount=80, dynamic={"ENABLED": True}
            )
        }

        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(target_map)),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "is_time_to_trade", return_value=True),
            patch.object(
                crypto_dca, "determine_dynamic_dca_decision", return_value=decision
            ) as determine_decision,
            patch.object(crypto_dca, "execute_trade") as execute_trade,
        ):
            result = crypto_dca.main()

        self.assertTrue(result)
        determine_decision.assert_called_once_with(
            "BTC_GBP", 80.0, {"ENABLED": True}
        )
        execute_trade.assert_called_once_with(
            "BTC_GBP",
            40,
            map_key="BTC_GBP",
            target_map=target_map,
            dca_decision=decision,
            expected_config=crypto_dca.get_config_for_symbol(
                "BTC_GBP", target_map, {}
            ),
        )


class DcaExecutionTests(unittest.TestCase):
    def setUp(self):
        self.today = datetime.now(crypto_dca.SELECTED_TZ).strftime("%Y-%m-%d")
        self.intent = pending_intent(trade_date=self.today)
        self.target_map = {"BTC_GBP": target_rule()}
        self.expected_config = crypto_dca.get_config_for_symbol(
            "BTC_GBP", self.target_map, {}
        )
        execution_timestamp = int(
            datetime(2026, 7, 13, 17, 0, 39, tzinfo=timezone.utc).timestamp()
        )
        self.order_data = {
            "order_id": "kraken-order-id",
            "pair": "BTC/GBP",
            "quote_currency": "GBP",
            "cost_gbp": 24.90,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.05,
            "fee_details": [
                {"currency": "GBP", "amount": 0.05, "gbp_equivalent": 0.05}
            ],
            "spent_gbp": 24.95,
            "received": 0.0005,
            "market_gbp_price_per_unit": 49_800.0,
            "gbp_price_per_unit": 49_800.0,
            "effective_gbp_price_per_unit": 49_900.0,
            "timestamp": execution_timestamp,
        }

    def _new_intent_patches(self):
        return (
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            patch.object(
                crypto_dca,
                "_revalidate_trade_intent",
                return_value=(self.target_map, {}),
            ),
            patch.object(
                crypto_dca,
                "build_client_order_id",
                return_value=self.intent["client_order_id"],
            ),
            patch.object(
                crypto_dca,
                "prepare_order_intent",
                return_value=(self.intent, False),
            ),
        )

    def test_confirmed_kraken_fill_completes_state_and_logs_gbp(self):
        fetch_state, revalidate, build_id, prepare = self._new_intent_patches()

        def place_order(symbol, amount, **kwargs):
            self.assertEqual((symbol, amount), ("BTC_GBP", 25.0))
            self.assertFalse(kwargs["reconcile_only"])
            kwargs["pre_submit_check"]()
            return self.order_data

        with (
            fetch_state,
            revalidate as revalidate_trade,
            build_id,
            prepare,
            patch.object(crypto_dca, "place_market_buy", side_effect=place_order),
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(
                crypto_dca, "get_ghostfolio_account_id", return_value="btc-account"
            ),
            patch.object(crypto_dca, "update_gist_log") as update_gist_log,
            patch.object(crypto_dca, "send_discord_alert") as send_discord_alert,
            patch.object(
                portfolio_logger, "log_to_ghostfolio", return_value=True
            ) as log_to_ghostfolio,
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                25,
                map_key="BTC_GBP",
                target_map=self.target_map,
                dca_decision={
                    "amount_gbp": 25,
                    "multiplier": 1,
                    "roi_percent": -1.75,
                    "reason": "Full buy (x1).",
                },
                expected_config=self.expected_config,
            )

        self.assertTrue(succeeded)
        self.assertEqual(revalidate_trade.call_count, 3)
        complete.assert_called_once_with(
            "BTC_GBP", self.intent["client_order_id"], self.today
        )
        clear.assert_not_called()
        expected_trade_data = {
            "ts": self.order_data["timestamp"],
            "amount_crypto": 0.0005,
            "amount_gbp": 24.95,
            "cost_gbp": 24.90,
            "fee_gbp": 0.05,
            "gbp_fee_debit": 0.05,
            "fee_details": self.order_data["fee_details"],
            "order_id": "kraken-order-id",
            "gbp_price_per_unit": 49_800.0,
            "effective_gbp_price_per_unit": 49_900.0,
            "exchange_pair": "BTC/GBP",
        }
        log_to_ghostfolio.assert_called_once_with(
            expected_trade_data,
            "BTC",
            "btc-account",
            exchange_pair="BTC/GBP",
        )
        update_gist_log.assert_called_once_with(
            expected_trade_data, symbol="BTC", saved_to_ghostfolio=True
        )
        success_message = send_discord_alert.call_args.args[0]
        self.assertIn("**Order cost:** £24.90", success_message)
        self.assertIn("**Kraken fee (GBP equivalent):** £0.05", success_message)
        self.assertIn("**Fee charged from GBP:** £0.05", success_message)
        self.assertIn("**Total GBP debit:** £24.95", success_message)
        self.assertNotIn("USD", success_message)

    def test_known_safe_order_failure_clears_intent_without_completion(self):
        fetch_state, revalidate, build_id, prepare = self._new_intent_patches()
        with (
            fetch_state,
            revalidate,
            build_id,
            prepare,
            patch.object(
                crypto_dca,
                "place_market_buy",
                side_effect=crypto_dca.KrakenPreSubmissionError("rejected"),
            ),
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "update_gist_log") as update_gist_log,
            patch.object(crypto_dca, "send_discord_alert") as send_discord_alert,
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                25,
                map_key="BTC_GBP",
                target_map=self.target_map,
                expected_config=self.expected_config,
            )

        self.assertFalse(succeeded)
        clear.assert_called_once_with("BTC_GBP", self.intent["client_order_id"])
        complete.assert_not_called()
        update_gist_log.assert_not_called()
        self.assertTrue(send_discord_alert.call_args.kwargs["is_error"])

    def test_unexpected_failure_retains_durable_intent(self):
        fetch_state, revalidate, build_id, prepare = self._new_intent_patches()
        with (
            fetch_state,
            revalidate,
            build_id,
            prepare,
            patch.object(
                crypto_dca,
                "place_market_buy",
                side_effect=RuntimeError("unexpected malformed response"),
            ),
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                25,
                map_key="BTC_GBP",
                target_map=self.target_map,
                expected_config=self.expected_config,
            )

        self.assertFalse(succeeded)
        clear.assert_not_called()
        complete.assert_not_called()
        self.assertIn("remains locked", alert.call_args.args[0])

    def test_unknown_existing_intent_stays_locked_across_date_rollover(self):
        old_intent = pending_intent(trade_date="2026-08-03", amount=30)
        state = {
            "BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": old_intent}
        }
        with (
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(
                crypto_dca,
                "place_market_buy",
                side_effect=crypto_dca.KrakenOrderStateUnknown("not visible yet"),
            ) as place,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP", 25, map_key="BTC_GBP", target_map=self.target_map
            )

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        clear.assert_not_called()
        complete.assert_not_called()
        self.assertEqual(place.call_args.args, ("BTC_GBP", 30.0))
        self.assertTrue(place.call_args.kwargs["reconcile_only"])

    def test_recovered_fill_completes_the_original_trade_date(self):
        old_intent = pending_intent(trade_date="2026-08-03")
        state = {"BTC_GBP": {"PENDING_ORDER": old_intent}}
        with (
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            patch.object(
                crypto_dca, "place_market_buy", return_value=self.order_data
            ) as place,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "get_ghostfolio_account_id", return_value=None),
            patch.object(crypto_dca, "update_gist_log"),
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP", 25, map_key="BTC_GBP", target_map=self.target_map
            )

        self.assertTrue(succeeded)
        self.assertTrue(place.call_args.kwargs["reconcile_only"])
        complete.assert_called_once_with(
            "BTC_GBP", old_intent["client_order_id"], "2026-08-03"
        )

    def test_live_disable_blocks_order_before_intent_is_persisted(self):
        disabled_map = {
            "BTC_GBP": target_rule(amount=25, enabled=False)
        }
        with (
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            patch.object(crypto_dca, "fetch_live_target_map", return_value=disabled_map),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(crypto_dca, "place_market_buy") as place,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                25,
                map_key="BTC_GBP",
                target_map=self.target_map,
                expected_config=self.expected_config,
            )

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        place.assert_not_called()

    def test_prepare_order_intent_creates_trader_owned_state(self):
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=({}, False)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            intent, existed = crypto_dca.prepare_order_intent(
                "BTC_GBP", self.intent["client_order_id"], self.today, 25
            )

        self.assertFalse(existed)
        self.assertEqual(intent, self.intent)
        write.assert_called_once_with(
            crypto_dca.EXECUTION_STATE_VARIABLE,
            {
                "BTC_GBP": {
                    "LAST_BUY_DATE": "",
                    "PENDING_ORDER": self.intent,
                }
            },
            exists=False,
        )

    def test_completion_preserves_other_execution_state(self):
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": dict(self.intent),
            },
            "ETH_GBP": {"LAST_BUY_DATE": "2026-08-02"},
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            crypto_dca.complete_order_intent(
                "BTC_GBP", self.intent["client_order_id"], self.today
            )

        written = write.call_args.args[1]
        self.assertEqual(written["BTC_GBP"], {"LAST_BUY_DATE": self.today})
        self.assertEqual(
            written["ETH_GBP"], {"LAST_BUY_DATE": "2026-08-02"}
        )

    def test_main_returns_failure_when_a_due_trade_fails(self):
        with (
            patch.object(
                crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(self.target_map)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "is_time_to_trade", return_value=True),
            patch.object(
                crypto_dca,
                "determine_dynamic_dca_decision",
                return_value={
                    "amount_gbp": 25,
                    "multiplier": 1,
                    "roi_percent": None,
                    "reason": "Full buy",
                },
            ),
            patch.object(crypto_dca, "execute_trade", return_value=False),
        ):
            result = crypto_dca.main()

        self.assertFalse(result)

    def test_main_reconciles_pending_intent_even_when_rule_is_disabled(self):
        target_map = {"BTC_GBP": target_rule(amount=0, enabled=False)}
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": dict(self.intent),
            }
        }
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(target_map)),
            patch.object(
                crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)
            ),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            result = crypto_dca.main()

        self.assertTrue(result)
        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], ("BTC_GBP", 25.0))
        self.assertIsNone(execute.call_args.kwargs["expected_config"])


if __name__ == "__main__":
    unittest.main()

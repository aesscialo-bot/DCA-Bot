import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import crypto_dca
from dca_config import (
    amount_tier_for_regime,
    default_rules_map,
    empty_analysis_state,
    rules_hash,
)


NOW = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)


def rules_with(*enabled_targets, low=10, up=20):
    rules = default_rules_map()
    for target in enabled_targets:
        rules[target] = {
            "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": up},
            "BUY_ENABLED": True,
        }
    return rules


def ready_decision(target, rule, *, now=NOW, regime="UPTREND", offset=0):
    execute_at = now + timedelta(minutes=offset)
    analyzed_at = min(now, execute_at - timedelta(minutes=30))
    tier = amount_tier_for_regime(regime)
    return {
        "STATUS": "READY",
        "REGIME": regime,
        "AMOUNT_TIER": tier,
        "EXECUTE_AT": execute_at.isoformat().replace("+00:00", "Z"),
        "VALID_UNTIL": (execute_at + timedelta(minutes=60))
        .isoformat()
        .replace("+00:00", "Z"),
        "DECISION_ID": f"decision-{target.lower()}-{int(execute_at.timestamp())}",
        "RULES_HASH": rules_hash(target, rule),
        "SIGNALS": {"SOURCE": "completed Kraken candles"},
        "TIMING": {
            "ANALYZED_AT": analyzed_at.isoformat().replace("+00:00", "Z")
        },
    }


def analysis_for(rules, decisions):
    state = empty_analysis_state(rules, now=NOW)
    state["TARGETS"].update(decisions)
    return state


def pending_intent(target="BTC_USD", *, amount=20, trade_date="2026-08-05"):
    return {
        "client_order_id": "dca-1234567890abcd",
        "funding_client_order_id": "dca-fedcba09876543",
        "trade_date": trade_date,
        "amount_gbp": float(amount),
        "decision_id": f"decision-{target.lower()}",
        "created_at": "2026-08-05T04:59:00Z",
    }


class DcaConfigurationTests(unittest.TestCase):
    def test_reads_final_rule_and_separate_execution_state(self):
        rules = rules_with("BTC_USD")
        state = {"BTC_USD": {"LAST_BUY_DATE": "2026-08-04"}}

        config = crypto_dca.get_config_for_symbol("BTC_USD", rules, state)

        self.assertEqual(config["REGIME_AMOUNTS_GBP"], {"LOW": 10, "UP": 20})
        self.assertEqual(config["LAST_BUY_DATE"], "2026-08-04")
        self.assertTrue(config["BUY_ENABLED"])

    def test_rejects_legacy_time_amount_and_dynamic_fields(self):
        for legacy in (
            {"TIME": "07:00", "AMOUNT_GBP": 10, "BUY_ENABLED": False},
            {
                "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
                "BUY_ENABLED": False,
                "DYNAMIC_DCA": {"ENABLED": True},
            },
        ):
            with self.subTest(legacy=legacy), self.assertRaises(ValueError):
                crypto_dca.get_config_for_symbol("BTC_USD", {"BTC_USD": legacy})

    def test_pending_intent_requires_originating_decision_id(self):
        pending = pending_intent()
        pending.pop("decision_id")
        with self.assertRaisesRegex(ValueError, "decision_id"):
            crypto_dca._normalise_pending_order(pending, "BTC_USD")

    def test_optional_symbol_filter_is_strict_and_deduplicated(self):
        self.assertEqual(
            crypto_dca._parse_symbol_filter('["BTC/USD", "BTC_USD", "HYPE_USD"]'),
            ("BTC_USD", "HYPE_USD"),
        )
        self.assertEqual(crypto_dca._parse_symbol_filter("[]"), ())
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            crypto_dca._parse_symbol_filter('["ETH_USD"]')

    def test_pending_intent_requires_distinct_funding_order_id(self):
        pending = pending_intent()
        pending["funding_client_order_id"] = pending["client_order_id"]

        with self.assertRaisesRegex(ValueError, "funding client order ID"):
            crypto_dca._normalise_pending_order(pending, "BTC_USD")


class DecisionGateTests(unittest.TestCase):
    def test_start_date_requires_same_local_day_analysis(self):
        rules = rules_with("BTC_USD", low=10, up=20)
        execute_at = datetime(2026, 8, 6, 21, 30, tzinfo=timezone.utc)
        decision = ready_decision("BTC_USD", rules["BTC_USD"])
        decision["EXECUTE_AT"] = execute_at.isoformat().replace("+00:00", "Z")
        decision["VALID_UNTIL"] = (
            execute_at + timedelta(minutes=60)
        ).isoformat().replace("+00:00", "Z")

        with patch.object(crypto_dca, "DCA_START_DATE", "2026-08-07"):
            status, reason, _amount = crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, execute_at
            )
            self.assertEqual(status, "ERROR")
            self.assertIn("predates", reason)

            decision["TIMING"]["ANALYZED_AT"] = "2026-08-06T21:00:00Z"
            status, _reason, amount = crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, execute_at
            )
            self.assertEqual((status, amount), ("READY", 10.0))

    def test_uptrend_uses_lower_budget(self):
        rules = rules_with("BTC_USD", low=10, up=20)
        decision = ready_decision("BTC_USD", rules["BTC_USD"])

        status, _reason, amount = crypto_dca._decision_gate(
            "BTC_USD", rules["BTC_USD"], decision, NOW
        )

        self.assertEqual((status, amount), ("READY", 10.0))

    def test_sideways_uses_midpoint_and_downtrend_uses_higher_budget(self):
        rules = rules_with("BTC_USD", low=10, up=20)
        expected = {"SIDEWAYS": 15.0, "DOWNTREND": 20.0}
        for regime, expected_amount in expected.items():
            decision = ready_decision(
                "BTC_USD", rules["BTC_USD"], regime=regime
            )
            with self.subTest(regime=regime):
                status, _reason, amount = crypto_dca._decision_gate(
                    "BTC_USD", rules["BTC_USD"], decision, NOW
                )
                self.assertEqual((status, amount), ("READY", expected_amount))

    def test_window_is_five_minutes_early_through_sixty_minutes_late(self):
        rules = rules_with("BTC_USD")
        decision = ready_decision(
            "BTC_USD", rules["BTC_USD"], now=NOW, offset=30
        )
        before = NOW + timedelta(minutes=24, seconds=59)
        opens = NOW + timedelta(minutes=25)
        closes = NOW + timedelta(minutes=90)
        after = closes + timedelta(seconds=1)

        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, before
            )[0],
            "NOT_DUE",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, opens
            )[0],
            "READY",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, closes
            )[0],
            "READY",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, after
            )[0],
            "MISSED",
        )

    def test_error_and_rules_hash_mismatch_fail_closed(self):
        rules = rules_with("BTC_USD")
        error_decision = empty_analysis_state(rules, now=NOW)["TARGETS"]["BTC_USD"]
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], error_decision, NOW
            )[0],
            "ERROR",
        )

        decision = ready_decision("BTC_USD", rules["BTC_USD"])
        decision["RULES_HASH"] = "0" * 64
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_USD", rules["BTC_USD"], decision, NOW
            )[0],
            "ERROR",
        )


class DurableIntentTests(unittest.TestCase):
    def test_prepare_persists_decision_bound_intent(self):
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=({}, False)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
        ):
            intent, existed = crypto_dca.prepare_order_intent(
                "BTC_USD",
                "dca-1234567890abcd",
                "dca-fedcba09876543",
                "2026-08-05",
                20,
                "decision-btc",
            )

        self.assertFalse(existed)
        self.assertEqual(intent["decision_id"], "decision-btc")
        self.assertEqual(intent["amount_gbp"], 20.0)
        self.assertEqual(intent["funding_client_order_id"], "dca-fedcba09876543")
        written = write.call_args.args[1]
        self.assertEqual(
            written["BTC_USD"]["PENDING_ORDER"]["decision_id"], "decision-btc"
        )

    def test_prepare_rejects_same_day_duplicate_without_writing(self):
        state = {"BTC_USD": {"LAST_BUY_DATE": "2026-08-05"}}
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            self.assertRaisesRegex(RuntimeError, "already marked"),
        ):
            crypto_dca.prepare_order_intent(
                "BTC_USD",
                "dca-1234567890abcd",
                "dca-fedcba09876543",
                "2026-08-05",
                20,
                "decision-btc",
            )
        write.assert_not_called()

    def test_completion_preserves_other_asset_state(self):
        intent = pending_intent()
        state = {
            "BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": intent},
            "HYPE_USD": {"LAST_BUY_DATE": "2026-08-04"},
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            crypto_dca.complete_order_intent(
                "BTC_USD",
                intent["client_order_id"],
                intent["trade_date"],
                intent["decision_id"],
            )

        written = write.call_args.args[1]
        self.assertEqual(written["BTC_USD"], {"LAST_BUY_DATE": "2026-08-05"})
        self.assertEqual(written["HYPE_USD"], {"LAST_BUY_DATE": "2026-08-04"})


class LiveRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.rules = rules_with("BTC_USD")
        self.decision = ready_decision("BTC_USD", self.rules["BTC_USD"])
        self.analysis = analysis_for(self.rules, {"BTC_USD": self.decision})

    def test_revalidation_rejects_live_budget_change(self):
        changed = rules_with("BTC_USD", low=11, up=21)
        changed_decision = ready_decision("BTC_USD", changed["BTC_USD"])
        changed_analysis = analysis_for(changed, {"BTC_USD": changed_decision})
        with (
            patch.object(crypto_dca, "fetch_live_target_map", return_value=changed),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=changed_analysis
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "budgets or enable state changed"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_blocks_new_order_before_local_start_date(self):
        with (
            patch.object(crypto_dca, "DCA_START_DATE", "2026-08-06"),
            patch.object(crypto_dca, "fetch_live_target_map") as fetch_rules,
            self.assertRaisesRegex(
                crypto_dca.KrakenPreSubmissionError,
                "Automated trading starts on 2026-08-06",
            ),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

        fetch_rules.assert_not_called()

    def test_revalidation_rejects_live_disable_before_add_order(self):
        disabled = json.loads(json.dumps(self.rules))
        disabled["BTC_USD"]["BUY_ENABLED"] = False
        with (
            patch.object(crypto_dca, "fetch_live_target_map", return_value=disabled),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=self.analysis
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "disabled before order submission"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_new_analysis_decision(self):
        newer = dict(self.decision)
        newer["DECISION_ID"] = "replacement-decision"
        newer_analysis = analysis_for(self.rules, {"BTC_USD": newer})
        with (
            patch.object(
                crypto_dca, "fetch_live_target_map", return_value=self.rules
            ),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=newer_analysis
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "analysis decision changed"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_analyzed_at_mutation(self):
        changed = json.loads(json.dumps(self.decision))
        changed["TIMING"]["ANALYZED_AT"] = "2026-08-05T04:29:00Z"
        changed_analysis = analysis_for(self.rules, {"BTC_USD": changed})
        with (
            patch.object(crypto_dca, "fetch_live_target_map", return_value=self.rules),
            patch.object(
                crypto_dca,
                "fetch_live_analysis_state",
                return_value=changed_analysis,
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "analysis decision changed"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_duplicate_and_conflicting_pending(self):
        for state, pattern in (
            ({"BTC_USD": {"LAST_BUY_DATE": "2026-08-05"}}, "already marked"),
            (
                {
                    "BTC_USD": {
                        "LAST_BUY_DATE": "",
                        "PENDING_ORDER": pending_intent(),
                    }
                },
                "acquired a pending",
            ),
        ):
            with (
                self.subTest(state=state),
                patch.object(
                    crypto_dca, "fetch_live_target_map", return_value=self.rules
                ),
                patch.object(
                    crypto_dca,
                    "fetch_live_analysis_state",
                    return_value=self.analysis,
                ),
                patch.object(
                    crypto_dca, "fetch_live_execution_state", return_value=state
                ),
                self.assertRaisesRegex(RuntimeError, pattern),
            ):
                crypto_dca._revalidate_trade_intent(
                    "BTC_USD",
                    self.rules["BTC_USD"],
                    self.decision,
                    "2026-08-05",
                    now=NOW,
                )

    def test_revalidation_rejects_pending_from_another_decision(self):
        pending = pending_intent()
        state = {"BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        with (
            patch.object(
                crypto_dca, "fetch_live_target_map", return_value=self.rules
            ),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=self.analysis
            ),
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            self.assertRaisesRegex(RuntimeError, "another decision"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_USD",
                self.rules["BTC_USD"],
                self.decision,
                "2026-08-05",
                expected_pending=pending,
                now=NOW,
            )


class TradeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.rules = rules_with("BTC_USD")
        self.decision = ready_decision("BTC_USD", self.rules["BTC_USD"])
        self.intent = pending_intent()
        self.intent["decision_id"] = self.decision["DECISION_ID"]
        self.order_data = {
            "order_id": "kraken-order-id",
            "funding_order_id": "kraken-funding-order-id",
            "pair": "BTC/USD",
            "quote_currency": "USD",
            "cost_gbp": 19.90,
            "fee_gbp": 0.04,
            "gbp_fee_debit": 0.0,
            "fee_details": [
                {"currency": "USD", "amount": 0.05, "usd_equivalent": 0.05}
            ],
            "spent_gbp": 20.0,
            "funded_usd": 25.5,
            "cost_usd": 25.43,
            "fee_usd": 0.05,
            "funding_fee_usd": 0.02,
            "usd_fee_debit": 0.05,
            "gbp_usd_rate": 1.275,
            "received": 0.0004,
            "market_gbp_price_per_unit": 49_750.0,
            "effective_gbp_price_per_unit": 50_000.0,
            "market_usd_price_per_unit": 63_431.25,
            "effective_usd_price_per_unit": 63_750.0,
            "timestamp": int(NOW.timestamp()),
        }

    def _new_intent_context(self):
        return (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            patch.object(
                crypto_dca,
                "_revalidate_trade_intent",
                return_value=(self.rules, {}, {}, 20.0),
            ),
            patch.object(
                crypto_dca,
                "build_client_order_id",
                side_effect=[
                    self.intent["client_order_id"],
                    self.intent["funding_client_order_id"],
                ],
            ),
            patch.object(
                crypto_dca,
                "prepare_order_intent",
                return_value=(self.intent, False),
            ),
        )

    def test_final_live_revalidation_runs_inside_pre_submit_callback(self):
        now_patch, state_patch, revalidate_patch, id_patch, intent_patch = (
            self._new_intent_context()
        )
        events = []

        def place(_symbol, _amount, **kwargs):
            events.append("place-entered")
            kwargs["pre_submit_check"]()
            events.append("after-check")
            return self.order_data

        with (
            now_patch,
            state_patch,
            revalidate_patch as revalidate,
            id_patch as build_id,
            intent_patch,
            patch.object(
                crypto_dca, "place_gbp_funded_market_buy", side_effect=place
            ) as place_order,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "_post_trade_logs", return_value=False),
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_USD",
                20,
                expected_rule=self.rules["BTC_USD"],
                expected_decision=self.decision,
            )

        self.assertTrue(succeeded)
        self.assertEqual(revalidate.call_count, 3)
        self.assertEqual(events, ["place-entered", "after-check"])
        self.assertEqual(build_id.call_count, 2)
        self.assertEqual(build_id.call_args_list[0].kwargs, {"purpose": "buy"})
        self.assertEqual(build_id.call_args_list[1].kwargs, {"purpose": "funding"})
        self.assertEqual(
            place_order.call_args.kwargs["funding_client_order_id"],
            self.intent["funding_client_order_id"],
        )
        complete.assert_called_once_with(
            "BTC_USD",
            self.intent["client_order_id"],
            self.intent["trade_date"],
            self.intent["decision_id"],
        )

    def test_requested_amount_must_equal_the_decision_tier(self):
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            patch.object(
                crypto_dca,
                "_revalidate_trade_intent",
                return_value=(self.rules, {}, {}, 20.0),
            ),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(crypto_dca, "place_gbp_funded_market_buy") as place,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_USD",
                10,
                expected_rule=self.rules["BTC_USD"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        place.assert_not_called()

    def test_safe_pre_submission_failure_clears_exact_intent(self):
        now_patch, state_patch, revalidate_patch, id_patch, intent_patch = (
            self._new_intent_context()
        )
        with (
            now_patch,
            state_patch,
            revalidate_patch,
            id_patch,
            intent_patch,
            patch.object(
                crypto_dca,
                "place_gbp_funded_market_buy",
                side_effect=crypto_dca.KrakenPreSubmissionError("rules changed"),
            ),
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_USD",
                20,
                expected_rule=self.rules["BTC_USD"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        clear.assert_called_once_with(
            "BTC_USD", self.intent["client_order_id"], self.intent["decision_id"]
        )
        complete.assert_not_called()

    def test_unexpected_failure_retains_durable_intent(self):
        now_patch, state_patch, revalidate_patch, id_patch, intent_patch = (
            self._new_intent_context()
        )
        with (
            now_patch,
            state_patch,
            revalidate_patch,
            id_patch,
            intent_patch,
            patch.object(
                crypto_dca,
                "place_gbp_funded_market_buy",
                side_effect=RuntimeError("ambiguous response"),
            ),
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_USD",
                20,
                expected_rule=self.rules["BTC_USD"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        clear.assert_not_called()
        complete.assert_not_called()
        self.assertIn("remains locked", alert.call_args.args[0])

    def test_pending_recovery_never_creates_a_new_intent(self):
        old = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": old}}
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(
                crypto_dca,
                "place_gbp_funded_market_buy",
                side_effect=crypto_dca.KrakenOrderStateUnknown("not visible"),
            ) as place,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade("BTC_USD", 20)

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        clear.assert_not_called()
        self.assertEqual(place.call_args.args, ("BTC_USD", 10.0))
        self.assertEqual(
            place.call_args.kwargs["funding_client_order_id"],
            old["funding_client_order_id"],
        )
        self.assertTrue(place.call_args.kwargs["reconcile_only"])

    def test_cross_midnight_recovery_records_confirmed_fill_day(self):
        old = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": old}}
        recovered = dict(self.order_data)
        recovered["timestamp"] = int(NOW.timestamp())
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value=state),
            patch.object(crypto_dca, "place_gbp_funded_market_buy", return_value=recovered),
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "_post_trade_logs", return_value=False),
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            self.assertTrue(crypto_dca.execute_trade("BTC_USD", 20))

        complete.assert_called_once_with(
            "BTC_USD",
            old["client_order_id"],
            "2026-08-05",
            old["decision_id"],
        )


class MainSchedulingTests(unittest.TestCase):
    def test_future_start_date_allows_analysis_but_blocks_new_orders(self):
        rules = rules_with("BTC_USD")
        analysis = analysis_for(
            rules, {"BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"])}
        )
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "DCA_START_DATE", "2026-08-06"),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_not_called()

    def test_start_date_is_inclusive_in_bangkok(self):
        rules = rules_with("BTC_USD")
        analysis = analysis_for(
            rules, {"BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"])}
        )
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "DCA_START_DATE", "2026-08-05"),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_called_once()

    def test_invalid_start_date_fails_closed_without_trading(self):
        with (
            patch.object(
                crypto_dca,
                "DCA_TARGET_MAP_JSON",
                json.dumps(default_rules_map()),
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "DCA_START_DATE", "06/08/2026"),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            self.assertFalse(crypto_dca.main())

        execute.assert_not_called()
        self.assertTrue(alert.call_args.kwargs["is_error"])

    def test_pending_recovery_runs_before_invalid_rules_block_new_orders(self):
        pending = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            self.assertFalse(crypto_dca.main())

        execute.assert_called_once_with("BTC_USD", 10.0, map_key="BTC_USD")

    def test_pending_recovery_runs_even_when_analysis_is_invalid(self):
        rules = default_rules_map()
        pending = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_USD": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_called_once_with("BTC_USD", 10.0, map_key="BTC_USD")

    def test_all_disabled_succeeds_even_with_no_analysis_and_never_trades(self):
        rules = default_rules_map()
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "execute_trade") as execute,
        ):
            self.assertTrue(crypto_dca.main())
        execute.assert_not_called()

    def test_same_day_purchase_is_suppressed(self):
        rules = rules_with("BTC_USD")
        analysis = analysis_for(
            rules, {"BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"])}
        )
        state = {"BTC_USD": {"LAST_BUY_DATE": "2026-08-05"}}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(
                crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)
            ),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
        ):
            self.assertTrue(crypto_dca.main())
        execute.assert_not_called()

    def test_multiple_assets_can_share_a_window_and_use_own_tiers(self):
        rules = rules_with("BTC_USD", "HYPE_USD", low=10, up=20)
        analysis = analysis_for(
            rules,
            {
                "BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"]),
                "HYPE_USD": ready_decision(
                    "HYPE_USD", rules["HYPE_USD"], regime="SIDEWAYS"
                ),
            },
        )
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            self.assertTrue(crypto_dca.main())

        self.assertEqual(execute.call_count, 2)
        calls = {call.args[0]: call.args[1] for call in execute.call_args_list}
        self.assertEqual(calls, {"BTC_USD": 10.0, "HYPE_USD": 15.0})

    def test_error_or_missed_decision_alerts_and_never_trades(self):
        rules = rules_with("BTC_USD")
        decision = ready_decision(
            "BTC_USD", rules["BTC_USD"], now=NOW - timedelta(hours=2)
        )
        analysis = analysis_for(rules, {"BTC_USD": decision})
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            self.assertFalse(crypto_dca.main())
        execute.assert_not_called()
        self.assertTrue(alert.call_args.kwargs["is_error"])

    def test_stale_disabled_asset_hash_does_not_block_another_asset(self):
        rules = rules_with("BTC_USD")
        analysis = analysis_for(
            rules, {"BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"])}
        )
        # Simulate an atomic budget edit on disabled ADA after the last analysis.
        rules["SOL_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_called_once()
        self.assertEqual(execute.call_args.args[:2], ("BTC_USD", 10.0))

    def test_explicit_empty_filter_is_a_successful_noop(self):
        rules = rules_with("BTC_USD")
        analysis = analysis_for(
            rules, {"BTC_USD": ready_decision("BTC_USD", rules["BTC_USD"])}
        )
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", "[]"),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
        ):
            self.assertTrue(crypto_dca.main())
        execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

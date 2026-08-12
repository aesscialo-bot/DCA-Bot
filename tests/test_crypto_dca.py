import hashlib
import json
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import crypto_dca
import dca_config
import gist_logger
from dca_config import (
    amount_tier_for_regime,
    default_rules_map,
    empty_analysis_state,
    rules_hash,
)


NOW = datetime(2026, 8, 5, 5, 0, tzinfo=timezone.utc)
crypto_dca.DCA_TRADING_MODE = "live"


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
        "ENABLED": bool(rule["BUY_ENABLED"]),
        "ANALYSIS_STATUS": "READY",
        "EXECUTION_STATUS": "ARMED",
        "REGIME": regime,
        "AMOUNT_TIER": tier,
        "SELECTED_AT": execute_at.isoformat().replace("+00:00", "Z"),
        "EXECUTE_AT": execute_at.isoformat().replace("+00:00", "Z"),
        "VALID_UNTIL": (execute_at + timedelta(minutes=60))
        .isoformat()
        .replace("+00:00", "Z"),
        "DECISION_ID": f"decision-{target.lower()}-{int(execute_at.timestamp())}",
        "RULES_HASH": rules_hash(target, rule),
        "POLICY_VERSION": dca_config.TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": now.astimezone(crypto_dca.SELECTED_TZ).date().isoformat(),
        "CATCHUP_APPLIED": False,
        "HISTORY": {"STATUS": "READY", "HASH": "a" * 64},
        "SIGNALS": {"SOURCE": "completed Kraken candles"},
        "TIMING": {
            "ANALYZED_AT": analyzed_at.isoformat().replace("+00:00", "Z")
        },
        "ERROR": None,
    }


def analysis_for(rules, decisions, *, now=NOW):
    state = empty_analysis_state(rules, now=now)
    for target in crypto_dca.ALLOWED_TARGETS:
        if target not in decisions:
            state["TARGETS"][target] = ready_decision(
                target, rules[target], now=now
            )
    state["TARGETS"].update(decisions)
    return state


def pending_intent(target="BTC_GBP", *, amount=20, trade_date="2026-08-05"):
    return {
        "client_order_id": "dca-1234567890abcd",
        "funding_client_order_id": "dca-fedcba09876543",
        "trade_date": trade_date,
        "amount_gbp": float(amount),
        "decision_id": f"decision-{target.lower()}",
        "created_at": "2026-08-05T04:59:00Z",
    }


def pending_gist_delivery(
    target="BTC_GBP", *, delivery_id="kraken-order-id", quantity="0.00040000"
):
    symbol = target.split("_", maxsplit=1)[0]
    row = (
        f"| 2026-08-05 12:00 +07 | GBP 20.00 | 1.275000 | USD 25.5000 | "
        f"USD 25.4300 | GBP equivalent 0.04 | USD 63,431.2500 | "
        f"{quantity} {symbol} | kraken-funding-order-id | {delivery_id} | "
        "optional/not saved |\n"
    )
    event = {
        "event_version": 3, "event_id": delivery_id,
        "occurred_at": "2026-08-05T05:00:00Z", "target": target,
        "base_currency": symbol, "quote_currency": "GBP", "budget_currency": "GBP",
        "funding_order_id": None, "crypto_order_id": delivery_id,
        "gbp_debit": "20", "gbp_usd_rate": "0", "funded_usd": "0",
        "route": "DIRECT_GBP", "crypto_cost_quote": "20",
        "crypto_quantity": quantity, "unit_price_quote": "50000",
        "funding_fee_quote": "0", "crypto_fee_quote": "0.04",
    }
    event["canonical_hash"] = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    event_hash = hashlib.sha256(
        json.dumps(event, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return {"version": 3, "delivery_id": delivery_id,
            "created_at": "2026-08-05T05:00:00Z", "symbol": symbol,
            "row": row, "row_sha256": hashlib.sha256(row.encode()).hexdigest(),
            "event": event, "event_sha256": event_hash}


class DcaConfigurationTests(unittest.TestCase):
    def test_reads_final_rule_and_separate_execution_state(self):
        rules = rules_with("BTC_GBP")
        state = {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-04"}}

        config = crypto_dca.get_config_for_symbol("BTC_GBP", rules, state)

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
                crypto_dca.get_config_for_symbol("BTC_GBP", {"BTC_GBP": legacy})

    def test_pending_intent_requires_originating_decision_id(self):
        pending = pending_intent()
        pending.pop("decision_id")
        with self.assertRaisesRegex(ValueError, "decision_id"):
            crypto_dca._normalise_pending_order(pending, "BTC_GBP")

    def test_optional_symbol_filter_is_strict_and_deduplicated(self):
        self.assertEqual(
            crypto_dca._parse_symbol_filter('["BTC/GBP", "BTC_GBP", "ETH_GBP"]'),
            ("BTC_GBP", "ETH_GBP"),
        )
        self.assertEqual(crypto_dca._parse_symbol_filter("[]"), ())
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            crypto_dca._parse_symbol_filter('["ETH_USD"]')

    def test_pending_intent_requires_distinct_funding_order_id(self):
        pending = pending_intent()
        pending["funding_client_order_id"] = pending["client_order_id"]

        with self.assertRaisesRegex(ValueError, "funding client order ID"):
            crypto_dca._normalise_pending_order(pending, "BTC_GBP")


class DecisionGateTests(unittest.TestCase):
    def test_global_history_gate_requires_all_three_current_ready_decisions(self):
        rules = rules_with("BTC_GBP", "ETH_GBP", "SOL_GBP")
        decisions = {
            target: ready_decision(target, rules[target])
            for target in crypto_dca.ALLOWED_TARGETS
        }
        analysis = analysis_for(rules, decisions)

        self.assertEqual(crypto_dca._global_history_gate(analysis, NOW)[0], True)

        rollover = datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc)
        ready, reason = crypto_dca._global_history_gate(analysis, rollover)
        self.assertFalse(ready)
        self.assertIn("analysis date is not 2026-08-06", reason)

        analysis["TARGETS"]["ETH_GBP"]["ANALYSIS_STATUS"] = "ERROR"
        analysis["TARGETS"]["ETH_GBP"]["HISTORY"] = {"STATUS": "ERROR"}
        ready, reason = crypto_dca._global_history_gate(analysis, NOW)
        self.assertFalse(ready)
        self.assertIn("ETH_GBP analysis is ERROR", reason)
        self.assertIn("ETH_GBP history is ERROR", reason)

    def test_shadow_and_canary_modes_block_unapproved_new_orders(self):
        rules = rules_with("BTC_GBP", "SOL_GBP")
        btc = ready_decision("BTC_GBP", rules["BTC_GBP"])
        sol = ready_decision("SOL_GBP", rules["SOL_GBP"])
        with patch.object(crypto_dca, "DCA_TRADING_MODE", "shadow"):
            self.assertEqual(
                crypto_dca._decision_gate("BTC_GBP", rules["BTC_GBP"], btc, NOW)[0],
                "SHADOW",
            )
        with (
            patch.object(crypto_dca, "DCA_TRADING_MODE", "canary"),
            patch.object(crypto_dca, "DCA_CANARY_SYMBOL", "SOL_GBP"),
        ):
            self.assertEqual(
                crypto_dca._decision_gate("BTC_GBP", rules["BTC_GBP"], btc, NOW)[0],
                "SHADOW",
            )
            self.assertEqual(
                crypto_dca._decision_gate("SOL_GBP", rules["SOL_GBP"], sol, NOW)[0],
                "READY",
            )

    def test_budget_change_requires_fresh_analysis_in_plain_language(self):
        original_rules = rules_with("SOL_GBP", low=10, up=20)
        decision = ready_decision("SOL_GBP", original_rules["SOL_GBP"])
        live_rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "UP": 20},
            "BUY_ENABLED": True,
        }

        status, reason, amount = crypto_dca._decision_gate(
            "SOL_GBP", live_rule, decision, NOW
        )

        self.assertEqual(status, "REFRESH_REQUIRED")
        self.assertIn("GBP budget changed", reason)
        self.assertIsNone(amount)

    def test_start_date_requires_same_local_day_analysis(self):
        rules = rules_with("BTC_GBP", low=10, up=20)
        execute_at = datetime(2026, 8, 6, 21, 30, tzinfo=timezone.utc)
        decision = ready_decision("BTC_GBP", rules["BTC_GBP"])
        decision["EXECUTE_AT"] = execute_at.isoformat().replace("+00:00", "Z")
        decision["VALID_UNTIL"] = (
            execute_at + timedelta(minutes=60)
        ).isoformat().replace("+00:00", "Z")

        with patch.object(crypto_dca, "DCA_START_DATE", "2026-08-07"):
            status, reason, _amount = crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, execute_at
            )
            self.assertEqual(status, "ERROR")
            self.assertIn("current Bangkok date", reason)

            decision["TIMING"]["ANALYZED_AT"] = "2026-08-06T21:00:00Z"
            decision["ANALYSIS_DATE"] = "2026-08-07"
            decision["SELECTED_AT"] = decision["EXECUTE_AT"]
            status, _reason, amount = crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, execute_at
            )
            self.assertEqual((status, amount), ("READY", 10.0))

    def test_uptrend_uses_lower_budget(self):
        rules = rules_with("BTC_GBP", low=10, up=20)
        decision = ready_decision("BTC_GBP", rules["BTC_GBP"])

        status, _reason, amount = crypto_dca._decision_gate(
            "BTC_GBP", rules["BTC_GBP"], decision, NOW
        )

        self.assertEqual((status, amount), ("READY", 10.0))

    def test_sideways_uses_midpoint_and_downtrend_uses_higher_budget(self):
        rules = rules_with("BTC_GBP", low=10, up=20)
        expected = {"SIDEWAYS": 15.0, "DOWNTREND": 20.0}
        for regime, expected_amount in expected.items():
            decision = ready_decision(
                "BTC_GBP", rules["BTC_GBP"], regime=regime
            )
            with self.subTest(regime=regime):
                status, _reason, amount = crypto_dca._decision_gate(
                    "BTC_GBP", rules["BTC_GBP"], decision, NOW
                )
                self.assertEqual((status, amount), ("READY", expected_amount))

    def test_window_is_five_minutes_early_through_sixty_minutes_late(self):
        rules = rules_with("BTC_GBP")
        decision = ready_decision(
            "BTC_GBP", rules["BTC_GBP"], now=NOW, offset=30
        )
        before = NOW + timedelta(minutes=24, seconds=59)
        opens = NOW + timedelta(minutes=25)
        closes = NOW + timedelta(minutes=90)
        after = closes + timedelta(seconds=1)

        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, before
            )[0],
            "NOT_DUE",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, opens
            )[0],
            "READY",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, closes
            )[0],
            "READY",
        )
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, after
            )[0],
            "MISSED",
        )

    def test_error_and_rules_hash_mismatch_fail_closed(self):
        rules = rules_with("BTC_GBP")
        error_decision = empty_analysis_state(rules, now=NOW)["TARGETS"]["BTC_GBP"]
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], error_decision, NOW
            )[0],
            "ERROR",
        )

        decision = ready_decision("BTC_GBP", rules["BTC_GBP"])
        decision["RULES_HASH"] = "0" * 64
        self.assertEqual(
            crypto_dca._decision_gate(
                "BTC_GBP", rules["BTC_GBP"], decision, NOW
            )[0],
            "REFRESH_REQUIRED",
        )


class DurableIntentTests(unittest.TestCase):
    def test_target_migration_lock_blocks_direct_execution_state_write(self):
        locked = MagicMock(status_code=200)
        with (
            patch.object(
                crypto_dca,
                "_github_variable_context",
                return_value=("variable-url", "collection-url", {}),
            ),
            patch.object(crypto_dca.requests, "get", return_value=locked),
            patch.object(crypto_dca.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "migration lock"),
        ):
            crypto_dca._write_repo_json_variable(
                crypto_dca.EXECUTION_STATE_VARIABLE, {}, exists=True
            )
        write.assert_not_called()

    def test_uncertain_target_migration_lock_state_blocks_execution_write(self):
        uncertain = MagicMock(status_code=503)
        with (
            patch.object(
                crypto_dca,
                "_github_variable_context",
                return_value=("variable-url", "collection-url", {}),
            ),
            patch.object(crypto_dca.requests, "get", return_value=uncertain),
            patch.object(crypto_dca.requests, "patch") as write,
            self.assertRaisesRegex(RuntimeError, "could not be checked"),
        ):
            crypto_dca._write_repo_json_variable(
                crypto_dca.EXECUTION_STATE_VARIABLE, {}, exists=True
            )
        write.assert_not_called()

    def test_prepare_persists_decision_bound_intent(self):
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=({}, False)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
        ):
            intent, existed = crypto_dca.prepare_order_intent(
                "BTC_GBP",
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
            written["BTC_GBP"]["PENDING_ORDER"]["decision_id"], "decision-btc"
        )

    def test_prepare_rejects_same_day_duplicate_without_writing(self):
        state = {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-05"}}
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            self.assertRaisesRegex(RuntimeError, "already marked"),
        ):
            crypto_dca.prepare_order_intent(
                "BTC_GBP",
                "dca-1234567890abcd",
                "dca-fedcba09876543",
                "2026-08-05",
                20,
                "decision-btc",
            )
        write.assert_not_called()

    def test_completion_preserves_other_asset_state(self):
        intent = pending_intent()
        delivery = pending_gist_delivery()
        state = {
            "BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": intent},
            "ETH_GBP": {"LAST_BUY_DATE": "2026-08-04"},
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            crypto_dca.complete_order_intent(
                "BTC_GBP",
                intent["client_order_id"],
                intent["trade_date"],
                intent["decision_id"],
                gist_delivery=delivery,
            )

        written = write.call_args.args[1]
        self.assertEqual(written["BTC_GBP"]["LAST_BUY_DATE"], "2026-08-05")
        self.assertEqual(
            written["BTC_GBP"]["PENDING_GIST_DELIVERIES"], [delivery]
        )
        self.assertNotIn("PENDING_ORDER", written["BTC_GBP"])
        self.assertEqual(written["ETH_GBP"], {"LAST_BUY_DATE": "2026-08-04"})

    def test_completion_atomically_enqueues_confirmed_fill_delivery(self):
        intent = pending_intent()
        delivery = pending_gist_delivery()
        state = {
            "BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": intent},
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            crypto_dca.complete_order_intent(
                "BTC_GBP",
                intent["client_order_id"],
                intent["trade_date"],
                intent["decision_id"],
                gist_delivery=delivery,
            )

        written = write.call_args.args[1]
        self.assertEqual(written["BTC_GBP"]["LAST_BUY_DATE"], "2026-08-05")
        self.assertNotIn("PENDING_ORDER", written["BTC_GBP"])
        self.assertEqual(
            written["BTC_GBP"]["PENDING_GIST_DELIVERIES"], [delivery]
        )

    def test_completion_requires_durable_delivery_evidence(self):
        intent = pending_intent()
        with self.assertRaisesRegex(TypeError, "gist_delivery"):
            crypto_dca.complete_order_intent(
                "BTC_GBP",
                intent["client_order_id"],
                intent["trade_date"],
                intent["decision_id"],
            )

    def test_acknowledgement_removes_only_the_exact_delivery(self):
        first = pending_gist_delivery(delivery_id="kraken-order-one")
        second = pending_gist_delivery(delivery_id="kraken-order-two")
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [first, second],
            },
            "SOL_GBP": {"LAST_BUY_DATE": "2026-08-04"},
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
        ):
            self.assertTrue(
                crypto_dca.acknowledge_gist_delivery(
                    "BTC_GBP", first["delivery_id"], first["row_sha256"]
                )
            )

        written = write.call_args.args[1]
        self.assertEqual(
            written["BTC_GBP"]["PENDING_GIST_DELIVERIES"], [second]
        )
        self.assertEqual(written["SOL_GBP"]["LAST_BUY_DATE"], "2026-08-04")

    def test_failed_delivery_stays_queued_and_never_calls_kraken(self):
        delivery = pending_gist_delivery()
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [delivery],
            }
        }
        with (
            patch.object(crypto_dca, "update_gist_log", return_value=False),
            patch.object(crypto_dca, "acknowledge_gist_delivery") as acknowledge,
            patch.object(crypto_dca, "place_market_buy") as place,
        ):
            self.assertFalse(crypto_dca.retry_pending_gist_deliveries(state))

        acknowledge.assert_not_called()
        place.assert_not_called()

    def test_full_delivery_queue_keeps_exact_order_intent_locked(self):
        intent = pending_intent()
        deliveries = [
            pending_gist_delivery(delivery_id=f"kraken-order-{index:02d}")
            for index in range(crypto_dca.MAX_PENDING_GIST_DELIVERIES)
        ]
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": intent,
                "PENDING_GIST_DELIVERIES": deliveries,
            }
        }
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=(state, True)
            ),
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            self.assertRaisesRegex(RuntimeError, "queue is full"),
        ):
            crypto_dca.complete_order_intent(
                "BTC_GBP",
                intent["client_order_id"],
                intent["trade_date"],
                intent["decision_id"],
                gist_delivery=pending_gist_delivery(
                    delivery_id="kraken-order-overflow"
                ),
            )

        write.assert_not_called()

    def test_prepare_reserves_delivery_capacity_before_persisting_new_intent(self):
        with (
            patch.object(
                crypto_dca, "_fetch_repo_json_variable", return_value=({}, False)
            ),
            patch.object(
                crypto_dca,
                "ensure_gist_delivery_capacity",
                side_effect=dca_config.ConfigError("no delivery capacity"),
            ) as reserve,
            patch.object(crypto_dca, "_write_repo_json_variable") as write,
            self.assertRaisesRegex(ValueError, "no delivery capacity"),
        ):
            crypto_dca.prepare_order_intent(
                "BTC_GBP",
                "dca-1234567890abcd",
                "dca-fedcba09876543",
                "2026-08-05",
                20,
                "decision-btc",
            )

        reserve.assert_called_once_with(
            {"BTC_GBP": {"LAST_BUY_DATE": ""}}, "BTC_GBP"
        )
        write.assert_not_called()

    def test_retry_acknowledges_delivery_without_calling_kraken(self):
        delivery = pending_gist_delivery()
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_GIST_DELIVERIES": [delivery],
            }
        }
        with (
            patch.object(crypto_dca, "update_gist_log", return_value=True),
            patch.object(
                crypto_dca, "acknowledge_gist_delivery", return_value=True
            ) as acknowledge,
            patch.object(crypto_dca, "place_market_buy") as place,
        ):
            self.assertTrue(crypto_dca.retry_pending_gist_deliveries(state))

        acknowledge.assert_called_once_with(
            "BTC_GBP", delivery["delivery_id"], delivery["row_sha256"]
        )
        place.assert_not_called()


class LiveRevalidationTests(unittest.TestCase):
    def setUp(self):
        self.rules = rules_with("BTC_GBP")
        self.decision = ready_decision("BTC_GBP", self.rules["BTC_GBP"])
        self.analysis = analysis_for(self.rules, {"BTC_GBP": self.decision})

    def test_revalidation_rejects_live_budget_change(self):
        changed = rules_with("BTC_GBP", low=11, up=21)
        changed_decision = ready_decision("BTC_GBP", changed["BTC_GBP"])
        changed_analysis = analysis_for(changed, {"BTC_GBP": changed_decision})
        with (
            patch.object(crypto_dca, "fetch_live_target_map", return_value=changed),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=changed_analysis
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "budgets or enable state changed"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_GBP",
                self.rules["BTC_GBP"],
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
                "BTC_GBP",
                self.rules["BTC_GBP"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

        fetch_rules.assert_not_called()

    def test_revalidation_rejects_live_disable_before_add_order(self):
        disabled = json.loads(json.dumps(self.rules))
        disabled["BTC_GBP"]["BUY_ENABLED"] = False
        with (
            patch.object(crypto_dca, "fetch_live_target_map", return_value=disabled),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=self.analysis
            ),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value={}),
            self.assertRaisesRegex(RuntimeError, "disabled before order submission"),
        ):
            crypto_dca._revalidate_trade_intent(
                "BTC_GBP",
                self.rules["BTC_GBP"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_new_analysis_decision(self):
        newer = dict(self.decision)
        newer["DECISION_ID"] = "replacement-decision"
        newer_analysis = analysis_for(self.rules, {"BTC_GBP": newer})
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
                "BTC_GBP",
                self.rules["BTC_GBP"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_analyzed_at_mutation(self):
        changed = json.loads(json.dumps(self.decision))
        changed["TIMING"]["ANALYZED_AT"] = "2026-08-05T04:29:00Z"
        changed_analysis = analysis_for(self.rules, {"BTC_GBP": changed})
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
                "BTC_GBP",
                self.rules["BTC_GBP"],
                self.decision,
                "2026-08-05",
                now=NOW,
            )

    def test_revalidation_rejects_duplicate_and_conflicting_pending(self):
        for state, pattern in (
            ({"BTC_GBP": {"LAST_BUY_DATE": "2026-08-05"}}, "already marked"),
            (
                {
                    "BTC_GBP": {
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
                    "BTC_GBP",
                    self.rules["BTC_GBP"],
                    self.decision,
                    "2026-08-05",
                    now=NOW,
                )

    def test_revalidation_rejects_pending_from_another_decision(self):
        pending = pending_intent()
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
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
                "BTC_GBP",
                self.rules["BTC_GBP"],
                self.decision,
                "2026-08-05",
                expected_pending=pending,
                now=NOW,
            )


class TradeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.rules = rules_with("BTC_GBP")
        self.decision = ready_decision("BTC_GBP", self.rules["BTC_GBP"])
        self.intent = pending_intent()
        self.intent["decision_id"] = self.decision["DECISION_ID"]
        self.order_data = {
            "order_id": "kraken-order-id",
            "pair": "BTC/GBP",
            "quote_currency": "GBP",
            "cost_gbp": 20.0,
            "fee_gbp": 0.04,
            "gbp_fee_debit": 0.0,
            "fee_details": [
                {
                    "currency": "BTC",
                    "amount": 0.0000008,
                    "quote_equivalent": 0.04,
                    "gbp_equivalent": 0.04,
                }
            ],
            "spent_gbp": 20.0,
            "cost_quote": 20.0,
            "fee_quote": 0.04,
            "quote_fee_debit": 0.0,
            "received": 0.0004,
            "market_gbp_price_per_unit": 49_750.0,
            "effective_gbp_price_per_unit": 50_000.0,
            "market_quote_price_per_unit": 49_750.0,
            "effective_quote_price_per_unit": 50_000.0,
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
                crypto_dca, "place_market_buy", side_effect=place
            ) as place_order,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "_post_trade_logs", return_value=False),
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                20,
                expected_rule=self.rules["BTC_GBP"],
                expected_decision=self.decision,
            )

        self.assertTrue(succeeded)
        self.assertEqual(revalidate.call_count, 3)
        self.assertTrue(
            all(
                call.kwargs.get("reserve_gist_delivery") is True
                for call in revalidate.call_args_list
            )
        )
        self.assertEqual(events, ["place-entered", "after-check"])
        self.assertEqual(build_id.call_count, 2)
        self.assertEqual(build_id.call_args_list[0].kwargs, {"purpose": "buy"})
        self.assertEqual(build_id.call_args_list[1].kwargs, {"purpose": "funding"})
        self.assertNotIn("funding_client_order_id", place_order.call_args.kwargs)
        complete.assert_called_once()
        self.assertEqual(
            complete.call_args.args,
            (
                "BTC_GBP",
                self.intent["client_order_id"],
                self.intent["trade_date"],
                self.intent["decision_id"],
            ),
        )
        self.assertEqual(
            complete.call_args.kwargs["gist_delivery"]["delivery_id"],
            self.order_data["order_id"],
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
            patch.object(crypto_dca, "place_market_buy") as place,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                10,
                expected_rule=self.rules["BTC_GBP"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        place.assert_not_called()

    def test_full_delivery_queue_blocks_new_order_before_kraken(self):
        deliveries = [
            pending_gist_delivery(delivery_id=f"kraken-order-{index:02d}")
            for index in range(dca_config.MAX_PENDING_GIST_DELIVERIES)
        ]
        state = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_GIST_DELIVERIES": deliveries,
            }
        }
        analysis = analysis_for(
            self.rules, {"BTC_GBP": self.decision}
        )
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            patch.object(
                crypto_dca, "fetch_live_target_map", return_value=self.rules
            ),
            patch.object(
                crypto_dca, "fetch_live_analysis_state", return_value=analysis
            ),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(crypto_dca, "place_market_buy") as place,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                20,
                expected_rule=self.rules["BTC_GBP"],
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
                "place_market_buy",
                side_effect=crypto_dca.KrakenPreSubmissionError("rules changed"),
            ),
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                20,
                expected_rule=self.rules["BTC_GBP"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        clear.assert_called_once_with(
            "BTC_GBP", self.intent["client_order_id"], self.intent["decision_id"]
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
                "place_market_buy",
                side_effect=RuntimeError("ambiguous response"),
            ),
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            succeeded = crypto_dca.execute_trade(
                "BTC_GBP",
                20,
                expected_rule=self.rules["BTC_GBP"],
                expected_decision=self.decision,
            )

        self.assertFalse(succeeded)
        clear.assert_not_called()
        complete.assert_not_called()
        self.assertIn("remains locked", alert.call_args.args[0])

    def test_pending_recovery_never_creates_a_new_intent(self):
        old = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": old}}
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(
                crypto_dca, "fetch_live_execution_state", return_value=state
            ),
            patch.object(crypto_dca, "prepare_order_intent") as prepare,
            patch.object(
                crypto_dca,
                "ensure_gist_delivery_capacity",
                side_effect=AssertionError("recovery must not run capacity gate"),
            ) as reserve,
            patch.object(
                crypto_dca,
                "place_market_buy",
                side_effect=crypto_dca.KrakenOrderStateUnknown("not visible"),
            ) as place,
            patch.object(crypto_dca, "clear_order_intent") as clear,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            succeeded = crypto_dca.execute_trade("BTC_GBP", 20)

        self.assertFalse(succeeded)
        prepare.assert_not_called()
        reserve.assert_not_called()
        clear.assert_not_called()
        self.assertEqual(place.call_args.args, ("BTC_GBP", 10.0))
        self.assertNotIn("funding_client_order_id", place.call_args.kwargs)
        self.assertTrue(place.call_args.kwargs["reconcile_only"])

    def test_cross_midnight_recovery_records_confirmed_fill_day(self):
        old = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": old}}
        recovered = dict(self.order_data)
        recovered["timestamp"] = int(NOW.timestamp())
        with (
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "fetch_live_execution_state", return_value=state),
            patch.object(crypto_dca, "place_market_buy", return_value=recovered),
            patch.object(crypto_dca, "complete_order_intent") as complete,
            patch.object(crypto_dca, "_post_trade_logs", return_value=False),
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            self.assertTrue(crypto_dca.execute_trade("BTC_GBP", 20))

        complete.assert_called_once()
        self.assertEqual(
            complete.call_args.args,
            (
                "BTC_GBP",
                old["client_order_id"],
                "2026-08-05",
                old["decision_id"],
            ),
        )
        self.assertEqual(
            complete.call_args.kwargs["gist_delivery"]["delivery_id"],
            recovered["order_id"],
        )


class MainSchedulingTests(unittest.TestCase):
    def test_prior_day_ready_state_is_quiet_before_daily_analysis(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
        )

        for current in (
            datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc),
            datetime(2026, 8, 5, 21, 19, 59, tzinfo=timezone.utc),
        ):
            with self.subTest(current=current):
                with (
                    patch.object(
                        crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)
                    ),
                    patch.object(
                        crypto_dca,
                        "DCA_ANALYSIS_STATE_JSON",
                        json.dumps(analysis),
                    ),
                    patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
                    patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
                    patch.object(crypto_dca, "_utc_now", return_value=current),
                    patch.object(crypto_dca, "execute_trade") as execute,
                    patch.object(crypto_dca, "send_discord_alert") as alert,
                ):
                    self.assertTrue(crypto_dca.main())
                    execute.assert_not_called()
                    alert.assert_not_called()

    def test_prior_day_ready_state_alerts_at_daily_analysis_deadline(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
        )
        deadline = datetime(2026, 8, 5, 21, 20, tzinfo=timezone.utc)
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=deadline),
            patch.object(crypto_dca, "execute_trade") as execute,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            self.assertFalse(crypto_dca.main())

        execute.assert_not_called()
        alert.assert_called_once()
        self.assertTrue(alert.call_args.kwargs["is_error"])

    def test_unhealthy_or_older_prior_state_is_not_quiet_during_rollover(self):
        rules = rules_with("BTC_GBP")
        unhealthy = empty_analysis_state(rules, now=NOW)
        older_time = NOW - timedelta(days=1)
        older = analysis_for(
            rules,
            {
                "BTC_GBP": ready_decision(
                    "BTC_GBP", rules["BTC_GBP"], now=older_time
                )
            },
            now=older_time,
        )
        rollover = datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc)
        for analysis in (unhealthy, older):
            with self.subTest(state_date=analysis["ANALYSIS_DATE"]):
                with (
                    patch.object(
                        crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)
                    ),
                    patch.object(
                        crypto_dca,
                        "DCA_ANALYSIS_STATE_JSON",
                        json.dumps(analysis),
                    ),
                    patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
                    patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
                    patch.object(crypto_dca, "_utc_now", return_value=rollover),
                    patch.object(crypto_dca, "execute_trade") as execute,
                    patch.object(crypto_dca, "send_discord_alert") as alert,
                ):
                    self.assertFalse(crypto_dca.main())

                execute.assert_not_called()
                alert.assert_called_once()

    def test_pending_recovery_still_runs_during_daily_rollover_wait(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
        )
        pending = pending_intent(trade_date="2026-08-05")
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        rollover = datetime(2026, 8, 5, 17, 2, tzinfo=timezone.utc)
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(
                crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)
            ),
            patch.object(
                crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)
            ),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=rollover),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_called_once_with("BTC_GBP", 20.0, map_key="BTC_GBP")
        alert.assert_not_called()

    def test_budget_change_sends_one_clear_refresh_notice_and_never_trades(self):
        original_rules = rules_with("SOL_GBP", low=10, up=20)
        analysis = analysis_for(
            original_rules,
            {"SOL_GBP": ready_decision("SOL_GBP", original_rules["SOL_GBP"])},
        )
        live_rules = json.loads(json.dumps(original_rules))
        live_rules["SOL_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 12.5
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(live_rules)),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", json.dumps(analysis)),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", "{}"),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "_utc_now", return_value=NOW),
            patch.object(crypto_dca, "execute_trade") as execute,
            patch.object(crypto_dca, "send_discord_alert") as alert,
        ):
            self.assertFalse(crypto_dca.main())

        execute.assert_not_called()
        alert.assert_called_once()
        self.assertIn("SOL/GBP is waiting for fresh analysis", alert.call_args.args[0])
        self.assertIn("No Kraken order was attempted", alert.call_args.args[0])
        self.assertEqual(
            alert.call_args.kwargs["title"], "🔄 DCA Analysis Refresh Required"
        )
        self.assertFalse(alert.call_args.kwargs["is_error"])

    def test_future_start_date_allows_analysis_but_blocks_new_orders(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
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
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
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
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
            patch.object(crypto_dca, "send_discord_alert"),
        ):
            self.assertFalse(crypto_dca.main())

        execute.assert_called_once_with("BTC_GBP", 10.0, map_key="BTC_GBP")

    def test_pending_recovery_runs_even_when_analysis_is_invalid(self):
        rules = default_rules_map()
        pending = pending_intent(amount=10, trade_date="2026-08-04")
        state = {"BTC_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": pending}}
        with (
            patch.object(crypto_dca, "DCA_TARGET_MAP_JSON", json.dumps(rules)),
            patch.object(crypto_dca, "DCA_ANALYSIS_STATE_JSON", "not-json"),
            patch.object(crypto_dca, "DCA_EXECUTION_STATE_JSON", json.dumps(state)),
            patch.object(crypto_dca, "DCA_SYMBOLS_JSON", ""),
            patch.object(crypto_dca, "execute_trade", return_value=True) as execute,
        ):
            self.assertTrue(crypto_dca.main())

        execute.assert_called_once_with("BTC_GBP", 10.0, map_key="BTC_GBP")

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
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
        )
        state = {"BTC_GBP": {"LAST_BUY_DATE": "2026-08-05"}}
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
        rules = rules_with("BTC_GBP", "ETH_GBP", low=10, up=20)
        analysis = analysis_for(
            rules,
            {
                "BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"]),
                "ETH_GBP": ready_decision(
                    "ETH_GBP", rules["ETH_GBP"], regime="SIDEWAYS"
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
        self.assertEqual(calls, {"BTC_GBP": 10.0, "ETH_GBP": 15.0})

    def test_missed_decision_is_expected_quiet_non_replay(self):
        rules = rules_with("BTC_GBP")
        decision = ready_decision(
            "BTC_GBP", rules["BTC_GBP"], now=NOW - timedelta(hours=2)
        )
        analysis = analysis_for(rules, {"BTC_GBP": decision})
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
            self.assertTrue(crypto_dca.main())
        execute.assert_not_called()
        alert.assert_not_called()

    def test_stale_disabled_asset_hash_does_not_block_another_asset(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
        )
        # Simulate an atomic budget edit on disabled ADA after the last analysis.
        rules["SOL_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
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
        self.assertEqual(execute.call_args.args[:2], ("BTC_GBP", 10.0))

    def test_explicit_empty_filter_is_a_successful_noop(self):
        rules = rules_with("BTC_GBP")
        analysis = analysis_for(
            rules, {"BTC_GBP": ready_decision("BTC_GBP", rules["BTC_GBP"])}
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

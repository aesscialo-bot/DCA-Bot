import copy
import json
import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, call, patch

from dca_config import default_rules_map, empty_analysis_state, rules_hash, validate_analysis_state
from dca_config_writer import apply_change, global_rules_pre_state_hash, prepare_enable_analysis_invalidation, main
from tests.history_fixtures import ready_history


def ready_signals():
    return {
        "DAILY_LAST_COMPLETE": "2026-08-04T00:00:00Z",
        "DAILY_CLOSE": 105.0,
        "DAILY_PREVIOUS_CLOSE": 104.0,
        "DAILY_TWO_DAYS_AGO_CLOSE": 97.0,
        "DAILY_SMA150": 100.0,
        "DAILY_PREVIOUS_SMA150": 99.0,
        "DAILY_TWO_DAYS_AGO_SMA150": 98.0,
        "DAILY_EMA20": 90.0,
        "DAILY_EMA50": 95.0,
        "DAILY_PREVIOUS_EMA20": 89.0,
        "DAILY_PREVIOUS_EMA50": 94.0,
        "WEEKLY_LAST_COMPLETE": "2026-07-27T00:00:00Z",
        "WEEKLY_CLOSE": 105.0,
        "WEEKLY_EMA20": 100.0,
        "SMA150_SLOPE_20D": -1.0,
        "TWO_DAY_ABOVE": False,
        "TWO_DAY_BELOW": False,
        "THREE_DAY_BELOW": False,
        "WEEKLY_ABOVE": True,
        "WEEKLY_BELOW": False,
        "SLOPE_POSITIVE": False,
        "SLOPE_NEGATIVE": True,
        "UPTREND_CONFIRMATION_REQUIRED": 3,
        "UPTREND_CONFIRMATION_COUNT": 2,
        "UPTREND_CONFIRMED": False,
        "REGIME_WITHOUT_OVERRIDE": "SIDEWAYS",
        "UPTREND_OVERRIDE_ACTIVE": False,
        "UPTREND_OVERRIDE_APPLIED": False,
        "UPTREND_OVERRIDE_REASON": None,
        "UPTREND_OVERRIDE_ACTIVATED_AT": None,
        "UPTREND_OVERRIDE_RELEASED_AT": None,
        "UPTREND_OVERRIDE_AUTO_RELEASED": False,
    }


class DcaConfigWriterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 4, 0, tzinfo=timezone.utc)
        self.rules = default_rules_map()

    def _ready_state(self):
        state = empty_analysis_state(self.rules, now=self.now)
        decision = state["TARGETS"]["BTC_GBP"]
        decision.update(
            {
                "ANALYSIS_STATUS": "READY",
                "EXECUTION_STATUS": "ARMED",
                "REGIME": "SIDEWAYS",
                "AMOUNT_TIER": "MID",
                "SELECTED_AT": (self.now + timedelta(hours=1)).isoformat(),
                "EXECUTE_AT": (self.now + timedelta(hours=1)).isoformat(),
                "VALID_UNTIL": (self.now + timedelta(hours=2)).isoformat(),
                "SIGNALS": ready_signals(),
                "HISTORY": ready_history("BTC_GBP", self.now),
                "ERROR": None,
            }
        )
        return state

    def test_budget_update_changes_all_tiers_atomically(self):
        updated, should_write = apply_change(
            self.rules,
            action="set_amounts",
            symbol="BTC_GBP",
            low_amount_gbp_json="10",
            mid_amount_gbp_json="12",
            up_amount_gbp_json="20",
        )
        self.assertTrue(should_write)
        self.assertEqual(
            updated["BTC_GBP"]["REGIME_AMOUNTS_GBP"],
            {"LOW": 10, "MID": 12, "UP": 20},
        )

    def test_budget_update_rejects_enabled_target(self):
        self.rules["BTC_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        with self.assertRaisesRegex(ValueError, "Disable BTC_GBP"):
            apply_change(
                self.rules,
                action="set_amounts",
                symbol="BTC_GBP",
                low_amount_gbp_json="11",
                up_amount_gbp_json="21",
            )

    def test_budget_update_rejects_inverted_or_overprecise_endpoints(self):
        with self.assertRaisesRegex(ValueError, "LOW <= MID <= UP"):
            apply_change(
                self.rules,
                action="set_amounts",
                symbol="BTC_GBP",
                low_amount_gbp_json="20",
                mid_amount_gbp_json="15",
                up_amount_gbp_json="10",
            )
        with self.assertRaisesRegex(ValueError, "no more than two decimal places"):
            apply_change(
                self.rules,
                action="set_amounts",
                symbol="BTC_GBP",
                low_amount_gbp_json="10.001",
                up_amount_gbp_json="20",
            )

    def test_dry_run_validates_without_requesting_a_write(self):
        updated, should_write = apply_change(
            self.rules,
            action="dry_run",
            symbol="BTC_GBP",
            low_amount_gbp_json="10",
            up_amount_gbp_json="20",
        )
        self.assertFalse(should_write)
        self.assertEqual(updated["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["UP"], 20)

    def test_enable_is_bound_to_decision_and_live_market_minimum(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = self._ready_state()
        expected_hash = rules_hash("BTC_GBP", self.rules["BTC_GBP"])
        state["TARGETS"]["BTC_GBP"]["RULES_HASH"] = expected_hash
        # A different disabled target may legitimately have an obsolete READY
        # decision after its own budget edit; it must not block BTC's enable.
        state["TARGETS"]["ETH_GBP"].update(
            {
                "ANALYSIS_STATUS": "READY",
                "EXECUTION_STATUS": "ARMED",
                "REGIME": "SIDEWAYS",
                "AMOUNT_TIER": "MID",
                "SELECTED_AT": (self.now + timedelta(hours=1)).isoformat(),
                "EXECUTE_AT": (self.now + timedelta(hours=1)).isoformat(),
                "VALID_UNTIL": (self.now + timedelta(hours=2)).isoformat(),
                "RULES_HASH": "0" * 64,
                "SIGNALS": ready_signals(),
                "HISTORY": ready_history("ETH_GBP", self.now),
                "ERROR": None,
            }
        )
        updated, should_write = apply_change(
            self.rules,
            state,
            {},
            action="set_enabled",
            symbol="BTC_GBP",
            enabled_json="true",
            expected_rules_hash=expected_hash,
            expected_decision_id=state["TARGETS"]["BTC_GBP"]["DECISION_ID"],
            expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
            market_minimum_provider=lambda _symbol: 5,
            now=self.now,
        )
        self.assertTrue(should_write)
        self.assertTrue(updated["BTC_GBP"]["BUY_ENABLED"])

    def test_enable_accepts_valid_non_ready_or_stale_analysis_for_next_cycle(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        expected_hash = rules_hash("BTC_GBP", self.rules["BTC_GBP"])
        for analysis in (
            empty_analysis_state(self.rules, now=self.now),
            empty_analysis_state(self.rules, now=self.now - timedelta(days=1)),
            self._ready_state(),
        ):
            with self.subTest(analysis=analysis is not None):
                updated, should_write = apply_change(
                    self.rules,
                    analysis,
                    {},
                    action="set_enabled",
                    symbol="BTC_GBP",
                    enabled_json="true",
                    expected_rules_hash=expected_hash,
                    expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
                    market_minimum_provider=lambda _symbol: 5,
                    now=self.now,
                )
                self.assertTrue(should_write)
                self.assertTrue(updated["BTC_GBP"]["BUY_ENABLED"])

    def test_enable_cannot_replace_missing_invalid_or_old_policy_analysis(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        old_policy = self._ready_state()
        old_policy["POLICY_VERSION"] = "obsolete-policy"
        for analysis in (None, {}, {"secret": "do not expose"}, old_policy):
            with self.subTest(analysis=type(analysis).__name__), self.assertRaisesRegex(
                ValueError, "run !dca analyze all before enabling"
            ):
                apply_change(
                    self.rules,
                    analysis,
                    {},
                    action="set_enabled",
                    symbol="BTC_GBP",
                    enabled_json="true",
                    expected_rules_hash=rules_hash("BTC_GBP", self.rules["BTC_GBP"]),
                    expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
                    market_minimum_provider=lambda _symbol: 5,
                    now=self.now,
                )

    def test_disable_reenable_invalidates_old_enabled_decision_without_touching_others(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = self._ready_state()
        # This READY decision predates the disable and originally allowed buying.
        state["TARGETS"]["BTC_GBP"]["ENABLED"] = True
        original = copy.deepcopy(state)
        updated, _ = apply_change(
            self.rules,
            state,
            {},
            action="set_enabled",
            symbol="BTC_GBP",
            enabled_json="true",
            expected_rules_hash=rules_hash("BTC_GBP", self.rules["BTC_GBP"]),
            expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
            market_minimum_provider=lambda _symbol: 5,
            now=self.now,
        )
        invalidated = prepare_enable_analysis_invalidation(updated, state, symbol="BTC_GBP", now=self.now)
        self.assertEqual(state, original)
        validate_analysis_state(invalidated)
        target = invalidated["TARGETS"]["BTC_GBP"]
        self.assertEqual(target["ANALYSIS_STATUS"], "ERROR")
        self.assertEqual(target["EXECUTION_STATUS"], "BLOCKED")
        self.assertTrue(target["ENABLED"])
        for key in ("SELECTED_AT", "EXECUTE_AT", "VALID_UNTIL", "REGIME", "AMOUNT_TIER"):
            self.assertIsNone(target[key])
        for other in ("ETH_GBP", "SOL_GBP", "DOGE_GBP"):
            self.assertEqual(invalidated["TARGETS"][other], original["TARGETS"][other])

    def test_invalidation_preserves_prior_day_envelope_and_override_audit(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        self.rules["BTC_GBP"]["BUY_ENABLED"] = True
        state = empty_analysis_state(self.rules, now=self.now - timedelta(days=1))
        signals = state["TARGETS"]["BTC_GBP"]["SIGNALS"]
        signals.update(ready_signals())
        signals.update({
            "UPTREND_OVERRIDE_ACTIVE": True,
            "UPTREND_OVERRIDE_REASON": "Operator approved emergency override",
            "UPTREND_OVERRIDE_ACTIVATED_AT": "2026-08-01T00:00:00Z",
        })
        result = prepare_enable_analysis_invalidation(self.rules, state, symbol="BTC_GBP", now=self.now)
        validate_analysis_state(result)
        self.assertEqual(result["ANALYSIS_DATE"], state["ANALYSIS_DATE"])
        self.assertEqual(result["TARGETS"]["BTC_GBP"]["ANALYSIS_DATE"], state["ANALYSIS_DATE"])
        for key in ("UPTREND_OVERRIDE_ACTIVE", "UPTREND_OVERRIDE_REASON", "UPTREND_OVERRIDE_ACTIVATED_AT"):
            self.assertEqual(result["TARGETS"]["BTC_GBP"]["SIGNALS"][key], signals[key])

    def test_enable_fails_when_minimum_or_confirmation_changed(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = self._ready_state()
        expected_hash = rules_hash("BTC_GBP", self.rules["BTC_GBP"])
        state["TARGETS"]["BTC_GBP"]["RULES_HASH"] = expected_hash
        with self.assertRaisesRegex(ValueError, "below Kraken"):
            apply_change(
                self.rules,
                state,
                {},
                action="set_enabled",
                symbol="BTC_GBP",
                enabled_json="true",
                expected_rules_hash=expected_hash,
                expected_decision_id=state["TARGETS"]["BTC_GBP"]["DECISION_ID"],
                expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
                market_minimum_provider=lambda _symbol: 15,
                now=self.now,
            )
        with self.assertRaisesRegex(ValueError, "Budgets changed"):
            apply_change(
                self.rules,
                state,
                {},
                action="set_enabled",
                symbol="BTC_GBP",
                enabled_json="true",
                expected_rules_hash="0" * 64,
                expected_decision_id=state["TARGETS"]["BTC_GBP"]["DECISION_ID"],
                expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
                market_minimum_provider=lambda _symbol: 5,
                now=self.now,
            )

    def test_second_queued_enable_rejects_changed_global_pre_state(self):
        for symbol in ("BTC_GBP", "ETH_GBP"):
            self.rules[symbol]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = empty_analysis_state(self.rules, now=self.now)
        for symbol in ("BTC_GBP", "ETH_GBP"):
            state["TARGETS"][symbol].update(
                {
                    "ANALYSIS_STATUS": "READY",
                    "EXECUTION_STATUS": "ARMED",
                    "REGIME": "SIDEWAYS",
                    "AMOUNT_TIER": "MID",
                    "SELECTED_AT": (self.now + timedelta(hours=1)).isoformat(),
                    "EXECUTE_AT": (self.now + timedelta(hours=1)).isoformat(),
                    "VALID_UNTIL": (self.now + timedelta(hours=2)).isoformat(),
                    "RULES_HASH": rules_hash(symbol, self.rules[symbol]),
                    "SIGNALS": ready_signals(),
                    "HISTORY": ready_history(symbol, self.now),
                    "ERROR": None,
                }
            )
        reviewed_global_hash = global_rules_pre_state_hash(self.rules)
        first, _ = apply_change(
            self.rules,
            state,
            {},
            action="set_enabled",
            symbol="BTC_GBP",
            enabled_json="true",
            expected_rules_hash=rules_hash("BTC_GBP", self.rules["BTC_GBP"]),
            expected_decision_id=state["TARGETS"]["BTC_GBP"]["DECISION_ID"],
            expected_global_rules_hash=reviewed_global_hash,
            market_minimum_provider=lambda _symbol: 5,
            now=self.now,
        )
        with self.assertRaisesRegex(ValueError, "Global DCA rules changed"):
            apply_change(
                first,
                state,
                {},
                action="set_enabled",
                symbol="ETH_GBP",
                enabled_json="true",
                expected_rules_hash=rules_hash("ETH_GBP", self.rules["ETH_GBP"]),
                expected_decision_id=state["TARGETS"]["ETH_GBP"]["DECISION_ID"],
                expected_global_rules_hash=reviewed_global_hash,
                market_minimum_provider=lambda _symbol: 5,
                now=self.now,
            )

    def test_enable_rejects_any_pending_order_reconciliation(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        state = self._ready_state()
        state["TARGETS"]["BTC_GBP"]["RULES_HASH"] = rules_hash(
            "BTC_GBP", self.rules["BTC_GBP"]
        )
        execution = {
            "SOL_GBP": {
                "LAST_BUY_DATE": "",
                "PENDING_ORDER": {
                    "client_order_id": "dca-1234567890abcd",
                    "funding_client_order_id": "dca-fedcba09876543",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-ada",
                    "created_at": self.now.isoformat(),
                },
            }
        }
        with self.assertRaisesRegex(ValueError, "reconciliation is pending for SOL_GBP"):
            apply_change(
                self.rules,
                state,
                execution,
                action="set_enabled",
                symbol="BTC_GBP",
                enabled_json="true",
                expected_rules_hash=rules_hash("BTC_GBP", self.rules["BTC_GBP"]),
                expected_decision_id=state["TARGETS"]["BTC_GBP"]["DECISION_ID"],
                expected_global_rules_hash=global_rules_pre_state_hash(self.rules),
                market_minimum_provider=lambda _symbol: 5,
                now=self.now,
            )
    def test_disabling_does_not_require_analysis_or_market_access(self):
        self.rules["BTC_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        updated, _ = apply_change(
            self.rules,
            action="set_enabled",
            symbol="BTC_GBP",
            enabled_json="false",
        )
        self.assertFalse(updated["BTC_GBP"]["BUY_ENABLED"])


class BulkConfigurationWriterTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 8, 5, 4, tzinfo=timezone.utc)
        self.rules = default_rules_map()
        for target, rule in self.rules.items():
            rule["REGIME_AMOUNTS_GBP"] = {"LOW": 5, "MID": 10, "UP": 20 if target == "BTC_GBP" else 15}
        self.analysis = empty_analysis_state(self.rules, now=self.now)

    def enable_all(self, **changes):
        reviewed = global_rules_pre_state_hash(self.rules)
        arguments = {
            "action": "set_enabled", "symbol": "all", "enabled_json": "true",
            "expected_rules_hash": reviewed, "expected_global_rules_hash": reviewed,
            "market_minimum_provider": lambda _target: 5, "now": self.now,
        }
        arguments.update(changes)
        return apply_change(self.rules, self.analysis, {}, **arguments)

    def test_enable_all_builds_one_complete_map_and_checks_every_market(self):
        before_rules, before_analysis = copy.deepcopy(self.rules), copy.deepcopy(self.analysis)
        minimum = MagicMock(return_value=5)
        updated, should_write = self.enable_all(market_minimum_provider=minimum)
        self.assertTrue(should_write)
        self.assertTrue(all(rule["BUY_ENABLED"] for rule in updated.values()))
        self.assertEqual(set(updated), set(self.rules))
        self.assertEqual(minimum.call_args_list, [call(target) for target in self.rules])
        for target in self.rules:
            self.assertEqual(updated[target]["REGIME_AMOUNTS_GBP"], self.rules[target]["REGIME_AMOUNTS_GBP"])
        self.assertEqual(self.rules, before_rules)
        self.assertEqual(self.analysis, before_analysis)

    def test_mixed_enable_flags_are_reviewed_globally_and_all_decisions_invalidated(self):
        self.rules["ETH_GBP"]["BUY_ENABLED"] = True
        old_date = self.now - timedelta(days=1)
        self.analysis = empty_analysis_state(self.rules, now=old_date)
        for target, decision in self.analysis["TARGETS"].items():
            decision["SIGNALS"].update(ready_signals())
            decision["SIGNALS"].update({
                "UPTREND_OVERRIDE_ACTIVE": True,
                "UPTREND_OVERRIDE_REASON": "Reviewed override for " + target,
                "UPTREND_OVERRIDE_ACTIVATED_AT": "2026-08-01T00:00:00Z",
            })
        before = copy.deepcopy(self.analysis)
        updated, _ = self.enable_all()
        invalidated = prepare_enable_analysis_invalidation(updated, self.analysis, symbol="all", now=self.now)
        validate_analysis_state(invalidated)
        self.assertEqual(invalidated["ANALYSIS_DATE"], before["ANALYSIS_DATE"])
        self.assertEqual(self.analysis, before)
        for target, decision in invalidated["TARGETS"].items():
            self.assertTrue(decision["ENABLED"])
            self.assertEqual(decision["ANALYSIS_STATUS"], "ERROR")
            self.assertEqual(decision["EXECUTION_STATUS"], "BLOCKED")
            self.assertEqual(decision["ANALYSIS_DATE"], before["ANALYSIS_DATE"])
            for field in ("SELECTED_AT", "EXECUTE_AT", "VALID_UNTIL", "REGIME", "AMOUNT_TIER"):
                self.assertIsNone(decision[field])
            for field, value in before["TARGETS"][target]["SIGNALS"].items():
                if field != "ERROR":
                    self.assertEqual(decision["SIGNALS"][field], value)

    def test_bulk_enable_requires_both_hashes_to_match_current_global_state(self):
        reviewed = global_rules_pre_state_hash(self.rules)
        for changes in (
            {"expected_rules_hash": ""}, {"expected_global_rules_hash": ""},
            {"expected_rules_hash": rules_hash("BTC_GBP", self.rules["BTC_GBP"])},
            {"expected_global_rules_hash": "0" * 64},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.enable_all(**changes)
        self.rules["SOL_GBP"]["BUY_ENABLED"] = True
        with self.assertRaisesRegex(ValueError, "Global DCA rules changed"):
            self.enable_all(expected_rules_hash=reviewed, expected_global_rules_hash=reviewed)

    def test_zero_doge_rejects_whole_enable_before_any_market_lookup(self):
        self.rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 0, "MID": 0, "UP": 0}
        before = copy.deepcopy(self.rules)
        minimum = MagicMock(return_value=5)
        with self.assertRaisesRegex(ValueError, "DOGE_GBP.LOW"):
            self.enable_all(market_minimum_provider=minimum)
        minimum.assert_not_called()
        self.assertEqual(self.rules, before)

    def test_one_failed_market_or_missing_quote_rejects_whole_enable(self):
        for bad_minimum in (6, None, float("nan"), float("inf"), True):
            minimums = {target: 5 for target in self.rules}
            minimums["DOGE_GBP"] = bad_minimum
            before = copy.deepcopy(self.rules)
            with self.subTest(minimum=bad_minimum), self.assertRaises(ValueError):
                self.enable_all(market_minimum_provider=minimums)
            self.assertEqual(self.rules, before)
        def source_failure(target):
            if target == "DOGE_GBP":
                raise RuntimeError("market unavailable")
            return 5
        with self.assertRaisesRegex(RuntimeError, "market unavailable"):
            self.enable_all(market_minimum_provider=source_failure)

    def test_pending_order_anywhere_or_malformed_execution_blocks_all_enable(self):
        pending = {"SOL_GBP": {"LAST_BUY_DATE": "", "PENDING_ORDER": {
            "client_order_id": "dca-1234567890abcd", "funding_client_order_id": "dca-fedcba09876543",
            "trade_date": "2026-08-05", "amount_gbp": 10, "decision_id": "old-decision",
            "created_at": self.now.isoformat(),
        }}}
        reviewed = global_rules_pre_state_hash(self.rules)
        for execution in (pending, "{", None, {"UNKNOWN_GBP": {}}):
            before = copy.deepcopy(execution)
            with self.subTest(kind=type(execution).__name__), self.assertRaises(ValueError):
                apply_change(self.rules, self.analysis, execution,
                    action="set_enabled", symbol="all", enabled_json="true",
                    expected_rules_hash=reviewed, expected_global_rules_hash=reviewed,
                    market_minimum_provider=lambda _: 5, now=self.now)
            self.assertEqual(execution, before)

    def test_disable_all_preserves_budgets_and_does_not_inspect_ancillary_state(self):
        for target in ("BTC_GBP", "SOL_GBP"):
            self.rules[target]["BUY_ENABLED"] = True
        self.rules["DOGE_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 0, "MID": 0, "UP": 0}
        before = copy.deepcopy(self.rules)
        minimum = MagicMock(side_effect=AssertionError("must not query market"))
        for analysis, execution in ((None, None), ("{", "{"), ({"invalid": True}, {"PENDING_ORDER": "preserve"})):
            updated, should_write = apply_change(self.rules, analysis, execution,
                action="set_enabled", symbol="all", enabled_json="false", market_minimum_provider=minimum)
            self.assertTrue(should_write)
            self.assertTrue(all(rule["BUY_ENABLED"] is False for rule in updated.values()))
            for target in before:
                self.assertEqual(updated[target]["REGIME_AMOUNTS_GBP"], before[target]["REGIME_AMOUNTS_GBP"])
        minimum.assert_not_called()
        self.assertEqual(self.rules, before)

    def test_only_exact_lowercase_all_set_enabled_is_supported(self):
        for symbol, action in (("ALL", "set_enabled"), ("All", "set_enabled"),
                               ("all ", "set_enabled"), ("all", "set_amounts"), ("all", "dry_run")):
            with self.subTest(symbol=symbol, action=action), self.assertRaises(ValueError):
                apply_change(self.rules, action=action, symbol=symbol, enabled_json="false",
                    low_amount_gbp_json="5", mid_amount_gbp_json="10", up_amount_gbp_json="15")

    def test_partial_bulk_validation_failure_creates_no_output_files(self):
        reviewed = global_rules_pre_state_hash(self.rules)
        environment = {"DCA_TARGET_MAP": json.dumps(self.rules),
                       "DCA_ANALYSIS_STATE": json.dumps(self.analysis), "DCA_EXECUTION_STATE": "{}"}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ, environment), patch(
            "kraken_client.get_market_minimum_gbp", side_effect=lambda target: 6 if target == "DOGE_GBP" else 5
        ):
            rules_output, analysis_output = Path(folder) / "rules.json", Path(folder) / "analysis.json"
            with self.assertRaisesRegex(ValueError, "below Kraken"):
                main(["--action", "set_enabled", "--symbol", "all", "--enabled-json", "true",
                      "--expected-rules-hash", reviewed, "--expected-global-rules-hash", reviewed,
                      "--output", str(rules_output), "--analysis-output", str(analysis_output)])
            self.assertFalse(rules_output.exists())
            self.assertFalse(analysis_output.exists())


if __name__ == "__main__":
    unittest.main()

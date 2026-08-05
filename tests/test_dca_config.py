import copy
from datetime import datetime, timedelta, timezone
import unittest

import dca_config


NOW = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)


def ready_state(rules=None):
    rules = rules or dca_config.default_rules_map()
    state = dca_config.empty_analysis_state(rules, now=NOW)
    for target in dca_config.ALLOWED_TARGETS:
        state["TARGETS"][target] = {
            "STATUS": "READY",
            "REGIME": "SIDEWAYS",
            "AMOUNT_TIER": "LOW",
            "EXECUTE_AT": "2026-08-06T01:00:00Z",
            "VALID_UNTIL": "2026-08-06T02:00:00Z",
            "DECISION_ID": f"decision-{target.lower()}",
            "RULES_HASH": dca_config.rules_hash(target, rules[target]),
            "SIGNALS": {"SMA150_SLOPE_20D": 1.2},
            "TIMING": {
                "ANALYZED_AT": "2026-08-05T21:00:00Z",
                "SELECTED_LOCAL_TIME": "08:00",
            },
        }
    return state


class RulesSchemaTests(unittest.TestCase):
    def test_global_rules_hash_covers_budgets_and_enable_flags(self):
        rules = dca_config.default_rules_map()
        baseline = dca_config.global_rules_hash(rules)
        changed = copy.deepcopy(rules)
        changed["ADA_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        self.assertNotEqual(baseline, dca_config.global_rules_hash(changed))
        changed["ADA_GBP"]["BUY_ENABLED"] = True
        self.assertNotEqual(
            dca_config.global_rules_hash(changed),
            dca_config.global_rules_hash({**changed, "ADA_GBP": {
                **changed["ADA_GBP"], "BUY_ENABLED": False
            }}),
        )

    def test_safe_default_has_exact_four_gbp_targets(self):
        rules = dca_config.validate_rules_map(dca_config.default_rules_map())
        self.assertEqual(tuple(rules), dca_config.ALLOWED_TARGETS)
        for rule in rules.values():
            self.assertEqual(
                rule,
                {
                    "REGIME_AMOUNTS_GBP": {"LOW": 0, "UP": 0},
                    "BUY_ENABLED": False,
                },
            )

    def test_rejects_thb_unknown_missing_and_legacy_fields(self):
        rules = dca_config.default_rules_map()
        rules["BTC_THB"] = rules.pop("BTC_GBP")
        with self.assertRaisesRegex(ValueError, "unsupported targets.*BTC_THB"):
            dca_config.validate_rules_map(rules)

        rules = dca_config.default_rules_map()
        rules.pop("ADA_GBP")
        with self.assertRaisesRegex(ValueError, "missing production targets.*ADA_GBP"):
            dca_config.validate_rules_map(rules)

        for legacy in ("TIME", "AMOUNT", "AMOUNT_GBP", "DYNAMIC_DCA"):
            with self.subTest(legacy=legacy):
                rules = dca_config.default_rules_map()
                rules["BTC_GBP"][legacy] = 10
                with self.assertRaisesRegex(ValueError, "unsupported fields"):
                    dca_config.validate_rules_map(rules)

    def test_zero_budget_is_allowed_only_while_disabled(self):
        rules = dca_config.default_rules_map()
        dca_config.validate_rules_map(rules)
        for amount in (0.01, 1, 4.99):
            with self.subTest(amount=amount):
                invalid = dca_config.default_rules_map()
                invalid["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = amount
                with self.assertRaisesRegex(ValueError, "£0.*or at least £5"):
                    dca_config.validate_rules_map(invalid)
        rules["BTC_GBP"]["BUY_ENABLED"] = True
        with self.assertRaisesRegex(ValueError, "at least £5 before enabling"):
            dca_config.validate_rules_map(rules)

    def test_enabled_budgets_require_bounds_and_live_market_minimum(self):
        rules = dca_config.default_rules_map()
        rules["BTC_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        dca_config.validate_rules_map(rules)
        with self.assertRaisesRegex(ValueError, "below Kraken's current"):
            dca_config.validate_enabled_market_minimums(rules, {"BTC_GBP": 11})
        validated = dca_config.validate_enabled_market_minimums(
            rules, {"BTC/GBP": 10}
        )
        self.assertEqual(validated["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["UP"], 20)

        rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["UP"] = 1000.01
        with self.assertRaisesRegex(ValueError, "between £0 and £1,000"):
            dca_config.validate_rules_map(rules)

    def test_rule_hash_is_stable_and_excludes_buy_enabled(self):
        disabled = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20.0},
            "BUY_ENABLED": False,
        }
        enabled = copy.deepcopy(disabled)
        enabled["BUY_ENABLED"] = True
        self.assertEqual(
            dca_config.rules_hash("BTC_GBP", disabled),
            dca_config.rules_hash("BTC_GBP", enabled),
        )
        changed = copy.deepcopy(disabled)
        changed["REGIME_AMOUNTS_GBP"]["UP"] = 21
        self.assertNotEqual(
            dca_config.rules_hash("BTC_GBP", disabled),
            dca_config.rules_hash("BTC_GBP", changed),
        )

    def test_effective_amount_and_aggregate_exposure(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": False,
        }
        self.assertEqual(dca_config.effective_amount(rule, "UPTREND"), 20.0)
        self.assertEqual(dca_config.effective_amount(rule, "SIDEWAYS"), 10.0)
        rules = dca_config.default_rules_map()
        rules["BTC_GBP"] = {**rule, "BUY_ENABLED": True}
        rules["ETH_GBP"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 5, "UP": 30},
            "BUY_ENABLED": True,
        }
        self.assertEqual(dca_config.maximum_daily_exposure_gbp(rules), 50)


class StateSchemaTests(unittest.TestCase):
    def test_ready_analysis_state_is_bound_to_current_rules(self):
        rules = dca_config.default_rules_map()
        state = ready_state(rules)
        validated = dca_config.validate_analysis_state(state, rules, now=NOW)
        self.assertEqual(set(validated["TARGETS"]), set(dca_config.ALLOWED_TARGETS))

        changed = copy.deepcopy(rules)
        changed["BTC_GBP"]["REGIME_AMOUNTS_GBP"]["LOW"] = 10
        with self.assertRaisesRegex(ValueError, "does not match the live budgets"):
            dca_config.validate_analysis_state(state, changed)

    def test_analysis_schema_rejects_tier_mismatch_and_legacy_target(self):
        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["REGIME"] = "UPTREND"
        with self.assertRaisesRegex(ValueError, "does not match REGIME"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_THB"] = state["TARGETS"].pop("BTC_GBP")
        with self.assertRaisesRegex(ValueError, "unsupported targets.*BTC_THB"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["VALID_UNTIL"] = "2026-08-06T02:01:00Z"
        with self.assertRaisesRegex(ValueError, "exactly 60 minutes"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_GBP"]["EXECUTE_AT"] = "2026-08-05T21:29:00Z"
        state["TARGETS"]["BTC_GBP"]["VALID_UNTIL"] = "2026-08-05T22:29:00Z"
        with self.assertRaisesRegex(ValueError, "at least 30 minutes"):
            dca_config.validate_analysis_state(state)

    def test_error_decisions_are_complete_but_never_executable(self):
        state = dca_config.empty_analysis_state(now=NOW)
        validated = dca_config.validate_analysis_state(state)
        self.assertTrue(
            all(item["STATUS"] == "ERROR" for item in validated["TARGETS"].values())
        )
        usable, reason = dca_config.decision_is_usable(
            validated["TARGETS"]["BTC_GBP"],
            target="BTC_GBP",
            expected_rules_hash=validated["TARGETS"]["BTC_GBP"]["RULES_HASH"],
            now=NOW,
        )
        self.assertFalse(usable)
        self.assertIn("ERROR", reason)

    def test_execution_window_is_inclusive_and_missed_decisions_are_stale(self):
        execute_at = "2026-08-06T01:00:00Z"
        self.assertTrue(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 0, 55, tzinfo=timezone.utc), execute_at
            )
        )
        self.assertTrue(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 2, 0, tzinfo=timezone.utc), execute_at
            )
        )
        self.assertFalse(
            dca_config.is_execution_window(
                datetime(2026, 8, 6, 2, 0, 1, tzinfo=timezone.utc),
                execute_at,
                "2026-08-06T10:00:00Z",
            )
        )

        state = ready_state()
        decision = state["TARGETS"]["BTC_GBP"]
        usable, reason = dca_config.decision_is_usable(
            decision,
            target="BTC_GBP",
            expected_rules_hash=decision["RULES_HASH"],
            now=datetime(2026, 8, 6, 2, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(usable)
        self.assertIn("stale", reason)

    def test_decision_age_uses_per_target_analysis_timestamp(self):
        decision = ready_state()["TARGETS"]["BTC_GBP"]
        self.assertEqual(
            dca_config.decision_age_minutes(
                decision, datetime(2026, 8, 5, 21, 45, tzinfo=timezone.utc)
            ),
            45.0,
        )

    def test_pending_intent_requires_originating_decision_id(self):
        valid = {
            "BTC_GBP": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_ORDER": {
                    "client_order_id": "dca-0123456789abcd",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-1",
                    "created_at": "2026-08-05T21:00:00Z",
                },
            }
        }
        self.assertEqual(
            dca_config.validate_execution_state(valid)["BTC_GBP"]["LAST_BUY_DATE"],
            "2026-08-05",
        )
        del valid["BTC_GBP"]["PENDING_ORDER"]["decision_id"]
        with self.assertRaisesRegex(ValueError, "decision_id"):
            dca_config.validate_execution_state(valid)

    def test_pending_intent_validates_every_durable_recovery_field(self):
        pending = {
            "client_order_id": "dca-0123456789abcd",
            "trade_date": "2026-08-05",
            "amount_gbp": 10,
            "decision_id": "decision-1",
            "created_at": "2026-08-05T21:00:00Z",
        }
        mutations = {
            "client_order_id": "wrong-id",
            "trade_date": "2026-02-30",
            "amount_gbp": 4.99,
            "decision_id": "",
            "created_at": "2026-08-05T21:00:00",
        }
        for field, invalid in mutations.items():
            with self.subTest(field=field):
                candidate = copy.deepcopy(pending)
                candidate[field] = invalid
                with self.assertRaises(ValueError):
                    dca_config.validate_execution_state(
                        {"BTC_GBP": {"PENDING_ORDER": candidate}}
                    )

        extra = copy.deepcopy(pending)
        extra["symbol"] = "BTC_GBP"
        with self.assertRaisesRegex(ValueError, "unsupported fields: symbol"):
            dca_config.validate_execution_state(
                {"BTC_GBP": {"PENDING_ORDER": extra}}
            )


if __name__ == "__main__":
    unittest.main()

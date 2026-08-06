import copy
from datetime import datetime, timedelta, timezone
import unittest
from unittest.mock import patch

import dca_config


NOW = datetime(2026, 8, 5, 21, 0, tzinfo=timezone.utc)


def ready_state(rules=None):
    rules = rules or dca_config.default_rules_map()
    state = dca_config.empty_analysis_state(rules, now=NOW)
    for target in dca_config.ALLOWED_TARGETS:
        state["TARGETS"][target] = {
            "STATUS": "READY",
            "REGIME": "SIDEWAYS",
            "AMOUNT_TIER": "MID",
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
        changed["HYPE_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 15}
        self.assertNotEqual(baseline, dca_config.global_rules_hash(changed))
        changed["HYPE_USD"]["BUY_ENABLED"] = True
        self.assertNotEqual(
            dca_config.global_rules_hash(changed),
            dca_config.global_rules_hash({**changed, "HYPE_USD": {
                **changed["HYPE_USD"], "BUY_ENABLED": False
            }}),
        )

    def test_safe_default_has_exact_three_usd_targets(self):
        rules = dca_config.validate_rules_map(dca_config.default_rules_map())
        self.assertEqual(
            dca_config.ALLOWED_TARGETS,
            ("BTC_USD", "HYPE_USD", "SOL_USD"),
        )
        self.assertEqual(
            dca_config.TARGET_SYMBOLS,
            {"BTC_USD": "BTC/USD", "HYPE_USD": "HYPE/USD", "SOL_USD": "SOL/USD"},
        )
        self.assertEqual(tuple(rules), dca_config.ALLOWED_TARGETS)
        for rule in rules.values():
            self.assertEqual(
                rule,
                {
                    "REGIME_AMOUNTS_GBP": {"LOW": 0, "UP": 0},
                    "BUY_ENABLED": False,
                },
            )

    def test_rejects_legacy_quote_targets_unknown_missing_and_legacy_fields(self):
        for legacy_target in ("BTC_GBP", "BTC_THB", "ETH_GBP", "ADA_GBP"):
            with self.subTest(legacy_target=legacy_target):
                rules = dca_config.default_rules_map()
                rules[legacy_target] = rules.pop("BTC_USD")
                with self.assertRaisesRegex(
                    ValueError, f"unsupported targets.*{legacy_target}"
                ):
                    dca_config.validate_rules_map(rules)

        rules = dca_config.default_rules_map()
        rules.pop("HYPE_USD")
        with self.assertRaisesRegex(ValueError, "missing production targets.*HYPE_USD"):
            dca_config.validate_rules_map(rules)

        for legacy in ("TIME", "AMOUNT", "AMOUNT_GBP", "DYNAMIC_DCA"):
            with self.subTest(legacy=legacy):
                rules = dca_config.default_rules_map()
                rules["BTC_USD"][legacy] = 10
                with self.assertRaisesRegex(ValueError, "unsupported fields"):
                    dca_config.validate_rules_map(rules)

    def test_zero_budget_is_allowed_only_while_disabled(self):
        rules = dca_config.default_rules_map()
        dca_config.validate_rules_map(rules)
        for amount in (0.01, 1, 4.99):
            with self.subTest(amount=amount):
                invalid = dca_config.default_rules_map()
                invalid["BTC_USD"]["REGIME_AMOUNTS_GBP"] = {
                    "LOW": amount,
                    "UP": amount,
                }
                with self.assertRaisesRegex(ValueError, "£0.*or at least £5"):
                    dca_config.validate_rules_map(invalid)
        rules["BTC_USD"]["BUY_ENABLED"] = True
        with self.assertRaisesRegex(ValueError, "at least £5 before enabling"):
            dca_config.validate_rules_map(rules)

    def test_enabled_budgets_require_bounds_and_live_market_minimum(self):
        rules = dca_config.default_rules_map()
        rules["BTC_USD"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": True,
        }
        dca_config.validate_rules_map(rules)
        with self.assertRaisesRegex(ValueError, "below Kraken's current"):
            dca_config.validate_enabled_market_minimums(rules, {"BTC_USD": 11})
        validated = dca_config.validate_enabled_market_minimums(
            rules, {"BTC/USD": 10}
        )
        self.assertEqual(validated["BTC_USD"]["REGIME_AMOUNTS_GBP"]["UP"], 20)

        rules["BTC_USD"]["REGIME_AMOUNTS_GBP"]["UP"] = 1000.01
        with self.assertRaisesRegex(ValueError, "between £0 and £1,000"):
            dca_config.validate_rules_map(rules)

    def test_budget_endpoints_are_ordered_and_currency_precision_is_bounded(self):
        rules = dca_config.default_rules_map()
        rules["BTC_USD"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 20, "UP": 10},
            "BUY_ENABLED": False,
        }
        with self.assertRaisesRegex(ValueError, "LOW must not exceed UP"):
            dca_config.validate_rules_map(rules)

        rules["BTC_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10.001, "UP": 20}
        with self.assertRaisesRegex(ValueError, "no more than two decimal places"):
            dca_config.validate_rules_map(rules)

        rules["BTC_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 10}
        validated = dca_config.validate_rules_map(rules)
        self.assertEqual(
            dca_config.effective_amount_gbp(validated["BTC_USD"], "SIDEWAYS"),
            10,
        )

    def test_rule_hash_is_stable_and_excludes_buy_enabled(self):
        disabled = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20.0},
            "BUY_ENABLED": False,
        }
        enabled = copy.deepcopy(disabled)
        enabled["BUY_ENABLED"] = True
        baseline_hash = dca_config.rules_hash("BTC_USD", disabled)
        self.assertEqual(
            baseline_hash,
            dca_config.rules_hash("BTC_USD", enabled),
        )
        changed = copy.deepcopy(disabled)
        changed["REGIME_AMOUNTS_GBP"]["UP"] = 21
        self.assertNotEqual(
            dca_config.rules_hash("BTC_USD", disabled),
            dca_config.rules_hash("BTC_USD", changed),
        )
        with patch.object(
            dca_config,
            "AMOUNT_POLICY_VERSION",
            dca_config.AMOUNT_POLICY_VERSION + 1,
        ):
            self.assertNotEqual(
                baseline_hash,
                dca_config.rules_hash("BTC_USD", disabled),
            )

    def test_effective_amount_and_aggregate_exposure(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
            "BUY_ENABLED": False,
        }
        self.assertEqual(dca_config.effective_amount(rule, "UPTREND"), 10.0)
        self.assertEqual(dca_config.effective_amount(rule, "SIDEWAYS"), 15.0)
        self.assertEqual(dca_config.effective_amount(rule, "DOWNTREND"), 20.0)
        self.assertEqual(dca_config.amount_tier_for_regime("UPTREND"), "LOW")
        self.assertEqual(dca_config.amount_tier_for_regime("SIDEWAYS"), "MID")
        self.assertEqual(dca_config.amount_tier_for_regime("DOWNTREND"), "HIGH")
        rules = dca_config.default_rules_map()
        rules["BTC_USD"] = {**rule, "BUY_ENABLED": True}
        rules["HYPE_USD"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 15},
            "BUY_ENABLED": True,
        }
        rules["SOL_USD"] = {
            "REGIME_AMOUNTS_GBP": {"LOW": 5, "UP": 15},
            "BUY_ENABLED": True,
        }
        self.assertEqual(dca_config.maximum_daily_exposure_gbp(rules), 50)

    def test_requested_three_asset_policy_keeps_gbp_budgets_on_usd_pairs(self):
        rules = {
            "BTC_USD": {
                "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 20},
                "BUY_ENABLED": True,
            },
            "HYPE_USD": {
                "REGIME_AMOUNTS_GBP": {"LOW": 10, "UP": 15},
                "BUY_ENABLED": True,
            },
            "SOL_USD": {
                "REGIME_AMOUNTS_GBP": {"LOW": 5, "UP": 15},
                "BUY_ENABLED": True,
            },
        }
        validated = dca_config.validate_rules_map(rules)
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "DOWNTREND") for rule in validated.values()],
            [20, 15, 15],
        )
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "SIDEWAYS") for rule in validated.values()],
            [15, 12.5, 10],
        )
        self.assertEqual(
            [dca_config.effective_amount_gbp(rule, "UPTREND") for rule in validated.values()],
            [10, 10, 5],
        )

    def test_sideways_midpoint_uses_half_up_penny_rounding(self):
        rule = {
            "REGIME_AMOUNTS_GBP": {"LOW": 10.01, "UP": 10.02},
            "BUY_ENABLED": False,
        }
        self.assertEqual(dca_config.effective_amount_gbp(rule, "SIDEWAYS"), 10.02)


class StateSchemaTests(unittest.TestCase):
    def test_ready_analysis_state_is_bound_to_current_rules(self):
        rules = dca_config.default_rules_map()
        state = ready_state(rules)
        validated = dca_config.validate_analysis_state(state, rules, now=NOW)
        self.assertEqual(set(validated["TARGETS"]), set(dca_config.ALLOWED_TARGETS))

        changed = copy.deepcopy(rules)
        changed["BTC_USD"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        with self.assertRaisesRegex(ValueError, "does not match the live budgets"):
            dca_config.validate_analysis_state(state, changed)

    def test_analysis_schema_rejects_tier_mismatch_and_legacy_target(self):
        state = ready_state()
        state["TARGETS"]["BTC_USD"]["REGIME"] = "UPTREND"
        with self.assertRaisesRegex(ValueError, "does not match REGIME"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        for legacy_target in ("BTC_GBP", "BTC_THB"):
            with self.subTest(legacy_target=legacy_target):
                state = ready_state()
                state["TARGETS"][legacy_target] = state["TARGETS"].pop("BTC_USD")
                with self.assertRaisesRegex(
                    ValueError, f"unsupported targets.*{legacy_target}"
                ):
                    dca_config.validate_analysis_state(state)

        state = ready_state()
        state["VERSION"] = 1
        with self.assertRaisesRegex(ValueError, "VERSION must be 2"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_USD"].update(
            {"REGIME": "UPTREND", "AMOUNT_TIER": "UP"}
        )
        with self.assertRaisesRegex(ValueError, "AMOUNT_TIER must be LOW, MID, or HIGH"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_USD"]["VALID_UNTIL"] = "2026-08-06T02:01:00Z"
        with self.assertRaisesRegex(ValueError, "exactly 60 minutes"):
            dca_config.validate_analysis_state(state)

        state = ready_state()
        state["TARGETS"]["BTC_USD"]["EXECUTE_AT"] = "2026-08-05T21:29:00Z"
        state["TARGETS"]["BTC_USD"]["VALID_UNTIL"] = "2026-08-05T22:29:00Z"
        with self.assertRaisesRegex(ValueError, "at least 30 minutes"):
            dca_config.validate_analysis_state(state)

    def test_error_decisions_are_complete_but_never_executable(self):
        state = dca_config.empty_analysis_state(now=NOW)
        validated = dca_config.validate_analysis_state(state)
        self.assertTrue(
            all(item["STATUS"] == "ERROR" for item in validated["TARGETS"].values())
        )
        usable, reason = dca_config.decision_is_usable(
            validated["TARGETS"]["BTC_USD"],
            target="BTC_USD",
            expected_rules_hash=validated["TARGETS"]["BTC_USD"]["RULES_HASH"],
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
        decision = state["TARGETS"]["BTC_USD"]
        usable, reason = dca_config.decision_is_usable(
            decision,
            target="BTC_USD",
            expected_rules_hash=decision["RULES_HASH"],
            now=datetime(2026, 8, 6, 2, 1, tzinfo=timezone.utc),
        )
        self.assertFalse(usable)
        self.assertIn("stale", reason)

    def test_decision_age_uses_per_target_analysis_timestamp(self):
        decision = ready_state()["TARGETS"]["BTC_USD"]
        self.assertEqual(
            dca_config.decision_age_minutes(
                decision, datetime(2026, 8, 5, 21, 45, tzinfo=timezone.utc)
            ),
            45.0,
        )

    def test_pending_intent_requires_originating_decision_id(self):
        valid = {
            "BTC_USD": {
                "LAST_BUY_DATE": "2026-08-05",
                "PENDING_ORDER": {
                    "client_order_id": "dca-0123456789abcd",
                    "funding_client_order_id": "dca-fedcba98765432",
                    "trade_date": "2026-08-05",
                    "amount_gbp": 10,
                    "decision_id": "decision-1",
                    "created_at": "2026-08-05T21:00:00Z",
                },
            }
        }
        self.assertEqual(
            dca_config.validate_execution_state(valid)["BTC_USD"]["LAST_BUY_DATE"],
            "2026-08-05",
        )
        del valid["BTC_USD"]["PENDING_ORDER"]["decision_id"]
        with self.assertRaisesRegex(ValueError, "decision_id"):
            dca_config.validate_execution_state(valid)

    def test_pending_intent_validates_every_durable_recovery_field(self):
        pending = {
            "client_order_id": "dca-0123456789abcd",
            "funding_client_order_id": "dca-fedcba98765432",
            "trade_date": "2026-08-05",
            "amount_gbp": 10,
            "decision_id": "decision-1",
            "created_at": "2026-08-05T21:00:00Z",
        }
        mutations = {
            "client_order_id": "wrong-id",
            "funding_client_order_id": "wrong-id",
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
                        {"BTC_USD": {"PENDING_ORDER": candidate}}
                    )

        duplicate_ids = copy.deepcopy(pending)
        duplicate_ids["funding_client_order_id"] = duplicate_ids["client_order_id"]
        with self.assertRaisesRegex(ValueError, "must differ from client_order_id"):
            dca_config.validate_execution_state(
                {"BTC_USD": {"PENDING_ORDER": duplicate_ids}}
            )

        extra = copy.deepcopy(pending)
        extra["symbol"] = "BTC_USD"
        with self.assertRaisesRegex(ValueError, "unsupported fields: symbol"):
            dca_config.validate_execution_state(
                {"BTC_USD": {"PENDING_ORDER": extra}}
            )


if __name__ == "__main__":
    unittest.main()

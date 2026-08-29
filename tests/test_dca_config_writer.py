import unittest
from datetime import datetime, timedelta, timezone

from dca_config import default_rules_map, empty_analysis_state, rules_hash
from dca_config_writer import apply_change, global_rules_pre_state_hash


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
                "HISTORY": {"STATUS": "READY", "HASH": "a" * 64},
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
                "HISTORY": {"STATUS": "READY", "HASH": "a" * 64},
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

    def test_enable_accepts_missing_or_stale_analysis_for_next_cycle(self):
        self.rules["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 10, "UP": 20}
        expected_hash = rules_hash("BTC_GBP", self.rules["BTC_GBP"])
        for analysis in (None, {}, self._ready_state()):
            if analysis:
                analysis["TARGETS"]["BTC_GBP"]["VALID_UNTIL"] = (
                    self.now - timedelta(days=1)
                ).isoformat()
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
                    "HISTORY": {"STATUS": "READY", "HASH": "a" * 64},
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


if __name__ == "__main__":
    unittest.main()

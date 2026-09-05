import copy
from datetime import datetime, timedelta, timezone
import unittest

import dca_config
from tests.history_fixtures import ready_history


ANALYZED_AT = datetime(2026, 8, 23, 0, 0, tzinfo=timezone.utc)


def ready_signals(regime="SIDEWAYS", override="none"):
    signals = {
        "DAILY_LAST_COMPLETE": "2026-08-22T00:00:00Z",
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
        "WEEKLY_LAST_COMPLETE": "2026-08-10T00:00:00Z",
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
        "REGIME_WITHOUT_OVERRIDE": regime,
        "UPTREND_OVERRIDE_ACTIVE": False,
        "UPTREND_OVERRIDE_APPLIED": False,
        "UPTREND_OVERRIDE_REASON": None,
        "UPTREND_OVERRIDE_ACTIVATED_AT": None,
        "UPTREND_OVERRIDE_RELEASED_AT": None,
        "UPTREND_OVERRIDE_AUTO_RELEASED": False,
    }
    if regime == "DOWNTREND":
        signals.update(
            {
                "DAILY_CLOSE": 90.0,
                "DAILY_PREVIOUS_CLOSE": 89.0,
                "DAILY_TWO_DAYS_AGO_CLOSE": 88.0,
                "TWO_DAY_BELOW": True,
                "THREE_DAY_BELOW": True,
                "WEEKLY_CLOSE": 90.0,
                "WEEKLY_ABOVE": False,
                "WEEKLY_BELOW": True,
                "UPTREND_CONFIRMATION_COUNT": 0,
            }
        )
    elif regime == "UPTREND":
        signals.update(
            {
                "DAILY_TWO_DAYS_AGO_CLOSE": 103.0,
                "UPTREND_CONFIRMATION_COUNT": 3,
                "UPTREND_CONFIRMED": True,
            }
        )

    if override == "active":
        signals.update(
            {
                "UPTREND_OVERRIDE_ACTIVE": True,
                "UPTREND_OVERRIDE_APPLIED": regime != "UPTREND",
                "UPTREND_OVERRIDE_REASON": "Operator-requested transition",
                "UPTREND_OVERRIDE_ACTIVATED_AT": "2026-08-22T12:00:00Z",
            }
        )
    elif override in {"released", "auto-released"}:
        released_at = (
            "2026-08-23T00:00:00Z"
            if override == "auto-released"
            else "2026-08-22T18:00:00Z"
        )
        signals.update(
            {
                "UPTREND_OVERRIDE_REASON": "Operator-requested transition",
                "UPTREND_OVERRIDE_ACTIVATED_AT": "2026-08-22T12:00:00Z",
                "UPTREND_OVERRIDE_RELEASED_AT": released_at,
                "UPTREND_OVERRIDE_AUTO_RELEASED": override == "auto-released",
            }
        )
    return signals


def ready_decision(regime="SIDEWAYS", override="none"):
    rules = dca_config.default_rules_map()
    decision = dca_config.empty_analysis_state(rules, now=ANALYZED_AT)["TARGETS"][
        "BTC_GBP"
    ]
    effective_regime = "UPTREND" if override == "active" else regime
    selected_at = ANALYZED_AT + timedelta(hours=1)
    decision.update(
        {
            "ANALYSIS_STATUS": "READY",
            "EXECUTION_STATUS": "ARMED",
            "REGIME": effective_regime,
            "AMOUNT_TIER": dca_config.amount_tier_for_regime(effective_regime),
            "SELECTED_AT": selected_at.isoformat().replace("+00:00", "Z"),
            "EXECUTE_AT": selected_at.isoformat().replace("+00:00", "Z"),
            "VALID_UNTIL": (selected_at + timedelta(minutes=60))
            .isoformat()
            .replace("+00:00", "Z"),
            "HISTORY": ready_history("BTC_GBP", ANALYZED_AT),
            "SIGNALS": ready_signals(regime, override),
            "TIMING": {"ANALYZED_AT": "2026-08-23T00:00:00Z"},
            "ERROR": None,
        }
    )
    return decision


class ReadyAnalysisSignalValidationTests(unittest.TestCase):
    def validate(self, decision):
        return dca_config.validate_analysis_decision("BTC_GBP", decision)

    def test_accepts_natural_active_override_and_released_lifecycles(self):
        cases = (
            ready_decision("SIDEWAYS"),
            ready_decision("DOWNTREND"),
            ready_decision("SIDEWAYS", "active"),
            ready_decision("SIDEWAYS", "released"),
            ready_decision("UPTREND", "auto-released"),
        )
        for decision in cases:
            with self.subTest(signals=decision["SIGNALS"]):
                self.assertEqual(self.validate(decision)["REGIME"], decision["REGIME"])

    def test_requires_exact_ready_signal_fields(self):
        missing = ready_decision()
        missing["SIGNALS"].pop("UPTREND_CONFIRMATION_COUNT")
        with self.assertRaisesRegex(ValueError, "is missing.*UPTREND_CONFIRMATION_COUNT"):
            self.validate(missing)

        extra = ready_decision()
        extra["SIGNALS"]["SOURCE"] = "unvalidated"
        with self.assertRaisesRegex(ValueError, "unsupported fields.*SOURCE"):
            self.validate(extra)

    def test_rejects_invalid_metric_boolean_and_confirmation_types(self):
        mutations = (
            ("DAILY_CLOSE", True, "finite number"),
            ("DAILY_CLOSE", float("inf"), "finite number"),
            ("DAILY_SMA150", 0, "greater than zero"),
            ("TWO_DAY_ABOVE", 1, "must be a boolean"),
            ("UPTREND_CONFIRMATION_REQUIRED", True, "must be 3"),
            ("UPTREND_CONFIRMATION_COUNT", True, "must be an integer"),
            ("UPTREND_CONFIRMATION_COUNT", -1, "between 0 and 3"),
            ("UPTREND_CONFIRMATION_COUNT", 4, "between 0 and 3"),
        )
        for field, value, error in mutations:
            with self.subTest(field=field, value=value):
                decision = ready_decision()
                decision["SIGNALS"][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    self.validate(decision)

    def test_signal_timestamps_must_be_canonical_completed_candle_opens(self):
        decision = ready_decision()
        decision["SIGNALS"]["DAILY_LAST_COMPLETE"] = "2026-08-22T00:00:00+00:00"
        with self.assertRaisesRegex(ValueError, "canonical UTC"):
            self.validate(decision)

        decision = ready_decision()
        decision["SIGNALS"]["DAILY_LAST_COMPLETE"] = "2026-08-22T01:00:00Z"
        with self.assertRaisesRegex(ValueError, "completed candle"):
            self.validate(decision)

        decision = ready_decision()
        decision["SIGNALS"]["WEEKLY_LAST_COMPLETE"] = "2026-08-17T00:00:00Z"
        with self.assertRaisesRegex(ValueError, "completed candle"):
            self.validate(decision)

    def test_confirmation_and_classifier_pairs_are_coherent(self):
        decision = ready_decision()
        decision["SIGNALS"]["UPTREND_CONFIRMED"] = True
        with self.assertRaisesRegex(ValueError, "must match confirmation count"):
            self.validate(decision)

        decision = ready_decision()
        decision["SIGNALS"]["TWO_DAY_ABOVE"] = True
        decision["SIGNALS"]["TWO_DAY_BELOW"] = True
        with self.assertRaisesRegex(ValueError, "cannot both be true"):
            self.validate(decision)

        decision = ready_decision()
        decision["SIGNALS"]["UPTREND_CONFIRMATION_COUNT"] = 1
        decision["SIGNALS"]["DAILY_EMA20"] = 96.0
        decision["SIGNALS"]["DAILY_PREVIOUS_EMA20"] = 95.0
        decision["SIGNALS"]["TWO_DAY_ABOVE"] = True
        with self.assertRaisesRegex(ValueError, "at least two confirming closes"):
            self.validate(decision)

        decision = ready_decision("DOWNTREND")
        decision["SIGNALS"]["UPTREND_CONFIRMATION_COUNT"] = 1
        with self.assertRaisesRegex(ValueError, "zero confirming closes"):
            self.validate(decision)

    def test_normal_regime_must_follow_confirmation_and_downtrend_conjunction(self):
        decision = ready_decision()
        decision["SIGNALS"]["REGIME_WITHOUT_OVERRIDE"] = "DOWNTREND"
        with self.assertRaisesRegex(ValueError, "does not match classifier signals"):
            self.validate(decision)

        decision = ready_decision("DOWNTREND")
        decision["SIGNALS"]["DAILY_EMA20"] = 110.0
        decision["SIGNALS"]["TWO_DAY_BELOW"] = False
        with self.assertRaisesRegex(ValueError, "does not match classifier signals"):
            self.validate(decision)

    def test_weekly_and_slope_signals_are_informational_for_downtrend(self):
        decision = ready_decision("DOWNTREND")
        decision["SIGNALS"].update(
            {
                "WEEKLY_CLOSE": 110.0,
                "WEEKLY_ABOVE": True,
                "WEEKLY_BELOW": False,
                "SMA150_SLOPE_20D": 1.0,
                "SLOPE_POSITIVE": True,
                "SLOPE_NEGATIVE": False,
            }
        )
        self.validate(decision)

    def test_classifier_booleans_cannot_contradict_rounded_metrics(self):
        mutations = (
            ("TWO_DAY_ABOVE", True, "persisted daily metrics"),
            ("WEEKLY_ABOVE", False, "persisted weekly metrics"),
            ("SLOPE_NEGATIVE", False, "SMA150_SLOPE_20D"),
        )
        for field, value, error in mutations:
            with self.subTest(field=field):
                decision = ready_decision()
                decision["SIGNALS"][field] = value
                with self.assertRaisesRegex(ValueError, error):
                    self.validate(decision)

    def test_classifier_comparison_tolerance_allows_rounded_crossovers(self):
        decision = ready_decision()
        decision["SIGNALS"].update(
            {
                "DAILY_EMA20": 95.0,
                "DAILY_EMA50": 95.0,
                "DAILY_PREVIOUS_EMA20": 94.0,
                "DAILY_PREVIOUS_EMA50": 94.0,
                "TWO_DAY_ABOVE": True,
                "SMA150_SLOPE_20D": 0.0,
                "SLOPE_POSITIVE": True,
                "SLOPE_NEGATIVE": False,
            }
        )
        self.validate(decision)

    def test_override_must_bind_effective_regime_and_applied_flag(self):
        decision = ready_decision("SIDEWAYS", "active")
        decision["REGIME"] = "SIDEWAYS"
        decision["AMOUNT_TIER"] = "MID"
        with self.assertRaisesRegex(ValueError, "REGIME does not match classifier"):
            self.validate(decision)

        decision = ready_decision("SIDEWAYS", "active")
        decision["SIGNALS"]["UPTREND_OVERRIDE_APPLIED"] = False
        with self.assertRaisesRegex(ValueError, "APPLIED does not match override state"):
            self.validate(decision)

    def test_override_lifecycle_requires_canonical_ordered_audit_metadata(self):
        decision = ready_decision("SIDEWAYS", "active")
        decision["SIGNALS"]["UPTREND_OVERRIDE_REASON"] = None
        with self.assertRaisesRegex(ValueError, "both be null or populated"):
            self.validate(decision)

        decision = ready_decision("SIDEWAYS", "active")
        decision["SIGNALS"]["UPTREND_OVERRIDE_ACTIVATED_AT"] = (
            "2026-08-22T12:00:00+00:00"
        )
        with self.assertRaisesRegex(ValueError, "canonical UTC"):
            self.validate(decision)

        decision = ready_decision("SIDEWAYS", "released")
        decision["SIGNALS"]["UPTREND_OVERRIDE_RELEASED_AT"] = "2026-08-22T11:00:00Z"
        with self.assertRaisesRegex(ValueError, "cannot precede ACTIVATED_AT"):
            self.validate(decision)

    def test_auto_release_requires_natural_confirmation_at_analysis_time(self):
        decision = ready_decision("SIDEWAYS", "released")
        decision["SIGNALS"]["UPTREND_OVERRIDE_AUTO_RELEASED"] = True
        with self.assertRaisesRegex(ValueError, "naturally confirmed UPTREND"):
            self.validate(decision)

        decision = ready_decision("UPTREND", "auto-released")
        decision["SIGNALS"]["UPTREND_OVERRIDE_RELEASED_AT"] = "2026-08-22T23:59:00Z"
        with self.assertRaisesRegex(ValueError, "must match ANALYZED_AT"):
            self.validate(decision)

    def test_confirmed_active_override_must_have_been_released(self):
        decision = ready_decision("UPTREND", "active")
        with self.assertRaisesRegex(ValueError, "must be auto-released"):
            self.validate(decision)


class NonReadyOverrideAuditCompatibilityTests(unittest.TestCase):
    def test_error_and_history_not_ready_do_not_require_classifier_signals(self):
        state = dca_config.empty_analysis_state(now=ANALYZED_AT)
        decision = state["TARGETS"]["BTC_GBP"]
        dca_config.validate_analysis_decision("BTC_GBP", decision)

        history_not_ready = copy.deepcopy(decision)
        history_not_ready["ANALYSIS_STATUS"] = "HISTORY_NOT_READY"
        history_not_ready["ERROR"] = "History is incomplete"
        history_not_ready["HISTORY"] = {"STATUS": "HISTORY_NOT_READY"}
        history_not_ready["SIGNALS"] = {"ERROR": "History is incomplete"}
        dca_config.validate_analysis_decision("BTC_GBP", history_not_ready)

    def test_non_ready_decision_can_audit_an_active_override(self):
        decision = dca_config.empty_analysis_state(now=ANALYZED_AT)["TARGETS"][
            "BTC_GBP"
        ]
        decision["SIGNALS"].update(
            {
                "REGIME_WITHOUT_OVERRIDE": None,
                "UPTREND_OVERRIDE_ACTIVE": True,
                "UPTREND_OVERRIDE_APPLIED": False,
                "UPTREND_OVERRIDE_REASON": "Operator-requested transition",
                "UPTREND_OVERRIDE_ACTIVATED_AT": "2026-08-22T12:00:00Z",
                "UPTREND_OVERRIDE_RELEASED_AT": None,
                "UPTREND_OVERRIDE_AUTO_RELEASED": False,
            }
        )
        validated = dca_config.validate_analysis_decision("BTC_GBP", decision)
        self.assertTrue(validated["SIGNALS"]["UPTREND_OVERRIDE_ACTIVE"])

        malformed = copy.deepcopy(decision)
        malformed["SIGNALS"]["UPTREND_OVERRIDE_RELEASED_AT"] = (
            "2026-08-22T13:00:00Z"
        )
        with self.assertRaisesRegex(ValueError, "must be null while active"):
            dca_config.validate_analysis_decision("BTC_GBP", malformed)


class DecisionOverrideBindingTests(unittest.TestCase):
    def test_decision_must_match_exact_live_override_entry(self):
        no_override = {"VERSION": 1, "TARGETS": {}}
        natural = ready_decision("SIDEWAYS")
        self.assertTrue(
            dca_config.analysis_decision_matches_uptrend_override(
                "BTC_GBP", natural, no_override
            )
        )

        active_override = {
            "VERSION": 1,
            "TARGETS": {
                "BTC_GBP": {
                    "ACTIVE": True,
                    "ACTIVATED_AT": "2026-08-22T12:00:00Z",
                    "RELEASED_AT": None,
                    "REASON": "Operator-requested transition",
                }
            },
        }
        self.assertFalse(
            dca_config.analysis_decision_matches_uptrend_override(
                "BTC_GBP", natural, active_override
            )
        )
        self.assertTrue(
            dca_config.analysis_decision_matches_uptrend_override(
                "BTC_GBP",
                ready_decision("SIDEWAYS", "active"),
                active_override,
            )
        )
        self.assertFalse(
            dca_config.analysis_decision_matches_uptrend_override(
                "BTC_GBP",
                ready_decision("SIDEWAYS", "active"),
                no_override,
            )
        )

        inactive_override = copy.deepcopy(active_override)
        inactive_entry = inactive_override["TARGETS"]["BTC_GBP"]
        inactive_entry["ACTIVE"] = False
        inactive_entry["RELEASED_AT"] = "2026-08-23T00:00:00Z"
        self.assertTrue(
            dca_config.analysis_decision_matches_uptrend_override(
                "BTC_GBP",
                ready_decision("UPTREND", "auto-released"),
                inactive_override,
            )
        )


if __name__ == "__main__":
    unittest.main()

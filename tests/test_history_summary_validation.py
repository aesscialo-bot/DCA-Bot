from copy import deepcopy
from datetime import datetime, timedelta, timezone
import unittest

import dca_config
from tests.history_fixtures import ready_history


class HistorySummaryValidationTests(unittest.TestCase):
    now = datetime(2026, 9, 5, 0, tzinfo=timezone.utc)

    def validate(self, summary):
        return dca_config.validate_history_summary("DOGE_GBP", summary, analyzed_at=self.now)

    def test_all_four_targets_require_exact_pair_evidence(self):
        for target in dca_config.TARGET_KEYS:
            summary = ready_history(target, self.now)
            self.assertEqual(dca_config.validate_history_summary(target, summary, analyzed_at=self.now), summary)
        with self.assertRaisesRegex(dca_config.ConfigError, "exact Kraken GBP pair"):
            self.validate(ready_history("BTC_GBP", self.now))

    def test_metadata_tampering_and_legacy_summary_fail_closed(self):
        summary = ready_history("DOGE_GBP", self.now)
        summary["LAST_REAL_CANDLE_AT"] = "2026-09-04T22:00:00Z"
        with self.assertRaisesRegex(dca_config.ConfigError, "coverage evidence"):
            self.validate(summary)
        with self.assertRaisesRegex(dca_config.ConfigError, "missing"):
            self.validate({"STATUS": "READY", "HASH": "a" * 64})

    def test_fresh_coverage_does_not_require_a_recent_real_trade(self):
        summary = ready_history("DOGE_GBP", self.now)
        summary["LAST_REAL_CANDLE_AT"] = "2026-09-04T22:00:00Z"
        summary["OVERLAP"]["THROUGH"] = summary["LAST_REAL_CANDLE_AT"]
        summary["CANDLE_COUNT"] -= 7
        summary["NO_TRADE_INTERVALS"] += 7
        summary["CARRIED_NO_TRADE_INTERVALS"] += 7
        summary["HASH"] = dca_config.history_summary_hash(summary)
        self.assertEqual(self.validate(summary), summary)

    def test_forty_five_minute_boundary_is_preserved(self):
        self.validate(ready_history("DOGE_GBP", self.now - timedelta(minutes=45)))
        with self.assertRaisesRegex(dca_config.ConfigError, "stale"):
            self.validate(ready_history("DOGE_GBP", self.now - timedelta(minutes=46)))
        with self.assertRaisesRegex(dca_config.ConfigError, "future"):
            self.validate(ready_history("DOGE_GBP", self.now + timedelta(minutes=15)))

    def test_rehashed_malformed_evidence_is_still_invalid(self):
        base = ready_history("DOGE_GBP", self.now)
        mutations = [
            ("COVERAGE_THROUGH", "2026-09-04T23:59:59Z"),
            ("CANDLE_COUNT", True), ("NO_TRADE_INTERVALS", 1),
            ("CARRIED_NO_TRADE_INTERVALS", 1),
            ("LAST_REAL_CANDLE_AT", "2026-09-05T00:00:00Z"),
            ("VERIFIED_AT", "2026-09-05T00:01:00Z"),
            ("PARTITIONS_HASH", "bad"),
            ("VERSION", 1),
        ]
        for field, value in mutations:
            summary = deepcopy(base)
            summary[field] = value
            summary["HASH"] = dca_config.history_summary_hash(summary)
            with self.subTest(field=field), self.assertRaises(dca_config.ConfigError):
                self.validate(summary)

    def test_previous_timing_policy_cannot_authorize_current_decision(self):
        rule = dca_config.default_rules_map()
        decision = dca_config.empty_analysis_state(rule, now=self.now)["TARGETS"]["DOGE_GBP"]
        decision["POLICY_VERSION"] = "sma150-3-close-responsive-v2+multi-window-3-5-7-14-30-45-60-v3"
        with self.assertRaisesRegex(dca_config.ConfigError, "POLICY_VERSION"):
            dca_config.validate_analysis_decision("DOGE_GBP", decision)


if __name__ == "__main__":
    unittest.main()

import json
from datetime import datetime, timezone
import unittest

from dca_config import empty_analysis_state
from dca_target_migration import migrate


NOW = datetime(2026, 8, 12, 3, 0, tzinfo=timezone.utc)


def source_rules(*, enabled=False):
    return {
        "BTC_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "UP": 25},
            "BUY_ENABLED": enabled,
        },
        "HYPE_USD": {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "UP": 18.75},
            "BUY_ENABLED": enabled,
        },
        "SOL_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "UP": 18.75},
            "BUY_ENABLED": enabled,
        },
    }


def source_analysis(rules=None):
    rules = rules or source_rules()
    mapped_rules = {
        "BTC_GBP": rules["BTC_GBP"],
        "ETH_GBP": rules["HYPE_USD"],
        "SOL_GBP": rules["SOL_GBP"],
    }
    state = empty_analysis_state(mapped_rules, now=NOW)
    state["TARGETS"]["HYPE_USD"] = state["TARGETS"].pop("ETH_GBP")
    return state


def ready_sideways_signals():
    return {
        "DAILY_LAST_COMPLETE": "2026-08-11T00:00:00Z",
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
        "WEEKLY_LAST_COMPLETE": "2026-08-03T00:00:00Z",
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


class TargetMigrationTests(unittest.TestCase):
    def test_replaces_hype_with_disabled_eth_and_resets_analysis(self):
        execution = {
            "BTC_GBP": {"LAST_BUY_DATE": "2026-08-11"},
            "HYPE_USD": {"LAST_BUY_DATE": "2026-08-10"},
            "SOL_GBP": {"LAST_BUY_DATE": "2026-08-09"},
        }
        analysis = source_analysis()
        result = migrate(source_rules(), analysis, execution, now=NOW)

        self.assertEqual(
            tuple(result["DCA_TARGET_MAP"]),
            ("BTC_GBP", "ETH_GBP", "SOL_GBP"),
        )
        self.assertEqual(
            result["DCA_TARGET_MAP"]["ETH_GBP"]["REGIME_AMOUNTS_GBP"],
            {"LOW": 12.5, "MID": 15.63, "UP": 18.75},
        )
        self.assertFalse(result["DCA_TARGET_MAP"]["ETH_GBP"]["BUY_ENABLED"])
        self.assertEqual(
            result["DCA_EXECUTION_STATE"],
            {
                "BTC_GBP": {"LAST_BUY_DATE": "2026-08-11"},
                "ETH_GBP": {"LAST_BUY_DATE": "2026-08-10"},
                "SOL_GBP": {"LAST_BUY_DATE": "2026-08-09"},
            },
        )
        self.assertTrue(
            all(
                row["ANALYSIS_STATUS"] == "AWAITING_ANALYSIS"
                for row in result["DCA_ANALYSIS_STATE"]["TARGETS"].values()
            )
        )
        self.assertEqual(result["DCA_TRADING_MODE"], "shadow")
        self.assertEqual(result["DCA_CANARY_SYMBOL"], "SOL_GBP")

        archive = result["DCA_RETIRED_TARGET_STATE"]
        self.assertEqual(archive["TARGET"], "HYPE_USD")
        self.assertEqual(archive["REPLACED_BY"], "ETH_GBP")
        self.assertEqual(
            archive["EXECUTION"], {"LAST_BUY_DATE": "2026-08-10"}
        )
        self.assertEqual(
            result["DCA_EXECUTION_STATE"]["ETH_GBP"], archive["EXECUTION"]
        )
        self.assertEqual(
            archive["ANALYSIS"], analysis["TARGETS"]["HYPE_USD"]
        )
        self.assertEqual(archive["VERSION"], 2)
        self.assertEqual(archive["MIGRATION"], "HYPE_USD_TO_ETH_GBP")
        self.assertEqual(archive["ANALYSIS_STATE"]["VERSION"], 3)
        self.assertRegex(archive["CANONICAL_HASH"], r"^[0-9a-f]{64}$")
        self.assertEqual(result["_MIGRATION_PHASE"], "SOURCE")
        self.assertFalse(result["_ARCHIVE_PRESENT"])
        self.assertEqual(
            set(result["_CURRENT_STATE_HASHES"]),
            {
                "DCA_TARGET_MAP",
                "DCA_ANALYSIS_STATE",
                "DCA_EXECUTION_STATE",
            },
        )

    def test_resumes_each_legal_partial_write_phase_from_archive(self):
        source_execution = {
            "BTC_GBP": {"LAST_BUY_DATE": "2026-08-11"},
            "HYPE_USD": {"LAST_BUY_DATE": "2026-08-10"},
            "SOL_GBP": {"LAST_BUY_DATE": "2026-08-09"},
        }
        initial = migrate(
            source_rules(), source_analysis(), source_execution, now=NOW
        )
        archive = initial["DCA_RETIRED_TARGET_STATE"]
        source = (source_rules(), source_analysis(), source_execution)
        phases = (
            (source, "ARCHIVED"),
            (
                (
                    source[0],
                    initial["DCA_ANALYSIS_STATE"],
                    source[2],
                ),
                "ANALYSIS_WRITTEN",
            ),
            (
                (
                    source[0],
                    initial["DCA_ANALYSIS_STATE"],
                    initial["DCA_EXECUTION_STATE"],
                ),
                "EXECUTION_WRITTEN",
            ),
            (
                (
                    initial["DCA_TARGET_MAP"],
                    initial["DCA_ANALYSIS_STATE"],
                    initial["DCA_EXECUTION_STATE"],
                ),
                "CORE_COMPLETE",
            ),
        )
        for values, expected_phase in phases:
            with self.subTest(phase=expected_phase):
                resumed = migrate(*values, archive, now=NOW)
                self.assertEqual(resumed["_MIGRATION_PHASE"], expected_phase)
                self.assertTrue(resumed["_ARCHIVE_PRESENT"])
                self.assertEqual(
                    resumed["DCA_RETIRED_TARGET_STATE"], archive
                )
                self.assertEqual(
                    resumed["DCA_TARGET_MAP"], initial["DCA_TARGET_MAP"]
                )
                self.assertEqual(
                    resumed["DCA_ANALYSIS_STATE"],
                    initial["DCA_ANALYSIS_STATE"],
                )
                self.assertEqual(
                    resumed["DCA_EXECUTION_STATE"],
                    initial["DCA_EXECUTION_STATE"],
                )

    def test_rejects_out_of_order_or_foreign_partial_state(self):
        source_execution = {
            target: {"LAST_BUY_DATE": ""}
            for target in ("BTC_GBP", "HYPE_USD", "SOL_GBP")
        }
        initial = migrate(
            source_rules(), source_analysis(), source_execution, now=NOW
        )
        archive = initial["DCA_RETIRED_TARGET_STATE"]
        with self.assertRaisesRegex(ValueError, "violates analysis"):
            migrate(
                source_rules(),
                source_analysis(),
                initial["DCA_EXECUTION_STATE"],
                archive,
                now=NOW,
            )

        changed = json.loads(json.dumps(initial["DCA_ANALYSIS_STATE"]))
        changed["TARGETS"]["ETH_GBP"]["ERROR"] = "foreign state"
        with self.assertRaisesRegex(ValueError, "neither the archived"):
            migrate(
                source_rules(), changed, source_execution, archive, now=NOW
            )

        tampered = json.loads(json.dumps(archive))
        tampered["EXECUTION"]["LAST_BUY_DATE"] = "2026-08-01"
        with self.assertRaisesRegex(ValueError, "canonical hash"):
            migrate(
                source_rules(), source_analysis(), source_execution, tampered, now=NOW
            )

    def test_empty_execution_object_is_valid_but_missing_input_is_not(self):
        result = migrate(source_rules(), source_analysis(), {}, now=NOW)
        self.assertEqual(
            result["DCA_EXECUTION_STATE"],
            {
                "BTC_GBP": {"LAST_BUY_DATE": ""},
                "ETH_GBP": {"LAST_BUY_DATE": ""},
                "SOL_GBP": {"LAST_BUY_DATE": ""},
            },
        )
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            migrate(source_rules(), source_analysis(), "", now=NOW)

    def test_current_day_hype_buy_date_carries_to_eth_with_audit_proof(self):
        execution = {
            "HYPE_USD": {"LAST_BUY_DATE": "2026-08-12"},
        }
        audit = {
            "audit_date": "2026-08-12",
            "hype_completed_flow_dates": ["2026-08-12"],
        }

        result = migrate(
            source_rules(),
            source_analysis(),
            execution,
            audit_raw=audit,
            now=NOW,
        )

        self.assertEqual(
            result["DCA_EXECUTION_STATE"]["ETH_GBP"]["LAST_BUY_DATE"],
            "2026-08-12",
        )

    def test_audit_evidence_must_match_hype_execution_date(self):
        execution = {"HYPE_USD": {"LAST_BUY_DATE": "2026-08-12"}}
        for audit, message in (
            (
                {"audit_date": "2026-08-12", "hype_completed_flow_dates": []},
                "lacks matching",
            ),
            (
                {
                    "audit_date": "2026-08-12",
                    "hype_completed_flow_dates": ["2026-08-11"],
                },
                "does not match",
            ),
            (
                {
                    "audit_date": "2026-08-11",
                    "hype_completed_flow_dates": [],
                },
                "later than",
            ),
        ):
            with self.subTest(audit=audit), self.assertRaisesRegex(
                ValueError, message
            ):
                migrate(
                    source_rules(),
                    source_analysis(),
                    execution,
                    audit_raw=audit,
                    now=NOW,
                )

    def test_audit_evidence_rejects_noncanonical_or_invalid_dates(self):
        for audit in (
            {"audit_date": "20260812", "hype_completed_flow_dates": []},
            {
                "audit_date": "2026-08-12",
                "hype_completed_flow_dates": ["20260812"],
            },
            {
                "audit_date": "2026-08-12",
                "hype_completed_flow_dates": ["2026-08-10"],
            },
            {
                "audit_date": "2026-08-12",
                "hype_completed_flow_dates": ["2026-08-12", "2026-08-11"],
            },
        ):
            with self.subTest(audit=audit), self.assertRaisesRegex(
                ValueError, "audit|multiple"
            ):
                migrate(
                    source_rules(),
                    source_analysis(),
                    {},
                    audit_raw=audit,
                    now=NOW,
                )

    def test_resume_rejects_target_execution_with_blank_carried_eth_date(self):
        source_execution = {
            "BTC_GBP": {"LAST_BUY_DATE": "2026-08-11"},
            "HYPE_USD": {"LAST_BUY_DATE": "2026-08-10"},
            "SOL_GBP": {"LAST_BUY_DATE": "2026-08-09"},
        }
        initial = migrate(
            source_rules(), source_analysis(), source_execution, now=NOW
        )
        changed = json.loads(json.dumps(initial["DCA_EXECUTION_STATE"]))
        changed["ETH_GBP"]["LAST_BUY_DATE"] = ""
        with self.assertRaisesRegex(ValueError, "neither the archived"):
            migrate(
                source_rules(),
                initial["DCA_ANALYSIS_STATE"],
                changed,
                initial["DCA_RETIRED_TARGET_STATE"],
                now=NOW,
            )

    def test_requires_every_source_target_to_be_disabled(self):
        with self.assertRaisesRegex(ValueError, "disable every"):
            rules = source_rules(enabled=True)
            migrate(rules, source_analysis(rules), {}, now=NOW)

    def test_rejects_wrong_source_membership(self):
        rules = source_rules()
        rules["ETH_GBP"] = rules.pop("HYPE_USD")
        with self.assertRaisesRegex(ValueError, "exactly BTC_GBP, HYPE_USD, SOL_GBP"):
            migrate(json.dumps(rules), source_analysis(), "{}", now=NOW)

    def test_blocks_pending_intents_and_deliveries_for_every_source_target(self):
        for target in ("BTC_GBP", "HYPE_USD", "SOL_GBP"):
            for field, value in (
                ("PENDING_ORDER", {"anything": True}),
                ("PENDING_GIST_DELIVERIES", [{}]),
            ):
                with self.subTest(target=target, field=field):
                    execution = {target: {"LAST_BUY_DATE": "", field: value}}
                    with self.assertRaisesRegex(ValueError, "before migration"):
                        migrate(
                            source_rules(), source_analysis(), execution, now=NOW
                        )

    def test_rejects_invalid_or_naive_migration_state(self):
        with self.assertRaisesRegex(ValueError, "unsupported targets"):
            migrate(
                source_rules(), source_analysis(), {"ADA_GBP": {}}, now=NOW
            )
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            migrate(
                source_rules(), source_analysis(), {}, now=NOW.replace(tzinfo=None)
            )

    def test_rejects_incomplete_or_rules_mismatched_source_analysis(self):
        incomplete = source_analysis()
        incomplete["TARGETS"].pop("HYPE_USD")
        with self.assertRaisesRegex(ValueError, "source analysis must contain exactly"):
            migrate(source_rules(), incomplete, {}, now=NOW)

        ready = source_analysis()
        decision = ready["TARGETS"]["HYPE_USD"]
        decision.update({
            "ANALYSIS_STATUS": "READY",
            "EXECUTION_STATUS": "DISABLED",
            "REGIME": "SIDEWAYS",
            "AMOUNT_TIER": "MID",
            "SELECTED_AT": "2026-08-12T04:00:00Z",
            "EXECUTE_AT": "2026-08-12T04:00:00Z",
            "VALID_UNTIL": "2026-08-12T05:00:00Z",
            "HISTORY": {"STATUS": "READY", "HASH": "a" * 64},
            "SIGNALS": ready_sideways_signals(),
            "ERROR": None,
        })
        decision["RULES_HASH"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "rules hash does not match HYPE_USD"):
            migrate(source_rules(), ready, {}, now=NOW)


if __name__ == "__main__":
    unittest.main()

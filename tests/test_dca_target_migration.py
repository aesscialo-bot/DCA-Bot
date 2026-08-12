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
            {"LOW": 12.5, "UP": 18.75},
        )
        self.assertFalse(result["DCA_TARGET_MAP"]["ETH_GBP"]["BUY_ENABLED"])
        self.assertEqual(
            result["DCA_EXECUTION_STATE"],
            {
                "BTC_GBP": {"LAST_BUY_DATE": "2026-08-11"},
                "ETH_GBP": {"LAST_BUY_DATE": ""},
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
            "ERROR": None,
        })
        decision["RULES_HASH"] = "b" * 64
        with self.assertRaisesRegex(ValueError, "rules hash does not match HYPE_USD"):
            migrate(source_rules(), ready, {}, now=NOW)


if __name__ == "__main__":
    unittest.main()

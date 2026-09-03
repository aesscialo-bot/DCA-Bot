import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
import unittest

from dca_add_target_migration import migrate
from dca_config import empty_analysis_state


NOW = datetime(2026, 9, 1, 3, 0, tzinfo=timezone.utc)
SOURCE_TARGETS = ("BTC_GBP", "ETH_GBP", "SOL_GBP")
TARGET_TARGETS = (*SOURCE_TARGETS, "DOGE_GBP")
ARCHIVE_NAME = "DCA_DOGE_GBP_MIGRATION_STATE"
ROOT = Path(__file__).resolve().parents[1]


def source_rules(*, enabled_target=None):
    rules = {
        "BTC_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "MID": 18.75, "UP": 25},
            "BUY_ENABLED": False,
        },
        "ETH_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 12.5, "MID": 15, "UP": 18.75},
            "BUY_ENABLED": False,
        },
        "SOL_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 10, "MID": 15, "UP": 20},
            "BUY_ENABLED": False,
        },
    }
    if enabled_target is not None:
        rules[enabled_target]["BUY_ENABLED"] = True
    return rules


def source_analysis(rules=None):
    rules = rules or source_rules()
    complete_rules = {
        **rules,
        "DOGE_GBP": {
            "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
            "BUY_ENABLED": False,
        },
    }
    state = empty_analysis_state(complete_rules, now=NOW)
    state["TARGETS"].pop("DOGE_GBP")
    return state


def source_execution():
    return {
        "BTC_GBP": {"LAST_BUY_DATE": "2026-08-31"},
        "ETH_GBP": {"LAST_BUY_DATE": "2026-08-30"},
        "SOL_GBP": {"LAST_BUY_DATE": "2026-08-29"},
    }


def canonical_hash(value):
    encoded = json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AddTargetMigrationTests(unittest.TestCase):
    def test_adds_disabled_zero_budget_doge_and_resets_complete_analysis(self):
        result = migrate(
            source_rules(), source_analysis(), source_execution(), now=NOW
        )

        self.assertEqual(tuple(result["DCA_TARGET_MAP"]), TARGET_TARGETS)
        self.assertEqual(
            result["DCA_TARGET_MAP"]["DOGE_GBP"],
            {
                "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
                "BUY_ENABLED": False,
            },
        )
        self.assertTrue(
            all(
                rule["BUY_ENABLED"] is False
                for rule in result["DCA_TARGET_MAP"].values()
            )
        )
        self.assertEqual(
            result["DCA_EXECUTION_STATE"],
            {
                "BTC_GBP": {"LAST_BUY_DATE": "2026-08-31"},
                "ETH_GBP": {"LAST_BUY_DATE": "2026-08-30"},
                "SOL_GBP": {"LAST_BUY_DATE": "2026-08-29"},
                "DOGE_GBP": {"LAST_BUY_DATE": ""},
            },
        )
        analysis = result["DCA_ANALYSIS_STATE"]
        self.assertEqual(tuple(analysis["TARGETS"]), TARGET_TARGETS)
        self.assertTrue(
            all(
                decision["ANALYSIS_STATUS"] == "AWAITING_ANALYSIS"
                and decision["REGIME"] is None
                and decision["EXECUTION_STATUS"] == "DISABLED"
                for decision in analysis["TARGETS"].values()
            )
        )
        self.assertEqual(result["DCA_TRADING_MODE"], "shadow")
        self.assertEqual(result["DCA_CANARY_SYMBOL"], "SOL_GBP")

        archive = result[ARCHIVE_NAME]
        self.assertEqual(archive["VERSION"], 1)
        self.assertEqual(archive["MIGRATION"], "ADD_DOGE_GBP")
        self.assertEqual(archive["ADDED_TARGET"], "DOGE_GBP")
        self.assertEqual(
            archive["SOURCE_LAST_BUY_DATES"],
            {
                "BTC_GBP": "2026-08-31",
                "ETH_GBP": "2026-08-30",
                "SOL_GBP": "2026-08-29",
            },
        )
        payload = {
            key: value
            for key, value in archive.items()
            if key != "CANONICAL_HASH"
        }
        self.assertEqual(archive["CANONICAL_HASH"], canonical_hash(payload))
        self.assertNotIn("DCA_RETIRED_TARGET_STATE", result)
        self.assertEqual(result["_MIGRATION_PHASE"], "SOURCE")
        self.assertFalse(result["_ARCHIVE_PRESENT"])

    def test_resumes_every_legal_partial_write_phase_idempotently(self):
        rules = source_rules()
        analysis = source_analysis()
        execution = source_execution()
        initial = migrate(rules, analysis, execution, now=NOW)
        archive = initial[ARCHIVE_NAME]
        phases = (
            ((rules, analysis, execution), "ARCHIVED"),
            (
                (rules, initial["DCA_ANALYSIS_STATE"], execution),
                "ANALYSIS_WRITTEN",
            ),
            (
                (
                    rules,
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
                self.assertEqual(resumed[ARCHIVE_NAME], archive)
                for name in (
                    "DCA_TARGET_MAP",
                    "DCA_ANALYSIS_STATE",
                    "DCA_EXECUTION_STATE",
                ):
                    self.assertEqual(resumed[name], initial[name])

    def test_rejects_out_of_order_foreign_or_tampered_resume_state(self):
        rules = source_rules()
        analysis = source_analysis()
        execution = source_execution()
        initial = migrate(rules, analysis, execution, now=NOW)
        archive = initial[ARCHIVE_NAME]

        with self.assertRaisesRegex(ValueError, "violates analysis"):
            migrate(
                rules,
                analysis,
                initial["DCA_EXECUTION_STATE"],
                archive,
                now=NOW,
            )

        foreign_analysis = json.loads(
            json.dumps(initial["DCA_ANALYSIS_STATE"])
        )
        foreign_analysis["TARGETS"]["DOGE_GBP"]["ERROR"] = "foreign state"
        with self.assertRaisesRegex(ValueError, "neither the archived"):
            migrate(rules, foreign_analysis, execution, archive, now=NOW)

        tampered = json.loads(json.dumps(archive))
        tampered["SOURCE_LAST_BUY_DATES"]["ETH_GBP"] = "2026-08-01"
        with self.assertRaisesRegex(ValueError, "canonical hash"):
            migrate(rules, analysis, execution, tampered, now=NOW)

    def test_requires_exact_disabled_three_target_source_rules_and_analysis(self):
        enabled = source_rules(enabled_target="ETH_GBP")
        with self.assertRaisesRegex(ValueError, "disable every source"):
            migrate(enabled, source_analysis(enabled), {}, now=NOW)

        for mutate_rules in ("missing", "extra"):
            with self.subTest(rules=mutate_rules):
                rules = source_rules()
                if mutate_rules == "missing":
                    rules.pop("SOL_GBP")
                else:
                    rules["DOGE_GBP"] = {
                        "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
                        "BUY_ENABLED": False,
                    }
                with self.assertRaisesRegex(ValueError, "exactly BTC_GBP"):
                    migrate(rules, source_analysis(), {}, now=NOW)

        for mutate_analysis in ("missing", "extra"):
            with self.subTest(analysis=mutate_analysis):
                analysis = source_analysis()
                if mutate_analysis == "missing":
                    analysis["TARGETS"].pop("ETH_GBP")
                else:
                    full = empty_analysis_state(
                        {
                            **source_rules(),
                            "DOGE_GBP": {
                                "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
                                "BUY_ENABLED": False,
                            },
                        },
                        now=NOW,
                    )
                    analysis["TARGETS"]["DOGE_GBP"] = full["TARGETS"][
                        "DOGE_GBP"
                    ]
                with self.assertRaisesRegex(ValueError, "source analysis must contain exactly"):
                    migrate(source_rules(), analysis, {}, now=NOW)

    def test_blocks_pending_intents_and_delivery_queues_for_each_source(self):
        for target in SOURCE_TARGETS:
            for field, value in (
                ("PENDING_ORDER", {"unresolved": True}),
                ("PENDING_GIST_DELIVERIES", [{"undelivered": True}]),
            ):
                with self.subTest(target=target, field=field):
                    execution = source_execution()
                    execution[target][field] = value
                    with self.assertRaisesRegex(ValueError, "before migration"):
                        migrate(
                            source_rules(), source_analysis(), execution, now=NOW
                        )

    def test_empty_execution_is_valid_but_missing_or_foreign_state_is_not(self):
        result = migrate(source_rules(), source_analysis(), {}, now=NOW)
        self.assertEqual(
            result["DCA_EXECUTION_STATE"],
            {
                target: {"LAST_BUY_DATE": ""}
                for target in TARGET_TARGETS
            },
        )
        with self.assertRaisesRegex(ValueError, "not valid JSON"):
            migrate(source_rules(), source_analysis(), "", now=NOW)
        with self.assertRaisesRegex(ValueError, "unsupported targets"):
            migrate(
                source_rules(),
                source_analysis(),
                {"DOGE_GBP": {"LAST_BUY_DATE": ""}},
                now=NOW,
            )

    def test_rejects_invalid_buy_date_and_naive_time(self):
        with self.assertRaisesRegex(ValueError, "invalid buy date"):
            migrate(
                source_rules(),
                source_analysis(),
                {"BTC_GBP": {"LAST_BUY_DATE": "09/01/2026"}},
                now=NOW,
            )
        with self.assertRaisesRegex(ValueError, "include a timezone"):
            migrate(
                source_rules(),
                source_analysis(),
                {},
                now=NOW.replace(tzinfo=None),
            )

class AddTargetWorkflowTests(unittest.TestCase):
    def test_workflow_is_guarded_audited_masked_and_cas_verified(self):
        text = (
            ROOT / ".github" / "workflows" / "add_doge_gbp.yml"
        ).read_text(encoding="utf-8")
        for required in (
            "github.ref == 'refs/heads/main'",
            "inputs.confirmation == 'ADD_DOGE_GBP'",
            "inputs.scheduler_confirmation == 'RAILWAY_CRON_PAUSED'",
            "DCA_TARGET_MIGRATION_LOCK",
            "ADD_DOGE_GBP:${GITHUB_RUN_ID}",
            "group: dca-execution-state-writers",
            "cancel-in-progress: false",
            "audit_orders",
            'result["flow_integrity_ok"]',
            'result["unresolved_bot_orders"]',
            'result["unknown_timestamp_closed_bot_orders"]',
            "gh variable get DCA_TARGET_MAP",
            "gh variable get DCA_ANALYSIS_STATE",
            "gh variable get DCA_EXECUTION_STATE",
            "::add-mask::%s",
            "dca_add_target_migration.py",
            "--migration-state \"$migration_state\"",
            "DCA_DOGE_GBP_MIGRATION_STATE",
            "DCA_CANARY_SYMBOL",
            'data.pop("_CURRENT_STATE_HASHES")',
            "changed after migration validation; refusing write",
            "readback verification failed",
            '"DCA_ANALYSIS_STATE",\n              "DCA_EXECUTION_STATE",\n              "DCA_TARGET_MAP"',
            "final {name} verification failed",
            "gh variable delete DCA_TARGET_MIGRATION_LOCK",
        ):
            with self.subTest(required=required):
                self.assertIn(required, text)
        self.assertNotIn("DCA_RETIRED_TARGET_STATE", text)
        self.assertNotIn('echo "$rules"', text)
        self.assertNotIn('echo "$analysis"', text)
        self.assertNotIn('echo "$execution"', text)
        self.assertLess(
            text.index("current_archive = read_optional_json"),
            text.index("# Rules are the compatibility boundary"),
        )
        self.assertLess(
            text.index(
                '"DCA_ANALYSIS_STATE",\n              "DCA_EXECUTION_STATE"'
            ),
            text.index(
                '"DCA_EXECUTION_STATE",\n              "DCA_TARGET_MAP"'
            ),
        )
        self.assertGreater(
            text.index("gh variable delete DCA_TARGET_MIGRATION_LOCK"),
            text.index("final {name} verification failed"),
        )


if __name__ == "__main__":
    unittest.main()

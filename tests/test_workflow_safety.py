import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


class WorkflowSafetyTests(unittest.TestCase):
    def _read(self, name):
        return (WORKFLOWS / name).read_text(encoding="utf-8")

    def test_official_actions_are_node_24_compatible(self):
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            with self.subTest(workflow=path.name):
                self.assertNotIn("actions/checkout@", text.replace(
                    "actions/checkout@v7.0.1", ""
                ))
                self.assertNotIn("actions/setup-python@", text.replace(
                    "actions/setup-python@v7.0.0", ""
                ))

    def test_state_writers_preserve_queued_requests_and_require_main(self):
        for name in (
            "crypto_analysis.yml",
            "daily_dca.yml",
            "update_dca_config.yml",
        ):
            text = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("cancel-in-progress: false", text)
                self.assertIn("github.ref == 'refs/heads/main'", text)

    def test_private_outbox_token_is_separate_from_repo_variable_token(self):
        for name in (
            "daily_dca.yml",
            "ghostfolio_holdings_snapshot.yml",
            "recover_ghostfolio_event.yml",
        ):
            text = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn(
                    "DCA_OUTBOX_REPOSITORY_TOKEN: ${{ secrets.DCA_OUTBOX_REPOSITORY_TOKEN }}",
                    text,
                )
                self.assertNotIn("GIST_ID:", text)
                self.assertNotIn("GIST_TOKEN:", text)
                self.assertIn("DCA_OUTBOX_REPOSITORY_OWNER:", text)
                self.assertIn("DCA_OUTBOX_REPOSITORY_NAME:", text)
                self.assertIn("DCA_OUTBOX_REPOSITORY_BRANCH:", text)
                self.assertIn("DCA_OUTBOX_AUDIT_PATH:", text)
                self.assertIn("DCA_OUTBOX_EVENT_PATH:", text)
                self.assertIn("DCA_OUTBOX_HOLDINGS_PATH:", text)
        self.assertIn(
            "GH_PAT_FOR_VARS: ${{ secrets.GH_PAT_FOR_VARS }}",
            self._read("daily_dca.yml"),
        )

    def test_daily_dca_has_no_direct_ghostfolio_credentials(self):
        text = self._read("daily_dca.yml")
        self.assertIn('GHOSTFOLIO_DIRECT_SYNC_ENABLED: "false"', text)
        self.assertNotIn("GHOSTFOLIO_TOKEN:", text)
        self.assertNotIn("GHOSTFOLIO_URL:", text)
        self.assertNotIn("PORTFOLIO_ACCOUNT_MAP:", text)

    def test_portfolio_has_no_push_trigger(self):
        text = self._read("portfolio_check.yml")
        self.assertNotRegex(text, r"(?m)^\s*push:\s*$")

    def test_workflows_do_not_echo_complete_production_state(self):
        forbidden = (
            'echo "$DCA_TARGET_MAP"',
            'echo "$DCA_ANALYSIS_STATE"',
            'echo "$DCA_EXECUTION_STATE"',
            "print(os.environ['DCA_TARGET_MAP'])",
            'print(os.environ["DCA_TARGET_MAP"])',
            "DCA_TARGET_MAP: ${{ vars.DCA_TARGET_MAP }}",
            "DCA_ANALYSIS_STATE: ${{ vars.DCA_ANALYSIS_STATE",
            "DCA_EXECUTION_STATE: ${{ vars.DCA_EXECUTION_STATE",
        )
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for value in forbidden:
                with self.subTest(workflow=path.name, forbidden=value):
                    self.assertNotIn(value, text)

    def test_production_json_is_loaded_and_masked_inside_workflows(self):
        required_variables = {
            "crypto_analysis.yml": ("DCA_TARGET_MAP", "DCA_ANALYSIS_STATE"),
            "daily_dca.yml": (
                "DCA_TARGET_MAP",
                "DCA_ANALYSIS_STATE",
                "DCA_EXECUTION_STATE",
            ),
            "portfolio_check.yml": ("DCA_TARGET_MAP",),
        }
        for name, variables in required_variables.items():
            text = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("::add-mask::%s", text)
                self.assertIn("$GITHUB_ENV", text)
                for variable in variables:
                    self.assertTrue(
                        f"gh variable get {variable}" in text
                        or f"load_variable {variable}" in text,
                        f"{name} does not load {variable}",
                    )

    def test_workflow_inputs_and_steps_describe_gbp_targets_only(self):
        analysis = self._read("crypto_analysis.yml")
        writer = self._read("update_dca_config.yml")
        portfolio = self._read("portfolio_check.yml")
        self.assertIn("BTC, ETH, SOL, a canonical configured pair, or all", analysis)
        self.assertIn("Analyze configured Kraken markets", analysis)
        self.assertNotIn("HYPE", analysis)
        self.assertNotIn("ADA", analysis)
        self.assertIn("BTC_GBP, ETH_GBP, or SOL_GBP", writer)
        self.assertIn("Canonical Kraken key", writer)
        self.assertIn("--targets BTC_GBP,ETH_GBP,SOL_GBP", portfolio)
        self.assertNotIn("HYPE_USD", portfolio)

    def test_hype_to_eth_migration_is_main_only_audited_and_masks_state(self):
        migration = self._read("migrate_hype_to_eth.yml")
        portfolio = self._read("portfolio_check.yml")
        for required in (
            "github.ref == 'refs/heads/main'",
            "MIGRATE_HYPE_TO_ETH_GBP",
            "RAILWAY_CRON_PAUSED",
            "audit_orders(profile=HYPE_TO_ETH_SOURCE_AUDIT_PROFILE)",
            'result["audit_profile"]',
            'result["safe_for_target_migration"]',
            'same_day_completed_dca_flow_dates_by_target',
            'migration-audit.json',
            "gh variable get DCA_TARGET_MAP",
            "gh variable get DCA_ANALYSIS_STATE",
            "gh variable get DCA_EXECUTION_STATE",
            "::add-mask::%s",
            'done <<< "$analysis"',
            "dca_target_migration.py",
            '--analysis "$analysis"',
            '--audit "$audit"',
            '_CURRENT_STATE_HASHES',
            "changed after migration validation; refusing write",
            "DCA_RETIRED_TARGET_STATE",
            "DCA_TARGET_MIGRATION_LOCK",
            "--retired \"$retired\"",
            '"DCA_ANALYSIS_STATE",\n              "DCA_EXECUTION_STATE",\n              "DCA_TARGET_MAP"',
            "readback verification failed",
            "group: dca-execution-state-writers",
            "cancel-in-progress: false",
        ):
            with self.subTest(required=required):
                self.assertIn(required, migration)
        self.assertNotIn('echo "$rules"', migration)
        self.assertNotIn('echo "$execution"', migration)
        self.assertNotIn("HYPE_TO_ETH_SOURCE_AUDIT_PROFILE", portfolio)
        self.assertNotIn(
            'DCA_EXECUTION_STATE --repo "$GITHUB_REPOSITORY" 2>/dev/null ||',
            migration,
        )
        self.assertIn("gh run list", migration)
        self.assertIn("--status in_progress", migration)
        self.assertLess(
            migration.index('"DCA_RETIRED_TARGET_STATE"'),
            migration.index('# Rules are the compatibility boundary'),
        )
        self.assertLess(
            migration.index('"DCA_ANALYSIS_STATE",\n              "DCA_EXECUTION_STATE"'),
            migration.index('"DCA_EXECUTION_STATE",\n              "DCA_TARGET_MAP"'),
        )
        self.assertGreater(
            migration.index("gh variable delete DCA_TARGET_MIGRATION_LOCK"),
            migration.index("final {name} verification failed"),
        )

    def test_cutover_docs_preserve_only_the_hype_daily_guard(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        guide = (ROOT / "00_START_HERE.md").read_text(encoding="utf-8")
        for text in (readme, guide):
            self.assertIn("validated `LAST_BUY_DATE`", text)
            self.assertIn("once-per-Bangkok-day allocation guard", text)
            self.assertNotIn("empty buy date", text)
            self.assertNotIn("no inherited buy date", text)
            self.assertNotIn("copy HYPE's buy date or analysis to ETH", text)

    def test_all_state_writers_observe_the_target_migration_lock(self):
        for name in (
            "crypto_analysis.yml",
            "daily_dca.yml",
            "update_dca_config.yml",
        ):
            text = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("DCA_TARGET_MIGRATION_LOCK", text)
        analysis_module = (ROOT / "crypto_analysis.py").read_text(encoding="utf-8")
        self.assertIn("TARGET_MIGRATION_LOCK_VARIABLE", analysis_module)
        self.assertIn("blocks analysis persistence", analysis_module)
        trader_module = (ROOT / "crypto_dca.py").read_text(encoding="utf-8")
        self.assertIn("TARGET_MIGRATION_LOCK_VARIABLE", trader_module)
        self.assertIn("blocks execution-state writes", trader_module)

    def test_start_date_gate_is_passed_to_analysis_and_trader(self):
        for name in ("crypto_analysis.yml", "daily_dca.yml"):
            text = self._read(name)
            with self.subTest(workflow=name):
                self.assertIn("DCA_START_DATE: ${{ vars.DCA_START_DATE }}", text)

    def test_enable_writer_receives_global_pre_state_and_execution_state(self):
        text = self._read("update_dca_config.yml")
        self.assertIn("expected_global_rules_hash:", text)
        self.assertIn("EXPECTED_GLOBAL_RULES_HASH: ${{ inputs.expected_global_rules_hash }}", text)
        self.assertIn("--expected-global-rules-hash", text)
        self.assertIn("gh variable get DCA_EXECUTION_STATE", text)
        self.assertIn("export DCA_TARGET_MAP DCA_ANALYSIS_STATE DCA_EXECUTION_STATE", text)
        self.assertIn("group: dca-rule-writers", text)

    def test_rule_writer_uses_max_queue_without_cancelling_active(self):
        text = self._read("update_dca_config.yml")
        self.assertIn("group: dca-rule-writers", text)
        self.assertIn("queue: max", text)
        self.assertIn("cancel-in-progress: false", text)


if __name__ == "__main__":
    unittest.main()

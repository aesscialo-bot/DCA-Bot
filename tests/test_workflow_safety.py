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

    def test_gist_token_is_separate_from_repo_variable_token(self):
        text = self._read("daily_dca.yml")
        self.assertIn("GIST_TOKEN: ${{ secrets.GIST_TOKEN }}", text)
        self.assertIn("GH_PAT_FOR_VARS: ${{ secrets.GH_PAT_FOR_VARS }}", text)

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

    def test_workflow_inputs_and_steps_describe_usd_targets_only(self):
        analysis = self._read("crypto_analysis.yml")
        writer = self._read("update_dca_config.yml")
        self.assertIn("BTC, HYPE, SOL, a canonical USD pair, or all", analysis)
        self.assertIn("Analyze Kraken USD markets", analysis)
        self.assertNotIn("ETH", analysis)
        self.assertNotIn("ADA", analysis)
        self.assertIn("BTC_USD, HYPE_USD, or SOL_USD", writer)
        self.assertNotIn("Canonical Kraken GBP key", writer)

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


if __name__ == "__main__":
    unittest.main()

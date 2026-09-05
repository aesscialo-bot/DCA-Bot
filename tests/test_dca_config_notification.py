import contextlib
import io
import json
import os
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import dca_config_notification as receipt
from dca_config import default_rules_map, empty_analysis_state


class ConfigurationReceiptTests(unittest.TestCase):
    def setUp(self):
        self.arguments = {
            "action": "set_enabled",
            "symbol": "DOGE_GBP",
            "outcome": "success",
            "verified_result": "applied",
            "repository": "aesscialo-bot/DCA-Bot",
            "run_id": "123456",
            "run_url": "https://github.com/aesscialo-bot/DCA-Bot/actions/runs/123456",
        }

    def payload(self, **changes):
        return receipt.notification_payload(**(self.arguments | changes))

    def test_applied_requires_success_and_confirmed_readback(self):
        self.assertIn("APPLIED", self.payload()["content"])
        for changes in (
            {"verified_result": ""},
            {"verified_result": "validated"},
            {"outcome": "failure"},
            {"outcome": "cancelled"},
            {"outcome": "skipped"},
        ):
            with self.subTest(changes=changes):
                content = self.payload(**changes)["content"]
                self.assertNotIn("APPLIED", content)
                self.assertIn("NOT CONFIRMED", content)
                self.assertIn("may already have occurred", content)

    def test_dry_run_is_not_applied(self):
        content = self.payload(action="dry_run", verified_result="validated")["content"]
        self.assertIn("VALIDATED", content)
        self.assertIn("no configuration was changed", content)
        self.assertNotIn("APPLIED", content)

    def test_untrusted_inputs_are_not_rendered_and_mentions_disabled(self):
        injected = "@everyone <@12345> ``` https://evil.example/secrets"
        result = self.payload(symbol=injected, action=injected)
        self.assertNotIn(injected, result["content"])
        self.assertNotIn("@", result["content"])
        self.assertNotIn("APPLIED", result["content"])
        self.assertEqual(result["allowed_mentions"]["parse"], [])
        self.assertFalse(result["allowed_mentions"]["replied_user"])
        self.assertLess(len(result["content"]), 2000)

    def test_external_or_mismatched_run_links_rejected(self):
        for url in (
            "https://evil.example/actions/runs/123456",
            "https://github.com.evil.example/aesscialo-bot/DCA-Bot/actions/runs/123456",
            "https://github.com/aesscialo-bot/other/actions/runs/123456",
            self.arguments["run_url"] + "?token=secret",
            self.arguments["run_url"] + "/other",
            self.arguments["run_url"].replace("123456", "123457"),
        ):
            with self.subTest(url=url), self.assertRaises(ValueError):
                self.payload(run_url=url)

    def test_rules_readback_requires_complete_exact_configuration(self):
        expected = default_rules_map()
        encoded = json.dumps(expected)
        receipt.verify_readback(encoded, json.dumps(expected, indent=2))
        changed = default_rules_map()
        changed["BTC_GBP"]["REGIME_AMOUNTS_GBP"] = {"LOW": 5, "MID": 10, "UP": 20}
        with self.assertRaisesRegex(ValueError, "did not match"):
            receipt.verify_readback(encoded, json.dumps(changed))
        for invalid in ("{", "{}", json.dumps(expected | {"@everyone": {}})):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                receipt.verify_readback(encoded, invalid)

    def test_send_is_single_attempt_with_bounded_timeout(self):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.status = 204
        webhook = "https://discord.com/api/webhooks/123/test_secret"
        payload = self.payload()
        self.assertTrue(receipt.send_notification(webhook, payload, opener=opener))
        opener.open.assert_called_once()
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 15)
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data), payload)

    def test_analysis_invalidation_readback_must_match_every_target(self):
        state = empty_analysis_state(default_rules_map())
        encoded = json.dumps(state)
        receipt.verify_analysis_readback(encoded, encoded)
        state["TARGETS"]["DOGE_GBP"]["ERROR"] = "Different write"
        with self.assertRaisesRegex(ValueError, "did not match"):
            receipt.verify_analysis_readback(encoded, json.dumps(state))
        with self.assertRaisesRegex(ValueError, "could not be validated"):
            receipt.verify_analysis_readback(encoded, "{}")

    def test_webhook_redirects_and_external_hosts_are_rejected(self):
        opener = MagicMock()
        for url in (
            "http://discord.com/api/webhooks/123/secret",
            "https://discord.com.evil.example/api/webhooks/123/secret",
            "https://evil.example/api/webhooks/123/secret",
            "https://discord.com@evil.example/api/webhooks/123/secret",
            "https://discord.com/api/webhooks/123/secret?redirect=https://evil.example",
        ):
            with self.subTest(url=url):
                self.assertFalse(receipt.send_notification(url, self.payload(), opener=opener))
        opener.open.assert_not_called()
        handler = receipt._NoRedirects()
        self.assertIsNone(handler.redirect_request(None, None, 302, None, None, "https://evil.example"))

    def test_notification_failure_is_visible_without_sensitive_error_or_retry(self):
        opener = MagicMock()
        opener.open.side_effect = RuntimeError("secret-webhook-token @everyone raw response")
        with patch.object(receipt, "build_opener", return_value=opener):
            environment = {
                "ACTION": "set_enabled",
                "SYMBOL": "DOGE_GBP",
                "CONFIG_STEP_OUTCOME": "success",
                "CONFIG_VERIFIED_RESULT": "applied",
                "GITHUB_REPOSITORY": self.arguments["repository"],
                "GITHUB_RUN_ID": self.arguments["run_id"],
                "CONFIG_RUN_URL": self.arguments["run_url"],
                "DISCORD_WEBHOOK_URL": "https://discord.com/api/webhooks/123/secret_token",
            }
            output = io.StringIO()
            with patch.dict(os.environ, environment, clear=True), contextlib.redirect_stdout(output):
                self.assertEqual(receipt.main(["notify"]), 1)
        self.assertEqual(opener.open.call_count, 1)
        self.assertIn("was not retried", output.getvalue())
        self.assertNotIn("secret", output.getvalue())
        self.assertNotIn("raw response", output.getvalue())

    def test_workflow_readback_precedes_applied_and_notification_is_separate(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/update_dca_config.yml"
        workflow = path.read_text(encoding="utf-8")
        self.assertLess(workflow.index("verify-readback"), workflow.index("verified_result=applied"))
        self.assertLess(workflow.index("verified_result=applied"), workflow.index("Applied one serialized"))
        self.assertEqual(workflow.count("gh variable set DCA_TARGET_MAP"), 1)
        notification_step = workflow.split("- name: Publish configuration result to Discord")[1]
        self.assertIn("if: always()", notification_step)
        self.assertIn("steps.config_update.outcome", notification_step)
        self.assertIn("steps.config_update.outputs.verified_result", notification_step)
        self.assertNotIn("gh variable set", notification_step)
        self.assertNotIn("dca_config_writer.py", notification_step)

    def test_enable_workflow_invalidates_under_both_locks_before_rule_write(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/update_dca_config.yml"
        workflow = path.read_text(encoding="utf-8")
        self.assertIn("group: dca-rule-writers", workflow)
        self.assertIn("inputs.action == 'set_enabled' && inputs.enabled_json == 'true' && 'dca-analysis-state-writers'", workflow)
        self.assertLess(workflow.index("'dca-analysis-state-writers'"), workflow.index("jobs:"))
        self.assertGreater(workflow.index("group: dca-rule-writers"), workflow.index("jobs:"))
        self.assertLess(workflow.index("gh variable set DCA_ANALYSIS_STATE"), workflow.index("gh variable set DCA_TARGET_MAP"))
        self.assertLess(workflow.index("verify-analysis-readback"), workflow.index("gh variable set DCA_TARGET_MAP"))
        self.assertNotIn("gh variable set DCA_EXECUTION_STATE", workflow)


if __name__ == "__main__":
    unittest.main()

import contextlib
import io
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import dca_readiness
from dca_config import ALLOWED_TARGETS


def rules():
    return {
        target: {
            "REGIME_AMOUNTS_GBP": {"LOW": 5, "MID": 10, "UP": 20 if target == "BTC_GBP" else 15},
            "BUY_ENABLED": True,
        }
        for target in ALLOWED_TARGETS
    }


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        # Any attempt to create/cancel orders fails this narrow exchange surface.
        self.exchange = Mock(spec_set=["fetch_balance", "fetch_closed_orders", "fetch_open_orders", "privatePostGetApiKeyInfo"])
        self.exchange.privatePostGetApiKeyInfo.return_value = {
            "error": [], "result": {"permissions": [*dca_readiness.QUERY_PERMISSIONS, "modify-trades"]},
        }
        self.exchange.fetch_balance.return_value = {"free": {"GBP": "65.65"}}
        self.exchange.fetch_closed_orders.return_value = []
        self.exchange.fetch_open_orders.return_value = []
        self.minimum = patch.object(
            dca_readiness, "get_market_minimum_gbp",
            return_value={"effective_minimum_gbp": 5},
        ).start()
        self.addCleanup(patch.stopall)

    def test_exact_maximum_plus_one_percent_reserve_passes_all_enabled_minimums(self):
        result = dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
        self.assertTrue(result["operational_preflight_passed"])
        self.assertEqual([call.args[0] for call in self.minimum.call_args_list], list(ALLOWED_TARGETS))
        self.assertEqual(set(result["targets"]), set(ALLOWED_TARGETS))
        self.assertNotIn("65.65", json.dumps(result))

    def test_balance_below_reserve_or_low_below_market_minimum_fails(self):
        self.exchange.fetch_balance.return_value = {"free": {"GBP": "65.64"}}
        result = dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
        self.assertFalse(result["funds_ready"])
        self.assertFalse(result["operational_preflight_passed"])
        self.exchange.fetch_balance.return_value = {"free": {"GBP": "65.65"}}
        self.minimum.return_value = {"effective_minimum_gbp": 5.01}
        result = dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
        self.assertFalse(result["targets"]["BTC_GBP"]["minimum_ready"])
        self.assertFalse(result["operational_preflight_passed"])

    def test_missing_nonfinite_negative_or_boolean_balance_fails_closed(self):
        for value in (None, "NaN", "Infinity", -1, True, {}):
            with self.subTest(value=value), self.assertRaises(Exception):
                self.exchange.fetch_balance.return_value = {"free": {"GBP": value}}
                dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)

    def test_invalid_minimum_fails_closed(self):
        for value in (None, float("nan"), -1, True):
            with self.subTest(value=value), self.assertRaises(Exception):
                self.minimum.return_value = {"effective_minimum_gbp": value}
                dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)

    def test_disabled_targets_do_not_add_funds_or_quote_checks(self):
        configured = rules()
        for target in ALLOWED_TARGETS:
            configured[target]["BUY_ENABLED"] = False
        result = dca_readiness.check_readiness(configured, {}, exchange=self.exchange)
        self.minimum.assert_not_called()
        self.assertFalse(result["enabled_targets_present"])
        self.assertFalse(result["operational_preflight_passed"])

    def test_pending_order_and_audit_failures_cannot_pass(self):
        pending = {"BTC_GBP": {"PENDING_ORDER": {
            "client_order_id": "dca-0123456789abcd",
            "funding_client_order_id": "dca-abcdef01234567",
            "trade_date": "2026-09-14", "amount_gbp": 5,
            "decision_id": "decision", "created_at": "2026-09-14T00:00:00Z",
        }}}
        result = dca_readiness.check_readiness(rules(), pending, exchange=self.exchange)
        self.assertFalse(result["pending_orders_clear"])
        self.assertFalse(result["operational_preflight_passed"])
        good_audit = {"flow_integrity_ok": True, "unresolved_bot_orders": 0,
                      "unknown_timestamp_closed_bot_orders": 0}
        for field, value in (("flow_integrity_ok", False), ("unresolved_bot_orders", 1),
                             ("unresolved_bot_orders", False), ("unknown_timestamp_closed_bot_orders", 1)):
            with self.subTest(field=field, value=value), patch.object(
                dca_readiness, "audit_orders", return_value={**good_audit, field: value}
            ):
                result = dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
                self.assertFalse(result["operational_preflight_passed"])

    def test_main_never_prints_provider_error_details(self):
        output = io.StringIO()
        with patch.object(dca_readiness, "check_readiness", side_effect=RuntimeError("private-balance-and-key")), contextlib.redirect_stdout(output):
            self.assertEqual(dca_readiness.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "checks_complete": False, "operational_preflight_passed": False,
            "failure_stage": "preflight",
        })

    def test_permission_failure_reports_booleans_without_sensitive_key_info(self):
        for permissions in (list(dca_readiness.QUERY_PERMISSIONS),
                            [*dca_readiness.QUERY_PERMISSIONS, "modify-trades", "withdraw-funds"],
                            ["modify-trades"]):
            with self.subTest(permissions=permissions):
                self.exchange.privatePostGetApiKeyInfo.return_value = {
                    "error": [], "result": {"permissions": permissions, "apiKey": "private-key"},
                }
                result = dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
                self.assertFalse(result["operational_preflight_passed"])
                self.assertEqual(result["failure_stage"], "permissions")
                self.assertNotIn("private-key", json.dumps(result))
                self.exchange.fetch_balance.assert_not_called()

    def test_each_failure_stage_is_sanitized_and_quote_failure_names_only_target(self):
        for payload in ({}, {"error": ["private-provider-message"]},
                        {"error": [], "result": {"permissions": "query-funds"}}):
            with self.subTest(payload=payload), self.assertRaises(dca_readiness.PreflightError) as caught:
                self.exchange.privatePostGetApiKeyInfo.return_value = payload
                dca_readiness.check_readiness(rules(), {}, exchange=self.exchange)
            self.assertEqual(caught.exception.stage, "permissions")
        output = io.StringIO()
        with patch.object(dca_readiness, "check_readiness", side_effect=dca_readiness.PreflightError("quote_minimum", "SOL_GBP")), contextlib.redirect_stdout(output):
            self.assertEqual(dca_readiness.main(), 1)
        self.assertEqual(json.loads(output.getvalue()), {
            "checks_complete": False, "operational_preflight_passed": False,
            "failure_stage": "quote_minimum", "target": "SOL_GBP",
        })

    def test_workflow_is_manual_main_only_and_has_no_mutating_or_notification_inputs(self):
        path = Path(__file__).resolve().parents[1] / ".github/workflows/dca_readiness.yml"
        workflow = path.read_text(encoding="utf-8")
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("if: github.ref == 'refs/heads/main'", workflow)
        self.assertIn("group: dca-execution-state-writers", workflow)
        self.assertIn("cancel-in-progress: false", workflow)
        self.assertIn("::add-mask::", workflow)
        for forbidden in ("schedule:", "gh variable set", "DISCORD", "GIST_TOKEN", "vars.DCA_", "crypto_dca.py"):
            self.assertNotIn(forbidden, workflow)


if __name__ == "__main__":
    unittest.main()

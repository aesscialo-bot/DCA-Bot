import copy
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import kraken_intent_recovery as recovery
from kraken_client import build_client_order_id


NOW = datetime(2026, 8, 30, 1, 0, tzinfo=timezone.utc)


def state():
    return {
        "BTC_GBP": {"LAST_BUY_DATE": "2026-08-30"},
        "ETH_GBP": {
            "LAST_BUY_DATE": "2026-08-28",
            "PENDING_ORDER": {
                "client_order_id": build_client_order_id("ETH_GBP", "2026-08-29"),
                "funding_client_order_id": build_client_order_id("ETH_GBP", "2026-08-29", purpose="funding"),
                "trade_date": "2026-08-29", "amount_gbp": 5.0,
                "decision_id": "eth-decision", "created_at": "2026-08-28T21:56:02Z",
            },
        },
        "SOL_GBP": {"LAST_BUY_DATE": "2026-08-25"},
    }


def exchange():
    client = MagicMock()
    client.privatePostClosedOrders.return_value = {"error": [], "result": {"closed": {}, "count": 0}}
    client.privatePostOpenOrders.return_value = {"error": [], "result": {"open": {}}}
    client.parse_order.side_effect = lambda row: row
    client.fetch_balance.return_value = {"free": {"GBP": 100.0}}
    return client


def native_order(**overrides):
    return {
        "symbol": "ETH/GBP", "side": "buy", "clientOrderId": "manual",
        "timestamp": datetime(2026, 8, 28, 21, 56, 10, tzinfo=timezone.utc).timestamp() * 1000,
        **overrides,
    }


class RecoveryAuditTests(unittest.TestCase):
    def setUp(self):
        self.permissions = patch.object(recovery, "ensure_history_permissions").start()
        self.addCleanup(patch.stopall)

    def test_complete_absence_is_read_only_and_checks_both_identities(self):
        client = exchange()
        result = recovery.audit_absent_intents(state(), client, now=NOW)
        self.assertTrue(result["all_confirmed_absent"])
        self.assertTrue(result["gbp_covers_combined_pending_budgets"])
        self.assertEqual(result["state_hash"], recovery.state_hash(state()))
        self.assertEqual(client.privatePostClosedOrders.call_count, 3)
        self.assertEqual(client.privatePostOpenOrders.call_count, 3)
        client.create_order.assert_not_called()
        client.create_market_buy_order_with_cost.assert_not_called()
        client.cancel_order.assert_not_called()
        self.permissions.assert_called_once()

    def test_exact_order_blocks_even_when_account_wide_scan_does_not_find_it(self):
        client = exchange()
        empty = client.privatePostClosedOrders.return_value
        client.privatePostClosedOrders.side_effect = [empty, {"error": [], "result": {
            "closed": {"O-FOUND": native_order()}, "count": 1,
        }}, empty]
        result = recovery.audit_absent_intents(state(), client, now=NOW)
        self.assertFalse(result["all_confirmed_absent"])

    def test_nearby_untagged_order_blocks_absence(self):
        client = exchange()
        empty = client.privatePostClosedOrders.return_value
        client.privatePostClosedOrders.side_effect = [{"error": [], "result": {
            "closed": {"O-UNTAGGED": native_order(clientOrderId=None)}, "count": 1,
        }}, empty, empty]
        result = recovery.audit_absent_intents(state(), client, now=NOW)
        self.assertEqual(result["targets"]["ETH_GBP"]["nearby_order_observations"], 1)
        self.assertFalse(result["all_confirmed_absent"])

    def test_matching_id_on_wrong_symbol_still_blocks(self):
        client = exchange()
        intent = state()["ETH_GBP"]["PENDING_ORDER"]
        client.privatePostOpenOrders.side_effect = [{"error": [], "result": {
            "open": {"O-WRONG": native_order(symbol="SOL/GBP", clientOrderId=intent["client_order_id"])},
        }}, {"error": [], "result": {"open": {}}}, {"error": [], "result": {"open": {}}}]
        self.assertFalse(recovery.audit_absent_intents(state(), client, now=NOW)["all_confirmed_absent"])

    def test_recent_or_inconsistent_intent_stops_before_kraken(self):
        for change in (
            {"created_at": "2026-08-29T21:56:02Z"},
            {"created_at": "2026-08-27T21:56:02Z"},
            {"client_order_id": "dca-00000000000000"},
            {"funding_client_order_id": "dca-00000000000000"},
        ):
            value = state()
            value["ETH_GBP"]["PENDING_ORDER"].update(change)
            client = exchange()
            with self.subTest(change=change), self.assertRaises(recovery.RecoveryError):
                recovery.audit_absent_intents(value, client, now=NOW)
            client.privatePostOpenOrders.assert_not_called()

    def test_unknown_timestamp_or_unparsed_symbol_fails_closed(self):
        for row in (native_order(timestamp=None), native_order(symbol=None)):
            client = exchange()
            client.privatePostClosedOrders.return_value = {"error": [], "result": {
                "closed": {"O-UNKNOWN": row}, "count": 1,
            }}
            with self.subTest(row=row), self.assertRaises(recovery.RecoveryError):
                recovery.audit_absent_intents(state(), client, now=NOW)

    def test_restricted_key_or_api_failure_cannot_establish_absence(self):
        self.permissions.side_effect = RuntimeError("restricted history")
        client = exchange()
        with self.assertRaises(RuntimeError):
            recovery.audit_absent_intents(state(), client, now=NOW)
        client.privatePostClosedOrders.assert_not_called()

    def test_native_pagination_requires_exact_stable_count_and_progress(self):
        for responses in (
            [{"error": [], "result": {"closed": {}, "count": 1}}],
            [{"error": [], "result": {"closed": {}, "count": "0"}}],
            [{"error": ["unavailable"], "result": {"closed": {}, "count": 0}}],
            [
                {"error": [], "result": {"closed": {"O-1": {}}, "count": 2}},
                {"error": [], "result": {"closed": {"O-1": {}}, "count": 2}},
            ],
            [
                {"error": [], "result": {"closed": {"O-1": {}}, "count": 2}},
                {"error": [], "result": {"closed": {"O-2": {}}, "count": 3}},
            ],
        ):
            client = exchange()
            client.privatePostClosedOrders.side_effect = responses
            with self.subTest(responses=responses), self.assertRaises(recovery.RecoveryError):
                recovery._closed_history(client, 1, 2)

    def test_native_pagination_covers_multiple_pages(self):
        client = exchange()
        client.privatePostClosedOrders.side_effect = [
            {"error": [], "result": {"closed": {"O-1": {}}, "count": 2}},
            {"error": [], "result": {"closed": {"O-2": {}}, "count": 2}},
        ]
        self.assertEqual(len(recovery._closed_history(client, 1, 2)), 2)
        self.assertEqual(client.privatePostClosedOrders.call_args.args[0]["ofs"], 1)


class RecoveryApplyTests(unittest.TestCase):
    def test_workflow_is_manual_main_only_and_serialized_with_trader(self):
        import yaml
        path = Path(__file__).resolve().parents[1] / ".github/workflows/recover_pending_intents.yml"
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        triggers = document.get("on", document.get(True))
        self.assertEqual(set(triggers), {"workflow_dispatch"})
        self.assertEqual(document["concurrency"]["group"], "dca-execution-state-writers")
        self.assertFalse(document["concurrency"]["cancel-in-progress"])
        self.assertEqual(document["jobs"]["recover"]["if"], "github.ref == 'refs/heads/main'")
        self.assertEqual(triggers["workflow_dispatch"]["inputs"]["mode"]["default"], "preview")

    def setUp(self):
        self.shadow = patch.object(recovery, "_assert_shadow").start()
        self.read = patch.object(recovery.crypto_dca, "fetch_live_execution_state", return_value=state()).start()
        self.write = patch.object(recovery.crypto_dca, "_write_repo_json_variable").start()
        self.audit = patch.object(recovery, "audit_absent_intents", return_value={
            "all_confirmed_absent": True, "targets": {"ETH_GBP": {}},
        }).start()
        self.addCleanup(patch.stopall)

    def test_preview_never_writes(self):
        recovery.recover(exchange=exchange())
        self.write.assert_not_called()

    def test_apply_preserves_buy_dates_and_unrelated_targets(self):
        before = state()
        after = copy.deepcopy(before)
        after["ETH_GBP"].pop("PENDING_ORDER")
        self.read.side_effect = [before, before, after]
        result = recovery.recover(mode="apply", expected_state_hash=recovery.state_hash(before), exchange=exchange())
        self.assertTrue(result["applied"])
        self.write.assert_called_once_with("DCA_EXECUTION_STATE", after, exists=True)
        self.assertEqual(self.shadow.call_count, 2)

    def test_changed_or_missing_review_hash_never_writes_or_audits(self):
        for value in ("", "0" * 64):
            with self.subTest(value=value), self.assertRaises(recovery.RecoveryError):
                recovery.recover(mode="apply", expected_state_hash=value, exchange=exchange())
        self.write.assert_not_called()
        self.audit.assert_not_called()

    def test_changed_state_during_audit_never_writes(self):
        changed = state()
        changed["BTC_GBP"]["LAST_BUY_DATE"] = "2026-08-29"
        self.read.side_effect = [state(), changed]
        with self.assertRaisesRegex(recovery.RecoveryError, "changed during"):
            recovery.recover(mode="apply", expected_state_hash=recovery.state_hash(state()), exchange=exchange())
        self.write.assert_not_called()

    def test_order_evidence_or_shadow_failure_never_writes(self):
        self.audit.return_value["all_confirmed_absent"] = False
        with self.assertRaisesRegex(recovery.RecoveryError, "order evidence"):
            recovery.recover(mode="apply", expected_state_hash=recovery.state_hash(state()), exchange=exchange())
        self.shadow.side_effect = recovery.RecoveryError("not shadow")
        with self.assertRaises(recovery.RecoveryError):
            recovery.recover(exchange=exchange())
        self.write.assert_not_called()


if __name__ == "__main__":
    unittest.main()

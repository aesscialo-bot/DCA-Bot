import hashlib
import json
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ghostfolio import kraken_holdings_snapshot


class Exchange:
    def fetch_balance(self):
        return {"total": {"BTC": 0.1, "HYPE": 2, "SOL": 3, "GBP": 4, "ETH": 5}}

    def fetch_ticker(self, pair):
        return {"last": {"BTC/GBP": 50000, "HYPE/USD": 40, "SOL/GBP": 50}[pair]}


class KrakenHoldingsSnapshotTests(unittest.TestCase):
    def test_snapshot_uses_canonical_mixed_pairs_and_signed_quantities(self):
        snapshot = kraken_holdings_snapshot.build_snapshot(
            Exchange(), now=datetime(2026, 8, 7, tzinfo=timezone.utc)
        )
        self.assertEqual(snapshot["holdings"]["BTC_GBP"]["pair"], "BTC/GBP")
        self.assertEqual(snapshot["holdings"]["HYPE_USD"]["pair"], "HYPE/USD")
        self.assertEqual(snapshot["holdings"]["SOL_GBP"]["pair"], "SOL/GBP")
        self.assertEqual(snapshot["unsupported_nonzero_assets"], ["ETH"])
        supplied = snapshot.pop("canonical_hash")
        actual = hashlib.sha256(
            json.dumps(snapshot, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        self.assertEqual(supplied, actual)

    def test_snapshot_is_blocked_by_pending_order(self):
        pending_order = {
            "client_order_id": "dca-1234567890abcd",
            "funding_client_order_id": "dca-fedcba09876543",
            "trade_date": "2026-08-07",
            "amount_gbp": 10.0,
            "decision_id": "decision-1",
            "created_at": "2026-08-07T01:00:00Z",
        }
        with self.assertRaisesRegex(RuntimeError, "pending-order"):
            kraken_holdings_snapshot.ensure_snapshot_safe_execution_state({
                "BTC_GBP": {
                    "LAST_BUY_DATE": "",
                    "PENDING_ORDER": pending_order,
                }
            })

        self.assertEqual(
            kraken_holdings_snapshot.ensure_snapshot_safe_execution_state({}),
            {},
        )

        with (
            patch.object(
                kraken_holdings_snapshot,
                "validate_execution_state",
                return_value={
                    "SOL_GBP": {"PENDING_GIST_DELIVERIES": [{}]}
                },
            ),
            self.assertRaisesRegex(RuntimeError, "pending-delivery"),
        ):
            kraken_holdings_snapshot.ensure_snapshot_safe_execution_state({})

    def test_snapshot_workflow_shares_state_lock_and_loads_live_state(self):
        workflow = (
            Path(__file__).parents[1]
            / ".github"
            / "workflows"
            / "ghostfolio_holdings_snapshot.yml"
        ).read_text(encoding="utf-8")
        self.assertIn("group: dca-execution-state-writers", workflow)
        self.assertIn("gh variable get DCA_EXECUTION_STATE", workflow)
        self.assertIn("secrets.GH_PAT_FOR_VARS", workflow)

    def test_snapshot_rejects_malformed_balances_and_naive_time(self):
        class MalformedExchange(Exchange):
            def fetch_balance(self):
                return {"total": {"BTC": 0.1, "ETH": float("nan")}}

        with self.assertRaisesRegex(RuntimeError, "invalid Kraken balance for ETH"):
            kraken_holdings_snapshot.build_snapshot(
                MalformedExchange(),
                now=datetime(2026, 8, 7, tzinfo=timezone.utc),
            )
        with self.assertRaisesRegex(RuntimeError, "must include a timezone"):
            kraken_holdings_snapshot.build_snapshot(
                Exchange(), now=datetime(2026, 8, 7)
            )


if __name__ == "__main__":
    unittest.main()

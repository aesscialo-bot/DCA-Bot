import hashlib
import importlib.util
import json
from pathlib import Path
import unittest


PATH = Path(__file__).parents[1] / "ghostfolio" / "ghostfolio_sync.py"
SPEC = importlib.util.spec_from_file_location("ghostfolio_sync", PATH)
ghostfolio_sync = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ghostfolio_sync)


def event(identifier="ORDER-1"):
    value = {
        "event_version": 3, "event_id": identifier,
        "occurred_at": "2026-08-06T01:00:00Z", "target": "SOL_GBP",
        "base_currency": "SOL", "quote_currency": "GBP", "budget_currency": "GBP",
        "funding_order_id": None, "crypto_order_id": identifier,
        "gbp_debit": "10", "gbp_usd_rate": "0", "funded_usd": "0",
        "route": "DIRECT_GBP", "crypto_cost_quote": "10", "crypto_quantity": "0.1",
        "unit_price_quote": "100", "funding_fee_quote": "0", "crypto_fee_quote": "0.03",
    }
    value["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(value).encode()
    ).hexdigest()
    return value


class GhostfolioSyncTests(unittest.TestCase):
    def test_signed_holdings_snapshot_and_drift(self):
        snapshot = {
            "version": 1,
            "as_of": "2026-08-07T04:00:00Z",
            "holdings": {
                "BTC_GBP": {"quantity": "0.2", "quote_currency": "GBP", "unit_price_quote": "50000"},
                "HYPE_USD": {"quantity": "2", "quote_currency": "USD", "unit_price_quote": "40"},
                "SOL_GBP": {"quantity": "3", "quote_currency": "GBP", "unit_price_quote": "50"},
            },
            "unsupported_nonzero_assets": [],
        }
        snapshot["canonical_hash"] = hashlib.sha256(
            ghostfolio_sync.canonical(snapshot).encode()
        ).hexdigest()
        parsed = ghostfolio_sync.parse_holdings_snapshot(
            ghostfolio_sync.canonical(snapshot)
        )
        self.assertEqual(
            ghostfolio_sync.holdings_drift(
                parsed, {"BTC_GBP": 0.1, "HYPE_USD": 2, "SOL_GBP": 3}
            ),
            {"BTC_GBP": 0.1},
        )
        prior = ghostfolio_sync.os.environ.get("GHOSTFOLIO_ACCOUNT_MAP")
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps(
            {"BTC_GBP": "kraken-account"}
        )
        try:
            activity = ghostfolio_sync.holdings_import_payload(
                parsed, "BTC_GBP", 0.1
            )
            self.assertEqual(activity["activities"][0]["type"], "BUY")
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior

    def test_holdings_snapshot_blocks_unmapped_assets(self):
        snapshot = {
            "version": 1,
            "as_of": "2026-08-07T04:00:00Z",
            "holdings": {},
            "unsupported_nonzero_assets": ["ETH"],
        }
        snapshot["canonical_hash"] = hashlib.sha256(
            ghostfolio_sync.canonical(snapshot).encode()
        ).hexdigest()
        with self.assertRaisesRegex(RuntimeError, "without a Ghostfolio mapping"):
            ghostfolio_sync.parse_holdings_snapshot(
                ghostfolio_sync.canonical(snapshot)
            )

    def test_hash_chain_and_duplicate_event_ids_are_enforced(self):
        first = event()
        content = ghostfolio_sync.canonical(first) + "\n"
        self.assertEqual(ghostfolio_sync.parse_events(content), [first])
        corrupt = dict(first, crypto_quantity="0.2")
        with self.assertRaisesRegex(RuntimeError, "invalid append-only hash"):
            ghostfolio_sync.parse_events(ghostfolio_sync.canonical(corrupt))
        with self.assertRaisesRegex(RuntimeError, "duplicates"):
            ghostfolio_sync.parse_events(content + content)

    def test_only_exact_duplicate_response_is_accepted(self):
        self.assertTrue(
            ghostfolio_sync.is_exact_duplicate(
                {"message": ["activities.0 is a duplicate activity"]}
            )
        )
        self.assertFalse(ghostfolio_sync.is_exact_duplicate({"message": ["currency conflict"]}))

    def test_import_payload_uses_local_custody_account_and_separate_fee_comment(self):
        prior = ghostfolio_sync.os.environ.get("GHOSTFOLIO_ACCOUNT_MAP")
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps({"SOL_GBP": "local-sol"})
        try:
            activity = ghostfolio_sync.import_payload(event())["activities"][0]
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior
        self.assertEqual(activity["accountId"], "local-sol")
        self.assertEqual(activity["symbol"], "solana")
        self.assertIn("funding fee GBP 0", activity["comment"])
        self.assertIn("crypto fee GBP 0.03", activity["comment"])


if __name__ == "__main__":
    unittest.main()

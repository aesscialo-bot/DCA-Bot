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
        "event_version": 2, "event_id": identifier,
        "occurred_at": "2026-08-06T01:00:00Z", "target": "SOL_USD",
        "base_currency": "SOL", "quote_currency": "USD", "budget_currency": "GBP",
        "funding_order_id": "FUND-1", "crypto_order_id": identifier,
        "gbp_debit": "10", "gbp_usd_rate": "1.3", "funded_usd": "13",
        "crypto_cost_usd": "12.9", "crypto_quantity": "0.1",
        "unit_price_usd": "129", "funding_fee_usd": "0.02", "crypto_fee_usd": "0.03",
    }
    value["canonical_hash"] = hashlib.sha256(
        ghostfolio_sync.canonical(value).encode()
    ).hexdigest()
    return value


class GhostfolioSyncTests(unittest.TestCase):
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
        ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = json.dumps({"SOL_USD": "local-sol"})
        try:
            activity = ghostfolio_sync.import_payload(event())["activities"][0]
        finally:
            if prior is None:
                ghostfolio_sync.os.environ.pop("GHOSTFOLIO_ACCOUNT_MAP", None)
            else:
                ghostfolio_sync.os.environ["GHOSTFOLIO_ACCOUNT_MAP"] = prior
        self.assertEqual(activity["accountId"], "local-sol")
        self.assertEqual(activity["symbol"], "solana")
        self.assertIn("funding fee USD 0.02", activity["comment"])
        self.assertIn("crypto fee USD 0.03", activity["comment"])


if __name__ == "__main__":
    unittest.main()

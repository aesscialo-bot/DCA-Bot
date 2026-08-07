import hashlib
import json
import unittest
from datetime import datetime, timezone

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


if __name__ == "__main__":
    unittest.main()

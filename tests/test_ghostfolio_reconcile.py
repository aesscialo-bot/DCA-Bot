import unittest

from ghostfolio.reconcile_legacy import legacy_candidates, reconcile


class GhostfolioReconciliationTests(unittest.TestCase):
    def test_missing_hosted_export_blocks_commit_but_preserves_insert_plan(self):
        candidates = legacy_candidates({"transactions": [{
            "account_id": "obsolete-hosted-uuid",
            "amount_crypto": 0.1,
            "executed_at": "2026-07-13T00:00:00Z",
            "order_id": "bitkub-order-1",
            "pair": "BTC_THB",
            "usd_price_per_unit": 60000,
        }]})
        report = reconcile(
            candidates,
            {"accounts": [], "activities": []},
            source_failures=[{"source": "hosted Ghostfolio export", "reason": "missing"}],
        )
        self.assertEqual(report["counts"], {
            "already_present": 0, "insert": 1, "conflict": 0, "failed": 1
        })
        self.assertFalse(report["can_commit"])
        self.assertEqual(report["insert"][0]["account"], "Bitkub Legacy")
        self.assertNotIn("account_id", report["insert"][0])

    def test_same_order_id_with_different_values_is_conflict(self):
        candidate = {
            "order_id": "kraken-order-1", "account": "Kraken DCA",
            "symbol": "bitcoin", "currency": "GBP",
            "date": "2026-08-05T17:35:00Z", "quantity": "0.1",
            "unit_price": "50000", "fee": "0", "canonical_hash": "x",
        }
        export = {
            "accounts": [{"id": "fresh-local", "name": "Kraken DCA"}],
            "activities": [{
                "accountId": "fresh-local", "comment": "Kraken kraken-order-1",
                "symbol": "bitcoin", "currency": "GBP",
                "date": "2026-08-05T17:35:00Z", "quantity": 0.2,
                "unitPrice": 50000, "fee": 0,
            }],
        }
        report = reconcile([candidate], export)
        self.assertEqual(report["counts"]["conflict"], 1)
        self.assertFalse(report["can_commit"])


if __name__ == "__main__":
    unittest.main()

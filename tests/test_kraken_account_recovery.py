import json
import unittest
from decimal import Decimal

from ghostfolio import kraken_account_recovery as recovery
from ghostfolio import kraken_opening_basis as basis


OPENING_TIME = basis.decimal_text(
    basis.epoch_decimal(basis.utc_timestamp(basis.CUTOVER_AT, "cutover")) - 3600
)
HYPE_ONE_TIME = basis.decimal_text(
    basis.epoch_decimal(basis.utc_timestamp(basis.CUTOVER_AT, "cutover")) + 73_599
)
HYPE_TWO_TIME = basis.decimal_text(Decimal(HYPE_ONE_TIME) + 3)
SOL_BUY_TIME = basis.decimal_text(Decimal(HYPE_TWO_TIME) + 90)
SOL_OUT_TIME = basis.decimal_text(Decimal(SOL_BUY_TIME) + 937)
BTC_BUY_TIME = basis.decimal_text(Decimal(SOL_OUT_TIME) + 373)
BTC_OUT_TIME = basis.decimal_text(Decimal(BTC_BUY_TIME) + 206)


def evidence(records):
    ordered = sorted(records, key=lambda row: row["id"])
    page = {
        "page": 1,
        "offset": 0,
        "returned_count": len(ordered),
        "response_count": len(ordered),
        "record_ids": [row["id"] for row in ordered],
        "canonical_hash": basis.canonical_hash(ordered),
    }
    return basis.history_evidence(ordered, page_count=1, pages=[page])


def opening_history():
    trades = evidence([{
        "id": "T-OPEN-BTC",
        "ordertxid": "O-OPEN-BTC",
        "pair": "BTCGBP",
        "time": OPENING_TIME,
        "type": "buy",
        "vol": "0.00010427",
        "cost": "5",
        "ledgers": ["L-OPEN-BTC", "L-OPEN-GBP"],
    }])
    ledgers = evidence([
        {
            "id": "L-OPEN-BTC", "refid": "T-OPEN-BTC", "time": OPENING_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "BTC",
            "amount": "0.00010427", "fee": "0.00000083", "balance": "0.00010344",
        },
        {
            "id": "L-OPEN-GBP", "refid": "T-OPEN-BTC", "time": OPENING_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-5", "fee": "0", "balance": "15",
        },
    ])
    orders = evidence([{"id": "O-OPEN-BTC", "cl_ord_id": None, "status": "closed"}])
    return trades, ledgers, orders


def activity_history(
    *, extra_hype_leg=False, unsupported=False, unknown_trade=False,
    orphan_trade_ledger=False,
):
    trade_rows = [
        {
            "id": "T-SOL", "ordertxid": "O-SOL", "pair": "SOLGBP",
            "time": SOL_BUY_TIME, "type": "buy", "vol": "0.06000042",
            "cost": "3.237", "ledgers": ["L-SOL", "L-SOL-GBP"],
        },
        {
            "id": "T-BTC", "ordertxid": "O-BTC", "pair": "BTCGBP",
            "time": BTC_BUY_TIME, "type": "buy", "vol": "0.0002091122",
            "cost": "10.00048", "ledgers": ["L-BTC", "L-BTC-GBP"],
        },
    ]
    rows = [
        {
            "id": "L-HYPE-1", "refid": "T-HYPE-1", "time": HYPE_ONE_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "HYPE",
            "amount": "0.1000002", "fee": "0", "balance": "0.1000002",
        },
        {
            "id": "L-HYPE-1-GBP", "refid": "T-HYPE-1", "time": HYPE_ONE_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-4.1486", "fee": "0.0332", "balance": "110.8182",
        },
        {
            "id": "L-HYPE-2", "refid": "T-HYPE-2", "time": HYPE_TWO_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "HYPE",
            "amount": "0.1000002", "fee": "0", "balance": "0.2000004",
        },
        {
            "id": "L-HYPE-2-GBP", "refid": "T-HYPE-2", "time": HYPE_TWO_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-4.1478", "fee": "0.0332", "balance": "106.6372",
        },
        {
            "id": "L-SOL", "refid": "T-SOL", "time": SOL_BUY_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "SOL",
            "amount": "0.06000042", "fee": "0", "balance": "0.06000042",
        },
        {
            "id": "L-SOL-GBP", "refid": "T-SOL", "time": SOL_BUY_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-3.237", "fee": "0.013", "balance": "103.3872",
        },
        {
            "id": "L-SOL-OUT", "refid": "W-SOL", "time": SOL_OUT_TIME,
            "type": "withdrawal", "subtype": None, "asset": "SOL",
            "amount": "-0.006", "fee": "0.005", "balance": "0.04900042",
        },
        {
            "id": "L-BTC", "refid": "T-BTC", "time": BTC_BUY_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "BTC",
            "amount": "0.0002091122", "fee": "0", "balance": "0.0003125522",
        },
        {
            "id": "L-BTC-GBP", "refid": "T-BTC", "time": BTC_BUY_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-10.00048", "fee": "0.04", "balance": "93.34672",
        },
        {
            "id": "L-BTC-OUT", "refid": "W-BTC", "time": BTC_OUT_TIME,
            "type": "withdrawal", "subtype": None, "asset": "BTC",
            "amount": "-0.000203", "fee": "0.000015", "balance": "0.0000945522",
        },
    ]
    if extra_hype_leg:
        rows.append({
            "id": "L-HYPE-1-EXTRA", "refid": "T-HYPE-1", "time": HYPE_ONE_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-0.01", "fee": "0", "balance": "110.8082",
        })
    if unsupported:
        rows.append({
            "id": "L-HYPE-REWARD", "refid": "R-HYPE", "time": HYPE_TWO_TIME,
            "type": "staking", "subtype": None, "asset": "HYPE",
            "amount": "0.01", "fee": "0", "balance": "0.2100004",
        })
    if unknown_trade:
        trade_rows.append({
            "id": "T-ETH", "ordertxid": "O-ETH", "pair": "ETHGBP",
            "time": HYPE_TWO_TIME, "type": "buy", "vol": "0.01",
            "cost": "1", "ledgers": ["L-ETH", "L-ETH-GBP"],
        })
        rows.extend([
            {
                "id": "L-ETH", "refid": "T-ETH", "time": HYPE_TWO_TIME,
                "type": "trade", "subtype": "tradespot", "asset": "ETH",
                "amount": "0.01", "fee": "0", "balance": "0.01",
            },
            {
                "id": "L-ETH-GBP", "refid": "T-ETH", "time": HYPE_TWO_TIME,
                "type": "trade", "subtype": "tradespot", "asset": "GBP",
                "amount": "-1", "fee": "0", "balance": "105.6372",
            },
        ])
    if orphan_trade_ledger:
        rows.append({
            "id": "L-ORPHAN-GBP", "refid": "T-ORPHAN", "time": HYPE_TWO_TIME,
            "type": "trade", "subtype": "tradespot", "asset": "GBP",
            "amount": "-1", "fee": "0", "balance": "105.6372",
        })
    trades = evidence(trade_rows)
    ledgers = evidence(rows)
    orders = evidence([
        {"id": "O-SOL", "cl_ord_id": None, "status": "closed"},
        {"id": "O-BTC", "cl_ord_id": None, "status": "closed"},
    ])
    return trades, ledgers, orders


def source_tuple(history, *, opening=False):
    trades, ledgers, orders = history
    generated = "2026-08-09T07:59:42Z"
    access = basis.AccessEvidence("0", "0", "0")
    if opening:
        artifact = basis.build_source_artifact(
            access, trades, ledgers, orders,
            generated_at=generated, producer_commit="a" * 40,
        )
    else:
        artifact = recovery.build_activity_source(
            access, trades, ledgers, orders,
            generated_at=generated, producer_commit="b" * 40,
        )
    return artifact, access, trades, ledgers, orders


def source_reference(filename, commit, source):
    return {
        "path": f"portfolio/{filename}",
        "blob_sha": "c" * 40,
        "repository_commit_sha": commit,
        "canonical_hash": source["canonical_hash"],
        "producer_commit": source["producer_commit"],
    }


def reviewed_end(**updates):
    quantities = {
        "BTC_GBP": Decimal("0.0000945522"),
        "HYPE_USD": Decimal("0.2000004"),
        "SOL_GBP": Decimal("0.04900042"),
    }
    quantities.update({key: Decimal(value) for key, value in updates.items()})
    return basis.OpeningBinding(
        repository_commit_sha=recovery.REVIEWED_END_REPOSITORY_COMMIT_SHA,
        holdings_path="portfolio/holdings.json",
        holdings_blob_sha="d" * 40,
        holdings_snapshot_hash="1" * 64,
        events_path="portfolio/events.jsonl",
        events_blob_sha="e" * 40,
        events_content_sha256="2" * 64,
        event_prefix_hash="3" * 64,
        accepted_event_count=3,
        opening_state_hash=recovery.REVIEWED_END_STATE_HASH,
        quantities=quantities,
    )


def build(*, activity=None, end=None):
    opening = source_tuple(opening_history(), opening=True)
    manual = source_tuple(activity or activity_history())
    return recovery.build_recovery_artifact(
        opening_source=opening,
        opening_source_reference=source_reference(
            basis.OPENING_BASIS_SOURCE_FILE,
            recovery.REVIEWED_OPENING_SOURCE_REPOSITORY_COMMIT_SHA,
            opening[0],
        ),
        activity_source=manual,
        activity_source_reference=source_reference(
            recovery.ACCOUNT_ACTIVITY_SOURCE_FILE, "f" * 40, manual[0]
        ),
        reviewed_end=end or reviewed_end(),
    )


class KrakenAccountRecoveryTests(unittest.TestCase):
    def test_fetch_surface_is_read_only_and_uses_lowercase_wire_booleans(self):
        history = activity_history()

        class Exchange:
            def __init__(self):
                self.calls = {}

            def privatePostGetApiKeyInfo(self, params):
                self.calls["access"] = dict(params)
                return {"error": [], "result": {
                    "permissions": ["query-closed-trades", "query-ledger"],
                    "queryFrom": 0, "queryTo": 0, "validUntil": 0,
                }}

            def privatePostTradesHistory(self, params):
                self.calls["trades"] = dict(params)
                return {"error": [], "result": {
                    "count": len(history[0].records),
                    "trades": {
                        row["id"]: {key: value for key, value in row.items() if key != "id"}
                        for row in history[0].records
                    },
                }}

            def privatePostLedgers(self, params):
                self.calls["ledgers"] = dict(params)
                return {"error": [], "result": {
                    "count": len(history[1].records),
                    "ledger": {
                        row["id"]: {key: value for key, value in row.items() if key != "id"}
                        for row in history[1].records
                    },
                }}

            def privatePostQueryOrders(self, params):
                self.calls["orders"] = dict(params)
                by_id = {row["id"]: row for row in history[2].records}
                return {"error": [], "result": {
                    order_id: {key: value for key, value in by_id[order_id].items() if key != "id"}
                    for order_id in params["txid"].split(",")
                }}

            def create_order(self, *_args, **_kwargs):
                raise AssertionError("reviewed recovery must never place an order")

        exchange = Exchange()
        recovery.fetch_activity_source(
            exchange, generated_at="2026-08-09T07:59:42Z"
        )

        after = int(basis.epoch_decimal(basis.utc_timestamp(
            recovery.ACTIVITY_AFTER, "after"
        )))
        through = int(basis.epoch_decimal(basis.utc_timestamp(
            recovery.ACTIVITY_THROUGH, "through"
        )))
        self.assertEqual(exchange.calls["access"], {})
        self.assertEqual(exchange.calls["trades"], {
            "start": after, "end": through, "ofs": 0, "limit": 100,
            "type": "all", "trades": "false", "without_count": "false",
            "consolidate_taker": "false", "ledgers": "true",
        })
        self.assertEqual(exchange.calls["ledgers"], {
            "start": after, "end": through, "ofs": 0,
            "type": "all", "without_count": "false",
        })
        self.assertEqual(exchange.calls["orders"], {
            "txid": "O-BTC,O-SOL", "trades": "false",
        })

    def test_source_round_trip_retains_the_exact_reviewed_seam(self):
        source, *_ = source_tuple(activity_history())
        parsed, access, trades, ledgers, orders = recovery.parse_activity_source(
            json.dumps(source)
        )

        self.assertEqual(parsed, source)
        self.assertEqual(access.query_from, "0")
        self.assertEqual(len(trades.records), 2)
        self.assertEqual(len(ledgers.records), 10)
        self.assertEqual(len(orders.records), 2)
        self.assertEqual(source["activity_after"], basis.CUTOVER_AT)
        self.assertEqual(source["activity_through"], recovery.ACTIVITY_THROUGH)

    def test_compact_recovery_reclassifies_opening_and_exact_manual_activity(self):
        artifact = build()

        self.assertEqual(
            artifact["opening_state"]["state_hash"],
            recovery.REVIEWED_TRUE_OPENING_STATE_HASH,
        )
        positions = {
            row["target"]: row for row in artifact["opening_state"]["positions"]
        }
        self.assertEqual(positions["BTC_GBP"]["quantity"], "0.00010344")
        self.assertEqual(positions["BTC_GBP"]["performance_cost_gbp"], "5")
        self.assertEqual(positions["HYPE_USD"]["quantity"], "0")
        self.assertEqual(positions["SOL_GBP"]["quantity"], "0")

        activities = artifact["activities"]
        self.assertEqual(len(activities), 6)
        hype = [row for row in activities if row["target"] == "HYPE_USD"]
        self.assertEqual(len(hype), 2)
        self.assertTrue(all(
            row["source"]["evidence_model"] == "paired-tradespot-ledgers-v1"
            for row in hype
        ))
        sol_out = next(
            row for row in activities
            if row["activity_id"] == "KRAKEN-MANUAL-OUT-L-SOL-OUT"
        )
        self.assertEqual(sol_out["quantity"], "0.011")
        self.assertEqual(sol_out["principal_quantity"], "0.006")
        self.assertEqual(sol_out["asset_fee_quantity"], "0.005")
        btc_out = next(row for row in activities if row["kind"] == "asset_out" and row["target"] == "BTC_GBP")
        self.assertEqual(btc_out["quantity"], "0.000218")
        self.assertEqual(
            artifact["reconciliation"]["ending_state_hash"],
            recovery.REVIEWED_END_STATE_HASH,
        )

    def test_buy_source_is_a_strict_discriminated_evidence_union(self):
        buys = [row for row in build()["activities"] if row["kind"] == "buy"]
        trade_backed = next(row for row in buys if row["target"] == "SOL_GBP")
        paired = next(row for row in buys if row["target"] == "HYPE_USD")

        self.assertEqual(set(trade_backed["source"]), {
            "evidence_model", "order_id", "trade_ids", "ledger_ids",
        })
        self.assertEqual(set(paired["source"]), {
            "evidence_model", "ledger_refid", "ledger_ids",
        })

    def test_extra_leg_or_unsupported_target_movement_fails_closed(self):
        for history in (
            activity_history(extra_hype_leg=True),
            activity_history(unsupported=True),
            activity_history(unknown_trade=True),
            activity_history(orphan_trade_ledger=True),
        ):
            with self.subTest(rows=len(history[1].records)), self.assertRaises(
                recovery.AccountRecoveryError
            ):
                build(activity=history)

    def test_recovery_must_reach_the_separately_reviewed_pre_dca_state(self):
        with self.assertRaisesRegex(
            recovery.AccountRecoveryError,
            "does not reach the bound pre-DCA state",
        ):
            build(end=reviewed_end(SOL_GBP="0.05"))

    def test_every_activity_and_artifact_hash_is_reproducible(self):
        artifact = build()
        for row in artifact["activities"]:
            unhashed = {key: value for key, value in row.items() if key != "canonical_hash"}
            self.assertEqual(row["canonical_hash"], basis.canonical_hash(unhashed))
        unhashed = {
            key: value for key, value in artifact.items() if key != "canonical_hash"
        }
        self.assertEqual(artifact["canonical_hash"], basis.canonical_hash(unhashed))
        recovery._serialized(artifact)

    def test_preview_summary_exposes_reviewable_costs_without_raw_ledgers(self):
        summary = recovery._recovery_summary(build(), source_commit="f" * 40)

        self.assertEqual(summary["opening_positions"][0], {
            "target": "BTC_GBP",
            "quantity": "0.00010344",
            "performance_cost_gbp": "5",
            "coverage": "complete",
        })
        hype = next(
            row for row in summary["activities"]
            if row["activity_id"] == "KRAKEN-MANUAL-BUY-T-HYPE-1"
        )
        self.assertEqual(hype["quote_cost"], "4.1486")
        self.assertEqual(hype["quote_fee"], "0.0332")
        self.assertEqual(hype["performance_cost_gbp"], "4.1818")
        self.assertEqual(hype["evidence_model"], "paired-tradespot-ledgers-v1")
        sol_out = next(
            row for row in summary["activities"] if row["kind"] == "asset_out"
        )
        self.assertEqual(sol_out["principal_quantity"], "0.006")
        self.assertEqual(sol_out["asset_fee_quantity"], "0.005")
        self.assertNotIn("ledger_ids", json.dumps(summary))


if __name__ == "__main__":
    unittest.main()

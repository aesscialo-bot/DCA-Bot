import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import unittest

import kraken_history


def trade(identifier, timestamp, price="100", quantity="1", pair="BTC/GBP"):
    return {
        "trade_id": identifier,
        "trade_ts": timestamp,
        "price": price,
        "quantity": quantity,
        "symbol": pair,
    }


class MemoryStore:
    def __init__(self, files=None):
        self.files = dict(files or {})

    def snapshot(self):
        return {"files": {name: {"content": content} for name, content in self.files.items()}}

    def read_file(self, filename, snapshot=None):
        return self.files.get(filename, "")

    def write_files(self, files):
        self.files.update(files)


class KrakenHistoryTests(unittest.TestCase):
    def test_normalized_cursor_boundary_cannot_reaggregate_earlier_trade(self):
        cursor = kraken_history._parse_iso(
            "2026-08-07T00:00:00.123456789Z", "cursor"
        )
        earlier = kraken_history._parse_iso(
            "2026-08-07T00:00:00.123455999Z", "trade"
        )
        same_boundary = kraken_history._parse_iso(
            "2026-08-07T00:00:00.123456999Z", "trade"
        )
        self.assertLess(earlier, cursor)
        self.assertEqual(same_boundary, cursor)

    def test_trade_aggregation_is_canonical_ohlcvt(self):
        candles = {}
        kraken_history._add_trade(
            candles, trade("A", "2026-08-07T00:01:00.000000001Z", "100", "2"), "BTC/GBP"
        )
        kraken_history._add_trade(
            candles, trade("B", "2026-08-07T00:14:59.999999999Z", "90", "3"), "BTC/GBP"
        )
        candle = next(iter(candles.values()))
        self.assertEqual(
            {key: candle[key] for key in ("open", "high", "low", "close", "volume", "trades")},
            {"open": "100", "high": "100", "low": "90", "close": "90", "volume": "5", "trades": 2},
        )

    def test_wrong_pair_and_missing_trade_id_fail_closed(self):
        with self.assertRaisesRegex(kraken_history.HistoryError, "different currency pair"):
            kraken_history._add_trade({}, trade("A", "2026-08-07T00:00:00Z", pair="SOL/GBP"), "BTC/GBP")
        with self.assertRaisesRegex(kraken_history.HistoryError, "trade_id"):
            kraken_history._add_trade({}, trade("", "2026-08-07T00:00:00Z"), "BTC/GBP")

    def test_monthly_partitions_are_sorted_and_hashed(self):
        candles = {}
        for identifier, timestamp in (("B", "2026-08-01T00:00:00Z"), ("A", "2026-07-31T23:45:00Z")):
            kraken_history._add_trade(candles, trade(identifier, timestamp), "BTC/GBP")
        contents, hashes = kraken_history._serialize_partitions("BTC_GBP", candles)
        self.assertEqual(len(contents), 2)
        for name, content in contents.items():
            self.assertEqual(hashes[name], sha256(content.encode()).hexdigest())

    def test_partition_hash_and_duplicate_timestamp_rejection(self):
        row = kraken_history._new_candle(
            datetime(2026, 8, 1, tzinfo=timezone.utc), Decimal("100"), Decimal("1")
        )
        content = json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n"
        filename = "kraken_history_v1_BTC_GBP_2026-08.jsonl"
        manifest = {"PARTITIONS": {filename: sha256(content.encode()).hexdigest()}}
        self.assertEqual(len(kraken_history._load_candles(MemoryStore({filename: content}), manifest)), 1)
        with self.assertRaisesRegex(kraken_history.HistoryError, "hash mismatch"):
            kraken_history._load_candles(MemoryStore({filename: content + " "}), manifest)
        duplicate = content + content
        manifest["PARTITIONS"][filename] = sha256(duplicate.encode()).hexdigest()
        with self.assertRaisesRegex(kraken_history.HistoryError, "duplicate candle"):
            kraken_history._load_candles(MemoryStore({filename: duplicate}), manifest)

    def test_genuine_no_trade_intervals_are_explicit_ranges(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        candles = {
            int(start.timestamp()): {},
            int((start + timedelta(minutes=45)).timestamp()): {},
        }
        summary = kraken_history._gap_summary(candles, start, start + timedelta(hours=1))
        self.assertEqual(summary["COUNT"], 2)
        self.assertEqual(summary["RANGES"][0]["INTERVALS"], 2)
        self.assertEqual(summary["RANGES"][0]["FROM"], "2026-08-01T00:15:00Z")

    def test_analysis_carries_internal_no_trade_intervals_without_extending_edges(self):
        start = datetime(2026, 8, 1, tzinfo=timezone.utc)
        candles = {
            int(start.timestamp()): kraken_history._new_candle(
                start, Decimal("100"), Decimal("2")
            ),
            int((start + timedelta(minutes=45)).timestamp()): kraken_history._new_candle(
                start + timedelta(minutes=45), Decimal("90"), Decimal("3")
            ),
        }
        rows, carried = kraken_history._analysis_rows(
            candles,
            start - timedelta(minutes=15),
            start + timedelta(minutes=75),
        )
        self.assertEqual(carried, 2)
        self.assertEqual([row[0] for row in rows], [
            int((start + timedelta(minutes=offset)).timestamp() * 1000)
            for offset in (0, 15, 30, 45)
        ])
        self.assertEqual(rows[1][1:], [100.0, 100.0, 100.0, 100.0, 0.0])
        self.assertEqual(rows[2][1:], [100.0, 100.0, 100.0, 100.0, 0.0])
        self.assertEqual(rows[3][1:], [90.0, 90.0, 90.0, 90.0, 3.0])

    def test_overlap_mismatch_and_minimum_are_blocking(self):
        cutoff = datetime(2026, 8, 2, tzinfo=timezone.utc)
        start = cutoff - timedelta(days=1)
        candles = {}
        rows = []
        for index in range(96):
            timestamp = start + timedelta(minutes=15 * index)
            epoch = int(timestamp.timestamp())
            candles[epoch] = kraken_history._new_candle(timestamp, Decimal("100"), Decimal("1"))
            rows.append([epoch, "100", "100", "100", "100", "1", "1", 1])
        client = type("Client", (), {"ohlc": lambda self, pair: rows})()
        overlap = kraken_history.validate_ohlc_overlap(client, "BTC/GBP", candles, cutoff)
        self.assertEqual(overlap["STATUS"], "VERIFIED")
        rows[0][4] = "101"
        with self.assertRaisesRegex(kraken_history.HistoryError, "overlap mismatch"):
            kraken_history.validate_ohlc_overlap(client, "BTC/GBP", candles, cutoff)

    def test_target_parser_is_strict_and_deduplicated(self):
        self.assertEqual(
            kraken_history.TARGET_PAIRS,
            {
                "BTC_GBP": "BTC/GBP",
                "ETH_GBP": "ETH/GBP",
                "SOL_GBP": "SOL/GBP",
                "DOGE_GBP": "DOGE/GBP",
            },
        )
        self.assertEqual(
            kraken_history._parse_targets("btc/gbp,BTC_GBP,eth_gbp,sol_gbp,doge_gbp"),
            ["BTC_GBP", "ETH_GBP", "SOL_GBP", "DOGE_GBP"],
        )
        with self.assertRaisesRegex(kraken_history.HistoryError, "unsupported"):
            kraken_history._parse_targets("ETH_USD")


if __name__ == "__main__":
    unittest.main()

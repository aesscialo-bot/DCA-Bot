import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import unittest
from unittest.mock import MagicMock, patch

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
    def as_legacy_checkpoint(self, store, **changes):
        manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
        entry = manifest["TARGETS"]["DOGE_GBP"]
        for field in ("SCAN_VERSION", "COVERAGE_VERSION", "COVERAGE_THROUGH", "VERIFIED_AT", "EVIDENCE_HASH"):
            entry.pop(field, None)
        entry.update(changes)
        store.files[kraken_history.MANIFEST_FILENAME] = json.dumps(manifest)

    def seeded_store(self, *, last_trade_age=timedelta(hours=2)):
        cutoff = datetime(2026, 9, 5, 0, tzinfo=timezone.utc)
        start = cutoff - timedelta(days=65)
        real_end = cutoff - last_trade_age
        timestamps = [start] + [real_end - timedelta(minutes=15 * index) for index in range(96)]
        candles = {int(ts.timestamp()): kraken_history._new_candle(ts, Decimal("100"), Decimal("1")) for ts in timestamps}
        store = MemoryStore()
        overlap = {"STATUS": "VERIFIED", "CANDLES": 96,
                   "FROM": kraken_history._iso(min(timestamps[1:])), "THROUGH": kraken_history._iso(real_end)}
        kraken_history._write_checkpoint(
            store, {"TARGETS": {}}, "DOGE_GBP", candles,
            status="READY", pair="DOGE/GBP", query_from=start, cutoff=cutoff,
            last_ts=cutoff, last_trade_ids=[], overlap=overlap, verified_at=cutoff,
        )
        return store, cutoff, candles, overlap

    def test_old_real_trade_with_fresh_verified_coverage_is_ready(self):
        store, cutoff, candles, _ = self.seeded_store()
        original_files = dict(store.files)
        rows, summary = kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)
        self.assertEqual(summary["COVERAGE_THROUGH"], kraken_history._iso(cutoff))
        self.assertEqual(summary["LAST_REAL_CANDLE_AT"], kraken_history._iso(cutoff - timedelta(hours=2)))
        self.assertEqual(len(rows), 65 * 96)
        self.assertEqual(rows[-1][-1], 0)
        self.assertEqual(summary["CANDLE_COUNT"], len(candles))
        self.assertEqual(store.files, original_files, "analysis must never persist synthetic rows")
        self.assertEqual(summary["CARRIED_NO_TRADE_INTERVALS"], len(rows) - len(candles))

    def test_verified_legacy_ready_prefix_is_adopted_without_rewriting_real_partitions(self):
        store, cutoff, _, overlap = self.seeded_store()
        self.as_legacy_checkpoint(store)
        original_partitions = {name: value for name, value in store.files.items() if name.endswith(".jsonl")}
        client = MagicMock()
        client.post_trade_page.return_value = {"count": 0, "trades": [], "last_ts": ""}
        later = cutoff + timedelta(minutes=15)
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            result = kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        self.assertEqual(result["SCAN_VERSION"], 2)
        self.assertEqual(result["STATUS"], "READY")
        self.assertEqual({name: value for name, value in store.files.items() if name.endswith(".jsonl")}, original_partitions)

    def test_unverified_legacy_partial_or_error_prefix_is_rejected_without_writes(self):
        invalid_prefixes = [
            {"STATUS": "ERROR"}, {"STATUS": "BOOTSTRAPPING"},
            {"OVERLAP": {}}, {"OVERLAP": {"STATUS": "VERIFIED", "CANDLES": 95}},
            {"LAST_TS": "2026-09-04T23:59:00Z"},
        ]
        for changed in invalid_prefixes:
            store, cutoff, _, _ = self.seeded_store()
            self.as_legacy_checkpoint(store, **changed)
            before = dict(store.files)
            client = MagicMock()
            with self.subTest(changed=changed), self.assertRaisesRegex(kraken_history.HistoryError, "legacy history adoption requires"):
                kraken_history.refresh_target(store, client, "DOGE_GBP", now=cutoff + timedelta(minutes=15))
            self.assertEqual(store.files, before)
            client.post_trade_page.assert_not_called()

    def test_new_error_checkpoint_resumes_exact_partial_progress(self):
        store, cutoff, _, overlap = self.seeded_store()
        later = cutoff + timedelta(minutes=15)
        cursor = "2026-09-05T00:01:00.123456789Z"
        one = {"count": 1, "trades": [trade("NEW", cursor, pair="DOGE/GBP")], "last_ts": cursor}
        client = MagicMock()
        client.post_trade_page.side_effect = [one, kraken_history.HistoryError("outage")]
        with self.assertRaisesRegex(kraken_history.HistoryError, "outage"):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        partial = json.loads(store.files[kraken_history.MANIFEST_FILENAME])["TARGETS"]["DOGE_GBP"]
        self.assertEqual(partial["STATUS"], "ERROR")
        self.assertEqual(partial["SCAN_VERSION"], 2)
        self.assertEqual(partial["LAST_TS"], cursor)
        self.assertIn("EVIDENCE_HASH", partial)
        client = MagicMock()
        client.post_trade_page.side_effect = [one, {"count": 0, "trades": [], "last_ts": ""}]
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        self.assertEqual(client.post_trade_page.call_args_list[0].kwargs["from_ts"], cursor)
        rows, summary = kraken_history.load_ready_history("DOGE_GBP", store=store, now=later)
        self.assertEqual(rows[-1][-1], 1, "resumption must not duplicate the accepted partial page")
        self.assertEqual(summary["CANDLE_COUNT"], 98)

    def test_tampered_new_partial_cursor_is_rejected_before_any_write(self):
        store, cutoff, candles, _ = self.seeded_store()
        manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
        kraken_history._write_checkpoint(store, manifest, "DOGE_GBP", candles,
            status="BOOTSTRAPPING", pair="DOGE/GBP", query_from=cutoff - timedelta(days=65),
            cutoff=cutoff + timedelta(minutes=15), last_ts=cutoff, last_trade_ids=[])
        manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
        manifest["TARGETS"]["DOGE_GBP"]["LAST_TS"] = "2026-09-05T00:14:59Z"
        store.files[kraken_history.MANIFEST_FILENAME] = json.dumps(manifest)
        before = dict(store.files)
        with self.assertRaisesRegex(kraken_history.HistoryError, "evidence hash mismatch"):
            kraken_history.refresh_target(store, MagicMock(), "DOGE_GBP", now=cutoff + timedelta(minutes=15))
        self.assertEqual(store.files, before)

    def test_unknown_or_noninteger_scan_versions_cannot_bypass_adoption_guard(self):
        for version in (None, True, "2", 1, 3):
            store, cutoff, _, _ = self.seeded_store()
            manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
            manifest["TARGETS"]["DOGE_GBP"]["SCAN_VERSION"] = version
            store.files[kraken_history.MANIFEST_FILENAME] = json.dumps(manifest)
            before = dict(store.files)
            with self.subTest(version=version), self.assertRaisesRegex(kraken_history.HistoryError, "unsupported scan version"):
                kraken_history.refresh_target(store, MagicMock(), "DOGE_GBP", now=cutoff + timedelta(minutes=15))
            self.assertEqual(store.files, before)

    def test_direct_reader_rejects_rehashed_unsupported_scan_version(self):
        for version in (None, True, "2", 1, 3):
            store, cutoff, _, _ = self.seeded_store()
            manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
            entry = manifest["TARGETS"]["DOGE_GBP"]
            entry["SCAN_VERSION"] = version
            evidence = {key: value for key, value in entry.items() if key != "EVIDENCE_HASH"}
            entry["EVIDENCE_HASH"] = sha256(kraken_history._canonical_json(evidence).encode("utf-8")).hexdigest()
            store.files[kraken_history.MANIFEST_FILENAME] = json.dumps(manifest)
            with self.subTest(version=version), self.assertRaisesRegex(kraken_history.HistoryError, "current verified coverage refresh"):
                kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)

    def test_stale_and_future_coverage_cannot_produce_ready_history(self):
        store, cutoff, _, _ = self.seeded_store()
        for reference, expected in ((cutoff + timedelta(minutes=46), "stale"),
                                    (cutoff - timedelta(minutes=1), "future")):
            with self.subTest(reference=reference), self.assertRaisesRegex(kraken_history.HistoryError, expected):
                kraken_history.load_ready_history("DOGE_GBP", store=store, now=reference)

    def test_coverage_and_partition_hash_tampering_are_rejected(self):
        store, cutoff, _, _ = self.seeded_store()
        manifest = json.loads(store.files[kraken_history.MANIFEST_FILENAME])
        manifest["TARGETS"]["DOGE_GBP"]["CUTOFF"] = "2026-09-05T00:15:00Z"
        store.files[kraken_history.MANIFEST_FILENAME] = json.dumps(manifest)
        with self.assertRaisesRegex(kraken_history.HistoryError, "evidence hash"):
            kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)
        store, cutoff, _, _ = self.seeded_store()
        filename = next(name for name in store.files if name.endswith(".jsonl"))
        store.files[filename] += " "
        with self.assertRaisesRegex(kraken_history.HistoryError, "partition hash"):
            kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)

    def test_no_trade_tail_changes_coverage_hash_without_changing_real_partitions(self):
        store, cutoff, _, overlap = self.seeded_store()
        _, old = kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)
        client = MagicMock()
        client.post_trade_page.return_value = {"count": 0, "trades": [], "last_ts": ""}
        later = cutoff + timedelta(minutes=15)
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        _, new = kraken_history.load_ready_history("DOGE_GBP", store=store, now=later)
        self.assertEqual(old["PARTITIONS_HASH"], new["PARTITIONS_HASH"])
        self.assertNotEqual(old["HASH"], new["HASH"])
        self.assertEqual(old["LAST_REAL_CANDLE_AT"], new["LAST_REAL_CANDLE_AT"])

    def test_short_page_requires_verified_terminal_page_and_keeps_exact_cursor(self):
        store, cutoff, _, overlap = self.seeded_store()
        cursor = "2026-09-05T00:01:00.123456789Z"
        client = MagicMock()
        client.post_trade_page.side_effect = [
            {"count": 1, "trades": [trade("NEXT", cursor, pair="DOGE/GBP")], "last_ts": cursor},
            {"count": 0, "trades": [], "last_ts": ""},
        ]
        later = cutoff + timedelta(minutes=15)
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        self.assertEqual(client.post_trade_page.call_args_list[1].kwargs["from_ts"], cursor)
        rows, summary = kraken_history.load_ready_history("DOGE_GBP", store=store, now=later)
        self.assertEqual(summary["CANDLE_COUNT"], 98)
        self.assertEqual(rows[-1][-1], 1)

    def test_exact_cutoff_trade_is_consumed_once_when_its_interval_completes(self):
        store, cutoff, _, overlap = self.seeded_store()
        cursor = kraken_history._iso(cutoff)
        client = MagicMock()
        client.post_trade_page.side_effect = [
            {"count": 1, "trades": [trade("BOUNDARY", cursor, pair="DOGE/GBP")], "last_ts": cursor},
            {"count": 0, "trades": [], "last_ts": ""},
            {"count": 0, "trades": [], "last_ts": ""},
        ]
        later = cutoff + timedelta(minutes=15)
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        first_call = client.post_trade_page.call_args_list[0]
        self.assertEqual(first_call.kwargs["from_ts"], "2026-09-04T23:59:59.999999999Z")
        self.assertEqual(first_call.kwargs["to_ts"], "2026-09-05T00:14:59.999999999Z")
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later + timedelta(minutes=15)):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later + timedelta(minutes=15))
        entry = json.loads(store.files[kraken_history.MANIFEST_FILENAME])["TARGETS"]["DOGE_GBP"]
        raw = kraken_history._load_candles(store, entry)
        self.assertEqual(raw[int(cutoff.timestamp())]["trades"], 1)
        self.assertEqual(raw[int(cutoff.timestamp())]["volume"], "1")

    def test_sparse_history_still_has_all_sixty_complete_timing_days(self):
        import crypto_analysis
        store, cutoff, _, _ = self.seeded_store()
        rows, _ = kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)
        selected, timing = crypto_analysis.select_best_time(rows, now=cutoff)
        self.assertEqual(selected, "00:00")
        self.assertEqual(timing["HISTORY_CANDLES"], 60 * 96)
        self.assertEqual(set(timing["WINDOWS"]), {"3", "5", "7", "14", "30", "45", "60"})

    def test_source_outage_and_partial_page_fail_closed_without_claiming_coverage(self):
        malformed = [
            {}, {"count": 0, "last_ts": ""},
            {"count": 1, "trades": [], "last_ts": ""},
            {"count": 0, "trades": [], "last_ts": "2026-09-05T00:10:00Z"},
            kraken_history.HistoryError("source outage"),
        ]
        for bad_page in malformed:
            store, cutoff, _, _ = self.seeded_store()
            cursor = "2026-09-05T00:01:00.123456789Z"
            client = MagicMock()
            client.post_trade_page.side_effect = [
                {"count": 1, "trades": [trade("NEXT", cursor, pair="DOGE/GBP")], "last_ts": cursor}, bad_page,
            ]
            with self.subTest(page=bad_page), self.assertRaises(kraken_history.HistoryError):
                kraken_history.refresh_target(store, client, "DOGE_GBP", now=cutoff + timedelta(minutes=15))
            entry = json.loads(store.files[kraken_history.MANIFEST_FILENAME])["TARGETS"]["DOGE_GBP"]
            self.assertEqual(entry["STATUS"], "ERROR")
            self.assertEqual(entry["LAST_TS"], cursor)
            self.assertNotIn("COVERAGE_THROUGH", entry)
            with self.assertRaisesRegex(kraken_history.HistoryError, "status is ERROR"):
                kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)

    def test_malformed_page_cursor_duplicates_and_order_are_rejected_atomically(self):
        lower, upper = "2026-09-05T00:00:00Z", "2026-09-05T00:15:00Z"
        one = trade("A", "2026-09-05T00:01:00.123456789Z")
        two = trade("B", "2026-09-05T00:02:00.123456789Z")
        bad_pages = [
            {"count": 1, "trades": [one], "last_ts": two["trade_ts"]},
            {"count": 2, "trades": [two, one], "last_ts": one["trade_ts"]},
            {"count": 2, "trades": [one, one], "last_ts": one["trade_ts"]},
            {"count": 1, "trades": [trade("A", "2026-09-04T23:59:59Z")], "last_ts": "2026-09-04T23:59:59Z"},
            {"count": True, "trades": [one], "last_ts": one["trade_ts"]},
        ]
        for page in bad_pages:
            with self.subTest(page=page), self.assertRaises(kraken_history.HistoryError):
                kraken_history._validate_post_trade_page(page, "BTC/GBP", lower, upper)

    def test_real_kraken_inclusive_tail_is_deduplicated_before_empty_terminal_probe(self):
        store, cutoff, _, overlap = self.seeded_store()
        cursor = "2026-09-05T00:01:00.123456789Z"
        tail = {"count": 1, "trades": [trade("NEXT", cursor, pair="DOGE/GBP")], "last_ts": cursor}
        client = MagicMock()
        client.post_trade_page.side_effect = [tail, tail, {"count": 0, "trades": [], "last_ts": ""}]
        later = cutoff + timedelta(minutes=15)
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=later):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=later)
        self.assertEqual(client.post_trade_page.call_args_list[2].kwargs["from_ts"], "2026-09-05T00:01:00.123456790Z")
        rows, _ = kraken_history.load_ready_history("DOGE_GBP", store=store, now=later)
        self.assertEqual(rows[-1][-1], 1)

    def test_stalled_full_timestamp_group_is_never_skipped(self):
        store, cutoff, _, _ = self.seeded_store()
        cursor = "2026-09-05T00:01:00.123456789Z"
        batch = {"count": 1000, "last_ts": cursor,
                 "trades": [trade(str(index), cursor, pair="DOGE/GBP") for index in range(1000)]}
        client = MagicMock()
        client.post_trade_page.return_value = batch
        with self.assertRaisesRegex(kraken_history.HistoryError, "no progress on a full page"):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=cutoff + timedelta(minutes=15))
        self.assertEqual(client.post_trade_page.call_count, 2)
        self.assertEqual(client.post_trade_page.call_args.kwargs["from_ts"], cursor)

    def test_first_bootstrap_boundary_is_included_without_leading_price_fabrication(self):
        store = MemoryStore()
        cutoff = datetime(2026, 9, 5, tzinfo=timezone.utc)
        start = cutoff - timedelta(days=65)
        stamp = kraken_history._iso(start)
        trades = [trade(str(index), kraken_history._iso(start + timedelta(minutes=index * 15)), pair="DOGE/GBP") for index in range(96)]
        client = MagicMock()
        client.post_trade_page.side_effect = [
            {"count": 96, "last_ts": trades[-1]["trade_ts"], "trades": trades},
            {"count": 0, "last_ts": "", "trades": []},
        ]
        overlap = {"STATUS": "VERIFIED", "CANDLES": 96, "FROM": stamp, "THROUGH": trades[-1]["trade_ts"]}
        with patch.object(kraken_history, "validate_ohlc_overlap", return_value=overlap), patch.object(kraken_history, "_utc_now", return_value=cutoff):
            kraken_history.refresh_target(store, client, "DOGE_GBP", now=cutoff)
        self.assertEqual(client.post_trade_page.call_args_list[0].kwargs["from_ts"], kraken_history._just_before(start))
        rows, summary = kraken_history.load_ready_history("DOGE_GBP", store=store, now=cutoff)
        self.assertEqual(rows[0][0], start.timestamp() * 1000)
        self.assertEqual(summary["CANDLE_COUNT"], 96)

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

    def test_analysis_carries_verified_trailing_gaps_but_never_leading_gaps(self):
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
        self.assertEqual(carried, 3)
        self.assertEqual([row[0] for row in rows], [
            int((start + timedelta(minutes=offset)).timestamp() * 1000)
            for offset in (0, 15, 30, 45, 60)
        ])
        self.assertEqual(rows[1][1:], [100.0, 100.0, 100.0, 100.0, 0.0])
        self.assertEqual(rows[2][1:], [100.0, 100.0, 100.0, 100.0, 0.0])
        self.assertEqual(rows[3][1:], [90.0, 90.0, 90.0, 90.0, 3.0])
        self.assertEqual(rows[4][1:], [90.0, 90.0, 90.0, 90.0, 0.0])

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

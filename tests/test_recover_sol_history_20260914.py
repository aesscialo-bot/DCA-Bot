import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import kraken_history as history
import recover_sol_history_20260914 as recovery


class FakeStore:
    def __init__(self):
        self.files = {}
        self.source = None
        self.writes = []
        self.before_recheck = None
        self.current_reads = 0
        self.corrupt_readback = False

    def write_files(self, files):
        self.files.update(files)

    def read_file(self, filename, snapshot=None):
        if snapshot is not None:
            return snapshot["files"].get(filename, {}).get("content", "")
        return self.files.get(filename, "")

    def snapshot_at(self, revision=None):
        if revision is not None:
            if revision != recovery.SOURCE_REVISION:
                raise AssertionError("source revision must remain pinned")
            return copy.deepcopy(self.source)
        self.current_reads += 1
        if self.current_reads == 2 and self.before_recheck:
            self.before_recheck(self)
        return {"files": {name: {"content": content} for name, content in self.files.items()}}

    def write_manifest(self, content):
        self.writes.append(content)
        self.files[history.MANIFEST_FILENAME] = content + (" " if self.corrupt_readback else "")
        return self.snapshot_at()


class SolHistoryRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.store = FakeStore()
        cutoff = datetime(2026, 9, 13, 23, tzinfo=timezone.utc)
        start = cutoff - timedelta(days=65)
        times = [start] + [cutoff - timedelta(minutes=15 * index) for index in range(1, 97)]
        candles = {int(ts.timestamp()): history._new_candle(ts, Decimal("100"), Decimal("1")) for ts in times}
        manifest = {"TARGETS": {"BTC_GBP": {"STATUS": "READY", "PRESERVE": True}}}
        overlap = {"STATUS": "VERIFIED", "CANDLES": 96,
                   "FROM": history._iso(cutoff - timedelta(days=1)),
                   "THROUGH": history._iso(cutoff - timedelta(minutes=15))}
        history._write_checkpoint(
            self.store, manifest, recovery.TARGET, candles, status="READY", pair="SOL/GBP",
            query_from=start, cutoff=cutoff, last_ts=cutoff, last_trade_ids=[],
            overlap=overlap, verified_at=cutoff + timedelta(minutes=5),
        )
        self.store.source = self.store.snapshot_at()
        history._write_checkpoint(
            self.store, manifest, recovery.TARGET, candles, status="BOOTSTRAPPING", pair="SOL/GBP",
            query_from=start, cutoff=cutoff, last_ts=cutoff - timedelta(minutes=15), last_trade_ids=[],
        )
        self.store.current_reads = 0
        self.before = dict(self.store.files)
        self.expected = recovery.manifest_hash(self.before[history.MANIFEST_FILENAME])
        self.shadow = MagicMock()

    def run_recovery(self, mode="apply", expected=None):
        return recovery.recover(
            mode=mode, expected_manifest_hash=self.expected if expected is None else expected,
            store=self.store, check_shadow=self.shadow,
        )

    def test_preview_is_read_only_and_returns_full_manifest_hash(self):
        result = self.run_recovery(mode="preview", expected="")
        self.assertEqual(result["current_manifest_sha256"], self.expected)
        self.assertEqual(result["source_revision"], recovery.SOURCE_REVISION)
        self.assertTrue(result["fresh_refresh_required"])
        self.assertFalse(result["applied"])
        self.assertEqual(self.store.files, self.before)
        self.assertEqual(self.store.writes, [])

    def test_apply_changes_only_sol_entry_and_preserves_historical_timestamps(self):
        result = self.run_recovery()
        self.assertTrue(result["applied"])
        before = json.loads(self.before[history.MANIFEST_FILENAME])
        after = json.loads(self.store.files[history.MANIFEST_FILENAME])
        source = json.loads(self.store.source["files"][history.MANIFEST_FILENAME]["content"])
        self.assertEqual(after["TARGETS"][recovery.TARGET], source["TARGETS"][recovery.TARGET])
        after["TARGETS"][recovery.TARGET] = before["TARGETS"][recovery.TARGET]
        self.assertEqual(after, before)
        self.assertEqual(
            {name: content for name, content in self.store.files.items() if name != history.MANIFEST_FILENAME},
            {name: content for name, content in self.before.items() if name != history.MANIFEST_FILENAME},
        )
        self.assertEqual(len(self.store.writes), 1)
        self.assertEqual(self.shadow.call_count, 2)

    def test_apply_rejects_missing_or_wrong_hash_without_writing(self):
        for expected in ("", "bad", "0" * 64):
            with self.subTest(expected=expected), self.assertRaises(recovery.RecoveryError):
                self.run_recovery(expected=expected)
        self.assertEqual(self.store.writes, [])

    def test_manifest_change_between_validation_and_write_is_blocked(self):
        self.store.before_recheck = lambda store: store.files.update({history.MANIFEST_FILENAME: self.before[history.MANIFEST_FILENAME] + " "})
        with self.assertRaisesRegex(recovery.RecoveryError, "changed during recovery"):
            self.run_recovery()
        self.assertEqual(self.store.writes, [])

    def test_current_or_rechecked_partition_tampering_is_blocked(self):
        filename = next(name for name in self.store.files if name.endswith(".jsonl"))
        for delayed in (False, True):
            self.store.files = dict(self.before)
            self.store.current_reads = 0
            mutate = lambda store: store.files.update({filename: store.files[filename] + " "})
            self.store.before_recheck = mutate if delayed else None
            if not delayed:
                mutate(self.store)
            with self.subTest(delayed=delayed), self.assertRaisesRegex(history.HistoryError, "partition hash"):
                self.run_recovery()
            self.assertEqual(self.store.writes, [])

    def test_shadow_mode_change_before_apply_is_blocked(self):
        self.shadow.side_effect = [None, recovery.RecoveryError("requires shadow")]
        with self.assertRaisesRegex(recovery.RecoveryError, "requires shadow"):
            self.run_recovery()
        self.assertEqual(self.store.writes, [])

    def test_unverified_source_entry_is_blocked(self):
        source = json.loads(self.store.source["files"][history.MANIFEST_FILENAME]["content"])
        source["TARGETS"][recovery.TARGET]["EVIDENCE_HASH"] = "0" * 64
        self.store.source["files"][history.MANIFEST_FILENAME]["content"] = json.dumps(source)
        with self.assertRaisesRegex(history.HistoryError, "evidence hash"):
            self.run_recovery()
        self.assertEqual(self.store.writes, [])

    def test_recovery_cannot_overwrite_a_different_incident_or_ready_entry(self):
        for changes in ({"STATUS": "READY"}, {"CUTOFF": "2026-09-14T23:00:00Z"}):
            current = json.loads(self.before[history.MANIFEST_FILENAME])
            current["TARGETS"][recovery.TARGET].update(changes)
            content = json.dumps(current)
            self.store.files[history.MANIFEST_FILENAME] = content
            with self.subTest(changes=changes), self.assertRaisesRegex(recovery.RecoveryError, "does not match this incident"):
                self.run_recovery(expected=recovery.manifest_hash(content))
            self.assertEqual(self.store.writes, [])

    def test_readback_mismatch_is_reported_after_single_write(self):
        self.store.corrupt_readback = True
        with self.assertRaisesRegex(recovery.RecoveryError, "readback differs"):
            self.run_recovery()
        self.assertEqual(len(self.store.writes), 1, "must not retry an uncertain write")

    def test_transport_patches_only_manifest_and_reads_immutable_revision(self):
        session = MagicMock()
        session.patch.return_value.json.return_value = {"history": [{"version": "a" * 40}]}
        session.get.return_value.json.return_value = {"files": {history.MANIFEST_FILENAME: {"content": "{}"}}}
        store = recovery.RecoveryStore("gist", "token", session=session)
        store.write_manifest("{}")
        self.assertEqual(session.patch.call_args.kwargs["json"], {"files": {history.MANIFEST_FILENAME: {"content": "{}"}}})
        self.assertEqual(session.get.call_args.args[0], store.url + "/" + "a" * 40)
        for version in (None, "", "main"):
            session.patch.return_value.json.return_value = {"history": [{"version": version}]}
            with self.subTest(version=version), self.assertRaisesRegex(recovery.RecoveryError, "immutable revision"):
                store.write_manifest("{}")
        for payload in ({}, {"history": []}, {"history": [{}]}):
            session.patch.return_value.json.return_value = payload
            with self.subTest(payload=payload), self.assertRaisesRegex(recovery.RecoveryError, "immutable revision"):
                store.write_manifest("{}")

    def test_shadow_guard_reads_live_repository_variable(self):
        session = MagicMock()
        with patch.dict(os.environ, {"GITHUB_REPOSITORY": "owner/repository", "GH_PAT_FOR_VARS": "token", "DCA_TRADING_MODE": "shadow"}):
            for value in ("live", "paused", None):
                session.get.return_value.json.return_value = {"value": value}
                with self.subTest(value=value), self.assertRaisesRegex(recovery.RecoveryError, "live repository"):
                    recovery.assert_shadow(session=session)
            session.get.return_value.json.return_value = {"value": "shadow"}
            recovery.assert_shadow(session=session)
        self.assertTrue(session.get.call_args.args[0].endswith("/actions/variables/DCA_TRADING_MODE"))
        self.assertEqual(session.get.call_args.kwargs["headers"]["Cache-Control"], "no-cache")

    def test_workflow_uses_history_lock_and_has_no_exchange_credentials(self):
        workflow = (Path(__file__).resolve().parents[1] / ".github/workflows/recover_sol_history_20260914.yml").read_text()
        for text in ("group: dca-kraken-history-writer", "queue: max", "cancel-in-progress: false", "github.ref == 'refs/heads/main'"):
            self.assertIn(text, workflow)
        self.assertNotIn("KRAKEN_API", workflow)
        self.assertNotIn("${{ inputs.", workflow.split("run: python -u")[-1])


if __name__ == "__main__":
    unittest.main()

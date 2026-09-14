"""Hash-bound recovery of the September 14 SOL history manifest regression.

Run only under the dca-kraken-history-writer workflow lock. This restores one
historical entry; normal history refresh must subsequently prove freshness.
"""

from __future__ import annotations

import argparse
import copy
from hashlib import sha256
import json
import os
import re

import requests

import kraken_history as history


SOURCE_REVISION = "724a3af1ffb6c98a4df87c20f6a1e9412e7b7c5c"
TARGET = "SOL_GBP"
INCIDENT_CUTOFF = "2026-09-13T23:00:00Z"


class RecoveryError(RuntimeError):
    """Safe recovery failure without credentials or raw stored state."""


def manifest_hash(content: str) -> str:
    return sha256(content.encode("utf-8")).hexdigest()


def assert_shadow(*, session=requests) -> None:
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    token = os.environ.get("GH_PAT_FOR_VARS", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not token:
        raise RecoveryError("Repository identity and variable-read credential are required")
    response = session.get(
        f"https://api.github.com/repos/{repository}/actions/variables/DCA_TRADING_MODE",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "Cache-Control": "no-cache"},
        timeout=history.REQUEST_TIMEOUT_SECONDS,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or payload.get("value") != "shadow":
        raise RecoveryError("Recovery requires live repository DCA_TRADING_MODE=shadow")


class RecoveryStore(history.HistoryGistStore):
    def snapshot_at(self, revision: str | None = None) -> dict:
        if revision is not None and not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RecoveryError("Gist revision must be immutable")
        response = self.session.get(
            self.url + (f"/{revision}" if revision else ""),
            headers=self.headers, timeout=history.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
            raise RecoveryError("History Gist snapshot is invalid")
        return payload

    def write_manifest(self, content: str) -> dict:
        # Deliberately bypass the writer's local read-your-writes overlay.
        # Readback must prove the server's immutable committed representation.
        response = self.session.patch(
            self.url, headers=self.headers,
            json={"files": {history.MANIFEST_FILENAME: {"content": content}}},
            timeout=history.REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        revisions = payload.get("history") if isinstance(payload, dict) else None
        if not isinstance(revisions, list) or not revisions or not isinstance(revisions[0], dict):
            raise RecoveryError("Recovery write returned no immutable revision; inspect before retrying")
        revision = revisions[0].get("version")
        if not isinstance(revision, str) or not re.fullmatch(r"[0-9a-f]{40}", revision):
            raise RecoveryError("Recovery write returned no immutable revision; inspect before retrying")
        return self.snapshot_at(revision)


class _SnapshotView:
    def __init__(self, store, snapshot):
        self.store, self.payload = store, snapshot

    def snapshot(self):
        return self.payload

    def read_file(self, filename, snapshot=None):
        return self.store.read_file(filename, self.payload if snapshot is None else snapshot)


def _validate_proposal(store, current: dict, proposed: str, entry: dict) -> dict:
    snapshot = {**current, "files": dict(current["files"])}
    snapshot["files"][history.MANIFEST_FILENAME] = {"content": proposed, "truncated": False}
    # Historical validation is intentional: preserve the authenticated old
    # timestamps, never relabel this recovery as fresh market coverage.
    _, summary = history.load_ready_history(
        TARGET, store=_SnapshotView(store, snapshot),
        now=history._parse_iso(entry.get("VERIFIED_AT"), "source VERIFIED_AT"),
    )
    return summary


def recover(*, mode: str, expected_manifest_hash: str = "", store=None, check_shadow=None) -> dict:
    if mode not in {"preview", "apply"}:
        raise RecoveryError("Recovery mode must be preview or apply")
    if (mode == "apply" or expected_manifest_hash) and not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_hash):
        raise RecoveryError("Apply requires the exact full manifest SHA256 from preview")
    check_shadow = check_shadow or assert_shadow
    check_shadow()
    store = store or RecoveryStore()
    current = store.snapshot_at()
    original = store.read_file(history.MANIFEST_FILENAME, current)
    original_hash = manifest_hash(original)
    if expected_manifest_hash and expected_manifest_hash != original_hash:
        raise RecoveryError("Current manifest changed from the reviewed SHA256")
    manifest = history.load_manifest(store, current)
    existing = manifest["TARGETS"].get(TARGET)
    if (not isinstance(existing, dict) or existing.get("STATUS") != "BOOTSTRAPPING"
            or existing.get("CUTOFF") != INCIDENT_CUTOFF):
        raise RecoveryError("Current SOL entry does not match this incident")
    source = history.load_manifest(store, store.snapshot_at(SOURCE_REVISION))
    entry = source["TARGETS"].get(TARGET)
    if (not isinstance(entry, dict) or entry.get("STATUS") != "READY"
            or entry.get("CUTOFF") != INCIDENT_CUTOFF):
        raise RecoveryError("Immutable source is not the incident's verified READY entry")
    proposed_manifest = copy.deepcopy(manifest)
    proposed_manifest["TARGETS"][TARGET] = copy.deepcopy(entry)
    proposed = json.dumps(proposed_manifest, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    summary = _validate_proposal(store, current, proposed, entry)
    report = {
        "mode": mode, "target": TARGET, "source_revision": SOURCE_REVISION,
        "current_manifest_sha256": original_hash,
        "proposed_manifest_sha256": manifest_hash(proposed),
        "coverage_through": summary["COVERAGE_THROUGH"],
        "original_verified_at": entry["VERIFIED_AT"],
        "real_candles_verified": summary["CANDLE_COUNT"],
        "fresh_refresh_required": True, "applied": False,
    }
    if mode == "preview":
        return report
    latest = store.snapshot_at()
    if manifest_hash(store.read_file(history.MANIFEST_FILENAME, latest)) != original_hash:
        raise RecoveryError("Manifest changed during recovery; no write performed")
    _validate_proposal(store, latest, proposed, entry)
    check_shadow()
    readback = store.write_manifest(proposed)
    if manifest_hash(store.read_file(history.MANIFEST_FILENAME, readback)) != manifest_hash(proposed):
        raise RecoveryError("Committed manifest readback differs; inspect before retrying")
    _validate_proposal(store, readback, proposed, entry)
    report["applied"] = True
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "apply"), default="preview")
    parser.add_argument("--expected-manifest-hash", default="")
    args = parser.parse_args(argv)
    try:
        report = recover(mode=args.mode, expected_manifest_hash=args.expected_manifest_hash)
    except RecoveryError as exc:
        print(f"Recovery blocked: {exc}", flush=True)
        return 1
    except Exception as exc:
        print(f"Recovery failed safely ({type(exc).__name__}); inspect state before retrying", flush=True)
        return 1
    print(json.dumps(report, sort_keys=True, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

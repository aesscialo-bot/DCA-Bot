"""Durable first-party Kraken 15-minute history for deterministic DCA timing.

The Spot OHLC endpoint exposes only the latest 720 candles.  This module uses
Kraken's public PostTrade feed to build and incrementally maintain an auditable
15-minute candle store in a dedicated private Gist.  Trading code never calls
this module and no Kraken account credential is required.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import os
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import requests


HISTORY_VERSION = 1
INTERVAL_MINUTES = 15
INTERVAL_SECONDS = INTERVAL_MINUTES * 60
BOOTSTRAP_DAYS = 65
POST_TRADE_URL = "https://api.kraken.com/0/public/PostTrade"
OHLC_URL = "https://api.kraken.com/0/public/OHLC"
MANIFEST_FILENAME = "kraken_history_manifest_v1.json"
REQUEST_TIMEOUT_SECONDS = 30
MAX_GIST_FILE_BYTES = 8_000_000
MAX_PAGE_SIZE = 1_000
MIN_OVERLAP_CANDLES = 96
TARGET_PAIRS = {
    "BTC_GBP": "BTC/GBP",
    "ETH_GBP": "ETH/GBP",
    "SOL_GBP": "SOL/GBP",
}


class HistoryError(RuntimeError):
    """Raised when history cannot be proven complete and authentic enough."""


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise HistoryError("history timestamps must include a timezone")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any, label: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise HistoryError(f"{label} must be a UTC ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise HistoryError(f"{label} must be a UTC ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise HistoryError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _completed_cutoff(now: datetime) -> datetime:
    if now.tzinfo is None or now.utcoffset() is None:
        raise HistoryError("history cutoff time must include a timezone")
    epoch = int(now.astimezone(timezone.utc).timestamp())
    return datetime.fromtimestamp(epoch - epoch % INTERVAL_SECONDS, tz=timezone.utc)


def _decimal(value: Any, label: str, *, allow_zero: bool = False) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise HistoryError(f"{label} must be a decimal number") from exc
    if not number.is_finite() or number < 0 or (number == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise HistoryError(f"{label} must be a {qualifier} decimal number")
    return number


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _safe_raw_url(value: Any) -> str:
    if not isinstance(value, str):
        raise HistoryError("truncated Gist file is missing its raw URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gist.githubusercontent.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise HistoryError("truncated Gist file returned an unsafe raw URL")
    return value


class HistoryGistStore:
    """Small authenticated text store backed by one private GitHub Gist."""

    def __init__(
        self,
        gist_id: str | None = None,
        token: str | None = None,
        *,
        session=requests,
    ) -> None:
        self.gist_id = (gist_id or os.environ.get("DCA_HISTORY_GIST_ID", "")).strip()
        self.token = (token or os.environ.get("GIST_TOKEN", "")).strip()
        if not self.gist_id or not self.token:
            raise HistoryError("DCA_HISTORY_GIST_ID and GIST_TOKEN are required")
        self.session = session
        self.url = f"https://api.github.com/gists/{self.gist_id}"
        self.headers = {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    def snapshot(self) -> dict[str, Any]:
        response = self.session.get(
            self.url, headers=self.headers, timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict) or not isinstance(payload.get("files"), dict):
            raise HistoryError("history Gist returned an invalid files payload")
        return payload

    def read_file(self, filename: str, snapshot: Mapping[str, Any] | None = None) -> str:
        gist = dict(snapshot) if snapshot is not None else self.snapshot()
        info = gist.get("files", {}).get(filename)
        if info is None:
            return ""
        if not isinstance(info, Mapping):
            raise HistoryError(f"history Gist file {filename} is invalid")
        if info.get("truncated") is True:
            response = self.session.get(
                _safe_raw_url(info.get("raw_url")),
                headers=self.headers,
                timeout=REQUEST_TIMEOUT_SECONDS,
            )
            response.raise_for_status()
            content = response.text
        else:
            content = info.get("content", "")
        if not isinstance(content, str):
            raise HistoryError(f"history Gist file {filename} is not text")
        if len(content.encode("utf-8")) > MAX_GIST_FILE_BYTES:
            raise HistoryError(f"history Gist file {filename} exceeds the size limit")
        return content

    def write_files(self, files: Mapping[str, str]) -> None:
        if not files:
            return
        body: dict[str, dict[str, str]] = {}
        for filename, content in files.items():
            if not isinstance(content, str):
                raise HistoryError(f"history file {filename} must be text")
            if len(content.encode("utf-8")) > MAX_GIST_FILE_BYTES:
                raise HistoryError(f"history file {filename} exceeds the size limit")
            body[filename] = {"content": content}
        response = self.session.patch(
            self.url,
            headers=self.headers,
            json={"files": body},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()


class KrakenPublicHistoryClient:
    """Rate-limited first-party Kraken PostTrade and OHLC client."""

    def __init__(self, *, session=requests, sleep=time.sleep, min_interval=1.0) -> None:
        self.session = session
        self.sleep = sleep
        self.min_interval = float(min_interval)
        self._last_request_monotonic: float | None = None

    def _pace(self) -> None:
        if self._last_request_monotonic is not None:
            remaining = self.min_interval - (
                time.monotonic() - self._last_request_monotonic
            )
            if remaining > 0:
                self.sleep(remaining)

    def _get(self, url: str, params: Mapping[str, Any]) -> dict[str, Any]:
        last_error: Exception | None = None
        for attempt in range(5):
            self._pace()
            try:
                response = self.session.get(
                    url,
                    params=dict(params),
                    headers={"Accept": "application/json"},
                    timeout=REQUEST_TIMEOUT_SECONDS,
                )
                self._last_request_monotonic = time.monotonic()
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    raise requests.HTTPError(f"Kraken HTTP {response.status_code}")
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise HistoryError("Kraken returned a non-object response")
                errors = payload.get("error", [])
                if errors:
                    raise HistoryError("Kraken returned a market-data error")
                return payload
            except (requests.RequestException, ValueError, HistoryError) as exc:
                last_error = exc
                if attempt == 4:
                    break
                self.sleep(min(2 ** attempt, 16))
        raise HistoryError(
            f"Kraken market-data request failed after retries ({type(last_error).__name__})"
        ) from last_error

    def post_trade_page(
        self, pair: str, *, from_ts: datetime, to_ts: datetime
    ) -> dict[str, Any]:
        payload = self._get(
            POST_TRADE_URL,
            {
                "symbol": pair,
                "from_ts": _iso(from_ts),
                "to_ts": _iso(to_ts),
                "count": MAX_PAGE_SIZE,
            },
        )
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise HistoryError("Kraken PostTrade response has no result")
        trades = result.get("trades", [])
        if not isinstance(trades, list):
            raise HistoryError("Kraken PostTrade trades must be an array")
        last_ts = result.get("last_ts")
        if trades and not isinstance(last_ts, str):
            raise HistoryError("Kraken PostTrade page is missing last_ts")
        return {"trades": trades, "last_ts": last_ts, "count": len(trades)}

    def ohlc(self, pair: str) -> list[list[Any]]:
        payload = self._get(OHLC_URL, {"pair": pair, "interval": INTERVAL_MINUTES})
        result = payload.get("result")
        if not isinstance(result, Mapping):
            raise HistoryError("Kraken OHLC response has no result")
        keys = [key for key in result if key != "last"]
        if len(keys) != 1 or not isinstance(result[keys[0]], list):
            raise HistoryError("Kraken OHLC response has an unexpected pair result")
        return list(result[keys[0]])


def _partition_filename(target: str, candle_start: datetime) -> str:
    return f"kraken_history_v1_{target}_{candle_start:%Y-%m}.jsonl"


def _new_candle(timestamp: datetime, price: Decimal, quantity: Decimal) -> dict[str, Any]:
    return {
        "ts": _iso(timestamp),
        "open": str(price),
        "high": str(price),
        "low": str(price),
        "close": str(price),
        "volume": str(quantity),
        "trades": 1,
        "traded": True,
    }


def _add_trade(candles: dict[int, dict[str, Any]], trade: Mapping[str, Any], pair: str) -> None:
    trade_id = trade.get("trade_id")
    if not isinstance(trade_id, str) or not trade_id.strip():
        raise HistoryError("Kraken PostTrade trade is missing trade_id")
    if str(trade.get("symbol", "")).upper() != pair:
        raise HistoryError("Kraken PostTrade returned a different currency pair")
    timestamp = _parse_iso(trade.get("trade_ts"), "Kraken trade_ts")
    price = _decimal(trade.get("price"), "Kraken trade price")
    quantity = _decimal(trade.get("quantity"), "Kraken trade quantity")
    bucket_epoch = int(timestamp.timestamp()) // INTERVAL_SECONDS * INTERVAL_SECONDS
    bucket = datetime.fromtimestamp(bucket_epoch, tz=timezone.utc)
    existing = candles.get(bucket_epoch)
    if existing is None:
        candles[bucket_epoch] = _new_candle(bucket, price, quantity)
        return
    existing["high"] = str(max(Decimal(existing["high"]), price))
    existing["low"] = str(min(Decimal(existing["low"]), price))
    existing["close"] = str(price)
    existing["volume"] = str(Decimal(existing["volume"]) + quantity)
    existing["trades"] = int(existing["trades"]) + 1


def _serialize_partitions(
    target: str, candles: Mapping[int, Mapping[str, Any]]
) -> tuple[dict[str, str], dict[str, str]]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for epoch in sorted(candles):
        timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
        grouped[_partition_filename(target, timestamp)].append(candles[epoch])
    contents: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for filename, rows in grouped.items():
        content = "".join(_canonical_json(row) + "\n" for row in rows)
        contents[filename] = content
        hashes[filename] = sha256(content.encode("utf-8")).hexdigest()
    return contents, hashes


def _gap_summary(
    candles: Mapping[int, Mapping[str, Any]],
    query_from: datetime,
    cutoff: datetime,
) -> dict[str, Any]:
    """Compress genuine no-trade intervals into explicit UTC ranges."""
    start_epoch = int(query_from.timestamp())
    cutoff_epoch = int(cutoff.timestamp())
    missing = [
        epoch
        for epoch in range(start_epoch, cutoff_epoch, INTERVAL_SECONDS)
        if epoch not in candles
    ]
    ranges: list[dict[str, Any]] = []
    for epoch in missing:
        if ranges and epoch == ranges[-1]["_last"] + INTERVAL_SECONDS:
            ranges[-1]["_last"] = epoch
            ranges[-1]["THROUGH"] = _iso(
                datetime.fromtimestamp(epoch + INTERVAL_SECONDS, tz=timezone.utc)
            )
            ranges[-1]["INTERVALS"] += 1
        else:
            ranges.append(
                {
                    "FROM": _iso(datetime.fromtimestamp(epoch, tz=timezone.utc)),
                    "THROUGH": _iso(
                        datetime.fromtimestamp(epoch + INTERVAL_SECONDS, tz=timezone.utc)
                    ),
                    "INTERVALS": 1,
                    "_last": epoch,
                }
            )
    for item in ranges:
        item.pop("_last", None)
    return {"COUNT": len(missing), "RANGES": ranges}


def _load_candles(
    store: HistoryGistStore,
    target_manifest: Mapping[str, Any],
    snapshot: Mapping[str, Any] | None = None,
) -> dict[int, dict[str, Any]]:
    partitions = target_manifest.get("PARTITIONS", {})
    if not isinstance(partitions, Mapping):
        raise HistoryError("history manifest partitions must be an object")
    candles: dict[int, dict[str, Any]] = {}
    for filename, expected_hash in sorted(partitions.items()):
        content = store.read_file(filename, snapshot)
        if sha256(content.encode("utf-8")).hexdigest() != expected_hash:
            raise HistoryError(f"history partition hash mismatch: {filename}")
        for line in content.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise HistoryError(f"history partition is malformed: {filename}") from exc
            if not isinstance(row, dict):
                raise HistoryError(f"history partition row is invalid: {filename}")
            timestamp = _parse_iso(row.get("ts"), "history candle timestamp")
            epoch = int(timestamp.timestamp())
            if epoch % INTERVAL_SECONDS:
                raise HistoryError("history candle is not aligned to 15 minutes")
            if epoch in candles:
                raise HistoryError("history contains a duplicate candle timestamp")
            for field in ("open", "high", "low", "close", "volume"):
                _decimal(row.get(field), f"history candle {field}", allow_zero=field == "volume")
            if type(row.get("trades")) is not int or row["trades"] < 1:
                raise HistoryError("history candle trade count must be positive")
            candles[epoch] = row
    return candles


def load_manifest(
    store: HistoryGistStore, snapshot: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    content = store.read_file(MANIFEST_FILENAME, snapshot)
    if not content.strip():
        return {"VERSION": HISTORY_VERSION, "UPDATED_AT": _iso(_utc_now()), "TARGETS": {}}
    try:
        manifest = json.loads(content)
    except json.JSONDecodeError as exc:
        raise HistoryError("history manifest is not valid JSON") from exc
    if not isinstance(manifest, dict) or manifest.get("VERSION") != HISTORY_VERSION:
        raise HistoryError(f"history manifest VERSION must be {HISTORY_VERSION}")
    if not isinstance(manifest.get("TARGETS"), dict):
        raise HistoryError("history manifest TARGETS must be an object")
    return manifest


def _write_checkpoint(
    store: HistoryGistStore,
    manifest: dict[str, Any],
    target: str,
    candles: Mapping[int, Mapping[str, Any]],
    *,
    status: str,
    pair: str,
    query_from: datetime,
    cutoff: datetime,
    last_ts: datetime,
    last_trade_ids: Iterable[str],
    overlap: Mapping[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    partition_contents, hashes = _serialize_partitions(target, candles)
    sorted_epochs = sorted(candles)
    expected = max(0, int((cutoff - query_from).total_seconds()) // INTERVAL_SECONDS)
    gaps = _gap_summary(candles, query_from, cutoff)
    entry = {
        "STATUS": status,
        "SOURCE": "Kraken PostTrade",
        "PAIR": pair,
        "INTERVAL_MINUTES": INTERVAL_MINUTES,
        "QUERY_FROM": _iso(query_from),
        "CUTOFF": _iso(cutoff),
        "LAST_TS": _iso(last_ts),
        "LAST_TRADE_IDS": sorted(set(last_trade_ids)),
        "CANDLE_START": _iso(datetime.fromtimestamp(sorted_epochs[0], tz=timezone.utc)) if sorted_epochs else None,
        "CANDLE_END": _iso(datetime.fromtimestamp(sorted_epochs[-1], tz=timezone.utc)) if sorted_epochs else None,
        "CANDLE_COUNT": len(candles),
        "EXPECTED_INTERVALS": expected,
        "NO_TRADE_INTERVALS": gaps["COUNT"],
        "GAP_SUMMARY": gaps,
        "PARTITIONS": hashes,
        "OVERLAP": dict(overlap or {}),
        "ERROR": error,
    }
    manifest["VERSION"] = HISTORY_VERSION
    manifest["UPDATED_AT"] = _iso(_utc_now())
    manifest.setdefault("TARGETS", {})[target] = entry
    files = dict(partition_contents)
    files[MANIFEST_FILENAME] = json.dumps(
        manifest, sort_keys=True, indent=2, ensure_ascii=False
    ) + "\n"
    store.write_files(files)
    return entry


def validate_ohlc_overlap(
    client: KrakenPublicHistoryClient,
    pair: str,
    candles: Mapping[int, Mapping[str, Any]],
    cutoff: datetime,
) -> dict[str, Any]:
    compared = 0
    first: datetime | None = None
    last: datetime | None = None
    for row in client.ohlc(pair):
        if not isinstance(row, list) or len(row) < 8:
            raise HistoryError("Kraken OHLC row is malformed")
        epoch = int(row[0])
        if epoch >= int(cutoff.timestamp()):
            continue
        trade_count = int(row[7])
        if trade_count == 0:
            continue
        candidate = candles.get(epoch)
        if candidate is None:
            raise HistoryError("PostTrade history is missing a traded OHLC candle")
        expected_values = [Decimal(str(value)) for value in row[1:5]]
        actual_values = [Decimal(candidate[name]) for name in ("open", "high", "low", "close")]
        if expected_values != actual_values or int(candidate["trades"]) != trade_count:
            raise HistoryError("PostTrade/OHLC overlap mismatch")
        compared += 1
        timestamp = datetime.fromtimestamp(epoch, tz=timezone.utc)
        first = timestamp if first is None else min(first, timestamp)
        last = timestamp if last is None else max(last, timestamp)
    if compared < MIN_OVERLAP_CANDLES:
        raise HistoryError(
            f"PostTrade/OHLC overlap has only {compared} candles; {MIN_OVERLAP_CANDLES} required"
        )
    return {
        "STATUS": "VERIFIED",
        "CANDLES": compared,
        "FROM": _iso(first),
        "THROUGH": _iso(last),
    }


def refresh_target(
    store: HistoryGistStore,
    client: KrakenPublicHistoryClient,
    target: str,
    *,
    now: datetime | None = None,
    checkpoint_pages: int = 25,
) -> dict[str, Any]:
    if target not in TARGET_PAIRS:
        raise HistoryError(f"unsupported Kraken history target: {target}")
    pair = TARGET_PAIRS[target]
    reference = now or _utc_now()
    cutoff = _completed_cutoff(reference)
    snapshot = store.snapshot()
    manifest = load_manifest(store, snapshot)
    previous = manifest.get("TARGETS", {}).get(target)
    if isinstance(previous, Mapping):
        query_from = _parse_iso(previous.get("QUERY_FROM"), "history QUERY_FROM")
        candles = _load_candles(store, previous, snapshot)
        last_ts = _parse_iso(previous.get("LAST_TS"), "history LAST_TS")
        boundary_ids = set(previous.get("LAST_TRADE_IDS") or [])
    else:
        query_from = cutoff - timedelta(days=BOOTSTRAP_DAYS)
        candles = {}
        last_ts = query_from
        boundary_ids: set[str] = set()
    required_start = cutoff - timedelta(days=BOOTSTRAP_DAYS)
    if query_from > required_start:
        raise HistoryError("existing history starts too late for the strict bootstrap")

    _write_checkpoint(
        store,
        manifest,
        target,
        candles,
        status="BOOTSTRAPPING",
        pair=pair,
        query_from=query_from,
        cutoff=cutoff,
        last_ts=last_ts,
        last_trade_ids=boundary_ids,
    )

    page_number = 0
    stalled = 0
    try:
        while last_ts < cutoff:
            # PostTrade defines ``from_ts`` as exclusive. Python normalizes
            # Kraken's nanosecond cursor to microseconds, so subtracting another
            # microsecond reintroduced already-aggregated trades at page
            # boundaries. Reuse the normalized boundary and deduplicate every
            # trade that maps to it by ID.
            page_from = max(query_from, last_ts)
            page = client.post_trade_page(pair, from_ts=page_from, to_ts=cutoff)
            trades = page["trades"]
            if not trades:
                last_ts = cutoff
                break
            page_seen: set[str] = set()
            newest = last_ts
            newest_ids: set[str] = set()
            new_count = 0
            for trade in trades:
                if not isinstance(trade, Mapping):
                    raise HistoryError("Kraken PostTrade trade must be an object")
                trade_id = str(trade.get("trade_id", ""))
                trade_time = _parse_iso(trade.get("trade_ts"), "Kraken trade_ts")
                if trade_time < last_ts:
                    continue
                if trade_id in page_seen or (trade_time == last_ts and trade_id in boundary_ids):
                    continue
                page_seen.add(trade_id)
                if trade_time < query_from or trade_time >= cutoff:
                    continue
                _add_trade(candles, trade, pair)
                new_count += 1
                if trade_time > newest:
                    newest = trade_time
                    newest_ids = {trade_id}
                elif trade_time == newest:
                    newest_ids.add(trade_id)
            if newest <= last_ts and new_count == 0:
                stalled += 1
                if stalled >= 2:
                    raise HistoryError("Kraken PostTrade pagination made no progress")
                last_ts += timedelta(microseconds=1)
            else:
                stalled = 0
                if newest == last_ts:
                    boundary_ids.update(newest_ids)
                else:
                    last_ts = newest
                    boundary_ids = newest_ids
            page_number += 1
            if page_number % max(1, checkpoint_pages) == 0:
                _write_checkpoint(
                    store,
                    manifest,
                    target,
                    candles,
                    status="BOOTSTRAPPING",
                    pair=pair,
                    query_from=query_from,
                    cutoff=cutoff,
                    last_ts=last_ts,
                    last_trade_ids=boundary_ids,
                )
            if len(trades) < MAX_PAGE_SIZE:
                last_ts = cutoff
                break

        overlap = validate_ohlc_overlap(client, pair, candles, cutoff)
        return _write_checkpoint(
            store,
            manifest,
            target,
            candles,
            status="READY",
            pair=pair,
            query_from=query_from,
            cutoff=cutoff,
            last_ts=cutoff,
            last_trade_ids=boundary_ids,
            overlap=overlap,
        )
    except Exception as exc:
        message = str(exc).strip() or type(exc).__name__
        _write_checkpoint(
            store,
            manifest,
            target,
            candles,
            status="ERROR",
            pair=pair,
            query_from=query_from,
            cutoff=cutoff,
            last_ts=last_ts,
            last_trade_ids=boundary_ids,
            error=message[:500],
        )
        raise


def load_ready_history(
    target: str,
    *,
    store: HistoryGistStore | None = None,
) -> tuple[list[list[float]], dict[str, Any]]:
    selected_store = store or HistoryGistStore()
    snapshot = selected_store.snapshot()
    manifest = load_manifest(selected_store, snapshot)
    entry = manifest.get("TARGETS", {}).get(target)
    if not isinstance(entry, Mapping):
        raise HistoryError(f"{target} history is not bootstrapped")
    if entry.get("STATUS") != "READY":
        raise HistoryError(f"{target} history status is {entry.get('STATUS', 'UNKNOWN')}")
    if entry.get("OVERLAP", {}).get("STATUS") != "VERIFIED":
        raise HistoryError(f"{target} history overlap is not verified")
    query_from = _parse_iso(entry.get("QUERY_FROM"), "history QUERY_FROM")
    cutoff = _parse_iso(entry.get("CUTOFF"), "history CUTOFF")
    if cutoff - query_from < timedelta(days=60):
        raise HistoryError(f"{target} history has less than 60 days of source coverage")
    candles = _load_candles(selected_store, entry, snapshot)
    rows = [
        [
            epoch * 1000,
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
        ]
        for epoch, row in sorted(candles.items())
        if query_from.timestamp() <= epoch < cutoff.timestamp()
    ]
    history_hash = sha256(
        _canonical_json(entry.get("PARTITIONS", {})).encode("utf-8")
    ).hexdigest()
    summary = {
        "VERSION": HISTORY_VERSION,
        "STATUS": "READY",
        "PAIR": entry["PAIR"],
        "FROM": entry["QUERY_FROM"],
        "THROUGH": entry["CUTOFF"],
        "CANDLE_COUNT": entry["CANDLE_COUNT"],
        "NO_TRADE_INTERVALS": entry["NO_TRADE_INTERVALS"],
        "OVERLAP": dict(entry["OVERLAP"]),
        "HASH": history_hash,
    }
    return rows, summary


def refresh_all(
    targets: Iterable[str],
    *,
    store: HistoryGistStore | None = None,
    client: KrakenPublicHistoryClient | None = None,
    now: datetime | None = None,
) -> dict[str, dict[str, Any]]:
    selected_store = store or HistoryGistStore()
    selected_client = client or KrakenPublicHistoryClient()
    results: dict[str, dict[str, Any]] = {}
    failures: list[str] = []
    for target in targets:
        try:
            results[target] = refresh_target(
                selected_store, selected_client, target, now=now
            )
            print(f"{target} history READY through {results[target]['CUTOFF']}", flush=True)
        except Exception as exc:
            failures.append(target)
            print(f"{target} history ERROR ({type(exc).__name__})", flush=True)
    if failures:
        raise HistoryError("history refresh failed for: " + ", ".join(failures))
    return results


def _parse_targets(value: str) -> list[str]:
    if value.strip().lower() == "all":
        return list(TARGET_PAIRS)
    targets = []
    for item in value.split(","):
        target = item.strip().upper().replace("/", "_")
        if target not in TARGET_PAIRS:
            raise HistoryError(f"unsupported history target: {item}")
        if target not in targets:
            targets.append(target)
    if not targets:
        raise HistoryError("at least one history target is required")
    return targets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "refresh"))
    parser.add_argument("--targets", default="all")
    args = parser.parse_args(argv)
    refresh_all(_parse_targets(args.targets))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

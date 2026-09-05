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
import math
import os
import re
import time
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse

import requests

from dca_config import ConfigError, history_summary_hash, validate_history_summary


HISTORY_VERSION = 1
SCAN_VERSION = 2
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
    "DOGE_GBP": "DOGE/GBP",
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


def _timestamp_ns(value: Any, label: str) -> int:
    """Compare Kraken cursors without dropping their nanosecond precision."""
    if not isinstance(value, str):
        raise HistoryError(f"{label} must be a UTC timestamp")
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?Z", value)
    if match is None:
        raise HistoryError(f"{label} must be a canonical UTC timestamp")
    seconds = int(_parse_iso(match[1] + "Z", label).timestamp())
    return seconds * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def _just_before(value: datetime) -> str:
    """The nanosecond before a whole-second candle boundary."""
    return (value - timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%S") + ".999999999Z"


def _next_nanosecond(value: str) -> str:
    seconds, nanos = divmod(_timestamp_ns(value, "history cursor") + 1, 1_000_000_000)
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}Z"


def _validate_post_trade_page(
    result: Any, pair: str, from_ts: str, to_ts: str
) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise HistoryError("Kraken PostTrade response has no result")
    trades = result.get("trades")
    count = result.get("count")
    if not isinstance(trades, list):
        raise HistoryError("Kraken PostTrade trades must be an explicit array")
    if type(count) is not int or count != len(trades) or not 0 <= count <= MAX_PAGE_SIZE:
        raise HistoryError("Kraken PostTrade count does not match the complete page")
    cursor = result.get("last_ts")
    if not trades:
        if cursor != "":
            raise HistoryError("Kraken empty PostTrade page must have an empty last_ts")
        return {"trades": [], "last_ts": "", "count": 0}
    lower = _timestamp_ns(from_ts, "PostTrade from_ts")
    upper = _timestamp_ns(to_ts, "PostTrade to_ts")
    previous = lower
    identifiers = set()
    for trade in trades:
        if not isinstance(trade, Mapping):
            raise HistoryError("Kraken PostTrade trade must be an object")
        identifier = trade.get("trade_id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise HistoryError("Kraken PostTrade trade_id is missing or duplicated")
        identifiers.add(identifier)
        timestamp = _timestamp_ns(trade.get("trade_ts"), "Kraken trade_ts")
        if timestamp < lower or timestamp < previous or timestamp > upper:
            raise HistoryError("Kraken PostTrade timestamps are out of order or request range")
        previous = timestamp
        if trade.get("symbol") != pair:
            raise HistoryError("Kraken PostTrade returned a different currency pair")
        _decimal(trade.get("price"), "Kraken trade price")
        _decimal(trade.get("quantity"), "Kraken trade quantity")
    if _timestamp_ns(cursor, "PostTrade last_ts") != previous:
        raise HistoryError("Kraken PostTrade last_ts does not match its last trade")
    return {"trades": list(trades), "last_ts": cursor, "count": count}


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
                errors = payload.get("error")
                if not isinstance(errors, list) or errors:
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
        self, pair: str, *, from_ts: datetime | str, to_ts: datetime | str
    ) -> dict[str, Any]:
        payload = self._get(
            POST_TRADE_URL,
            {
                "symbol": pair,
                "from_ts": from_ts if isinstance(from_ts, str) else _iso(from_ts),
                "to_ts": to_ts if isinstance(to_ts, str) else _iso(to_ts),
                "count": MAX_PAGE_SIZE,
            },
        )
        return _validate_post_trade_page(
            payload.get("result"), pair,
            from_ts if isinstance(from_ts, str) else _iso(from_ts),
            to_ts if isinstance(to_ts, str) else _iso(to_ts),
        )

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


def _analysis_rows(
    candles: Mapping[int, Mapping[str, Any]],
    query_from: datetime,
    cutoff: datetime,
) -> tuple[list[list[float]], int]:
    """Return exact-cadence analysis rows without inventing market trades.

    Kraken PostTrade correctly omits intervals in which a thin market had no
    executions. Call only after validating complete PostTrade source coverage.
    Proven no-trade intervals have unchanged OHLC and zero volume through that
    exclusive cutoff. Leading gaps remain absent; no price precedes a real trade.
    These rows are analysis-only and must never price or size an order.
    """

    lower = int(query_from.timestamp())
    upper = int(cutoff.timestamp())
    selected = {
        epoch: row
        for epoch, row in candles.items()
        if lower <= epoch < upper
    }
    if not selected:
        return [], 0
    first = min(selected)
    rows: list[list[float]] = []
    previous_close: float | None = None
    carried = 0
    for epoch in range(first, upper, INTERVAL_SECONDS):
        row = selected.get(epoch)
        if row is None:
            if previous_close is None:  # Defensive; ``first`` is a real candle.
                continue
            rows.append(
                [epoch * 1000, previous_close, previous_close, previous_close, previous_close, 0.0]
            )
            carried += 1
            continue
        values = [
            float(row["open"]),
            float(row["high"]),
            float(row["low"]),
            float(row["close"]),
            float(row["volume"]),
        ]
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise HistoryError("real history contains unrepresentable analysis prices or volume")
        rows.append([epoch * 1000, *values])
        previous_close = values[3]
    return rows, carried


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
            if timestamp.timestamp() % INTERVAL_SECONDS:
                raise HistoryError("history candle is not aligned to 15 minutes")
            if epoch in candles:
                raise HistoryError("history contains a duplicate candle timestamp")
            for field in ("open", "high", "low", "close", "volume"):
                _decimal(row.get(field), f"history candle {field}")
            if type(row.get("trades")) is not int or row["trades"] < 1:
                raise HistoryError("history candle trade count must be positive")
            if row.get("traded") is not True:
                raise HistoryError("raw history partitions may contain only real trades")
            if not (Decimal(row["low"]) <= min(Decimal(row["open"]), Decimal(row["close"]))
                    <= max(Decimal(row["open"]), Decimal(row["close"])) <= Decimal(row["high"])):
                raise HistoryError("history candle OHLC range is inconsistent")
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
    last_ts: datetime | str,
    last_trade_ids: Iterable[str],
    overlap: Mapping[str, Any] | None = None,
    error: str | None = None,
    verified_at: datetime | None = None,
) -> dict[str, Any]:
    partition_contents, hashes = _serialize_partitions(target, candles)
    sorted_epochs = sorted(candles)
    expected = max(0, int((cutoff - query_from).total_seconds()) // INTERVAL_SECONDS)
    gaps = _gap_summary(candles, query_from, cutoff)
    entry = {
        "SCAN_VERSION": SCAN_VERSION,
        "STATUS": status,
        "SOURCE": "Kraken PostTrade",
        "PAIR": pair,
        "INTERVAL_MINUTES": INTERVAL_MINUTES,
        "QUERY_FROM": _iso(query_from),
        "CUTOFF": _iso(cutoff),
        "LAST_TS": last_ts if isinstance(last_ts, str) else _iso(last_ts),
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
    if status == "READY":
        if verified_at is None or entry["LAST_TS"] != _iso(cutoff):
            raise HistoryError("READY history requires completed coverage verification")
        entry["COVERAGE_VERSION"] = 2
        entry["COVERAGE_THROUGH"] = _iso(cutoff)
        entry["VERIFIED_AT"] = _iso(verified_at)
    # Partial progress is resumable only when it was produced by this stricter
    # scanner, with its exact cursor and preserved partition hashes bound too.
    entry["EVIDENCE_HASH"] = sha256(_canonical_json(entry).encode("utf-8")).hexdigest()
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
    if previous is not None and not isinstance(previous, Mapping):
        raise HistoryError("existing history checkpoint must be an object")
    if isinstance(previous, Mapping):
        scan_version = previous.get("SCAN_VERSION")
        if "SCAN_VERSION" not in previous:
            # Preserve reviewed legacy READY prefixes, not unproven partial
            # ingestion. Adoption is not a retrospective 65-day source rescan.
            overlap = previous.get("OVERLAP")
            if (previous.get("STATUS") != "READY"
                or not isinstance(overlap, Mapping)
                or overlap.get("STATUS") != "VERIFIED"
                or type(overlap.get("CANDLES")) is not int
                or overlap["CANDLES"] < MIN_OVERLAP_CANDLES
                or previous.get("LAST_TS") != previous.get("CUTOFF")):
                raise HistoryError("legacy history adoption requires READY, verified overlap, and a completed cutoff cursor")
        elif type(scan_version) is not int or scan_version != SCAN_VERSION:
            raise HistoryError("existing history has an unsupported scan version")
        if scan_version == SCAN_VERSION or previous.get("COVERAGE_VERSION") == 2:
            previous_evidence = {key: value for key, value in previous.items() if key != "EVIDENCE_HASH"}
            if previous.get("EVIDENCE_HASH") != sha256(_canonical_json(previous_evidence).encode("utf-8")).hexdigest():
                raise HistoryError("existing history coverage evidence hash mismatch")
        query_from = _parse_iso(previous.get("QUERY_FROM"), "history QUERY_FROM")
        candles = _load_candles(store, previous, snapshot)
        last_ts = previous.get("LAST_TS")
        _timestamp_ns(last_ts, "history LAST_TS")
        boundary_ids = set(previous.get("LAST_TRADE_IDS") or [])
        if previous.get("PAIR") != pair or previous.get("SOURCE") != "Kraken PostTrade":
            raise HistoryError("existing history has a different source or pair")
        if not _timestamp_ns(_iso(query_from), "QUERY_FROM") <= _timestamp_ns(last_ts, "LAST_TS") <= _timestamp_ns(_iso(cutoff), "CUTOFF"):
            raise HistoryError("existing history cursor is outside the requested coverage")
    else:
        query_from = cutoff - timedelta(days=BOOTSTRAP_DAYS)
        candles = {}
        last_ts = _iso(query_from)
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
    terminal_probe_from: str | None = None
    try:
        while _timestamp_ns(last_ts, "history cursor") < _timestamp_ns(_iso(cutoff), "cutoff"):
            # Kraken documents an exclusive from_ts, but the live endpoint
            # repeats exact-boundary trades. Preserve nanoseconds and IDs;
            # never skip a stalled full page or an unobserved timestamp group.
            cursor_ns = _timestamp_ns(last_ts, "history cursor")
            cursor_epoch = cursor_ns // 1_000_000_000
            unconsumed_boundary = (cursor_ns % (INTERVAL_SECONDS * 1_000_000_000) == 0
                                   and cursor_epoch not in candles)
            page_from = _just_before(_parse_iso(last_ts, "history cursor")) if unconsumed_boundary else last_ts
            if terminal_probe_from is not None:
                page_from = terminal_probe_from
                terminal_probe_from = None
            page_to = _just_before(cutoff)
            page = _validate_post_trade_page(
                client.post_trade_page(pair, from_ts=page_from, to_ts=page_to),
                pair, page_from, page_to,
            )
            trades = page["trades"]
            if not trades:
                last_ts = _iso(cutoff)
                break
            new_count = 0
            for trade in trades:
                trade_ns = _timestamp_ns(trade["trade_ts"], "trade_ts")
                # The one-nanosecond overlap before a completed coverage
                # boundary is already represented in the preserved partition.
                if trade_ns < cursor_ns:
                    continue
                # Legacy checkpoints stored microsecond cursors with the IDs
                # already aggregated at that boundary. Preserve those candles
                # while upgrading to exact nanosecond cursor continuation.
                if trade["trade_id"] in boundary_ids and _parse_iso(trade["trade_ts"], "trade_ts") == _parse_iso(last_ts, "last_ts"):
                    continue
                _add_trade(candles, trade, pair)
                new_count += 1
            newest_ns = _timestamp_ns(page["last_ts"], "PostTrade last_ts")
            if newest_ns <= cursor_ns and not new_count:
                if len(trades) >= MAX_PAGE_SIZE:
                    raise HistoryError("Kraken PostTrade pagination made no progress on a full page")
                # This validated short tail proves the entire remaining group
                # consists of known IDs. Only then probe one nanosecond later
                # for an explicit empty terminal response (never one microsecond).
                terminal_probe_from = _next_nanosecond(last_ts)
            else:
                newest_ids = {trade["trade_id"] for trade in trades if _timestamp_ns(trade["trade_ts"], "trade_ts") == newest_ns}
                boundary_ids = boundary_ids | newest_ids if newest_ns == cursor_ns else newest_ids
                last_ts = page["last_ts"]
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
            # A short page is not itself proof of exhaustion: request the tail
            # and require an explicit, schema-validated empty result.

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
            verified_at=max(reference, _utc_now()),
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
    now: datetime | None = None,
) -> tuple[list[list[float]], dict[str, Any]]:
    selected_store = store or HistoryGistStore()
    snapshot = selected_store.snapshot()
    manifest = load_manifest(selected_store, snapshot)
    entry = manifest.get("TARGETS", {}).get(target)
    if not isinstance(entry, Mapping):
        raise HistoryError(f"{target} history is not bootstrapped")
    if entry.get("STATUS") != "READY":
        raise HistoryError(f"{target} history status is {entry.get('STATUS', 'UNKNOWN')}")
    if (entry.get("COVERAGE_VERSION") != 2
        or type(entry.get("SCAN_VERSION")) is not int or entry["SCAN_VERSION"] != SCAN_VERSION
        or entry.get("SOURCE") != "Kraken PostTrade"
        or entry.get("PAIR") != TARGET_PAIRS.get(target)
        or entry.get("INTERVAL_MINUTES") != INTERVAL_MINUTES):
        raise HistoryError(f"{target} history requires a current verified coverage refresh")
    evidence = {key: value for key, value in entry.items() if key != "EVIDENCE_HASH"}
    if entry.get("EVIDENCE_HASH") != sha256(_canonical_json(evidence).encode("utf-8")).hexdigest():
        raise HistoryError(f"{target} history coverage evidence hash mismatch")
    if not isinstance(entry.get("OVERLAP"), Mapping) or entry["OVERLAP"].get("STATUS") != "VERIFIED":
        raise HistoryError(f"{target} history overlap is not verified")
    query_from = _parse_iso(entry.get("QUERY_FROM"), "history QUERY_FROM")
    cutoff = _parse_iso(entry.get("CUTOFF"), "history CUTOFF")
    if cutoff - query_from < timedelta(days=60):
        raise HistoryError(f"{target} history has less than 60 days of source coverage")
    candles = _load_candles(selected_store, entry, snapshot)
    epochs = sorted(candles)
    if not epochs or any(not int(query_from.timestamp()) <= epoch < int(cutoff.timestamp()) for epoch in epochs):
        raise HistoryError(f"{target} real candles lie outside source coverage")
    first = _iso(datetime.fromtimestamp(epochs[0], tz=timezone.utc))
    last = _iso(datetime.fromtimestamp(epochs[-1], tz=timezone.utc))
    gaps = _gap_summary(candles, query_from, cutoff)
    if (entry.get("LAST_TS") != entry.get("CUTOFF")
        or entry.get("COVERAGE_THROUGH") != entry.get("CUTOFF")
        or entry.get("CANDLE_COUNT") != len(candles)
        or entry.get("CANDLE_START") != first or entry.get("CANDLE_END") != last
        or entry.get("NO_TRADE_INTERVALS") != gaps["COUNT"]
        or entry.get("GAP_SUMMARY") != gaps
        or entry.get("EXPECTED_INTERVALS") != int((cutoff - query_from).total_seconds() // INTERVAL_SECONDS)):
        raise HistoryError(f"{target} history coverage does not match its real candles")
    rows, carried_intervals = _analysis_rows(candles, query_from, cutoff)
    history_hash = sha256(
        _canonical_json(entry.get("PARTITIONS", {})).encode("utf-8")
    ).hexdigest()
    summary = {
        "VERSION": 2,
        "STATUS": "READY",
        "PAIR": entry["PAIR"],
        "FROM": entry["QUERY_FROM"],
        "THROUGH": entry["CUTOFF"],
        "COVERAGE_THROUGH": entry["COVERAGE_THROUGH"],
        "VERIFIED_AT": entry["VERIFIED_AT"],
        "LAST_REAL_CANDLE_AT": last,
        "CANDLE_COUNT": entry["CANDLE_COUNT"],
        "NO_TRADE_INTERVALS": entry["NO_TRADE_INTERVALS"],
        "ANALYSIS_CANDLE_COUNT": len(rows),
        "CARRIED_NO_TRADE_INTERVALS": carried_intervals,
        "OVERLAP": dict(entry["OVERLAP"]),
        "PARTITIONS_HASH": history_hash,
    }
    summary["HASH"] = history_summary_hash(summary)
    try:
        validate_history_summary(target, summary, analyzed_at=now or _utc_now())
    except ConfigError as exc:
        raise HistoryError(str(exc)) from exc
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

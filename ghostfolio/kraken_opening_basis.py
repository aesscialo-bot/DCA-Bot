"""Build hash-bound, fail-closed Kraken pre-cutover performance cost evidence.

This artifact is deliberately separate from the holdings snapshot.  Holdings
prices are current market observations and must never be used as acquisition
cost.  A position receives a complete GBP performance-book basis only when
fully paginated Kraken trade and ledger evidence reconstructs its reviewed
opening quantity.  Deposits, transfers, rewards, sells, missing ledger links,
or an unproved historical USD/GBP route leave that position explicitly missing.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo

from github_contents import (
    GitHubContentsClient,
    configured_opening_basis_path,
    configured_opening_basis_source_path,
    configured_outbox_paths,
)
from ghostfolio.ghostfolio_sync import parse_events, parse_holdings_snapshot
from ghostfolio.kraken_holdings_snapshot import ensure_snapshot_safe_execution_state
from kraken_client import build_client_order_id, get_kraken_exchange


VERSION = 1
BASIS_TYPE = "performance_book_cost"
METHOD = "kraken-pre-cutover-weighted-average-v1"
CUTOVER_AT = "2026-08-06T04:21:00.000Z"
OPENING_MODEL = "holdings-minus-events-v1"
REVIEWED_OPENING_STATE_HASH = (
    "0535e6245f7ed5606e226a101b7bb3e858c111ee1bf3eb1f46776c5656ea8471"
)
REVIEWED_OPENING_REPOSITORY_COMMIT_SHA = (
    "b69734117ba55cf74724bc0a208dd941b971b62d"
)
FUNDING_LINKS_ENV = "DCA_OPENING_BASIS_FUNDING_LINKS_JSON"
HISTORY_FROM = "1970-01-01T00:00:00Z"
REQUIRED_PERMISSIONS = frozenset({"query-closed-trades", "query-ledger"})
OPENING_BASIS_FILE = "kraken_opening_basis_v1.json"
OPENING_BASIS_SOURCE_FILE = "kraken_opening_basis_source_v1.json"
SOURCE_EVIDENCE_TYPE = "kraken_opening_basis_source"
SOURCE_NORMALIZATION = "kraken-opening-basis-source-v1"
CONSUMER_MAX_BYTES = 1_000_000
SELECTED_TZ = ZoneInfo("Asia/Bangkok")

TARGETS = {
    "BTC_GBP": {"asset": "BTC", "places": 10, "tolerance": Decimal("0.0000000001")},
    "HYPE_USD": {"asset": "HYPE", "places": 8, "tolerance": Decimal("0.00000001")},
    "SOL_GBP": {"asset": "SOL", "places": 8, "tolerance": Decimal("0.00000001")},
}


class OpeningBasisError(RuntimeError):
    """Safe failure that never contains credentials or raw Kraken responses."""


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _strict_json(content: str, label: str):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise OpeningBasisError(f"{label} contains duplicate JSON keys")
            result[key] = value
        return result

    def invalid_constant(_value):
        raise OpeningBasisError(f"{label} contains a non-finite JSON number")

    try:
        return json.loads(
            content,
            object_pairs_hook=pairs,
            parse_constant=invalid_constant,
        )
    except (TypeError, json.JSONDecodeError) as error:
        raise OpeningBasisError(f"{label} is malformed") from error


def decimal_value(value, label: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise OpeningBasisError(f"{label} is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OpeningBasisError(f"{label} is invalid") from error
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise OpeningBasisError(f"{label} is invalid")
    return result


def signed_decimal(value, label: str) -> Decimal:
    if isinstance(value, bool):
        raise OpeningBasisError(f"{label} is invalid")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as error:
        raise OpeningBasisError(f"{label} is invalid") from error
    if not result.is_finite():
        raise OpeningBasisError(f"{label} is invalid")
    return result


def decimal_text(value) -> str:
    rendered = format(decimal_value(value, "decimal").normalize(), "f")
    return "0" if rendered in {"", "-0"} else rendered


def signed_decimal_text(value) -> str:
    rendered = format(signed_decimal(value, "decimal").normalize(), "f")
    return "0" if rendered in {"", "-0"} else rendered


def utc_timestamp(value: str, label: str) -> datetime:
    if (
        not isinstance(value, str)
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
            value,
        )
        is None
    ):
        raise OpeningBasisError(f"{label} must be a canonical UTC timestamp")
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise OpeningBasisError(f"{label} must be a canonical UTC timestamp") from error
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        raise OpeningBasisError(f"{label} must be a canonical UTC timestamp")
    return parsed


def epoch_decimal(value: datetime) -> Decimal:
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    delta = value.astimezone(timezone.utc) - epoch
    return (
        Decimal(delta.days * 86400 + delta.seconds)
        + Decimal(delta.microseconds) / Decimal(1_000_000)
    )


def timestamp_text(value) -> str:
    seconds = signed_decimal(value, "Kraken timestamp")
    if seconds < 0:
        raise OpeningBasisError("Kraken timestamp is invalid")
    micros = (seconds * Decimal(1_000_000)).quantize(
        Decimal(1), rounding=ROUND_HALF_UP
    )
    whole_seconds = int(micros // Decimal(1_000_000))
    remainder = int(micros - Decimal(whole_seconds) * Decimal(1_000_000))
    try:
        result = datetime.fromtimestamp(whole_seconds, tz=timezone.utc)
    except (OSError, OverflowError, ValueError) as error:
        raise OpeningBasisError("Kraken timestamp is invalid") from error
    return f"{result.isoformat(timespec='seconds').replace('+00:00', '')}.{remainder:06d}Z"


@dataclass(frozen=True)
class HistoryEvidence:
    records: tuple[dict, ...]
    complete: bool
    page_count: int
    canonical_hash: str
    pages: tuple[dict, ...] = ()

    def metadata(self) -> dict:
        return {
            "complete": self.complete,
            "page_count": self.page_count,
            "record_count": len(self.records),
            "canonical_hash": self.canonical_hash,
        }


@dataclass(frozen=True)
class AccessEvidence:
    query_from: str
    query_to: str
    key_valid_until: str

    def metadata(self) -> dict:
        return {
            "query_from": self.query_from,
            "query_to": self.query_to,
            "key_valid_until": self.key_valid_until,
        }


@dataclass(frozen=True)
class OpeningBinding:
    repository_commit_sha: str
    holdings_path: str
    holdings_blob_sha: str
    holdings_snapshot_hash: str
    events_path: str
    events_blob_sha: str
    events_content_sha256: str
    event_prefix_hash: str
    accepted_event_count: int
    opening_state_hash: str
    quantities: dict[str, Decimal]


def history_evidence(
    records: list[dict],
    *,
    page_count: int,
    complete: bool = True,
    pages: list[dict] | None = None,
) -> HistoryEvidence:
    if type(page_count) is not int or page_count < 1:
        raise OpeningBasisError("Kraken history page count is invalid")
    if any(not isinstance(row, dict) or not isinstance(row.get("id"), str) for row in records):
        raise OpeningBasisError("Kraken history record is invalid")
    if len({row["id"] for row in records}) != len(records):
        raise OpeningBasisError("Kraken history contains duplicate record identifiers")
    ordered = tuple(sorted(records, key=lambda row: row["id"]))
    page_rows = tuple(pages or [])
    if page_rows and len(page_rows) != page_count:
        raise OpeningBasisError("Kraken page manifest count is invalid")
    return HistoryEvidence(
        records=ordered,
        complete=complete,
        page_count=page_count,
        canonical_hash=canonical_hash(list(ordered)),
        pages=page_rows,
    )


def _hash_artifact(value: dict) -> dict:
    result = dict(value)
    result["canonical_hash"] = canonical_hash(result)
    return result


def _api_result(response, label: str) -> dict:
    if not isinstance(response, dict):
        raise OpeningBasisError(f"Kraken {label} response is malformed")
    errors = response.get("error")
    if errors not in (None, []):
        raise OpeningBasisError(f"Kraken {label} request failed")
    result = response.get("result", response)
    if not isinstance(result, dict):
        raise OpeningBasisError(f"Kraken {label} response is malformed")
    return result


def _integer_setting(value, label: str) -> int:
    if type(value) is int:
        parsed = value
    elif isinstance(value, str) and re.fullmatch(r"(?:0|[1-9]\d*)", value):
        try:
            parsed = int(value)
        except ValueError as error:
            raise OpeningBasisError(f"Kraken {label} is invalid") from error
    else:
        raise OpeningBasisError(f"Kraken {label} is invalid")
    if parsed < 0:
        raise OpeningBasisError(f"Kraken {label} is invalid")
    return parsed


def _api_integer_setting(result: dict, key: str, label: str) -> int:
    """Map Kraken's present-null "not set" value to zero, but reject omission."""
    if key not in result:
        raise OpeningBasisError(f"Kraken {label} is missing")
    value = result[key]
    return 0 if value is None else _integer_setting(value, label)


def _api_history_bounds(result: dict) -> tuple[int, int, int]:
    """Normalize Kraken's bounds only when their group is complete or absent."""
    fields = (
        ("queryFrom", "API-key queryFrom"),
        ("queryTo", "API-key queryTo"),
        ("validUntil", "API-key validUntil"),
    )
    present = [key in result for key, _label in fields]
    if not any(present):
        return 0, 0, 0
    if not all(present):
        raise OpeningBasisError("Kraken API-key history bounds are incomplete")
    return tuple(
        _api_integer_setting(result, key, label) for key, label in fields
    )


def _required_identifier(value, label: str) -> str:
    if not isinstance(value, str) or _IDENTIFIER.fullmatch(value) is None:
        raise OpeningBasisError(f"Kraken {label} is invalid")
    return value


def _normalize_trade(record_id: str, row: dict) -> dict:
    if not isinstance(row, dict):
        raise OpeningBasisError("Kraken trade history is malformed")
    linked = row.get("ledgers")
    if not isinstance(linked, list) or not linked:
        raise OpeningBasisError("Kraken trade is missing related ledger identifiers")
    ledger_ids = sorted({_required_identifier(item, "ledger identifier") for item in linked})
    if len(ledger_ids) != len(linked):
        raise OpeningBasisError("Kraken trade contains duplicate ledger identifiers")
    pair = _pair_key(row.get("pair"))
    side = str(row.get("type") or "").lower()
    if not pair or side not in {"buy", "sell"}:
        raise OpeningBasisError("Kraken trade identity is invalid")
    return {
        "id": _required_identifier(record_id, "trade identifier"),
        "ordertxid": _required_identifier(row.get("ordertxid"), "order identifier"),
        "pair": pair,
        "time": decimal_text(row.get("time")),
        "type": side,
        "vol": decimal_text(decimal_value(row.get("vol"), "Kraken trade volume", positive=True)),
        "cost": decimal_text(decimal_value(row.get("cost"), "Kraken trade cost", positive=True)),
        "ledgers": ledger_ids,
    }


def _optional_text(value, label: str) -> str | None:
    if value is None or value == "":
        return None
    if not isinstance(value, str) or len(value) > 256 or any(ord(item) < 32 for item in value):
        raise OpeningBasisError(f"Kraken {label} is invalid")
    return value


def _normalize_ledger(record_id: str, row: dict) -> dict:
    if not isinstance(row, dict):
        raise OpeningBasisError("Kraken ledger history is malformed")
    ledger_type = str(row.get("type") or "").strip().lower()
    subtype = row.get("subtype")
    if not ledger_type:
        raise OpeningBasisError("Kraken ledger type is invalid")
    if subtype in (None, ""):
        normalized_subtype = None
    elif isinstance(subtype, str) and len(subtype) <= 128:
        normalized_subtype = subtype.lower()
    else:
        raise OpeningBasisError("Kraken ledger subtype is invalid")
    asset = _asset(row.get("asset"))
    if not asset:
        raise OpeningBasisError("Kraken ledger asset is invalid")
    return {
        "id": _required_identifier(record_id, "ledger identifier"),
        "refid": _optional_text(row.get("refid"), "ledger reference"),
        "time": decimal_text(row.get("time")),
        "type": ledger_type,
        "subtype": normalized_subtype,
        "asset": asset,
        "amount": signed_decimal_text(row.get("amount")),
        "fee": decimal_text(row.get("fee", 0)),
        "balance": decimal_text(row.get("balance")),
    }


def _normalize_order(record_id: str, row: dict) -> dict:
    if not isinstance(row, dict):
        raise OpeningBasisError("Kraken order history is malformed")
    status = str(row.get("status") or "").strip().lower()
    if not status:
        raise OpeningBasisError("Kraken order status is invalid")
    client_order_id = row.get("cl_ord_id")
    if client_order_id in (None, ""):
        client_order_id = row.get("clientOrderId")
    if client_order_id in (None, ""):
        info = row.get("info")
        client_order_id = info.get("cl_ord_id") if isinstance(info, dict) else None
    client_order_id = _optional_text(client_order_id, "client order identifier")
    return {
        "id": _required_identifier(record_id, "order identifier"),
        "cl_ord_id": client_order_id,
        "status": status,
    }


def ensure_history_permissions(exchange, *, cutover_at: str, generated_at: str) -> AccessEvidence:
    try:
        result = _api_result(exchange.privatePostGetApiKeyInfo({}), "API-key info")
    except OpeningBasisError:
        raise
    except Exception as error:
        raise OpeningBasisError("Kraken API-key permission check failed") from error
    permissions = result.get("permissions")
    if not isinstance(permissions, list) or any(not isinstance(item, str) for item in permissions):
        raise OpeningBasisError("Kraken API-key permissions are unavailable")
    missing = REQUIRED_PERMISSIONS.difference(permissions)
    if missing:
        raise OpeningBasisError(
            "Kraken API key lacks required read-only history permissions: "
            + ", ".join(sorted(missing))
        )

    cutover_seconds = epoch_decimal(utc_timestamp(cutover_at, "cutover"))
    generated_seconds = epoch_decimal(utc_timestamp(generated_at, "generated_at"))
    query_from, query_to, valid_until = _api_history_bounds(result)
    if query_from != 0:
        raise OpeningBasisError("Kraken API key cannot prove unrestricted history start")
    if query_to != 0 and Decimal(query_to) < cutover_seconds:
        raise OpeningBasisError("Kraken API key history ends before the cutover")
    if valid_until != 0 and Decimal(valid_until) < generated_seconds:
        raise OpeningBasisError("Kraken API key expired before artifact generation")
    return AccessEvidence(str(query_from), str(query_to), str(valid_until))


def _fetch_pages(exchange, *, method_name: str, container: str, end: int, exact_end: Decimal, page_size: int) -> HistoryEvidence:
    method = getattr(exchange, method_name, None)
    if not callable(method):
        raise OpeningBasisError("Kraken history endpoint is unavailable")
    offset = 0
    expected_count = None
    pages = 0
    records = []
    seen = set()
    page_manifest = []
    while expected_count is None or offset < expected_count:
        params = {"start": 0, "end": end, "ofs": offset}
        if container == "trades":
            params.update({
                "limit": page_size,
                "type": "all",
                "trades": False,
                "without_count": False,
                "consolidate_taker": False,
                "ledgers": True,
            })
        else:
            params.update({"type": "all", "without_count": False})
        try:
            result = _api_result(method(params), container)
        except OpeningBasisError:
            raise
        except Exception as error:
            raise OpeningBasisError(f"Kraken {container} request failed") from error
        count = result.get("count")
        rows = result.get(container)
        if type(count) is not int or count < 0 or not isinstance(rows, dict):
            raise OpeningBasisError(f"Kraken {container} pagination metadata is invalid")
        if expected_count is None:
            expected_count = count
        elif count != expected_count:
            raise OpeningBasisError(f"Kraken {container} count changed during pagination")
        pages += 1
        if expected_count and not rows:
            raise OpeningBasisError(f"Kraken {container} pagination ended early")
        page_records = []
        for record_id, row in rows.items():
            if not isinstance(record_id, str) or record_id in seen or not isinstance(row, dict):
                raise OpeningBasisError(f"Kraken {container} history is malformed")
            row_time = signed_decimal(row.get("time"), f"Kraken {container} timestamp")
            if row_time < 0 or row_time > exact_end:
                raise OpeningBasisError(f"Kraken {container} history exceeds the cutover")
            seen.add(record_id)
            normalized = (
                _normalize_trade(record_id, row)
                if container == "trades"
                else _normalize_ledger(record_id, row)
            )
            records.append(normalized)
            page_records.append(normalized)
        ordered_page = sorted(page_records, key=lambda row: row["id"])
        page_manifest.append({
            "page": pages,
            "offset": offset,
            "returned_count": len(page_records),
            "response_count": count,
            "record_ids": [row["id"] for row in ordered_page],
            "canonical_hash": canonical_hash(ordered_page),
        })
        offset += len(rows)
        if offset > expected_count:
            raise OpeningBasisError(f"Kraken {container} pagination exceeded its count")
    if len(records) != expected_count:
        raise OpeningBasisError(f"Kraken {container} history is incomplete")
    return history_evidence(records, page_count=pages, pages=page_manifest)


def _fetch_orders(exchange, trades: HistoryEvidence) -> HistoryEvidence:
    method = getattr(exchange, "privatePostQueryOrders", None)
    if not callable(method):
        raise OpeningBasisError("Kraken order-history endpoint is unavailable")
    order_ids = sorted({
        _required_identifier(row.get("ordertxid"), "order identifier")
        for row in trades.records
        if _pair_key(row.get("pair")) in set(_PAIR_ALIASES) | set(_FUNDING_PAIRS)
    })
    records = []
    pages = 0
    page_manifest = []
    for index in range(0, len(order_ids), 50):
        batch = order_ids[index:index + 50]
        try:
            result = _api_result(method({"txid": ",".join(batch), "trades": False}), "orders")
        except OpeningBasisError:
            raise
        except Exception as error:
            raise OpeningBasisError("Kraken orders request failed") from error
        pages += 1
        if set(result) != set(batch) or any(not isinstance(result[item], dict) for item in batch):
            raise OpeningBasisError("Kraken order history does not cover every trade order")
        page_rows = [_normalize_order(item, result[item]) for item in batch]
        records.extend(page_rows)
        page_manifest.append({
            "page": pages,
            "offset": index,
            "returned_count": len(page_rows),
            "response_count": len(order_ids),
            "record_ids": [row["id"] for row in page_rows],
            "canonical_hash": canonical_hash(page_rows),
        })
    if not page_manifest:
        page_manifest.append({
            "page": 1, "offset": 0, "returned_count": 0,
            "response_count": 0, "record_ids": [],
            "canonical_hash": canonical_hash([]),
        })
    return history_evidence(records, page_count=max(1, pages), pages=page_manifest)


def fetch_kraken_history(
    exchange,
    *,
    cutover_at: str = CUTOVER_AT,
    generated_at: str,
) -> tuple[AccessEvidence, HistoryEvidence, HistoryEvidence, HistoryEvidence]:
    cutover = utc_timestamp(cutover_at, "cutover")
    access = ensure_history_permissions(
        exchange, cutover_at=cutover_at, generated_at=generated_at
    )
    exact_end = epoch_decimal(cutover)
    end = int(exact_end)
    trades = _fetch_pages(
        exchange,
        method_name="privatePostTradesHistory",
        container="trades",
        end=end,
        exact_end=exact_end,
        page_size=100,
    )
    ledgers = _fetch_pages(
        exchange,
        method_name="privatePostLedgers",
        container="ledger",
        end=end,
        exact_end=exact_end,
        page_size=50,
    )
    orders = _fetch_orders(exchange, trades)
    return access, trades, ledgers, orders


def build_source_artifact(
    access: AccessEvidence,
    trades: HistoryEvidence,
    ledgers: HistoryEvidence,
    orders: HistoryEvidence,
    *,
    generated_at: str,
    producer_commit: str,
) -> dict:
    generated = utc_timestamp(generated_at, "generated_at")
    if generated < utc_timestamp(CUTOVER_AT, "cutover"):
        raise OpeningBasisError("generated_at predates cutover")
    if not isinstance(producer_commit, str) or re.fullmatch(r"[0-9a-f]{40}", producer_commit) is None:
        raise OpeningBasisError("producer commit must be a full lowercase Git SHA")
    _validate_access(access, generated_at=generated_at)
    for name, evidence, normalizer in (
        ("trades", trades, _normalize_trade),
        ("ledgers", ledgers, _normalize_ledger),
        ("orders", orders, _normalize_order),
    ):
        _validate_source_history(name, evidence, normalizer)
    required_orders = {
        row["ordertxid"] for row in trades.records
        if row["pair"] in set(_PAIR_ALIASES) | set(_FUNDING_PAIRS)
    }
    if required_orders != {row["id"] for row in orders.records}:
        raise OpeningBasisError("Kraken order evidence does not exactly cover relevant trades")
    end = str(int(epoch_decimal(utc_timestamp(CUTOVER_AT, "cutover"))))
    source = {
        "version": 1,
        "evidence_type": SOURCE_EVIDENCE_TYPE,
        "normalization": SOURCE_NORMALIZATION,
        "generated_at": generated_at,
        "cutover_at": CUTOVER_AT,
        "producer_commit": producer_commit,
        "access": {
            "permissions": sorted(REQUIRED_PERMISSIONS),
            **access.metadata(),
        },
        "requests": {
            "trades": {
                "endpoint": "TradesHistory", "start": "0", "end": end,
                "page_size": 100, "type": "all", "trades": False,
                "without_count": False, "consolidate_taker": False,
                "ledgers": True,
            },
            "ledgers": {
                "endpoint": "Ledgers", "start": "0", "end": end,
                "page_size": 50, "type": "all", "without_count": False,
            },
            "orders": {
                "endpoint": "QueryOrders", "batch_size": 50, "trades": False,
            },
        },
        "pagination": {
            "trades": list(trades.pages),
            "ledgers": list(ledgers.pages),
            "orders": list(orders.pages),
        },
        "trades": list(trades.records),
        "ledgers": list(ledgers.records),
        "orders": list(orders.records),
    }
    return _hash_artifact(source)


def _validate_access(access: AccessEvidence, *, generated_at: str) -> None:
    query_from = _integer_setting(access.query_from, "source query_from")
    query_to = _integer_setting(access.query_to, "source query_to")
    valid_until = _integer_setting(access.key_valid_until, "source key_valid_until")
    cutover_seconds = epoch_decimal(utc_timestamp(CUTOVER_AT, "cutover"))
    generated_seconds = epoch_decimal(
        utc_timestamp(generated_at, "source generated_at")
    )
    if query_from != 0:
        raise OpeningBasisError("opening-basis source history start is restricted")
    if query_to != 0 and Decimal(query_to) < cutover_seconds:
        raise OpeningBasisError("opening-basis source history ends before cutover")
    if valid_until != 0 and Decimal(valid_until) < generated_seconds:
        raise OpeningBasisError("opening-basis source key expired before generation")


def _validate_source_history(name: str, evidence: HistoryEvidence, normalizer) -> None:
    if not evidence.complete or not evidence.pages:
        raise OpeningBasisError(f"Kraken {name} source history is incomplete")
    if len(evidence.pages) != evidence.page_count:
        raise OpeningBasisError(f"Kraken {name} page manifest count is invalid")
    if evidence.canonical_hash != canonical_hash(list(evidence.records)):
        raise OpeningBasisError(f"Kraken {name} source hash is invalid")
    record_by_id = {row["id"]: row for row in evidence.records}
    if len(record_by_id) != len(evidence.records):
        raise OpeningBasisError(f"Kraken {name} source identifiers are duplicated")
    for row in evidence.records:
        if set(row) != set(normalizer(row["id"], row)) or row != normalizer(row["id"], row):
            raise OpeningBasisError(f"Kraken {name} source row is not normalized")
        if name != "orders" and signed_decimal(
            row["time"], f"Kraken {name} time"
        ) > epoch_decimal(utc_timestamp(CUTOVER_AT, "cutover")):
            raise OpeningBasisError(f"Kraken {name} source exceeds cutover")

    next_offset = 0
    flattened = []
    total = len(evidence.records)
    for index, page in enumerate(evidence.pages, start=1):
        if not isinstance(page, dict) or set(page) != {
            "page", "offset", "returned_count", "response_count", "record_ids", "canonical_hash"
        }:
            raise OpeningBasisError(f"Kraken {name} page manifest is invalid")
        ids = page["record_ids"]
        if (
            type(page["page"]) is not int
            or page["page"] != index
            or type(page["offset"]) is not int
            or page["offset"] != next_offset
            or type(page["returned_count"]) is not int
            or page["returned_count"] < 0
            or type(page["response_count"]) is not int
            or page["response_count"] != total
            or not isinstance(ids, list)
            or ids != sorted(ids)
            or len(ids) != page["returned_count"]
            or len(set(ids)) != len(ids)
            or any(item not in record_by_id for item in ids)
            or page["returned_count"] > {
                "trades": 100, "ledgers": 50, "orders": 50
            }[name]
        ):
            raise OpeningBasisError(f"Kraken {name} page manifest is invalid")
        page_rows = [record_by_id[item] for item in ids]
        if page["canonical_hash"] != canonical_hash(page_rows):
            raise OpeningBasisError(f"Kraken {name} page hash is invalid")
        flattened.extend(ids)
        next_offset += len(ids)
    if sorted(flattened) != sorted(record_by_id) or next_offset != total:
        raise OpeningBasisError(f"Kraken {name} page coverage is invalid")


def parse_source_artifact(content: str) -> tuple[dict, AccessEvidence, HistoryEvidence, HistoryEvidence, HistoryEvidence]:
    try:
        source = _strict_json(content, "opening-basis source evidence")
    except OpeningBasisError:
        raise
    required = {
        "version", "evidence_type", "normalization", "generated_at", "cutover_at",
        "producer_commit", "access", "requests", "pagination", "trades", "ledgers",
        "orders", "canonical_hash",
    }
    if not isinstance(source, dict) or set(source) != required:
        raise OpeningBasisError("opening-basis source evidence schema is invalid")
    unhashed = {key: value for key, value in source.items() if key != "canonical_hash"}
    if (
        source.get("version") != 1
        or source.get("evidence_type") != SOURCE_EVIDENCE_TYPE
        or source.get("normalization") != SOURCE_NORMALIZATION
        or source.get("cutover_at") != CUTOVER_AT
        or source.get("canonical_hash") != canonical_hash(unhashed)
        or re.fullmatch(r"[0-9a-f]{40}", str(source.get("producer_commit"))) is None
    ):
        raise OpeningBasisError("opening-basis source evidence identity is invalid")
    utc_timestamp(source["generated_at"], "source generated_at")
    access_value = source.get("access")
    if not isinstance(access_value, dict) or set(access_value) != {
        "permissions", "query_from", "query_to", "key_valid_until"
    } or access_value.get("permissions") != sorted(REQUIRED_PERMISSIONS):
        raise OpeningBasisError("opening-basis source access evidence is invalid")
    access = AccessEvidence(
        str(_integer_setting(access_value["query_from"], "source query_from")),
        str(_integer_setting(access_value["query_to"], "source query_to")),
        str(_integer_setting(access_value["key_valid_until"], "source key_valid_until")),
    )
    _validate_access(access, generated_at=source["generated_at"])
    expected_end = str(int(epoch_decimal(utc_timestamp(CUTOVER_AT, "cutover"))))
    if source.get("requests") != {
        "trades": {
            "endpoint": "TradesHistory", "start": "0", "end": expected_end,
            "page_size": 100, "type": "all", "trades": False,
            "without_count": False, "consolidate_taker": False,
            "ledgers": True,
        },
        "ledgers": {
            "endpoint": "Ledgers", "start": "0", "end": expected_end,
            "page_size": 50, "type": "all", "without_count": False,
        },
        "orders": {"endpoint": "QueryOrders", "batch_size": 50, "trades": False},
    }:
        raise OpeningBasisError("opening-basis source request contract is invalid")
    pagination = source.get("pagination")
    if not isinstance(pagination, dict) or set(pagination) != {"trades", "ledgers", "orders"}:
        raise OpeningBasisError("opening-basis source pagination is invalid")
    histories = []
    for name, normalizer in (
        ("trades", _normalize_trade),
        ("ledgers", _normalize_ledger),
        ("orders", _normalize_order),
    ):
        records = source.get(name)
        pages = pagination.get(name)
        if not isinstance(records, list) or not isinstance(pages, list) or not pages:
            raise OpeningBasisError("opening-basis source history is invalid")
        evidence = history_evidence(records, page_count=len(pages), pages=pages)
        _validate_source_history(name, evidence, normalizer)
        histories.append(evidence)
    relevant_orders = {
        row["ordertxid"] for row in histories[0].records
        if row["pair"] in set(_PAIR_ALIASES) | set(_FUNDING_PAIRS)
    }
    if relevant_orders != {row["id"] for row in histories[2].records}:
        raise OpeningBasisError("opening-basis order evidence coverage is invalid")
    return source, access, histories[0], histories[1], histories[2]


def _event_rows(content: str) -> list[tuple[int, int, dict]]:
    parsed = parse_events(content)
    rows = []
    ordinal = 0
    parsed_index = 0
    for line_number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        ordinal += 1
        rows.append((ordinal, line_number, parsed[parsed_index]))
        parsed_index += 1
    return rows


def derive_opening_binding(
    holdings_content: str,
    events_content: str,
    *,
    repository_commit_sha: str,
    holdings_path: str,
    holdings_blob_sha: str,
    events_path: str,
    events_blob_sha: str,
) -> OpeningBinding:
    if re.fullmatch(r"[0-9a-f]{40}", repository_commit_sha) is None:
        raise OpeningBasisError("reviewed opening repository commit is invalid")
    if (
        not isinstance(holdings_path, str)
        or not holdings_path
        or holdings_path.startswith("/")
        or holdings_path.endswith("/")
        or "\\" in holdings_path
        or any(item in {"", ".", ".."} for item in holdings_path.split("/"))
    ):
        raise OpeningBasisError("reviewed opening holdings path is invalid")
    if (
        not isinstance(events_path, str)
        or not events_path
        or events_path.startswith("/")
        or events_path.endswith("/")
        or "\\" in events_path
        or any(item in {"", ".", ".."} for item in events_path.split("/"))
        or events_path == holdings_path
    ):
        raise OpeningBasisError("reviewed opening events path is invalid")
    for value, label in (
        (holdings_blob_sha, "holdings"),
        (events_blob_sha, "events"),
    ):
        if not isinstance(value, str) or re.fullmatch(
            r"(?:[0-9a-f]{40}|[0-9a-f]{64})", value
        ) is None:
            raise OpeningBasisError(f"reviewed opening {label} blob is invalid")
    snapshot = parse_holdings_snapshot(holdings_content)
    if snapshot is None:
        raise OpeningBasisError("Kraken holdings snapshot is missing")
    cutover = utc_timestamp(CUTOVER_AT, "cutover")
    accepted = [
        row for row in _event_rows(events_content)
        if utc_timestamp(row[2]["occurred_at"], "Portfolio event") > cutover
    ]
    purchased = {target: Decimal(0) for target in TARGETS}
    for _ordinal, _line, event in accepted:
        target = event.get("target")
        if target not in purchased:
            raise OpeningBasisError("Portfolio event has an unsupported target")
        purchased[target] += decimal_value(
            event.get("crypto_quantity"), "Portfolio event quantity", positive=True
        )
    quantities = {}
    state_positions = []
    for target, contract in TARGETS.items():
        holding = snapshot["holdings"][target]
        opening = decimal_value(holding["quantity"], f"{target} holding") - purchased[target]
        tolerance = contract["tolerance"]
        if opening < -tolerance:
            raise OpeningBasisError(f"{target} canonical purchases exceed holdings")
        opening = max(opening, Decimal(0)).quantize(
            Decimal(1).scaleb(-contract["places"]), rounding=ROUND_HALF_UP
        )
        quantities[target] = opening
        state_positions.append({"asset": contract["asset"], "quantity": decimal_text(opening)})
    event_prefix = "\n".join(
        f"{ordinal}:{line_number}:{event['canonical_hash']}"
        for ordinal, line_number, event in accepted
    )
    state_hash = canonical_hash({
        "version": 1,
        "model": OPENING_MODEL,
        "cutoverAt": CUTOVER_AT,
        "positions": state_positions,
    })
    if state_hash != REVIEWED_OPENING_STATE_HASH:
        raise OpeningBasisError("derived opening state does not match the reviewed commitment")
    return OpeningBinding(
        repository_commit_sha=repository_commit_sha,
        holdings_path=holdings_path,
        holdings_blob_sha=holdings_blob_sha,
        holdings_snapshot_hash=snapshot["canonical_hash"],
        events_path=events_path,
        events_blob_sha=events_blob_sha,
        events_content_sha256=hashlib.sha256(events_content.encode("utf-8")).hexdigest(),
        event_prefix_hash=hashlib.sha256(event_prefix.encode("utf-8")).hexdigest(),
        accepted_event_count=len(accepted),
        opening_state_hash=state_hash,
        quantities=quantities,
    )


_PAIR_ALIASES = {
    "BTCGBP": ("BTC_GBP", "GBP"), "XBTGBP": ("BTC_GBP", "GBP"),
    "XXBTZGBP": ("BTC_GBP", "GBP"),
    "BTCUSD": ("BTC_GBP", "USD"), "XBTUSD": ("BTC_GBP", "USD"),
    "XXBTZUSD": ("BTC_GBP", "USD"),
    "HYPEUSD": ("HYPE_USD", "USD"), "HYPEZUSD": ("HYPE_USD", "USD"),
    "SOLGBP": ("SOL_GBP", "GBP"), "SOLZGBP": ("SOL_GBP", "GBP"),
    "SOLUSD": ("SOL_GBP", "USD"), "SOLZUSD": ("SOL_GBP", "USD"),
}
_FUNDING_PAIRS = frozenset({"GBPUSD", "ZGBPZUSD"})
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def _pair_key(value) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def _asset(value) -> str | None:
    raw = str(value or "").upper().split(".", 1)[0]
    return {"XXBT": "BTC", "XBT": "BTC", "ZGBP": "GBP", "ZUSD": "USD"}.get(raw, raw or None)


def _groups(trades: HistoryEvidence) -> dict[tuple[str, str, str], list[dict]]:
    result = {}
    for trade in trades.records:
        pair = _pair_key(trade.get("pair"))
        if pair not in _PAIR_ALIASES and pair not in _FUNDING_PAIRS:
            continue
        order_id = trade.get("ordertxid")
        side = str(trade.get("type") or "").lower()
        if not isinstance(order_id, str) or _IDENTIFIER.fullmatch(order_id) is None:
            raise OpeningBasisError("Kraken trade has an invalid order identifier")
        if side not in {"buy", "sell"}:
            raise OpeningBasisError("Kraken trade has an invalid side")
        result.setdefault((order_id, pair, side), []).append(trade)
    return result


def _validate_global_trade_ownership(
    trades: HistoryEvidence, ledgers: HistoryEvidence
) -> None:
    """Reject cross-trade ledger reuse and mixed-identity order evidence."""
    ledger_owner = {}
    ledger_by_id = {row["id"]: row for row in ledgers.records}
    order_identity = {}
    relevant_pairs = set(_PAIR_ALIASES) | set(_FUNDING_PAIRS)
    for trade in trades.records:
        trade_id = trade["id"]
        for ledger_id in trade.get("ledgers", []):
            previous = ledger_owner.setdefault(ledger_id, trade_id)
            if previous != trade_id:
                raise OpeningBasisError(
                    "Kraken ledger identifier is referenced by multiple trades"
                )
            ledger = ledger_by_id.get(ledger_id)
            if not isinstance(ledger, dict) or ledger.get("refid") != trade_id:
                raise OpeningBasisError(
                    "Kraken ledger reference does not match its owning trade"
                )
        pair = _pair_key(trade.get("pair"))
        if pair not in relevant_pairs:
            continue
        order_id = trade.get("ordertxid")
        identity = (pair, str(trade.get("type") or "").lower())
        previous_identity = order_identity.setdefault(order_id, identity)
        if previous_identity != identity:
            raise OpeningBasisError(
                "Kraken order contains multiple pair or side identities"
            )


def _ledger_rows(group: list[dict], ledger_by_id: dict[str, dict]) -> tuple[list[str], list[dict]]:
    ids = []
    for trade in group:
        linked = trade.get("ledgers")
        if not isinstance(linked, list) or not linked:
            raise OpeningBasisError("Kraken trade is missing related ledger identifiers")
        for ledger_id in linked:
            if not isinstance(ledger_id, str) or _IDENTIFIER.fullmatch(ledger_id) is None:
                raise OpeningBasisError("Kraken trade has an invalid ledger identifier")
            if ledger_id not in ids:
                ids.append(ledger_id)
            row = ledger_by_id.get(ledger_id)
            if not isinstance(row, dict) or row.get("refid") != trade["id"]:
                raise OpeningBasisError(
                    "Kraken ledger reference does not match its trade"
                )
    try:
        rows = [ledger_by_id[ledger_id] for ledger_id in ids]
    except KeyError as error:
        raise OpeningBasisError("Kraken trade references missing ledger evidence") from error
    if any(
        str(row.get("type") or "").lower() != "trade"
        or row.get("subtype") is not None
        for row in rows
    ):
        raise OpeningBasisError("Kraken trade references non-trade ledger evidence")
    return sorted(ids), rows


def _spot_leg(group, target: str, quote: str, ledger_by_id) -> dict:
    asset = TARGETS[target]["asset"]
    ledger_ids, rows = _ledger_rows(group, ledger_by_id)
    base_rows = [row for row in rows if _asset(row.get("asset")) == asset]
    quote_rows = [row for row in rows if _asset(row.get("asset")) == quote]
    if not base_rows or not quote_rows or len(base_rows) + len(quote_rows) != len(rows):
        raise OpeningBasisError("Kraken acquisition ledger currencies do not match its pair")
    gross = sum((decimal_value(row.get("vol"), "Kraken trade volume", positive=True) for row in group), Decimal(0))
    quote_cost = sum((decimal_value(row.get("cost"), "Kraken trade cost", positive=True) for row in group), Decimal(0))
    base_amount = sum((signed_decimal(row.get("amount"), "Kraken base ledger amount") for row in base_rows), Decimal(0))
    base_fee = sum((decimal_value(row.get("fee", 0), "Kraken base ledger fee") for row in base_rows), Decimal(0))
    if base_amount <= 0 or base_fee >= base_amount:
        raise OpeningBasisError("Kraken acquisition base ledger is inconsistent")
    tolerance = TARGETS[target]["tolerance"]
    if abs(base_amount - gross) > tolerance:
        raise OpeningBasisError("Kraken trade volume does not match its base ledger")
    quote_amount = sum((signed_decimal(row.get("amount"), "Kraken quote ledger amount") for row in quote_rows), Decimal(0))
    quote_fee = sum((decimal_value(row.get("fee", 0), "Kraken quote ledger fee") for row in quote_rows), Decimal(0))
    if quote_amount >= 0:
        raise OpeningBasisError("Kraken acquisition quote ledger is inconsistent")
    quote_tolerance = max(Decimal("0.00000001"), quote_cost * Decimal("0.00000001"))
    if abs(abs(quote_amount) - quote_cost) > quote_tolerance:
        raise OpeningBasisError("Kraken trade cost does not match its quote ledger")
    return {
        "trade_ids": sorted(row["id"] for row in group),
        "order_id": str(group[0]["ordertxid"]),
        "occurred_at": max(timestamp_text(row["time"]) for row in group),
        "pair": str(group[0]["pair"]),
        "gross_quantity": decimal_text(gross),
        "base_fee_quantity": decimal_text(base_fee),
        "net_quantity": decimal_text(base_amount - base_fee),
        "quote_currency": quote,
        "quote_cost": decimal_text(quote_cost),
        "quote_fee": decimal_text(quote_fee),
        "ledger_ids": ledger_ids,
        "_quote_ledger_ids": [row["id"] for row in quote_rows],
    }


def _funding_leg(group, ledger_by_id) -> dict:
    ledger_ids, rows = _ledger_rows(group, ledger_by_id)
    gbp_rows = [row for row in rows if _asset(row.get("asset")) == "GBP"]
    usd_rows = [row for row in rows if _asset(row.get("asset")) == "USD"]
    if not gbp_rows or not usd_rows or len(gbp_rows) + len(usd_rows) != len(rows):
        raise OpeningBasisError("Kraken funding ledger currencies do not match GBP/USD")
    gbp_amount = sum((signed_decimal(row.get("amount"), "Kraken funding GBP amount") for row in gbp_rows), Decimal(0))
    gbp_fee = sum((decimal_value(row.get("fee", 0), "Kraken funding GBP fee") for row in gbp_rows), Decimal(0))
    usd_amount = sum((signed_decimal(row.get("amount"), "Kraken funding USD amount") for row in usd_rows), Decimal(0))
    usd_fee = sum((decimal_value(row.get("fee", 0), "Kraken funding USD fee") for row in usd_rows), Decimal(0))
    if gbp_amount >= 0 or gbp_fee != 0 or usd_amount <= 0 or usd_fee > usd_amount:
        raise OpeningBasisError("Kraken GBP/USD funding ledger is inconsistent")
    gross_gbp = sum((decimal_value(row.get("vol"), "Kraken funding volume", positive=True) for row in group), Decimal(0))
    gross_usd = sum((decimal_value(row.get("cost"), "Kraken funding cost", positive=True) for row in group), Decimal(0))
    if abs(gross_gbp - abs(gbp_amount)) > max(Decimal("0.00000001"), gross_gbp * Decimal("0.00000001")):
        raise OpeningBasisError("Kraken funding trade does not match its GBP ledger")
    if abs(gross_usd - usd_amount) > max(Decimal("0.00000001"), gross_usd * Decimal("0.00000001")):
        raise OpeningBasisError("Kraken funding trade does not match its USD ledger")
    rate = gross_usd / gross_gbp
    return {
        "rate": rate,
        "fee_usd": usd_fee,
        "net_usd": usd_amount - usd_fee,
        "usd_ledger_ids": [row["id"] for row in usd_rows],
        "gbp_ledger_ids": [row["id"] for row in gbp_rows],
        "fx": {
            "method": "kraken_gbp_usd_fill",
            "rate": decimal_text(rate),
            "trade_ids": sorted(row["id"] for row in group),
            "order_id": str(group[0]["ordertxid"]),
            "occurred_at": max(timestamp_text(row["time"]) for row in group),
            "pair": str(group[0]["pair"]),
            "ledger_ids": ledger_ids,
        },
    }


def _order_client_id(order: dict) -> str | None:
    value = order.get("cl_ord_id")
    return str(value) if value else None


def _closed_order(order_by_id: dict[str, dict], order_id: str) -> dict:
    order = order_by_id.get(order_id)
    if not isinstance(order, dict) or order.get("status") != "closed":
        raise OpeningBasisError("Kraken trade order is not proven closed")
    return order


def _expected_client_ids(target: str, quote: str, occurred_at: str) -> tuple[str, str]:
    trade_date = datetime.fromisoformat(occurred_at[:-1] + "+00:00").astimezone(
        SELECTED_TZ
    ).date()
    historical_target = f"{TARGETS[target]['asset']}_{quote}"
    return (
        build_client_order_id(historical_target, trade_date, purpose="buy"),
        build_client_order_id(historical_target, trade_date, purpose="funding"),
    )


def _validate_usd_conservation(
    leg: dict,
    funding: dict,
    ledgers: HistoryEvidence,
    ledger_by_id: dict[str, dict],
) -> None:
    if len(funding["usd_ledger_ids"]) != 1 or len(leg["_quote_ledger_ids"]) != 1:
        raise OpeningBasisError("Kraken USD cash lineage is not singular")
    funding_row = ledger_by_id[funding["usd_ledger_ids"][0]]
    acquisition_row = ledger_by_id[leg["_quote_ledger_ids"][0]]
    funding_time = signed_decimal(funding_row.get("time"), "funding ledger time")
    acquisition_time = signed_decimal(acquisition_row.get("time"), "acquisition ledger time")
    if acquisition_time < funding_time or acquisition_time - funding_time > 900:
        raise OpeningBasisError("Kraken USD acquisition is not contiguous with funding")
    allowed = set(funding["fx"]["ledger_ids"]) | set(leg["ledger_ids"])
    intervening = [
        row for row in ledgers.records
        if _asset(row.get("asset")) == "USD"
        and funding_time <= signed_decimal(row.get("time"), "USD ledger time") <= acquisition_time
        and row["id"] not in allowed
    ]
    if intervening:
        raise OpeningBasisError("Kraken USD funding was commingled before acquisition")
    funding_amount = signed_decimal(funding_row.get("amount"), "funding USD amount")
    funding_fee = decimal_value(funding_row.get("fee", 0), "funding USD fee")
    funding_balance = decimal_value(funding_row.get("balance"), "funding USD balance")
    pre_funding = funding_balance - (funding_amount - funding_fee)
    if abs(pre_funding) > Decimal("0.00000001"):
        raise OpeningBasisError("Kraken USD funding entered a non-zero cash pool")
    acquisition_amount = signed_decimal(acquisition_row.get("amount"), "acquisition USD amount")
    acquisition_fee = decimal_value(acquisition_row.get("fee", 0), "acquisition USD fee")
    acquisition_balance = decimal_value(acquisition_row.get("balance"), "acquisition USD balance")
    expected_balance = funding_balance + acquisition_amount - acquisition_fee
    if abs(acquisition_balance - expected_balance) > Decimal("0.00000001"):
        raise OpeningBasisError("Kraken USD cash balance does not conserve")
    if abs(acquisition_balance) > Decimal("0.00000001"):
        raise OpeningBasisError("Kraken USD funding left unallocated cash basis")
    funding_net = funding_amount - funding_fee
    acquisition_debit = abs(acquisition_amount) + acquisition_fee
    if abs(funding_net - acquisition_debit) > Decimal("0.00000001"):
        raise OpeningBasisError("Kraken USD funding does not exactly fund acquisition")


def parse_funding_links(value: str | None) -> dict[str, str]:
    if value is None or not value.strip():
        return {}
    try:
        links = _strict_json(value, FUNDING_LINKS_ENV)
    except OpeningBasisError as error:
        raise OpeningBasisError(f"{FUNDING_LINKS_ENV} is invalid") from error
    if not isinstance(links, dict) or any(
        not isinstance(key, str) or _IDENTIFIER.fullmatch(key) is None
        or not isinstance(item, str) or _IDENTIFIER.fullmatch(item) is None
        for key, item in links.items()
    ):
        raise OpeningBasisError(f"{FUNDING_LINKS_ENV} is invalid")
    if len(set(links.values())) != len(links):
        raise OpeningBasisError("one Kraken funding order cannot support multiple acquisitions")
    return links


def _unresolved(kind: str, *, ledger=None, occurred_at=None, quantity=None) -> dict:
    return {
        "kind": kind,
        "ledger_id": ledger,
        "occurred_at": occurred_at,
        "quantity": None if quantity is None else decimal_text(abs(quantity)),
    }


def _position(
    target, opening, trade_groups, funding_groups, ledger_by_id, order_by_id,
    trades, ledgers, funding_links, used_funding, used_ledger_links,
) -> dict:
    acquisitions = []
    unresolved = []
    used_target_ledgers = set()
    for (order_id, pair, side), group in sorted(trade_groups.items()):
        pair_target, quote = _PAIR_ALIASES[pair]
        if pair_target != target:
            continue
        if side == "sell":
            quantity = sum((decimal_value(row.get("vol"), "Kraken sell volume", positive=True) for row in group), Decimal(0))
            unresolved.append(_unresolved("sell", occurred_at=max(timestamp_text(row["time"]) for row in group), quantity=quantity))
            try:
                ids, _rows = _ledger_rows(group, ledger_by_id)
                used_target_ledgers.update(ids)
            except OpeningBasisError:
                pass
            continue
        try:
            leg = _spot_leg(group, target, quote, ledger_by_id)
            used_target_ledgers.update(leg["ledger_ids"])
            quote_ledger_ids = leg.pop("_quote_ledger_ids")
            order = _closed_order(order_by_id, order_id)
            buy_client = _order_client_id(order)
            if quote == "GBP":
                acquisition = {
                    **leg,
                    "route": "DIRECT_GBP",
                    "funding_fee_quote": "0",
                    "performance_cost_gbp": decimal_text(
                        decimal_value(leg["quote_cost"], "quote cost")
                        + decimal_value(leg["quote_fee"], "quote fee")
                    ),
                    "funding_order_id": None,
                    "client_order_id": buy_client,
                    "funding_client_order_id": None,
                    "fx": {"method": "identity", "rate": "1"},
                }
            else:
                expected_buy, expected_funding = _expected_client_ids(
                    target, quote, leg["occurred_at"]
                )
                if buy_client != expected_buy:
                    raise OpeningBasisError(
                        "Kraken USD acquisition lacks deterministic DCA linkage"
                    )
                candidates = []
                for (candidate, _funding_pair, funding_side), rows in funding_groups.items():
                    candidate_order = order_by_id.get(candidate)
                    if (
                        funding_side == "sell"
                        and isinstance(candidate_order, dict)
                        and candidate_order.get("status") == "closed"
                        and _order_client_id(candidate_order) == expected_funding
                        and max(timestamp_text(row["time"]) for row in rows) <= leg["occurred_at"]
                    ):
                        candidates.append(candidate)
                candidates = sorted(set(candidates))
                constrained = funding_links.get(order_id)
                if len(candidates) != 1 or (
                    constrained is not None and constrained != candidates[0]
                ):
                    unresolved.append(_unresolved("usd_gbp_funding_unproven", occurred_at=leg["occurred_at"], quantity=Decimal(leg["net_quantity"])))
                    continue
                funding_order = candidates[0]
                if funding_order in used_funding:
                    raise OpeningBasisError("one Kraken funding order cannot support multiple acquisitions")
                funding_group = next((rows for (candidate, _pair, side), rows in funding_groups.items() if candidate == funding_order and side == "sell"), None)
                if funding_group is None:
                    unresolved.append(_unresolved("usd_gbp_funding_unproven", occurred_at=leg["occurred_at"], quantity=Decimal(leg["net_quantity"])))
                    continue
                funding = _funding_leg(funding_group, ledger_by_id)
                if funding["fx"]["occurred_at"] > leg["occurred_at"]:
                    raise OpeningBasisError("Kraken funding fill occurred after its acquisition")
                quote_debit = decimal_value(leg["quote_cost"], "quote cost") + decimal_value(leg["quote_fee"], "quote fee")
                if quote_debit > funding["net_usd"] + Decimal("0.00000001"):
                    raise OpeningBasisError("Kraken acquisition exceeds linked funding proceeds")
                leg["_quote_ledger_ids"] = quote_ledger_ids
                _validate_usd_conservation(leg, funding, ledgers, ledger_by_id)
                leg.pop("_quote_ledger_ids")
                funding_order_row = _closed_order(order_by_id, funding_order)
                funding_client = _order_client_id(funding_order_row)
                if funding_client != expected_funding:
                    raise OpeningBasisError("Kraken USD orders lack deterministic DCA linkage")
                used_funding.add(funding_order)
                acquisition = {
                    **leg,
                    "route": "GBP_TO_USD",
                    "funding_fee_quote": decimal_text(funding["fee_usd"]),
                    "performance_cost_gbp": decimal_text((quote_debit + funding["fee_usd"]) / funding["rate"]),
                    "funding_order_id": funding_order,
                    "client_order_id": buy_client,
                    "funding_client_order_id": funding_client,
                    "fx": funding["fx"],
                }
            acquisition_links = set(acquisition["ledger_ids"]) | set(
                acquisition["fx"].get("ledger_ids", [])
            )
            if acquisition_links & used_ledger_links:
                raise OpeningBasisError("Kraken ledger evidence was reused")
            used_ledger_links.update(acquisition_links)
            acquisitions.append(acquisition)
        except OpeningBasisError:
            unresolved.append(_unresolved("unlinked_trade", occurred_at=max(timestamp_text(row["time"]) for row in group)))

    target_asset = TARGETS[target]["asset"]
    for ledger in ledgers.records:
        if _asset(ledger.get("asset")) != target_asset:
            continue
        ledger_id = ledger["id"]
        if ledger_id in used_target_ledgers:
            continue
        kind = str(ledger.get("type") or "").lower()
        mapped = kind if kind in {"deposit", "withdrawal", "transfer", "staking", "dividend", "adjustment"} else (
            "reward" if kind in {"credit", "reward"} else "unlinked_trade" if kind == "trade" else "unsupported"
        )
        unresolved.append(_unresolved(
            mapped,
            ledger=ledger_id,
            occurred_at=timestamp_text(ledger.get("time")),
            quantity=signed_decimal(ledger.get("amount"), "Kraken ledger amount"),
        ))
    if not trades.complete or not ledgers.complete:
        unresolved.append(_unresolved("history_incomplete"))
    acquired = sum((decimal_value(item["net_quantity"], "net acquisition quantity") for item in acquisitions), Decimal(0))
    if abs(acquired - opening) > TARGETS[target]["tolerance"]:
        unresolved.append(_unresolved("quantity_mismatch", quantity=abs(acquired - opening)))
    unresolved = sorted(
        {canonical(item): item for item in unresolved}.values(),
        key=lambda item: canonical(item),
    )
    complete = not unresolved
    cost = sum((decimal_value(item["performance_cost_gbp"], "performance cost") for item in acquisitions), Decimal(0))
    allocation_ambiguous = any(item["kind"] in {
        "sell", "withdrawal", "transfer", "staking", "adjustment", "unsupported", "unlinked_trade"
    } for item in unresolved)
    conservative_covered = Decimal(0) if allocation_ambiguous or acquired > opening else acquired
    if complete:
        conservative_covered = opening
    position = {
        "asset": TARGETS[target]["asset"],
        "opening_quantity": decimal_text(opening),
        "coverage": "complete" if complete else "missing",
        "covered_quantity": decimal_text(conservative_covered),
        "missing_quantity": decimal_text(max(Decimal(0), opening - conservative_covered)),
        "cost_basis_gbp": decimal_text(cost) if complete else None,
        "average_unit_cost_gbp": decimal_text(cost / opening) if complete and opening else ("0" if complete else None),
        "acquisitions": sorted(acquisitions, key=lambda item: (item["occurred_at"], item["order_id"])),
        "unresolved": unresolved,
    }
    position["evidence_hash"] = canonical_hash(position)
    return position


def build_artifact(
    binding: OpeningBinding,
    trades: HistoryEvidence,
    ledgers: HistoryEvidence,
    orders: HistoryEvidence,
    *,
    generated_at: str,
    access: AccessEvidence,
    source_evidence: dict,
    funding_links: dict[str, str] | None = None,
) -> dict:
    generated = utc_timestamp(generated_at, "generated_at")
    if generated < utc_timestamp(CUTOVER_AT, "cutover"):
        raise OpeningBasisError("generated_at predates cutover")
    _validate_access(access, generated_at=generated_at)
    _validate_source_reference(source_evidence)
    _validate_global_trade_ownership(trades, ledgers)
    trade_groups = _groups(trades)
    funding_groups = {
        key: value for key, value in trade_groups.items() if key[1] in _FUNDING_PAIRS
    }
    target_groups = {
        key: value for key, value in trade_groups.items() if key[1] in _PAIR_ALIASES
    }
    ledger_by_id = {row["id"]: row for row in ledgers.records}
    order_by_id = {row["id"]: row for row in orders.records}
    links = funding_links or {}
    if len(set(links.values())) != len(links):
        raise OpeningBasisError("one Kraken funding order cannot support multiple acquisitions")
    usd_buy_orders = {
        order_id
        for (order_id, pair, side) in target_groups
        if side == "buy" and _PAIR_ALIASES[pair][1] == "USD"
    }
    funding_orders = {
        order_id
        for (order_id, _pair, side) in funding_groups
        if side == "sell"
    }
    if not set(links).issubset(usd_buy_orders) or not set(links.values()).issubset(funding_orders):
        raise OpeningBasisError("reviewed funding links do not match Kraken history")
    used_funding = set()
    used_ledger_links = set()
    positions = {
        target: _position(
            target,
            binding.quantities[target],
            target_groups,
            funding_groups,
            ledger_by_id,
            order_by_id,
            trades,
            ledgers,
            links,
            used_funding,
            used_ledger_links,
        )
        for target in TARGETS
    }
    artifact = {
        "version": VERSION,
        "basis_type": BASIS_TYPE,
        "method": METHOD,
        "generated_at": generated_at,
        "cutover_at": CUTOVER_AT,
        "opening_binding": {
            "repository_commit_sha": binding.repository_commit_sha,
            "opening_state_hash": binding.opening_state_hash,
            "holdings": {
                "path": binding.holdings_path,
                "blob_sha": binding.holdings_blob_sha,
                "canonical_hash": binding.holdings_snapshot_hash,
            },
            "events": {
                "path": binding.events_path,
                "blob_sha": binding.events_blob_sha,
                "content_sha256": binding.events_content_sha256,
                "prefix_hash": binding.event_prefix_hash,
                "accepted_event_count": binding.accepted_event_count,
            },
        },
        "source_evidence": source_evidence,
        "history": {
            "from": HISTORY_FROM,
            "through": CUTOVER_AT,
            "access": access.metadata(),
            "trades": trades.metadata(),
            "ledgers": ledgers.metadata(),
            "orders": orders.metadata(),
        },
        "positions": positions,
    }
    return _hash_artifact(artifact)


def _validate_source_reference(value: dict) -> None:
    if not isinstance(value, dict) or set(value) != {
        "path", "blob_sha", "repository_commit_sha", "canonical_hash", "producer_commit"
    }:
        raise OpeningBasisError("opening-basis source reference is invalid")
    if (
        not isinstance(value["path"], str)
        or value["path"].split("/")[-1] != OPENING_BASIS_SOURCE_FILE
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", str(value["blob_sha"])) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(value["repository_commit_sha"])) is None
        or re.fullmatch(r"[0-9a-f]{64}", str(value["canonical_hash"])) is None
        or re.fullmatch(r"[0-9a-f]{40}", str(value["producer_commit"])) is None
    ):
        raise OpeningBasisError("opening-basis source reference is invalid")


def _serialized_artifact(artifact: dict) -> str:
    if not isinstance(artifact, dict) or re.fullmatch(
        r"[0-9a-f]{64}", str(artifact.get("canonical_hash"))
    ) is None:
        raise OpeningBasisError("opening-basis artifact hash is invalid")
    unhashed = {key: value for key, value in artifact.items() if key != "canonical_hash"}
    if artifact["canonical_hash"] != canonical_hash(unhashed):
        raise OpeningBasisError("opening-basis artifact hash does not match its content")
    content = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if len(content.encode("utf-8")) > CONSUMER_MAX_BYTES:
        raise OpeningBasisError("opening-basis artifact exceeds the consumer size limit")
    return content


def _publish_write_once(
    artifact: dict,
    *,
    path: str,
    expected_filename: str,
    expected_canonical_hash: str,
    message: str,
    client=None,
):
    if path.split("/")[-1] != expected_filename:
        raise OpeningBasisError(f"opening-basis path must end with {expected_filename}")
    if (
        not isinstance(expected_canonical_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", expected_canonical_hash) is None
        or artifact.get("canonical_hash") != expected_canonical_hash
    ):
        raise OpeningBasisError("reviewed opening-basis hash does not match")
    repository = client or GitHubContentsClient.from_env()
    content = _serialized_artifact(artifact)
    result = repository.write_once_text(path, content, message=message)
    if result.content != content:
        raise OpeningBasisError("private repository did not confirm opening-basis evidence")
    return result


def publish(
    artifact: dict,
    *,
    expected_canonical_hash: str,
    client=None,
):
    """Write the compact performance-basis artifact once, or prove an exact retry."""
    return _publish_write_once(
        artifact,
        path=configured_opening_basis_path(),
        expected_filename=OPENING_BASIS_FILE,
        expected_canonical_hash=expected_canonical_hash,
        message="Create immutable Kraken opening performance basis",
        client=client,
    )


def publish_source(
    artifact: dict,
    *,
    expected_canonical_hash: str,
    client=None,
):
    """Write normalized Kraken source evidence once, or prove an exact retry."""
    return _publish_write_once(
        artifact,
        path=configured_opening_basis_source_path(),
        expected_filename=OPENING_BASIS_SOURCE_FILE,
        expected_canonical_hash=expected_canonical_hash,
        message="Create immutable Kraken opening-basis source evidence",
        client=client,
    )


def _source_summary(source: dict, *, repository_commit_sha: str | None = None) -> dict:
    result = {
        "stage": "source",
        "canonical_hash": source["canonical_hash"],
        "generated_at": source["generated_at"],
        "record_counts": {
            name: len(source[name]) for name in ("trades", "ledgers", "orders")
        },
    }
    if repository_commit_sha is not None:
        result["repository_commit_sha"] = repository_commit_sha
    return result


def _basis_summary(artifact: dict) -> dict:
    return {
        "stage": "basis",
        "canonical_hash": artifact["canonical_hash"],
        "generated_at": artifact["generated_at"],
        "source_repository_commit_sha": artifact["source_evidence"]["repository_commit_sha"],
        "opening_binding": artifact["opening_binding"],
        "coverage": {
            target: {
                "coverage": position["coverage"],
                "opening_quantity": position["opening_quantity"],
                "missing_quantity": position["missing_quantity"],
            }
            for target, position in artifact["positions"].items()
        },
    }


def _existing_source(repository, path: str, generated_at: str) -> dict | None:
    current = repository.read_text(path)
    if not current.exists:
        return None
    source, *_history = parse_source_artifact(current.content)
    if source["generated_at"] != generated_at:
        raise OpeningBasisError(
            "immutable source exists with a different generated_at; use its original value"
        )
    return source


def _run_source(args, repository) -> dict:
    source_path = configured_opening_basis_source_path()
    if source_path.split("/")[-1] != OPENING_BASIS_SOURCE_FILE:
        raise OpeningBasisError(
            f"opening-basis source path must end with {OPENING_BASIS_SOURCE_FILE}"
        )
    source = _existing_source(repository, source_path, args.generated_at)
    if source is None:
        producer_commit = str(os.environ.get("GITHUB_SHA", "")).lower()
        access, trades, ledgers, orders = fetch_kraken_history(
            get_kraken_exchange(), generated_at=args.generated_at
        )
        source = build_source_artifact(
            access,
            trades,
            ledgers,
            orders,
            generated_at=args.generated_at,
            producer_commit=producer_commit,
        )
    _serialized_artifact(source)
    if args.mode == "publish":
        publish_source(
            source,
            expected_canonical_hash=args.expected_canonical_hash,
            client=repository,
        )
    source_commit = repository.resolve_commit_sha() if args.mode == "publish" else None
    return _source_summary(source, repository_commit_sha=source_commit)


def _build_basis_at_commit(
    repository,
    *,
    pinned_commit: str,
    generated_at: str,
    funding_links: dict[str, str],
) -> dict:
    paths = configured_outbox_paths()
    source_path = configured_opening_basis_source_path()
    source_file = repository.read_text_at_commit(source_path, pinned_commit)
    if not source_file.exists:
        raise OpeningBasisError(
            "pinned source commit lacks opening-basis source evidence"
        )
    holdings_file = repository.read_text_at_commit(
        paths.holdings, REVIEWED_OPENING_REPOSITORY_COMMIT_SHA
    )
    events_file = repository.read_text_at_commit(
        paths.event, REVIEWED_OPENING_REPOSITORY_COMMIT_SHA
    )
    if (
        not holdings_file.exists
        or holdings_file.sha is None
        or not events_file.exists
        or events_file.sha is None
    ):
        raise OpeningBasisError(
            "reviewed opening commit lacks holdings or canonical events"
        )
    source, access, trades, ledgers, orders = parse_source_artifact(source_file.content)
    if source["generated_at"] != generated_at:
        raise OpeningBasisError(
            "basis generated_at must reuse the immutable source generated_at"
        )
    binding = derive_opening_binding(
        holdings_file.content,
        events_file.content,
        repository_commit_sha=REVIEWED_OPENING_REPOSITORY_COMMIT_SHA,
        holdings_path=paths.holdings,
        holdings_blob_sha=holdings_file.sha,
        events_path=paths.event,
        events_blob_sha=events_file.sha,
    )
    source_reference = {
        "path": source_path,
        "blob_sha": source_file.sha,
        "repository_commit_sha": pinned_commit,
        "canonical_hash": source["canonical_hash"],
        "producer_commit": source["producer_commit"],
    }
    return build_artifact(
        binding,
        trades,
        ledgers,
        orders,
        generated_at=source["generated_at"],
        access=access,
        source_evidence=source_reference,
        funding_links=funding_links,
    )


def _run_basis(args, repository) -> dict:
    basis_path = configured_opening_basis_path()
    if basis_path.split("/")[-1] != OPENING_BASIS_FILE:
        raise OpeningBasisError(
            f"opening-basis path must end with {OPENING_BASIS_FILE}"
        )
    if args.mode == "publish":
        existing = repository.read_text(basis_path)
        if existing.exists:
            try:
                artifact = _strict_json(
                    existing.content, "immutable opening-basis artifact"
                )
            except OpeningBasisError:
                raise
            _serialized_artifact(artifact)
            if (
                artifact.get("version") != VERSION
                or artifact.get("basis_type") != BASIS_TYPE
                or artifact.get("method") != METHOD
                or artifact.get("generated_at") != args.generated_at
                or artifact.get("cutover_at") != CUTOVER_AT
            ):
                raise OpeningBasisError(
                    "immutable opening-basis artifact identity is invalid"
                )
            _validate_source_reference(artifact.get("source_evidence"))
            rebuilt = _build_basis_at_commit(
                repository,
                pinned_commit=artifact["source_evidence"]["repository_commit_sha"],
                generated_at=args.generated_at,
                funding_links={},
            )
            if _serialized_artifact(rebuilt) != _serialized_artifact(artifact):
                raise OpeningBasisError(
                    "immutable opening-basis artifact does not match its source commit"
                )
            publish(
                artifact,
                expected_canonical_hash=args.expected_canonical_hash,
                client=repository,
            )
            return _basis_summary(artifact)
    pinned_commit = args.source_commit_sha or repository.resolve_commit_sha()
    if re.fullmatch(r"[0-9a-f]{40}", pinned_commit) is None:
        raise OpeningBasisError("source commit must be a full lowercase Git SHA")
    if args.mode == "publish" and args.source_commit_sha is None:
        raise OpeningBasisError(
            "basis publish requires --source-commit-sha from its reviewed preview"
        )
    artifact = _build_basis_at_commit(
        repository,
        pinned_commit=pinned_commit,
        generated_at=args.generated_at,
        funding_links=parse_funding_links(os.environ.get(FUNDING_LINKS_ENV)),
    )
    _serialized_artifact(artifact)
    if args.mode == "publish":
        publish(
            artifact,
            expected_canonical_hash=args.expected_canonical_hash,
            client=repository,
        )
    return _basis_summary(artifact)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build immutable pre-cutover Kraken performance cost evidence"
    )
    parser.add_argument("--stage", choices=("source", "basis"), required=True)
    parser.add_argument("--mode", choices=("preview", "publish"), default="preview")
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--expected-canonical-hash")
    parser.add_argument("--source-commit-sha")
    args = parser.parse_args()
    utc_timestamp(args.generated_at, "generated_at")
    if args.mode == "publish" and (
        not isinstance(args.expected_canonical_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", args.expected_canonical_hash) is None
    ):
        raise OpeningBasisError(
            "publish mode requires --expected-canonical-hash from a reviewed preview"
        )
    ensure_snapshot_safe_execution_state(os.environ["DCA_EXECUTION_STATE"])
    repository = GitHubContentsClient.from_env()
    summary = (
        _run_source(args, repository)
        if args.stage == "source"
        else _run_basis(args, repository)
    )
    summary["mode"] = args.mode
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()

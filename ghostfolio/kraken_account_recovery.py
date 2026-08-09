"""Build immutable evidence for the reviewed Kraken account-history recovery.

The normal DCA event ledger intentionally contains only bot-created purchases.
This one-time contract records the exchange activity that happened after the
Portfolio Compass cutover but before the first canonical DCA event.  It never
places an order: the only Kraken methods used are API-key metadata, closed trade
history, ledger history, and closed-order lookup.

The compact artifact proves three independently useful facts:

* the true cutover position comes from unrestricted Kraken ledger history;
* every manual buy or asset debit in the bounded seam has exact source rows;
* applying those rows reaches the separately reviewed pre-DCA position state.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from decimal import Decimal, ROUND_HALF_UP

from github_contents import (
    GitHubContentsClient,
    configured_account_activity_source_path,
    configured_account_recovery_path,
    configured_opening_basis_source_path,
    configured_outbox_paths,
)
from ghostfolio import kraken_opening_basis as basis
from ghostfolio.kraken_holdings_snapshot import ensure_snapshot_safe_execution_state
from kraken_client import get_kraken_exchange


VERSION = 1
SOURCE_VERSION = 1
RECOVERY_TYPE = "kraken_reviewed_account_recovery"
RECOVERY_METHOD = "opening-plus-manual-ledger-v1"
SOURCE_EVIDENCE_TYPE = "kraken_account_activity_source"
SOURCE_NORMALIZATION = "kraken-account-activity-source-v1"
OPENING_MODEL = "kraken-ledger-at-cutover-v1"
ACTIVITY_AFTER = basis.CUTOVER_AT
ACTIVITY_THROUGH = "2026-08-07T03:31:59.999999Z"
ACCOUNT_ACTIVITY_SOURCE_FILE = "kraken_account_activity_source_v1.json"
ACCOUNT_RECOVERY_FILE = "kraken_account_recovery_v1.json"
REVIEWED_OPENING_SOURCE_REPOSITORY_COMMIT_SHA = (
    "cc244a7821e4a819709bfe5916dc9db34ad08f69"
)
REVIEWED_END_REPOSITORY_COMMIT_SHA = basis.REVIEWED_OPENING_REPOSITORY_COMMIT_SHA
REVIEWED_END_STATE_HASH = basis.REVIEWED_OPENING_STATE_HASH
REVIEWED_TRUE_OPENING_STATE_HASH = (
    "8009256dde71cf239ceef0d7937ff16c42026f466ac34868f9715b515ef48ccf"
)
CONSUMER_MAX_BYTES = 1_000_000


class AccountRecoveryError(RuntimeError):
    """Safe failure that never exposes credentials or unnormalized responses."""


def _error(message: str, error: Exception | None = None):
    if error is None:
        raise AccountRecoveryError(message)
    raise AccountRecoveryError(message) from error


def _source_params(*, start: int, end: int, offset: int, page_size: int, container: str):
    params = {"start": start, "end": end, "ofs": offset}
    if container == "trades":
        params.update({
            "limit": page_size,
            "type": "all",
            "trades": "false",
            "without_count": "false",
            "consolidate_taker": "false",
            "ledgers": "true",
        })
    else:
        params.update({"type": "all", "without_count": "false"})
    return params


def _fetch_interval_pages(
    exchange,
    *,
    method_name: str,
    container: str,
    after: Decimal,
    through: Decimal,
    page_size: int,
) -> basis.HistoryEvidence:
    method = getattr(exchange, method_name, None)
    if not callable(method):
        _error("Kraken reviewed-activity history endpoint is unavailable")
    request_start = int(after)
    request_end = int(through)
    offset = 0
    expected_count = None
    page_number = 0
    records: list[dict] = []
    pages: list[dict] = []
    seen: set[str] = set()
    while expected_count is None or offset < expected_count:
        params = _source_params(
            start=request_start,
            end=request_end,
            offset=offset,
            page_size=page_size,
            container=container,
        )
        try:
            result = basis._api_result(method(params), container)
        except basis.OpeningBasisError as error:
            _error(f"Kraken {container} reviewed-activity request failed", error)
        except Exception as error:
            _error(f"Kraken {container} reviewed-activity request failed", error)
        count = result.get("count")
        rows = result.get(container)
        if type(count) is not int or count < 0 or not isinstance(rows, dict):
            _error(f"Kraken {container} reviewed-activity pagination is invalid")
        if expected_count is None:
            expected_count = count
        elif expected_count != count:
            _error(f"Kraken {container} reviewed-activity count changed")
        page_number += 1
        if expected_count and not rows:
            _error(f"Kraken {container} reviewed-activity pagination ended early")
        page_rows = []
        for record_id, row in rows.items():
            if not isinstance(record_id, str) or record_id in seen or not isinstance(row, dict):
                _error(f"Kraken {container} reviewed-activity history is malformed")
            occurred = basis.signed_decimal(
                row.get("time"), f"Kraken {container} reviewed-activity timestamp"
            )
            if occurred <= after or occurred > through:
                _error(f"Kraken {container} history falls outside the reviewed seam")
            try:
                normalized = (
                    basis._normalize_trade(record_id, row)
                    if container == "trades"
                    else basis._normalize_ledger(record_id, row)
                )
            except basis.OpeningBasisError as error:
                _error(f"Kraken {container} reviewed-activity history is malformed", error)
            seen.add(record_id)
            records.append(normalized)
            page_rows.append(normalized)
        ordered_page = sorted(page_rows, key=lambda item: item["id"])
        pages.append({
            "page": page_number,
            "offset": offset,
            "returned_count": len(ordered_page),
            "response_count": count,
            "record_ids": [item["id"] for item in ordered_page],
            "canonical_hash": basis.canonical_hash(ordered_page),
        })
        offset += len(rows)
        if offset > expected_count:
            _error(f"Kraken {container} reviewed-activity pagination exceeded its count")
    if len(records) != expected_count:
        _error(f"Kraken {container} reviewed-activity history is incomplete")
    return basis.history_evidence(
        sorted(records, key=lambda item: item["id"]),
        page_count=page_number,
        pages=pages,
    )


def _validate_access_through(access: basis.AccessEvidence, generated_at: str) -> None:
    try:
        query_from = basis._integer_setting(access.query_from, "source query_from")
        query_to = basis._integer_setting(access.query_to, "source query_to")
        valid_until = basis._integer_setting(
            access.key_valid_until, "source key_valid_until"
        )
        through = basis.epoch_decimal(
            basis.utc_timestamp(ACTIVITY_THROUGH, "activity through")
        )
        generated = basis.epoch_decimal(
            basis.utc_timestamp(generated_at, "source generated_at")
        )
    except basis.OpeningBasisError as error:
        _error("reviewed-activity access evidence is invalid", error)
    if query_from != 0:
        _error("Kraken key cannot prove unrestricted reviewed-activity history")
    if query_to != 0 and Decimal(query_to) < through:
        _error("Kraken key history ends before the reviewed-activity boundary")
    if valid_until != 0 and Decimal(valid_until) < generated:
        _error("Kraken key expired before reviewed-activity generation")


def fetch_activity_source(exchange, *, generated_at: str):
    after = basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_AFTER, "activity after"))
    through = basis.epoch_decimal(
        basis.utc_timestamp(ACTIVITY_THROUGH, "activity through")
    )
    try:
        access = basis.ensure_history_permissions(
            exchange, cutover_at=ACTIVITY_THROUGH, generated_at=generated_at
        )
    except basis.OpeningBasisError as error:
        _error("Kraken reviewed-activity permission check failed", error)
    trades = _fetch_interval_pages(
        exchange,
        method_name="privatePostTradesHistory",
        container="trades",
        after=after,
        through=through,
        page_size=100,
    )
    ledgers = _fetch_interval_pages(
        exchange,
        method_name="privatePostLedgers",
        container="ledger",
        after=after,
        through=through,
        page_size=50,
    )
    try:
        orders = basis._fetch_orders(exchange, trades)
    except basis.OpeningBasisError as error:
        _error("Kraken reviewed-activity closed orders are incomplete", error)
    return access, trades, ledgers, orders


def _validate_interval_history(
    name: str,
    evidence: basis.HistoryEvidence,
    normalizer,
) -> None:
    if not evidence.complete or not evidence.pages or len(evidence.pages) != evidence.page_count:
        _error(f"Kraken {name} reviewed-activity source is incomplete")
    if evidence.canonical_hash != basis.canonical_hash(list(evidence.records)):
        _error(f"Kraken {name} reviewed-activity source hash is invalid")
    record_by_id = {row["id"]: row for row in evidence.records}
    if len(record_by_id) != len(evidence.records):
        _error(f"Kraken {name} reviewed-activity identifiers are duplicated")
    after = basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_AFTER, "activity after"))
    through = basis.epoch_decimal(
        basis.utc_timestamp(ACTIVITY_THROUGH, "activity through")
    )
    for row in evidence.records:
        try:
            normalized = normalizer(row["id"], row)
        except basis.OpeningBasisError as error:
            _error(f"Kraken {name} reviewed-activity row is invalid", error)
        if row != normalized:
            _error(f"Kraken {name} reviewed-activity row is not normalized")
        if name != "orders":
            occurred = basis.signed_decimal(row["time"], f"Kraken {name} time")
            if occurred <= after or occurred > through:
                _error(f"Kraken {name} reviewed-activity source exceeds its seam")
    next_offset = 0
    flattened: list[str] = []
    total = len(evidence.records)
    limits = {"trades": 100, "ledgers": 50, "orders": 50}
    for index, page in enumerate(evidence.pages, start=1):
        required = {
            "page", "offset", "returned_count", "response_count",
            "record_ids", "canonical_hash",
        }
        ids = page.get("record_ids") if isinstance(page, dict) else None
        if (
            not isinstance(page, dict)
            or set(page) != required
            or type(page.get("page")) is not int
            or page["page"] != index
            or type(page.get("offset")) is not int
            or page["offset"] != next_offset
            or type(page.get("returned_count")) is not int
            or page["returned_count"] < 0
            or type(page.get("response_count")) is not int
            or page["response_count"] != total
            or not isinstance(ids, list)
            or ids != sorted(ids)
            or len(ids) != page["returned_count"]
            or len(set(ids)) != len(ids)
            or any(item not in record_by_id for item in ids)
            or page["returned_count"] > limits[name]
        ):
            _error(f"Kraken {name} reviewed-activity page manifest is invalid")
        page_rows = [record_by_id[item] for item in ids]
        if page["canonical_hash"] != basis.canonical_hash(page_rows):
            _error(f"Kraken {name} reviewed-activity page hash is invalid")
        flattened.extend(ids)
        next_offset += len(ids)
    if sorted(flattened) != sorted(record_by_id) or next_offset != total:
        _error(f"Kraken {name} reviewed-activity page coverage is invalid")


def build_activity_source(
    access: basis.AccessEvidence,
    trades: basis.HistoryEvidence,
    ledgers: basis.HistoryEvidence,
    orders: basis.HistoryEvidence,
    *,
    generated_at: str,
    producer_commit: str,
) -> dict:
    generated = basis.utc_timestamp(generated_at, "generated_at")
    if generated < basis.utc_timestamp(ACTIVITY_THROUGH, "activity through"):
        _error("generated_at predates reviewed activity")
    if re.fullmatch(r"[0-9a-f]{40}", str(producer_commit)) is None:
        _error("producer commit must be a full lowercase Git SHA")
    _validate_access_through(access, generated_at)
    for name, evidence, normalizer in (
        ("trades", trades, basis._normalize_trade),
        ("ledgers", ledgers, basis._normalize_ledger),
        ("orders", orders, basis._normalize_order),
    ):
        _validate_interval_history(name, evidence, normalizer)
    relevant_orders = {
        row["ordertxid"] for row in trades.records
        if basis._pair_key(row["pair"]) in set(basis._PAIR_ALIASES) | set(basis._FUNDING_PAIRS)
    }
    if relevant_orders != {row["id"] for row in orders.records}:
        _error("Kraken reviewed-activity orders do not exactly cover relevant trades")
    start = str(int(basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_AFTER, "activity after"))))
    end = str(int(basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_THROUGH, "activity through"))))
    source = {
        "version": SOURCE_VERSION,
        "evidence_type": SOURCE_EVIDENCE_TYPE,
        "normalization": SOURCE_NORMALIZATION,
        "generated_at": generated_at,
        "activity_after": ACTIVITY_AFTER,
        "activity_through": ACTIVITY_THROUGH,
        "producer_commit": producer_commit,
        "access": {
            "permissions": sorted(basis.REQUIRED_PERMISSIONS),
            **access.metadata(),
        },
        "requests": {
            "trades": {
                "endpoint": "TradesHistory", "start": start, "end": end,
                "page_size": 100, "type": "all", "trades": False,
                "without_count": False, "consolidate_taker": False,
                "ledgers": True,
            },
            "ledgers": {
                "endpoint": "Ledgers", "start": start, "end": end,
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
    return basis._hash_artifact(source)


def parse_activity_source(content: str):
    try:
        source = basis._strict_json(content, "reviewed account-activity source")
    except basis.OpeningBasisError as error:
        _error("reviewed account-activity source is malformed", error)
    required = {
        "version", "evidence_type", "normalization", "generated_at",
        "activity_after", "activity_through", "producer_commit", "access",
        "requests", "pagination", "trades", "ledgers", "orders",
        "canonical_hash",
    }
    unhashed = {
        key: value for key, value in source.items() if key != "canonical_hash"
    } if isinstance(source, dict) else {}
    if (
        not isinstance(source, dict)
        or set(source) != required
        or source.get("version") != SOURCE_VERSION
        or source.get("evidence_type") != SOURCE_EVIDENCE_TYPE
        or source.get("normalization") != SOURCE_NORMALIZATION
        or source.get("activity_after") != ACTIVITY_AFTER
        or source.get("activity_through") != ACTIVITY_THROUGH
        or source.get("canonical_hash") != basis.canonical_hash(unhashed)
        or re.fullmatch(r"[0-9a-f]{40}", str(source.get("producer_commit"))) is None
    ):
        _error("reviewed account-activity source identity is invalid")
    basis.utc_timestamp(source["generated_at"], "source generated_at")
    access_value = source.get("access")
    if (
        not isinstance(access_value, dict)
        or set(access_value) != {
            "permissions", "query_from", "query_to", "key_valid_until"
        }
        or access_value.get("permissions") != sorted(basis.REQUIRED_PERMISSIONS)
    ):
        _error("reviewed account-activity access evidence is invalid")
    try:
        access = basis.AccessEvidence(
            str(basis._integer_setting(access_value["query_from"], "source query_from")),
            str(basis._integer_setting(access_value["query_to"], "source query_to")),
            str(basis._integer_setting(access_value["key_valid_until"], "source key_valid_until")),
        )
    except basis.OpeningBasisError as error:
        _error("reviewed account-activity access evidence is invalid", error)
    _validate_access_through(access, source["generated_at"])
    start = str(int(basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_AFTER, "activity after"))))
    end = str(int(basis.epoch_decimal(basis.utc_timestamp(ACTIVITY_THROUGH, "activity through"))))
    if source.get("requests") != {
        "trades": {
            "endpoint": "TradesHistory", "start": start, "end": end,
            "page_size": 100, "type": "all", "trades": False,
            "without_count": False, "consolidate_taker": False,
            "ledgers": True,
        },
        "ledgers": {
            "endpoint": "Ledgers", "start": start, "end": end,
            "page_size": 50, "type": "all", "without_count": False,
        },
        "orders": {"endpoint": "QueryOrders", "batch_size": 50, "trades": False},
    }:
        _error("reviewed account-activity request contract is invalid")
    pagination = source.get("pagination")
    if not isinstance(pagination, dict) or set(pagination) != {
        "trades", "ledgers", "orders"
    }:
        _error("reviewed account-activity pagination is invalid")
    histories = []
    for name, normalizer in (
        ("trades", basis._normalize_trade),
        ("ledgers", basis._normalize_ledger),
        ("orders", basis._normalize_order),
    ):
        rows = source.get(name)
        pages = pagination.get(name)
        if not isinstance(rows, list) or not isinstance(pages, list) or not pages:
            _error("reviewed account-activity source history is invalid")
        try:
            evidence = basis.history_evidence(rows, page_count=len(pages), pages=pages)
        except basis.OpeningBasisError as error:
            _error("reviewed account-activity source history is invalid", error)
        _validate_interval_history(name, evidence, normalizer)
        histories.append(evidence)
    relevant_orders = {
        row["ordertxid"] for row in histories[0].records
        if basis._pair_key(row["pair"]) in set(basis._PAIR_ALIASES) | set(basis._FUNDING_PAIRS)
    }
    if relevant_orders != {row["id"] for row in histories[2].records}:
        _error("reviewed account-activity order coverage is invalid")
    return source, access, histories[0], histories[1], histories[2]


def _position_quantities(ledgers: basis.HistoryEvidence) -> dict[str, Decimal]:
    quantities: dict[str, Decimal] = {}
    for target, contract in basis.TARGETS.items():
        quantity = sum((
            basis.signed_decimal(row["amount"], "Kraken target ledger amount")
            - basis.decimal_value(row.get("fee", 0), "Kraken target ledger fee")
            for row in ledgers.records
            if basis._asset(row.get("asset")) == contract["asset"]
        ), Decimal(0))
        tolerance = contract["tolerance"]
        if quantity < -tolerance:
            _error(f"{target} Kraken ledger position is negative")
        quantity = max(Decimal(0), quantity)
        quantized = quantity.quantize(
            Decimal(1).scaleb(-contract["places"]), rounding=ROUND_HALF_UP
        )
        if abs(quantity - quantized) > tolerance:
            _error(f"{target} Kraken ledger position exceeds contract precision")
        quantities[target] = quantized
    return quantities


def _state(model: str, cutover_at: str, quantities: dict[str, Decimal]) -> dict:
    value = {
        "version": 1,
        "model": model,
        "cutoverAt": cutover_at,
        "positions": [
            {
                "asset": contract["asset"],
                "quantity": basis.decimal_text(quantities[target]),
            }
            for target, contract in basis.TARGETS.items()
        ],
    }
    return {**value, "state_hash": basis.canonical_hash(value)}


def _opening_positions(
    quantities: dict[str, Decimal],
    trades: basis.HistoryEvidence,
    ledgers: basis.HistoryEvidence,
    orders: basis.HistoryEvidence,
) -> list[dict]:
    try:
        basis._validate_global_trade_ownership(trades, ledgers)
    except basis.OpeningBasisError as error:
        _error("opening Kraken trade ownership is invalid", error)
    groups = basis._groups(trades)
    target_groups = {
        key: value for key, value in groups.items() if key[1] in basis._PAIR_ALIASES
    }
    funding_groups = {
        key: value for key, value in groups.items() if key[1] in basis._FUNDING_PAIRS
    }
    ledger_by_id = {row["id"]: row for row in ledgers.records}
    order_by_id = {row["id"]: row for row in orders.records}
    used_funding: set[str] = set()
    used_ledgers: set[str] = set()
    result = []
    for target, contract in basis.TARGETS.items():
        position = basis._position(
            target,
            quantities[target],
            target_groups,
            funding_groups,
            ledger_by_id,
            order_by_id,
            trades,
            ledgers,
            {},
            used_funding,
            used_ledgers,
        )
        if position["coverage"] != "complete":
            _error(f"{target} true opening cost is not completely evidenced")
        result.append({
            "target": target,
            "asset": contract["asset"],
            "quantity": position["opening_quantity"],
            "performance_cost_gbp": position["cost_basis_gbp"],
            "average_unit_cost_gbp": position["average_unit_cost_gbp"],
            "acquisitions": position["acquisitions"],
            "evidence_hash": position["evidence_hash"],
        })
    return result


def _activity_hash(row: dict) -> dict:
    if "canonical_hash" in row:
        _error("reviewed activity row already contains a hash")
    return {**row, "canonical_hash": basis.canonical_hash(row)}


def _manual_activities(
    trades: basis.HistoryEvidence,
    ledgers: basis.HistoryEvidence,
    orders: basis.HistoryEvidence,
) -> list[dict]:
    try:
        basis._validate_global_trade_ownership(trades, ledgers)
    except basis.OpeningBasisError as error:
        _error("manual Kraken trade ownership is invalid", error)
    unsupported_pairs = sorted({
        basis._pair_key(row.get("pair")) for row in trades.records
        if basis._pair_key(row.get("pair")) not in basis._PAIR_ALIASES
    })
    if unsupported_pairs:
        _error("reviewed seam contains an unsupported or funding trade")
    groups = basis._groups(trades)
    ledger_by_id = {row["id"]: row for row in ledgers.records}
    order_by_id = {row["id"]: row for row in orders.records}
    used_target_ledgers: set[str] = set()
    activities = []
    for (order_id, pair, side), group in sorted(groups.items()):
        if pair not in basis._PAIR_ALIASES:
            continue
        target, quote = basis._PAIR_ALIASES[pair]
        if side != "buy":
            _error("reviewed seam contains a target-asset sell")
        if quote != "GBP":
            _error("reviewed manual acquisition is not directly GBP evidenced")
        try:
            leg = basis._spot_leg(group, target, quote, ledger_by_id)
            basis._closed_order(order_by_id, order_id)
        except basis.OpeningBasisError as error:
            _error("reviewed manual acquisition is not completely evidenced", error)
        leg.pop("_quote_ledger_ids")
        used_target_ledgers.update(leg["ledger_ids"])
        performance_cost = (
            basis.decimal_value(leg["quote_cost"], "manual acquisition cost")
            + basis.decimal_value(leg["quote_fee"], "manual acquisition quote fee")
        )
        row = {
            "activity_version": 1,
            "activity_id": f"KRAKEN-MANUAL-BUY-{order_id}",
            "kind": "buy",
            "occurred_at": leg["occurred_at"],
            "target": target,
            "asset": basis.TARGETS[target]["asset"],
            "quantity": leg["net_quantity"],
            "gross_quantity": leg["gross_quantity"],
            "asset_fee_quantity": leg["base_fee_quantity"],
            "quote_currency": "GBP",
            "quote_cost": leg["quote_cost"],
            "quote_fee": leg["quote_fee"],
            "performance_cost_gbp": basis.decimal_text(performance_cost),
            "linked_external_flow": {
                "model": "linked-gbp-deposit-v1",
                "currency": "GBP",
                "amount": basis.decimal_text(performance_cost),
            },
            "source": {
                "evidence_model": "trade-backed-v1",
                "order_id": order_id,
                "trade_ids": leg["trade_ids"],
                "ledger_ids": leg["ledger_ids"],
            },
        }
        activities.append(_activity_hash(row))

    trade_ids = {row["id"] for row in trades.records}
    ledger_groups: dict[str, list[dict]] = {}
    for ledger in ledgers.records:
        if ledger["id"] in used_target_ledgers:
            continue
        if ledger.get("type") != "trade" or ledger.get("subtype") != "tradespot":
            continue
        refid = ledger.get("refid")
        if not isinstance(refid, str) or basis._IDENTIFIER.fullmatch(refid) is None:
            _error("paired spot ledgers lack a stable Kraken reference")
        ledger_groups.setdefault(refid, []).append(ledger)

    for refid, rows in sorted(ledger_groups.items()):
        positive_target_rows = [
            row for row in rows
            if any(
                basis._asset(row.get("asset")) == contract["asset"]
                for contract in basis.TARGETS.values()
            )
            and basis.signed_decimal(row["amount"], "paired target amount") > 0
        ]
        if not positive_target_rows:
            continue
        if refid in trade_ids or len(rows) != 2 or len(positive_target_rows) != 1:
            _error("paired spot acquisition has ambiguous Kraken ledger evidence")
        target_row = positive_target_rows[0]
        quote_rows = [
            row for row in rows
            if basis._asset(row.get("asset")) == "GBP"
            and basis.signed_decimal(row["amount"], "paired GBP amount") < 0
        ]
        if len(quote_rows) != 1 or len({row["time"] for row in rows}) != 1:
            _error("paired spot acquisition does not have one synchronized GBP leg")
        quote_row = quote_rows[0]
        target = next(
            name for name, contract in basis.TARGETS.items()
            if basis._asset(target_row.get("asset")) == contract["asset"]
        )
        gross = basis.signed_decimal(target_row["amount"], "paired target amount")
        asset_fee = basis.decimal_value(
            target_row.get("fee", 0), "paired target fee"
        )
        if asset_fee >= gross:
            _error("paired spot acquisition target fee consumes its quantity")
        quote_cost = abs(basis.signed_decimal(
            quote_row["amount"], "paired GBP amount"
        ))
        quote_fee = basis.decimal_value(quote_row.get("fee", 0), "paired GBP fee")
        performance_cost = quote_cost + quote_fee
        ledger_ids = sorted(row["id"] for row in rows)
        used_target_ledgers.update(ledger_ids)
        row = {
            "activity_version": 1,
            "activity_id": f"KRAKEN-MANUAL-BUY-{refid}",
            "kind": "buy",
            "occurred_at": basis.timestamp_text(target_row["time"]),
            "target": target,
            "asset": basis.TARGETS[target]["asset"],
            "quantity": basis.decimal_text(gross - asset_fee),
            "gross_quantity": basis.decimal_text(gross),
            "asset_fee_quantity": basis.decimal_text(asset_fee),
            "quote_currency": "GBP",
            "quote_cost": basis.decimal_text(quote_cost),
            "quote_fee": basis.decimal_text(quote_fee),
            "performance_cost_gbp": basis.decimal_text(performance_cost),
            "linked_external_flow": {
                "model": "linked-gbp-deposit-v1",
                "currency": "GBP",
                "amount": basis.decimal_text(performance_cost),
            },
            "source": {
                "evidence_model": "paired-tradespot-ledgers-v1",
                "ledger_refid": refid,
                "ledger_ids": ledger_ids,
            },
        }
        activities.append(_activity_hash(row))

    if any(
        ledger.get("type") == "trade" and ledger["id"] not in used_target_ledgers
        for ledger in ledgers.records
    ):
        _error("reviewed seam contains an unclassified trade ledger")

    for ledger in ledgers.records:
        target = next((
            name for name, contract in basis.TARGETS.items()
            if basis._asset(ledger.get("asset")) == contract["asset"]
        ), None)
        if target is None or ledger["id"] in used_target_ledgers:
            continue
        if ledger.get("type") != "withdrawal":
            _error("reviewed seam contains an unsupported target-asset movement")
        principal = basis.signed_decimal(
            ledger["amount"], "Kraken withdrawal principal"
        )
        fee = basis.decimal_value(ledger.get("fee", 0), "Kraken withdrawal fee")
        if principal >= 0:
            _error("Kraken withdrawal principal is not negative")
        total = abs(principal) + fee
        row = {
            "activity_version": 1,
            "activity_id": f"KRAKEN-MANUAL-OUT-{ledger['id']}",
            "kind": "asset_out",
            "occurred_at": basis.timestamp_text(ledger["time"]),
            "target": target,
            "asset": basis.TARGETS[target]["asset"],
            "quantity": basis.decimal_text(total),
            "principal_quantity": basis.decimal_text(abs(principal)),
            "asset_fee_quantity": basis.decimal_text(fee),
            "cost_disposition": "moving-average-pro-rata",
            "review_reason": "external-destination-ownership-unknown",
            "source": {
                "ledger_id": ledger["id"],
                "refid": ledger.get("refid"),
            },
        }
        activities.append(_activity_hash(row))
    activities.sort(key=lambda item: (item["occurred_at"], item["activity_id"]))
    if len({item["activity_id"] for item in activities}) != len(activities):
        _error("reviewed account-activity identities are duplicated")
    return activities


def _source_reference(path: str, file, commit: str, source: dict) -> dict:
    if (
        not file.exists
        or file.sha is None
        or re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", file.sha) is None
        or re.fullmatch(r"[0-9a-f]{40}", commit) is None
    ):
        _error("reviewed account-history source reference is invalid")
    return {
        "path": path,
        "blob_sha": file.sha,
        "repository_commit_sha": commit,
        "canonical_hash": source["canonical_hash"],
        "producer_commit": source["producer_commit"],
    }


def build_recovery_artifact(
    *,
    opening_source: tuple[dict, basis.AccessEvidence, basis.HistoryEvidence, basis.HistoryEvidence, basis.HistoryEvidence],
    opening_source_reference: dict,
    activity_source: tuple[dict, basis.AccessEvidence, basis.HistoryEvidence, basis.HistoryEvidence, basis.HistoryEvidence],
    activity_source_reference: dict,
    reviewed_end: basis.OpeningBinding,
) -> dict:
    _opening, _opening_access, opening_trades, opening_ledgers, opening_orders = opening_source
    activity, _activity_access, activity_trades, activity_ledgers, activity_orders = activity_source
    opening_quantities = _position_quantities(opening_ledgers)
    opening_state = _state(OPENING_MODEL, ACTIVITY_AFTER, opening_quantities)
    if opening_state["state_hash"] != REVIEWED_TRUE_OPENING_STATE_HASH:
        _error("true Kraken opening state does not match the reviewed commitment")
    costed_opening = _opening_positions(
        opening_quantities, opening_trades, opening_ledgers, opening_orders
    )
    activities = _manual_activities(
        activity_trades, activity_ledgers, activity_orders
    )

    # Interval ledgers can have negative net movement, unlike a from-zero
    # position, so retain their signed deltas.
    seam_delta = {
        target: sum((
            basis.signed_decimal(row["amount"], "reviewed seam ledger amount")
            - basis.decimal_value(row.get("fee", 0), "reviewed seam ledger fee")
            for row in activity_ledgers.records
            if basis._asset(row.get("asset")) == contract["asset"]
        ), Decimal(0))
        for target, contract in basis.TARGETS.items()
    }
    ending_quantities = {
        target: opening_quantities[target] + seam_delta[target]
        for target in basis.TARGETS
    }
    for target, expected in reviewed_end.quantities.items():
        if abs(ending_quantities[target] - expected) > basis.TARGETS[target]["tolerance"]:
            _error(f"{target} reviewed seam does not reach the bound pre-DCA state")
    end_state = _state(
        basis.OPENING_MODEL, ACTIVITY_AFTER, reviewed_end.quantities
    )
    if end_state["state_hash"] != REVIEWED_END_STATE_HASH:
        _error("reviewed pre-DCA state hash is invalid")
    deltas = [
        {
            "target": target,
            "asset": contract["asset"],
            "opening_quantity": basis.decimal_text(opening_quantities[target]),
            "activity_delta": basis.signed_decimal_text(seam_delta[target]),
            "ending_quantity": basis.decimal_text(ending_quantities[target]),
        }
        for target, contract in basis.TARGETS.items()
    ]
    artifact = {
        "version": VERSION,
        "recovery_type": RECOVERY_TYPE,
        "method": RECOVERY_METHOD,
        "generated_at": activity["generated_at"],
        "cutover_at": ACTIVITY_AFTER,
        "activity_through": ACTIVITY_THROUGH,
        "opening_source_evidence": opening_source_reference,
        "activity_source_evidence": activity_source_reference,
        "reviewed_end_binding": {
            "repository_commit_sha": reviewed_end.repository_commit_sha,
            "opening_state_hash": reviewed_end.opening_state_hash,
            "holdings": {
                "path": reviewed_end.holdings_path,
                "blob_sha": reviewed_end.holdings_blob_sha,
                "canonical_hash": reviewed_end.holdings_snapshot_hash,
            },
            "events": {
                "path": reviewed_end.events_path,
                "blob_sha": reviewed_end.events_blob_sha,
                "content_sha256": reviewed_end.events_content_sha256,
                "prefix_hash": reviewed_end.event_prefix_hash,
                "accepted_event_count": reviewed_end.accepted_event_count,
            },
        },
        "opening_state": {
            "version": opening_state["version"],
            "model": opening_state["model"],
            "state_hash": opening_state["state_hash"],
            "positions": costed_opening,
        },
        "activities": activities,
        "reconciliation": {
            "opening_state_hash": opening_state["state_hash"],
            "ending_state_hash": end_state["state_hash"],
            "positions": deltas,
            "activity_count": len(activities),
            "activity_prefix_hash": basis.canonical_hash([
                item["canonical_hash"] for item in activities
            ]),
        },
    }
    return basis._hash_artifact(artifact)


def _serialized(artifact: dict) -> str:
    if (
        not isinstance(artifact, dict)
        or re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("canonical_hash"))) is None
    ):
        _error("reviewed account-recovery artifact hash is invalid")
    unhashed = {key: value for key, value in artifact.items() if key != "canonical_hash"}
    if artifact["canonical_hash"] != basis.canonical_hash(unhashed):
        _error("reviewed account-recovery artifact hash does not match")
    content = json.dumps(artifact, sort_keys=True, indent=2, ensure_ascii=False) + "\n"
    if len(content.encode("utf-8")) > CONSUMER_MAX_BYTES:
        _error("reviewed account-recovery artifact exceeds the consumer limit")
    return content


def _publish_write_once(
    artifact: dict,
    *,
    path: str,
    filename: str,
    expected_hash: str,
    message: str,
    repository,
):
    if path.split("/")[-1] != filename:
        _error(f"reviewed account-recovery path must end with {filename}")
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(expected_hash)) is None
        or artifact.get("canonical_hash") != expected_hash
    ):
        _error("reviewed account-recovery hash does not match its preview")
    content = _serialized(artifact)
    result = repository.write_once_text(path, content, message=message)
    if result.content != content:
        _error("private repository did not confirm reviewed account recovery")
    return result


def _existing_source(repository, path: str, generated_at: str):
    current = repository.read_text(path)
    if not current.exists:
        return None
    source, *_ = parse_activity_source(current.content)
    if source["generated_at"] != generated_at:
        _error("immutable account-activity source uses a different generated_at")
    return source


def _run_source(args, repository) -> dict:
    path = configured_account_activity_source_path()
    if path.split("/")[-1] != ACCOUNT_ACTIVITY_SOURCE_FILE:
        _error(f"account-activity source path must end with {ACCOUNT_ACTIVITY_SOURCE_FILE}")
    source = _existing_source(repository, path, args.generated_at)
    if source is None:
        producer_commit = str(os.environ.get("GITHUB_SHA", "")).lower()
        access, trades, ledgers, orders = fetch_activity_source(
            get_kraken_exchange(), generated_at=args.generated_at
        )
        source = build_activity_source(
            access, trades, ledgers, orders,
            generated_at=args.generated_at,
            producer_commit=producer_commit,
        )
    _serialized(source)
    if args.mode == "publish":
        _publish_write_once(
            source,
            path=path,
            filename=ACCOUNT_ACTIVITY_SOURCE_FILE,
            expected_hash=args.expected_canonical_hash,
            message="Create immutable reviewed Kraken account-activity source",
            repository=repository,
        )
    result = {
        "stage": "source",
        "canonical_hash": source["canonical_hash"],
        "generated_at": source["generated_at"],
        "record_counts": {
            name: len(source[name]) for name in ("trades", "ledgers", "orders")
        },
    }
    if args.mode == "publish":
        result["repository_commit_sha"] = repository.resolve_commit_sha()
    return result


def _build_recovery_at_commit(repository, *, source_commit: str) -> dict:
    if re.fullmatch(r"[0-9a-f]{40}", source_commit) is None:
        _error("account-activity source commit must be a full lowercase Git SHA")
    paths = configured_outbox_paths()
    opening_path = configured_opening_basis_source_path()
    activity_path = configured_account_activity_source_path()
    opening_file = repository.read_text_at_commit(
        opening_path, REVIEWED_OPENING_SOURCE_REPOSITORY_COMMIT_SHA
    )
    activity_file = repository.read_text_at_commit(activity_path, source_commit)
    holdings_file = repository.read_text_at_commit(
        paths.holdings, REVIEWED_END_REPOSITORY_COMMIT_SHA
    )
    events_file = repository.read_text_at_commit(
        paths.event, REVIEWED_END_REPOSITORY_COMMIT_SHA
    )
    if not all(item.exists and item.sha for item in (
        opening_file, activity_file, holdings_file, events_file
    )):
        _error("reviewed account recovery lacks commit-pinned evidence")
    try:
        opening = basis.parse_source_artifact(opening_file.content)
        reviewed_end = basis.derive_opening_binding(
            holdings_file.content,
            events_file.content,
            repository_commit_sha=REVIEWED_END_REPOSITORY_COMMIT_SHA,
            holdings_path=paths.holdings,
            holdings_blob_sha=holdings_file.sha,
            events_path=paths.event,
            events_blob_sha=events_file.sha,
        )
    except basis.OpeningBasisError as error:
        _error("reviewed opening evidence is invalid", error)
    activity = parse_activity_source(activity_file.content)
    return build_recovery_artifact(
        opening_source=opening,
        opening_source_reference=_source_reference(
            opening_path,
            opening_file,
            REVIEWED_OPENING_SOURCE_REPOSITORY_COMMIT_SHA,
            opening[0],
        ),
        activity_source=activity,
        activity_source_reference=_source_reference(
            activity_path, activity_file, source_commit, activity[0]
        ),
        reviewed_end=reviewed_end,
    )


def _recovery_summary(artifact: dict, *, source_commit: str) -> dict:
    return {
        "stage": "recovery",
        "canonical_hash": artifact["canonical_hash"],
        "generated_at": artifact["generated_at"],
        "source_repository_commit_sha": source_commit,
        "opening_state_hash": artifact["opening_state"]["state_hash"],
        "ending_state_hash": artifact["reconciliation"]["ending_state_hash"],
        "opening_positions": [
            {
                "target": item["target"],
                "quantity": item["quantity"],
                "performance_cost_gbp": item["performance_cost_gbp"],
                "coverage": "complete",
            }
            for item in artifact["opening_state"]["positions"]
        ],
        "activity_count": len(artifact["activities"]),
        "activities": [
            ({
                "activity_id": item["activity_id"],
                "kind": item["kind"],
                "target": item["target"],
                "quantity": item["quantity"],
                "quote_cost": item["quote_cost"],
                "quote_fee": item["quote_fee"],
                "performance_cost_gbp": item["performance_cost_gbp"],
                "evidence_model": item["source"]["evidence_model"],
            } if item["kind"] == "buy" else {
                "activity_id": item["activity_id"],
                "kind": item["kind"],
                "target": item["target"],
                "quantity": item["quantity"],
                "principal_quantity": item["principal_quantity"],
                "asset_fee_quantity": item["asset_fee_quantity"],
            })
            for item in artifact["activities"]
        ],
    }


def _run_recovery(args, repository) -> dict:
    path = configured_account_recovery_path()
    if path.split("/")[-1] != ACCOUNT_RECOVERY_FILE:
        _error(f"account-recovery path must end with {ACCOUNT_RECOVERY_FILE}")
    source_commit = args.source_commit_sha
    artifact = _build_recovery_at_commit(repository, source_commit=source_commit)
    if artifact["generated_at"] != args.generated_at:
        _error("account-recovery generated_at must match its source")
    _serialized(artifact)
    if args.mode == "publish":
        _publish_write_once(
            artifact,
            path=path,
            filename=ACCOUNT_RECOVERY_FILE,
            expected_hash=args.expected_canonical_hash,
            message="Create immutable reviewed Kraken account recovery",
            repository=repository,
        )
    return _recovery_summary(artifact, source_commit=source_commit)


def _arguments(argv=None):
    parser = argparse.ArgumentParser(
        description="Build immutable reviewed Kraken account recovery evidence"
    )
    parser.add_argument("--stage", choices=("source", "recovery"), required=True)
    parser.add_argument("--mode", choices=("preview", "publish"), required=True)
    parser.add_argument("--generated-at", required=True)
    parser.add_argument("--expected-canonical-hash")
    parser.add_argument("--source-commit-sha")
    args = parser.parse_args(argv)
    if args.mode == "publish" and not args.expected_canonical_hash:
        parser.error("publish mode requires --expected-canonical-hash")
    if args.stage == "source" and args.source_commit_sha:
        parser.error("source stage does not accept --source-commit-sha")
    if args.stage == "recovery" and not args.source_commit_sha:
        parser.error("recovery stage requires --source-commit-sha")
    return args


def main(argv=None) -> int:
    try:
        args = _arguments(argv)
        basis.utc_timestamp(args.generated_at, "generated_at")
        ensure_snapshot_safe_execution_state(os.environ["DCA_EXECUTION_STATE"])
        repository = GitHubContentsClient.from_env()
        result = (
            _run_source(args, repository)
            if args.stage == "source"
            else _run_recovery(args, repository)
        )
        print(json.dumps(result, sort_keys=True))
        return 0
    except (AccountRecoveryError, basis.OpeningBasisError) as error:
        print(f"Reviewed Kraken account recovery refused: {error}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

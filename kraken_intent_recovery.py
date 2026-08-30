"""Manually reviewed recovery of old, absent direct-GBP order intents.

No order creation/cancellation endpoint is used. Preview is read-only. Apply
requires shadow mode, the exact reviewed state hash, a fresh complete Kraken
audit, and an unchanged execution state under the workflow writer lock.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
from datetime import datetime, timedelta, timezone

import crypto_dca
from dca_config import TARGET_ROUTES, parse_utc_iso, validate_execution_state
from ghostfolio.kraken_opening_basis import ensure_history_permissions
from kraken_client import build_client_order_id, get_kraken_exchange, to_kraken_symbol


MINIMUM_AGE = timedelta(hours=24)
MAX_PAGES = 100


class RecoveryError(RuntimeError):
    """Safe diagnostic that contains no raw account response."""


def state_hash(state: dict) -> str:
    return hashlib.sha256(json.dumps(
        validate_execution_state(state), sort_keys=True,
        separators=(",", ":"), allow_nan=False
    ).encode()).hexdigest()


def _result(response, container: str):
    if (
        not isinstance(response, dict) or response.get("error") != []
        or not isinstance(response.get("result"), dict)
        or not isinstance(response["result"].get(container), dict)
    ):
        raise RecoveryError(f"Kraken {container} response is incomplete")
    return response["result"]


def _closed_history(exchange, start: int, end: int) -> list[dict]:
    """Fixed close-time interval with native count-checked account-wide pages."""
    records = []
    seen = set()
    count = None
    for _page in range(MAX_PAGES):
        result = _result(exchange.privatePostClosedOrders({
            "start": start, "end": end, "closetime": "close",
            "ofs": len(records), "trades": "false",
        }), "closed")
        page_count = result.get("count")
        rows = result["closed"]
        if type(page_count) is not int or page_count < 0:
            raise RecoveryError("Kraken closed-order count is unavailable")
        if count is None:
            count = page_count
        if count != page_count or len(rows) > 50:
            raise RecoveryError("Kraken closed-order pagination changed")
        for order_id, row in rows.items():
            if not isinstance(order_id, str) or not order_id or order_id in seen:
                raise RecoveryError("Kraken closed-order page repeats an identifier")
            if not isinstance(row, dict):
                raise RecoveryError("Kraken closed-order row is malformed")
            seen.add(order_id)
            records.append({**row, "id": order_id})
        if len(records) == count:
            return records
        if not rows or len(records) > count:
            raise RecoveryError("Kraken closed-order history is incomplete")
    raise RecoveryError("Kraken closed-order pagination exceeded its safety limit")


def _open_orders(exchange, params=None) -> list[dict]:
    rows = _result(exchange.privatePostOpenOrders(params or {}), "open")["open"]
    if any(not isinstance(row, dict) or not order_id for order_id, row in rows.items()):
        raise RecoveryError("Kraken open-order row is malformed")
    return [{**row, "id": order_id} for order_id, row in rows.items()]


def _assert_shadow():
    url, _collection, headers = crypto_dca._github_variable_context("DCA_TRADING_MODE")
    response = crypto_dca.requests.get(url, headers=headers, timeout=15)
    if response.status_code != 200 or response.json().get("value") != "shadow":
        raise RecoveryError("Recovery requires live DCA_TRADING_MODE=shadow")


def audit_absent_intents(state: dict, exchange, *, now=None) -> dict:
    state = validate_execution_state(state)
    now = now or datetime.now(timezone.utc)
    pending = {
        symbol: entry["PENDING_ORDER"] for symbol, entry in state.items()
        if entry.get("PENDING_ORDER") is not None
    }
    if not pending:
        raise RecoveryError("There are no pending intents to recover")
    for symbol, intent in pending.items():
        created = parse_utc_iso(intent["created_at"])
        if TARGET_ROUTES[symbol] != "DIRECT_GBP":
            raise RecoveryError("Only direct-GBP intents can use absence recovery")
        if now - created < MINIMUM_AGE:
            raise RecoveryError(f"{symbol} intent is less than 24 hours old")
        if intent["client_order_id"] != build_client_order_id(symbol, intent["trade_date"]):
            raise RecoveryError(f"{symbol} intent does not match its deterministic identity")
        if intent["funding_client_order_id"] != build_client_order_id(symbol, intent["trade_date"], purpose="funding"):
            raise RecoveryError(f"{symbol} funding identity is inconsistent")
        if created.astimezone(crypto_dca.SELECTED_TZ).date().isoformat() != intent["trade_date"]:
            raise RecoveryError(f"{symbol} intent creation date is inconsistent")
    stamp = now.isoformat().replace("+00:00", "Z")
    # This also rejects restricted historical query windows or expired keys.
    ensure_history_permissions(exchange, cutover_at=stamp, generated_at=stamp)
    exchange.load_markets()
    start = int(min(parse_utc_iso(i["created_at"]).timestamp() for i in pending.values())) - 60
    closed = _closed_history(exchange, start, int(now.timestamp()))
    opened = _open_orders(exchange)
    parsed = [exchange.parse_order(row) for row in [*closed, *opened]]
    results = {}
    for symbol, intent in pending.items():
        pair = to_kraken_symbol(symbol)
        created_ms = parse_utc_iso(intent["created_at"]).timestamp() * 1000
        matching = 0
        nearby = 0
        for order in parsed:
            if not isinstance(order, dict) or not order.get("id") or not order.get("symbol"):
                raise RecoveryError("Kraken order identity could not be parsed")
            if order.get("clientOrderId") in {
                intent["client_order_id"], intent["funding_client_order_id"]
            }:
                matching += 1
            if order["symbol"] == pair:
                timestamp = order.get("timestamp")
                if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
                    raise RecoveryError(f"{symbol} history has an unknown order time")
                # Even an untagged/mistagged order near submission needs review.
                if created_ms - 60_000 <= timestamp <= created_ms + 900_000:
                    nearby += 1
        # Fresh exact native lookups after the full scan catch state transitions
        # and bypass CCXT's client-side symbol filtering entirely.
        for client_id in (intent["client_order_id"], intent["funding_client_order_id"]):
            matching += len(_open_orders(exchange, {"cl_ord_id": client_id}))
            exact = _result(exchange.privatePostClosedOrders({"cl_ord_id": client_id}), "closed")
            if type(exact.get("count")) is not int or exact["count"] != len(exact["closed"]):
                raise RecoveryError("Kraken exact-ID history is incomplete")
            matching += len(exact["closed"])
        results[symbol] = {
            "trade_date": intent["trade_date"],
            "matching_order_observations": matching,
            "nearby_order_observations": nearby,
            "confirmed_absent": matching == 0 and nearby == 0,
        }
    balance = exchange.fetch_balance()
    free_gbp = (balance.get("free") or {}).get("GBP")
    if not isinstance(free_gbp, (int, float)) or not math.isfinite(free_gbp) or free_gbp < 0:
        raise RecoveryError("Kraken free GBP balance is unavailable")
    return {
        "state_hash": state_hash(state), "targets": results,
        "closed_orders_scanned": len(closed), "open_orders_scanned": len(opened),
        "gbp_covers_combined_pending_budgets": free_gbp >= sum(i["amount_gbp"] for i in pending.values()),
        "all_confirmed_absent": all(row["confirmed_absent"] for row in results.values()),
    }


def recover(*, mode="preview", expected_state_hash="", exchange=None) -> dict:
    if mode not in {"preview", "apply"}:
        raise RecoveryError("Unknown recovery mode")
    _assert_shadow()
    state = crypto_dca.fetch_live_execution_state()
    if mode == "apply" and expected_state_hash != state_hash(state):
        raise RecoveryError("Execution state differs from the reviewed preview")
    report = audit_absent_intents(state, exchange or get_kraken_exchange())
    if mode == "preview":
        return report
    if not report["all_confirmed_absent"]:
        raise RecoveryError("Kraken order evidence prevents clearing an intent")
    _assert_shadow()
    if crypto_dca.fetch_live_execution_state() != state:
        raise RecoveryError("Execution state changed during the Kraken audit")
    updated = copy.deepcopy(state)
    for symbol in report["targets"]:
        updated[symbol].pop("PENDING_ORDER")
    crypto_dca._write_repo_json_variable(crypto_dca.EXECUTION_STATE_VARIABLE, updated, exists=True)
    if crypto_dca.fetch_live_execution_state() != updated:
        raise RecoveryError("Recovery write could not be verified; inspect live state")
    report["applied"] = True
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("preview", "apply"), default="preview")
    parser.add_argument("--expected-state-hash", default="")
    args = parser.parse_args()
    try:
        report = recover(mode=args.mode, expected_state_hash=args.expected_state_hash)
    except Exception as error:
        # Raw Kraken/HTTP exceptions can contain account data. Log only our own
        # reviewed errors; chaining is deliberately not printed by this CLI.
        message = str(error) if isinstance(error, RecoveryError) else type(error).__name__
        raise SystemExit(f"Pending-intent recovery stopped safely: {message}") from None
    print(json.dumps(report, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()

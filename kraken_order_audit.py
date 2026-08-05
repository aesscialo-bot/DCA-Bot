"""Read-only audit for unresolved or same-day Kraken DCA bot orders.

The command prints only aggregate counts and short order-ID suffixes.  It never
calls an order creation endpoint and never emits credentials, balances, amounts,
or complete exchange order payloads.
"""

import json
import re
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from kraken_client import _order_client_id, get_kraken_exchange, to_kraken_symbol


TARGETS = ("BTC_GBP", "ETH_GBP", "SOL_GBP", "ADA_GBP")
CLIENT_ORDER_ID_PATTERN = re.compile(r"^dca-[0-9a-f]{14}$")
TIMEZONE_NAME = "Asia/Bangkok"
CLOSED_ORDER_PAGE_SIZE = 50
MAX_CLOSED_ORDER_PAGES = 100


def _is_bot_order(order: dict) -> bool:
    client_order_id = _order_client_id(order)
    return bool(client_order_id and CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id))


def _order_suffix(order: dict) -> str:
    order_id = str(order.get("id") or "")
    return order_id[-6:] if order_id else "unknown"


def _terminal_order_local_days(order: dict, selected_tz: ZoneInfo) -> set:
    """Return every validated local day associated with a terminal order.

    CCXT maps Kraken ``opentm`` to ``timestamp`` and ``closetm`` to
    ``lastUpdateTimestamp``.  The native close time is retained as seconds in
    ``info.closetm``.  Checking all three prevents an order opened just before
    Bangkok midnight and filled just after it from being mistaken for an old
    order.
    """

    info = order.get("info")
    native_close = info.get("closetm") if isinstance(info, dict) else None
    candidates = (
        (order.get("timestamp"), 1000),
        (order.get("lastUpdateTimestamp"), 1000),
        (native_close, 1),
    )
    local_days = set()
    for raw_timestamp, divisor in candidates:
        if raw_timestamp is None:
            continue
        try:
            timestamp = float(raw_timestamp) / divisor
            local_days.add(
                datetime.fromtimestamp(timestamp, tz=timezone.utc)
                .astimezone(selected_tz)
                .date()
            )
        except (TypeError, ValueError, OSError, OverflowError):
            continue
    return local_days


def _same_local_day(order: dict, local_day, selected_tz: ZoneInfo) -> bool:
    return local_day in _terminal_order_local_days(order, selected_tz)


def _has_valid_timestamp(order: dict) -> bool:
    return bool(_terminal_order_local_days(order, ZoneInfo(TIMEZONE_NAME)))


def _fetch_closed_orders_paginated(client, since_ms: int) -> list[dict]:
    """Fetch Kraken's account-wide closed orders without trusting one page.

    Kraken's native ClosedOrders endpoint is account-wide and uses ``ofs``
    pagination.  Passing a symbol to CCXT can label/filter the native page
    before the audit sees it, so fetch each native page once and group the
    parsed unified symbols locally.  IDs are mandatory because they are the
    only safe way to detect repeated pages and deduplicate moving history.
    """

    orders: list[dict] = []
    seen_ids: set[str] = set()
    offset = 0

    for _page_number in range(MAX_CLOSED_ORDER_PAGES):
        page = client.fetch_closed_orders(
            None,
            limit=CLOSED_ORDER_PAGE_SIZE,
            params={
                "ofs": offset,
                # Keep the filtering native and close-time based. Supplying
                # CCXT's ``since`` argument would filter parsed orders again by
                # ``timestamp`` (Kraken opentm), dropping cross-midnight fills.
                "start": since_ms // 1000,
                "closetime": "close",
            },
        ) or []
        if not isinstance(page, list):
            raise RuntimeError("Kraken closed-order history was not a list")
        if len(page) > CLOSED_ORDER_PAGE_SIZE:
            raise RuntimeError("Kraken closed-order page exceeded the requested limit")
        if not page:
            return orders

        new_orders = 0
        for order in page:
            if not isinstance(order, dict):
                raise RuntimeError("Kraken returned an invalid closed order")
            order_id = order.get("id")
            if order_id is None or not str(order_id).strip():
                raise RuntimeError("Kraken returned a closed order without an ID")
            normalized_id = str(order_id)
            if normalized_id in seen_ids:
                continue
            seen_ids.add(normalized_id)
            orders.append(order)
            new_orders += 1

        if new_orders == 0:
            raise RuntimeError("Kraken closed-order pagination made no progress")
        if len(page) < CLOSED_ORDER_PAGE_SIZE:
            return orders
        offset += CLOSED_ORDER_PAGE_SIZE

    raise RuntimeError("Kraken closed-order pagination exceeded its safety limit")


def audit_orders(exchange=None, *, now=None) -> dict:
    """Query open and today's closed bot orders for the four production markets."""
    selected_tz = ZoneInfo(TIMEZONE_NAME)
    local_now = now.astimezone(selected_tz) if now else datetime.now(selected_tz)
    local_day = local_now.date()
    day_start = datetime.combine(local_day, time.min, tzinfo=selected_tz)
    since_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
    client = exchange or get_kraken_exchange()
    closed_orders = _fetch_closed_orders_paginated(client, since_ms)

    markets = {}
    unresolved_total = 0
    same_day_total = 0
    unknown_closed_timestamp_total = 0
    for target in TARGETS:
        symbol = to_kraken_symbol(target)
        open_orders = client.fetch_open_orders(symbol) or []
        market_closed_orders = [
            order for order in closed_orders if order.get("symbol") == symbol
        ]
        unresolved = [order for order in open_orders if _is_bot_order(order)]
        closed_bot_orders = [
            order for order in market_closed_orders if _is_bot_order(order)
        ]
        same_day = [
            order
            for order in closed_bot_orders
            if _same_local_day(order, local_day, selected_tz)
        ]
        unknown_timestamp = [
            order
            for order in closed_bot_orders
            if not _same_local_day(order, local_day, selected_tz)
            and not _has_valid_timestamp(order)
        ]
        unresolved_total += len(unresolved)
        same_day_total += len(same_day)
        unknown_closed_timestamp_total += len(unknown_timestamp)
        markets[target] = {
            "unresolved_count": len(unresolved),
            "same_day_closed_count": len(same_day),
            "unknown_timestamp_closed_count": len(unknown_timestamp),
            "unresolved_order_id_suffixes": sorted(
                {_order_suffix(order) for order in unresolved}
            ),
            "same_day_order_id_suffixes": sorted(
                {_order_suffix(order) for order in same_day}
            ),
            "unknown_timestamp_order_id_suffixes": sorted(
                {_order_suffix(order) for order in unknown_timestamp}
            ),
        }

    return {
        "audit_date": local_day.isoformat(),
        "timezone": TIMEZONE_NAME,
        "unresolved_bot_orders": unresolved_total,
        "same_day_closed_bot_orders": same_day_total,
        "unknown_timestamp_closed_bot_orders": unknown_closed_timestamp_total,
        "safe_to_initialize_empty_execution_state": (
            unresolved_total == 0
            and same_day_total == 0
            and unknown_closed_timestamp_total == 0
        ),
        "markets": markets,
    }


def main() -> int:
    try:
        result = audit_orders()
    except Exception as error:
        # The exception type and message can contain provider internals.  Keep the
        # public output deliberately generic and fail closed.
        print(
            json.dumps(
                {
                    "status": "ERROR",
                    "safe_to_initialize_empty_execution_state": False,
                    "message": "Kraken order audit could not be completed.",
                    "error_type": type(error).__name__,
                },
                sort_keys=True,
            )
        )
        return 1

    print(json.dumps({"status": "OK", **result}, sort_keys=True))
    return 0 if result["safe_to_initialize_empty_execution_state"] else 2


if __name__ == "__main__":
    raise SystemExit(main())

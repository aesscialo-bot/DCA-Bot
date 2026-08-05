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


def _is_bot_order(order: dict) -> bool:
    client_order_id = _order_client_id(order)
    return bool(client_order_id and CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id))


def _order_suffix(order: dict) -> str:
    order_id = str(order.get("id") or "")
    return order_id[-6:] if order_id else "unknown"


def _same_local_day(order: dict, local_day, selected_tz: ZoneInfo) -> bool:
    timestamp_ms = order.get("timestamp")
    if timestamp_ms is None:
        return False
    try:
        timestamp = float(timestamp_ms) / 1000
    except (TypeError, ValueError):
        return False
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).astimezone(
        selected_tz
    ).date() == local_day


def audit_orders(exchange=None, *, now=None) -> dict:
    """Query open and today's closed bot orders for the four production markets."""
    selected_tz = ZoneInfo(TIMEZONE_NAME)
    local_now = now.astimezone(selected_tz) if now else datetime.now(selected_tz)
    local_day = local_now.date()
    day_start = datetime.combine(local_day, time.min, tzinfo=selected_tz)
    since_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
    client = exchange or get_kraken_exchange()

    markets = {}
    unresolved_total = 0
    same_day_total = 0
    for target in TARGETS:
        symbol = to_kraken_symbol(target)
        open_orders = client.fetch_open_orders(symbol) or []
        closed_orders = client.fetch_closed_orders(symbol, since=since_ms) or []
        unresolved = [order for order in open_orders if _is_bot_order(order)]
        same_day = [
            order
            for order in closed_orders
            if _is_bot_order(order)
            and _same_local_day(order, local_day, selected_tz)
        ]
        unresolved_total += len(unresolved)
        same_day_total += len(same_day)
        markets[target] = {
            "unresolved_count": len(unresolved),
            "same_day_closed_count": len(same_day),
            "unresolved_order_id_suffixes": sorted(
                {_order_suffix(order) for order in unresolved}
            ),
            "same_day_order_id_suffixes": sorted(
                {_order_suffix(order) for order in same_day}
            ),
        }

    return {
        "audit_date": local_day.isoformat(),
        "timezone": TIMEZONE_NAME,
        "unresolved_bot_orders": unresolved_total,
        "same_day_closed_bot_orders": same_day_total,
        "safe_to_initialize_empty_execution_state": (
            unresolved_total == 0 and same_day_total == 0
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

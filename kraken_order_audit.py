"""Read-only audit for unresolved or same-day Kraken DCA bot orders.

The command prints only aggregate counts and short order-ID suffixes.  It never
calls an order creation endpoint and never emits credentials, balances, amounts,
or complete exchange order payloads.
"""

import json
import math
import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from types import MappingProxyType
from typing import Mapping
from zoneinfo import ZoneInfo

from kraken_client import (
    _order_client_id,
    build_client_order_id,
    get_kraken_exchange,
    to_kraken_symbol,
)


@dataclass(frozen=True)
class AuditProfile:
    """Fixed internal contract for deterministic DCA flow validation."""

    name: str
    routes: Mapping[str, str]
    require_successful_fills: bool = False


ACTIVE_AUDIT_PROFILE = AuditProfile(
    "ACTIVE_ETH_GBP",
    MappingProxyType(
        {
            "BTC_GBP": "DIRECT_GBP",
            "ETH_GBP": "DIRECT_GBP",
            "SOL_GBP": "DIRECT_GBP",
        }
    ),
)
HYPE_TO_ETH_SOURCE_AUDIT_PROFILE = AuditProfile(
    "HYPE_TO_ETH_SOURCE",
    MappingProxyType(
        {
            "BTC_GBP": "DIRECT_GBP",
            "HYPE_USD": "GBP_TO_USD",
            "SOL_GBP": "DIRECT_GBP",
        }
    ),
    require_successful_fills=True,
)
AUDIT_PROFILES = (
    ACTIVE_AUDIT_PROFILE,
    HYPE_TO_ETH_SOURCE_AUDIT_PROFILE,
)
TARGETS = tuple(ACTIVE_AUDIT_PROFILE.routes)
FUNDING_MARKET = "GBP_USD"
LEGACY_AUDIT_MARKETS = ("HYPE_USD", FUNDING_MARKET)
AUDIT_MARKETS = (*TARGETS, *LEGACY_AUDIT_MARKETS)
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


def _expected_leg_lookup(
    local_day, profile: AuditProfile
) -> dict[str, dict[str, str]]:
    """Map deterministic IDs to their target and leg without exposing the IDs.

    Include the previous local date because an order may open shortly before
    Bangkok midnight and close shortly after it. Older or otherwise unrecognized
    bot IDs remain visible as mismatched legs and make the audit fail closed.
    """

    lookup = {}
    for trade_day in (local_day - timedelta(days=1), local_day):
        for target in profile.routes:
            legs = {"crypto": (target, "buy")}
            if profile.routes[target] == "GBP_TO_USD":
                legs["funding"] = (FUNDING_MARKET, "funding")
            for leg_name, (market_key, purpose) in legs.items():
                client_order_id = build_client_order_id(
                    target, trade_day, purpose=purpose
                )
                if client_order_id in lookup:
                    raise RuntimeError("Deterministic Kraken client order ID collision")
                lookup[client_order_id] = {
                    "target": target,
                    "trade_date": trade_day.isoformat(),
                    "leg": leg_name,
                    "symbol": to_kraken_symbol(market_key),
                }
    return lookup


def _successful_terminal_fill(order: dict) -> bool:
    if str(order.get("status") or "").strip().lower() != "closed":
        return False
    for field in ("filled", "cost"):
        if isinstance(order.get(field), bool):
            return False
        try:
            value = float(order.get(field))
        except (TypeError, ValueError):
            return False
        if not math.isfinite(value) or value <= 0:
            return False
    return True


def _flow_summary(
    orders: list[dict], local_day, profile: AuditProfile
) -> dict:
    """Classify bot order legs into complete, incomplete, or invalid flows."""

    expected_legs = _expected_leg_lookup(local_day, profile)
    flows: dict[tuple[str, str], dict[str, list[dict]]] = {}
    mismatched = []
    invalid_terminal = []
    for order in orders:
        expected = expected_legs.get(_order_client_id(order))
        if expected is None or order.get("symbol") != expected["symbol"]:
            mismatched.append(order)
            continue
        if profile.require_successful_fills and not _successful_terminal_fill(order):
            invalid_terminal.append(order)
            continue
        flow_key = (expected["target"], expected["trade_date"])
        flow = flows.setdefault(flow_key, {"funding": [], "crypto": []})
        flow[expected["leg"]].append(order)

    complete_flow_count = 0
    incomplete_flow_count = 0
    duplicate_leg_count = 0
    completed_flows = []
    for (target, _trade_date), flow in flows.items():
        funding_count = len(flow["funding"])
        crypto_count = len(flow["crypto"])
        duplicate_leg_count += max(0, funding_count - 1)
        duplicate_leg_count += max(0, crypto_count - 1)
        funding_required = profile.routes[target] == "GBP_TO_USD"
        if crypto_count == 1 and funding_count == (1 if funding_required else 0):
            complete_flow_count += 1
            completed_flows.append(
                {"target": target, "trade_date": _trade_date}
            )
        elif funding_count or crypto_count:
            incomplete_flow_count += 1

    return {
        "complete_flow_count": complete_flow_count,
        "incomplete_flow_count": incomplete_flow_count,
        "duplicate_leg_count": duplicate_leg_count,
        "mismatched_leg_count": len(mismatched),
        "invalid_terminal_leg_count": len(invalid_terminal),
        "completed_flows": sorted(
            completed_flows,
            key=lambda flow: (flow["trade_date"], flow["target"]),
        ),
        "mismatched_order_id_suffixes": sorted(
            {_order_suffix(order) for order in mismatched}
        ),
        "invalid_terminal_order_id_suffixes": sorted(
            {_order_suffix(order) for order in invalid_terminal}
        ),
        "integrity_ok": (
            incomplete_flow_count == 0
            and duplicate_leg_count == 0
            and not mismatched
            and not invalid_terminal
        ),
    }


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


def audit_orders(
    exchange=None,
    *,
    now=None,
    profile: AuditProfile = ACTIVE_AUDIT_PROFILE,
) -> dict:
    """Read account-wide order state for the three targets and their FX leg."""
    if not any(profile is candidate for candidate in AUDIT_PROFILES):
        raise ValueError("unsupported Kraken order audit profile")
    selected_tz = ZoneInfo(TIMEZONE_NAME)
    local_now = now.astimezone(selected_tz) if now else datetime.now(selected_tz)
    local_day = local_now.date()
    day_start = datetime.combine(local_day, time.min, tzinfo=selected_tz)
    since_ms = int(day_start.astimezone(timezone.utc).timestamp() * 1000)
    client = exchange or get_kraken_exchange()
    closed_orders = _fetch_closed_orders_paginated(client, since_ms)
    open_orders = client.fetch_open_orders(None) or []
    if not isinstance(open_orders, list):
        raise RuntimeError("Kraken open-order state was not a list")

    open_bot_orders = [order for order in open_orders if _is_bot_order(order)]
    closed_bot_orders = [order for order in closed_orders if _is_bot_order(order)]
    same_day_bot_orders = [
        order
        for order in closed_bot_orders
        if _same_local_day(order, local_day, selected_tz)
    ]
    unknown_timestamp_orders = [
        order for order in closed_bot_orders if not _has_valid_timestamp(order)
    ]
    open_flow_summary = _flow_summary(open_bot_orders, local_day, profile)
    closed_flow_summary = _flow_summary(same_day_bot_orders, local_day, profile)

    markets = {}
    for target in AUDIT_MARKETS:
        symbol = to_kraken_symbol(target)
        market_closed_orders = [
            order for order in closed_orders if order.get("symbol") == symbol
        ]
        unresolved = [
            order
            for order in open_bot_orders
            if order.get("symbol") == symbol
        ]
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

    unresolved_total = len(open_bot_orders)
    same_day_total = len(same_day_bot_orders)
    unknown_closed_timestamp_total = len(unknown_timestamp_orders)
    known_symbols = {to_kraken_symbol(target) for target in AUDIT_MARKETS}
    unexpected_market_orders = [
        order
        for order in (*open_bot_orders, *same_day_bot_orders)
        if order.get("symbol") not in known_symbols
    ]
    flow_integrity_ok = (
        open_flow_summary["integrity_ok"]
        and closed_flow_summary["integrity_ok"]
        and not unexpected_market_orders
    )
    completed_by_target = {
        target: sum(
            flow["target"] == target
            for flow in closed_flow_summary["completed_flows"]
        )
        for target in profile.routes
    }
    completed_dates_by_target = {
        target: sorted(
            flow["trade_date"]
            for flow in closed_flow_summary["completed_flows"]
            if flow["target"] == target
        )
        for target in profile.routes
    }
    safe_for_target_migration = (
        unresolved_total == 0
        and unknown_closed_timestamp_total == 0
        and flow_integrity_ok
    )

    return {
        "audit_profile": profile.name,
        "audit_date": local_day.isoformat(),
        "timezone": TIMEZONE_NAME,
        "unresolved_bot_orders": unresolved_total,
        "same_day_closed_bot_orders": same_day_total,
        "unknown_timestamp_closed_bot_orders": unknown_closed_timestamp_total,
        "same_day_completed_dca_flows": closed_flow_summary[
            "complete_flow_count"
        ],
        "same_day_incomplete_dca_flows": closed_flow_summary[
            "incomplete_flow_count"
        ],
        "same_day_duplicate_order_legs": closed_flow_summary[
            "duplicate_leg_count"
        ],
        "same_day_mismatched_order_legs": closed_flow_summary[
            "mismatched_leg_count"
        ],
        "same_day_invalid_terminal_order_legs": closed_flow_summary[
            "invalid_terminal_leg_count"
        ],
        "same_day_completed_dca_flows_by_target": completed_by_target,
        "same_day_completed_dca_flow_dates_by_target": completed_dates_by_target,
        "unresolved_incomplete_dca_flows": open_flow_summary[
            "incomplete_flow_count"
        ],
        "unresolved_duplicate_order_legs": open_flow_summary[
            "duplicate_leg_count"
        ],
        "unresolved_mismatched_order_legs": open_flow_summary[
            "mismatched_leg_count"
        ],
        "unresolved_invalid_terminal_order_legs": open_flow_summary[
            "invalid_terminal_leg_count"
        ],
        "unexpected_market_bot_order_legs": len(unexpected_market_orders),
        "flow_integrity_ok": flow_integrity_ok,
        "safe_for_target_migration": safe_for_target_migration,
        "safe_to_initialize_empty_execution_state": (
            unresolved_total == 0
            and same_day_total == 0
            and unknown_closed_timestamp_total == 0
            and flow_integrity_ok
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

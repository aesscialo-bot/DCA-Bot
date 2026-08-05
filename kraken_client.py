"""GBP-native Kraken spot client used by the DCA trade executor."""

import hashlib
import math
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import ccxt


QUOTE_CURRENCY = "GBP"
MIN_DCA_GBP = 5.0
MAX_DCA_GBP = 1000.0
_CONFIG_SYMBOL_PATTERN = re.compile(r"^[A-Z0-9]+(?:_GBP|/GBP)$")
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^dca-[0-9a-f]{14}$")
_TERMINAL_ORDER_STATUSES = frozenset({"closed", "canceled", "expired", "rejected"})
ORDER_POLL_DELAYS_SECONDS = (1, 2, 4, 8, 10)
SUBMISSION_RECONCILE_DELAYS_SECONDS = (1, 2, 4)
ORDER_SUBMISSION_DEADLINE_SECONDS = 15


class KrakenOrderStateUnknown(RuntimeError):
    """Raised when Kraken may have accepted an order but its outcome is unknown."""


class KrakenOrderNoFill(RuntimeError):
    """Raised only for a confirmed terminal order with no executed quantity."""


class KrakenPreSubmissionError(RuntimeError):
    """Raised when Kraken definitely received no AddOrder request in this run."""


def get_kraken_exchange():
    """Create an authenticated CCXT Kraken spot client."""
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if not api_key or not api_secret:
        raise ValueError("Missing KRAKEN_API_KEY or KRAKEN_API_SECRET")

    return ccxt.kraken(
        {
            "apiKey": api_key,
            "secret": api_secret,
            "enableRateLimit": True,
            "options": {"defaultType": "spot"},
        }
    )


def to_kraken_symbol(config_symbol: str) -> str:
    """Convert a strict COIN_GBP or COIN/GBP key to CCXT's COIN/GBP form."""
    if not isinstance(config_symbol, str):
        raise ValueError("Kraken trading symbols must be strings ending in _GBP")

    normalized = config_symbol.strip().upper()
    if not _CONFIG_SYMBOL_PATTERN.fullmatch(normalized):
        raise ValueError(
            "Only GBP Kraken pairs are supported; use COIN_GBP or COIN/GBP"
        )

    base = normalized.replace("_", "/").split("/", maxsplit=1)[0]
    return f"{base}/{QUOTE_CURRENCY}"


def build_client_order_id(config_symbol: str, trade_date=None) -> str:
    """Build a deterministic Kraken client ID for one market and local trade day."""
    symbol = to_kraken_symbol(config_symbol)
    if trade_date is None:
        timezone_name = os.environ.get("TIMEZONE", "Asia/Bangkok")
        date_key = datetime.now(ZoneInfo(timezone_name)).date().isoformat()
    elif isinstance(trade_date, datetime):
        date_key = trade_date.date().isoformat()
    elif isinstance(trade_date, date):
        date_key = trade_date.isoformat()
    else:
        try:
            date_key = date.fromisoformat(str(trade_date)).isoformat()
        except ValueError as error:
            raise ValueError("trade_date must be an ISO date such as 2026-08-04") from error

    digest = hashlib.sha256(f"{symbol}|{date_key}".encode("utf-8")).hexdigest()[:14]
    return f"dca-{digest}"


def validate_kraken_credentials() -> dict:
    """Authenticate without trading and return non-sensitive GBP availability."""
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()
    return {
        "exchange": exchange.id,
        "quote_currency": QUOTE_CURRENCY,
        "quote_available": float(balance.get("free", {}).get(QUOTE_CURRENCY, 0)),
    }


def get_market_minimum_gbp(config_symbol: str, *, exchange=None) -> dict:
    """Return Kraken's current effective GBP minimum without creating an order.

    Kraken can publish both a quote-cost minimum and a base-amount minimum.  The
    effective cash minimum is the larger of those two values at the current ask.
    This helper is intentionally read-only so configuration and health checks can
    validate budgets without sharing any order-submission code.
    """
    client = exchange or get_kraken_exchange()
    symbol = to_kraken_symbol(config_symbol)
    client.load_markets()
    if symbol not in client.markets:
        raise ValueError(f"Kraken spot market is unavailable: {symbol}")

    market = client.market(symbol)
    limits = market.get("limits", {}) or {}
    minimum_cost = float((limits.get("cost", {}) or {}).get("min") or 0)
    minimum_amount = float((limits.get("amount", {}) or {}).get("min") or 0)
    ask = 0.0
    if minimum_amount > 0:
        ticker = client.fetch_ticker(symbol)
        ask = float(ticker.get("ask") or ticker.get("last") or 0)
        if ask <= 0:
            raise RuntimeError(f"Kraken returned no usable ask price for {symbol}")

    effective_minimum = max(minimum_cost, minimum_amount * ask)
    if not math.isfinite(effective_minimum) or effective_minimum < 0:
        raise RuntimeError(f"Kraken returned invalid market limits for {symbol}")
    return {
        "pair": symbol,
        "minimum_cost_gbp": minimum_cost,
        "minimum_amount": minimum_amount,
        "ask_gbp": ask,
        "effective_minimum_gbp": effective_minimum,
    }


def _positive_gbp_amount(amount_gbp: float) -> float:
    try:
        amount = float(amount_gbp)
    except (TypeError, ValueError) as error:
        raise ValueError("GBP order amount must be numeric") from error

    if not math.isfinite(amount):
        raise ValueError("GBP order amount must be finite")
    if amount < MIN_DCA_GBP or amount > MAX_DCA_GBP:
        raise ValueError(
            f"GBP order amount must be between {MIN_DCA_GBP:.0f} and "
            f"{MAX_DCA_GBP:.0f}"
        )
    return amount


def _order_client_id(order: dict) -> str | None:
    client_order_id = order.get("clientOrderId")
    if client_order_id:
        return str(client_order_id)
    info = order.get("info") or {}
    if isinstance(info, dict):
        raw_client_order_id = info.get("cl_ord_id")
        if raw_client_order_id:
            return str(raw_client_order_id)
    return None


def _find_matching_order(exchange, symbol: str, client_order_id: str) -> dict | None:
    """Return the one matching Kraken order, querying both open and closed state."""
    # Kraken's read endpoints expose the native cl_ord_id filter. Using the raw
    # name avoids relying on a CCXT translation that is not present on every
    # account-history method.
    params = {"cl_ord_id": client_order_id}
    open_orders = exchange.fetch_open_orders(symbol, params=params.copy())
    closed_orders = exchange.fetch_closed_orders(symbol, params=params.copy())

    matches: dict[str, dict] = {}
    for order in [*(open_orders or []), *(closed_orders or [])]:
        found_client_order_id = _order_client_id(order)
        if found_client_order_id is None:
            raise RuntimeError(
                "Kraken returned an order without its requested client order ID; "
                "refusing to place another order."
            )
        if found_client_order_id != client_order_id:
            continue

        order_symbol = order.get("symbol")
        if order_symbol and order_symbol != symbol:
            raise RuntimeError(
                f"Kraken client order ID collision: expected {symbol}, got {order_symbol}"
            )
        side = str(order.get("side") or "buy").lower()
        if side != "buy":
            raise RuntimeError(
                f"Kraken client order ID collision: expected buy, got {side}"
            )

        order_id = order.get("id")
        if not order_id:
            raise RuntimeError(
                "Kraken returned a matching client order without an order ID"
            )
        matches[str(order_id)] = order

    if len(matches) > 1:
        raise RuntimeError(
            f"Multiple Kraken orders use client order ID {client_order_id}; "
            "refusing to place or assume another order."
        )
    return next(iter(matches.values()), None)


def _fee_entries(order: dict) -> list[dict]:
    fees = order.get("fees")
    if isinstance(fees, list) and fees:
        return [fee for fee in fees if isinstance(fee, dict)]

    fee = order.get("fee")
    if isinstance(fee, dict):
        return [fee]

    info = order.get("info") or {}
    if isinstance(info, dict) and "fee" in info:
        flags = str(info.get("oflags") or "")
        currency = QUOTE_CURRENCY if "fciq" in flags else None
        return [{"cost": info.get("fee"), "currency": currency}]
    return []


def _finite_nonnegative(value, field_name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise KrakenOrderStateUnknown(
            f"Kraken returned invalid {field_name}"
        ) from error
    if not math.isfinite(number) or number < 0:
        raise KrakenOrderStateUnknown(f"Kraken returned invalid {field_name}")
    return number


def _terminal_timestamp_seconds(order: dict, order_id: str) -> int:
    """Return a validated terminal-event timestamp, preferring Kraken close time."""
    for field_name in ("lastTradeTimestamp", "lastUpdateTimestamp"):
        value = order.get(field_name)
        if value is None:
            continue
        milliseconds = _finite_nonnegative(value, field_name)
        if milliseconds <= 0:
            raise KrakenOrderStateUnknown(
                f"Kraken order {order_id} returned invalid {field_name}"
            )
        return int(milliseconds / 1000)

    info = order.get("info") or {}
    if isinstance(info, dict) and info.get("closetm") is not None:
        close_seconds = _finite_nonnegative(info["closetm"], "close timestamp")
        if close_seconds <= 0:
            raise KrakenOrderStateUnknown(
                f"Kraken order {order_id} returned invalid close timestamp"
            )
        return int(close_seconds)

    created_ms = order.get("timestamp")
    if created_ms is None:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id} returned no terminal timestamp"
        )
    created_ms = _finite_nonnegative(created_ms, "order timestamp")
    if created_ms <= 0:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id} returned invalid order timestamp"
        )
    return int(created_ms / 1000)


def _normalise_terminal_fill(
    order: dict, symbol: str, client_order_id: str
) -> dict:
    """Normalize one terminal Kraken order, including its actual fee treatment."""
    order_id = str(order.get("id") or "")
    status = str(order.get("status") or "unknown").lower()
    if status not in _TERMINAL_ORDER_STATUSES:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} is not terminal; status={status}"
        )

    if order.get("filled") is None or order.get("cost") is None:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} returned incomplete "
            "terminal fill data"
        )
    gross_received = _finite_nonnegative(order["filled"], "filled amount")
    cost_gbp = _finite_nonnegative(order["cost"], "GBP cost")
    if gross_received <= 0 and cost_gbp <= 0:
        if status in {"canceled", "expired", "rejected"}:
            raise KrakenOrderNoFill(
                f"Kraken order {order_id or client_order_id} reached "
                f"status={status} without a confirmed fill"
            )
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} reported status={status} "
            "with zero fill; manual review is required"
        )
    if gross_received <= 0 or cost_gbp <= 0:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} returned inconsistent "
            "terminal fill and cost values"
        )

    market_price = cost_gbp / gross_received
    fee_entries = _fee_entries(order)
    if not fee_entries:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} returned no fee information"
        )

    base_currency = symbol.split("/", maxsplit=1)[0]
    quote_fee_gbp = 0.0
    base_fee = 0.0
    fee_gbp = 0.0
    fee_details = []
    for fee in fee_entries:
        if fee.get("cost") is None:
            raise KrakenOrderStateUnknown(
                f"Kraken order {order_id or client_order_id} returned a fee "
                "without its amount"
            )
        fee_cost = _finite_nonnegative(fee["cost"], "order fee")
        raw_currency = fee.get("currency")
        if not raw_currency:
            raise KrakenOrderStateUnknown(
                f"Kraken order {order_id or client_order_id} returned a fee "
                "without its currency"
            )
        currency = str(raw_currency).upper()
        if currency == QUOTE_CURRENCY:
            quote_fee_gbp += fee_cost
            fee_equivalent_gbp = fee_cost
        elif currency == base_currency:
            base_fee += fee_cost
            fee_equivalent_gbp = fee_cost * market_price
        else:
            raise KrakenOrderStateUnknown(
                f"Kraken order {order_id or client_order_id} charged an unsupported "
                f"fee currency: {currency}"
            )
        fee_gbp += fee_equivalent_gbp
        fee_details.append(
            {
                "currency": currency,
                "amount": fee_cost,
                "gbp_equivalent": fee_equivalent_gbp,
            }
        )

    received = gross_received - base_fee
    if received <= 0:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} has no net received amount"
        )

    spent_gbp = cost_gbp + quote_fee_gbp
    terminal_timestamp = _terminal_timestamp_seconds(
        order, order_id or client_order_id
    )
    effective_price = spent_gbp / received
    return {
        "order_id": order_id,
        "client_order_id": client_order_id,
        "pair": symbol,
        "quote_currency": QUOTE_CURRENCY,
        "cost_gbp": cost_gbp,
        "fee_gbp": fee_gbp,
        "gbp_fee_debit": quote_fee_gbp,
        "fee_details": fee_details,
        "spent_gbp": spent_gbp,
        "gross_received": gross_received,
        "received": received,
        "market_gbp_price_per_unit": market_price,
        "effective_gbp_price_per_unit": effective_price,
        # Compatibility alias: unit price excludes the separately reported fee.
        "gbp_price_per_unit": market_price,
        "timestamp": terminal_timestamp,
    }


def _poll_order_to_terminal(
    exchange,
    symbol: str,
    order_id: str,
    client_order_id: str,
    initial_order: dict | None = None,
) -> dict:
    """Poll a Kraken order until it is terminal; open partial fills never qualify."""
    current_order = initial_order
    last_error = None

    for attempt in range(len(ORDER_POLL_DELAYS_SECONDS) + 1):
        if current_order is not None:
            status = str(current_order.get("status") or "").lower()
            if status in _TERMINAL_ORDER_STATUSES:
                return _normalise_terminal_fill(
                    current_order, symbol, client_order_id
                )

        try:
            current_order = exchange.fetch_order(order_id, symbol)
            last_error = None
        except Exception as error:  # CCXT uses several transient API exception types.
            last_error = error
        else:
            status = str(current_order.get("status") or "").lower()
            if status in _TERMINAL_ORDER_STATUSES:
                return _normalise_terminal_fill(
                    current_order, symbol, client_order_id
                )

        if attempt < len(ORDER_POLL_DELAYS_SECONDS):
            time.sleep(ORDER_POLL_DELAYS_SECONDS[attempt])

    last_status = str((current_order or {}).get("status") or "unknown")
    detail = f"; last query error={last_error}" if last_error else ""
    raise KrakenOrderStateUnknown(
        f"Kraken order {order_id} remains non-terminal; status={last_status}{detail}"
    )


def _reconcile_after_submission_error(
    exchange, symbol: str, client_order_id: str
) -> dict | None:
    """Retry order lookup after an ambiguous submission response."""
    last_error = None
    for attempt in range(len(SUBMISSION_RECONCILE_DELAYS_SECONDS) + 1):
        try:
            matching_order = _find_matching_order(
                exchange, symbol, client_order_id
            )
            last_error = None
        except Exception as error:
            matching_order = None
            last_error = error
        if matching_order is not None:
            return matching_order
        if attempt < len(SUBMISSION_RECONCILE_DELAYS_SECONDS):
            time.sleep(SUBMISSION_RECONCILE_DELAYS_SECONDS[attempt])

    if last_error:
        raise KrakenOrderStateUnknown(
            "Kraken submission failed and reconciliation could not be completed: "
            f"{last_error}"
        ) from last_error
    return None


def _submission_deadline() -> str:
    """Return Kraken's short RFC3339 matching-engine deadline."""
    deadline = datetime.now(timezone.utc) + timedelta(
        seconds=ORDER_SUBMISSION_DEADLINE_SECONDS
    )
    return deadline.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _poll_known_order(
    exchange,
    symbol: str,
    order_id: str,
    client_order_id: str,
    initial_order: dict | None = None,
) -> dict:
    """Poll an order that may already exist, retaining the lock on surprises."""
    try:
        return _poll_order_to_terminal(
            exchange,
            symbol,
            order_id,
            client_order_id,
            initial_order=initial_order,
        )
    except (KrakenOrderStateUnknown, KrakenOrderNoFill):
        raise
    except Exception as error:
        raise KrakenOrderStateUnknown(
            f"Kraken order {order_id or client_order_id} could not be safely "
            f"reconciled: {error}"
        ) from error


def place_market_buy(
    config_symbol: str,
    amount_gbp: float,
    client_order_id: str | None = None,
    *,
    reconcile_only: bool = False,
    pre_submit_check=None,
) -> dict:
    """Place or reconcile today's idempotent GBP market buy on Kraken."""
    try:
        requested_gbp = _positive_gbp_amount(amount_gbp)
        exchange = get_kraken_exchange()
        symbol = to_kraken_symbol(config_symbol)
        exchange.load_markets()
        if symbol not in exchange.markets:
            raise ValueError(f"Kraken spot market is unavailable: {symbol}")

        client_order_id = client_order_id or build_client_order_id(config_symbol)
        if not _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id):
            raise ValueError(
                "client_order_id must be a deterministic 18-character DCA ID"
            )
    except Exception as setup_error:
        error_type = (
            KrakenOrderStateUnknown if reconcile_only else KrakenPreSubmissionError
        )
        raise error_type(
            f"Kraken setup failed before AddOrder: {setup_error}"
        ) from setup_error

    try:
        existing_order = _find_matching_order(exchange, symbol, client_order_id)
    except Exception as lookup_error:
        if reconcile_only:
            raise KrakenOrderStateUnknown(
                f"Kraken reconciliation lookup failed for client ID "
                f"{client_order_id}: {lookup_error}"
            ) from lookup_error
        raise KrakenPreSubmissionError(
            f"Kraken duplicate check failed before AddOrder: {lookup_error}"
        ) from lookup_error
    if existing_order is not None:
        return _poll_known_order(
            exchange,
            symbol,
            str(existing_order["id"]),
            client_order_id,
            initial_order=existing_order,
        )
    if reconcile_only:
        raise KrakenOrderStateUnknown(
            "A durable order intent exists but Kraken returned no matching open or "
            f"closed order for client ID {client_order_id}; manual review is required."
        )

    try:
        if exchange.has.get("createMarketBuyOrderWithCost") is not True:
            raise RuntimeError(
                "Kraken quote-cost market buys are unavailable; refusing to "
                "approximate the GBP budget with a base-amount order."
            )

        minimum = get_market_minimum_gbp(config_symbol, exchange=exchange)
        effective_minimum = minimum["effective_minimum_gbp"]
        if requested_gbp < effective_minimum:
            raise ValueError(
                f"GBP amount {requested_gbp:.2f} is below Kraken's current minimum "
                f"of approximately {effective_minimum:.2f} GBP for {symbol}."
            )

        precise_cost = float(exchange.cost_to_precision(symbol, requested_gbp))
        if precise_cost <= 0 or precise_cost < effective_minimum:
            raise ValueError(
                f"GBP amount becomes {precise_cost:.2f} after Kraken precision, "
                f"below the current minimum of approximately "
                f"{effective_minimum:.2f} GBP for {symbol}."
            )
        if precise_cost > requested_gbp + 1e-9:
            raise ValueError(
                "Kraken cost precision would raise the GBP debit above the "
                "configured budget"
            )

        if pre_submit_check is not None:
            pre_submit_check()
        # Generate Kraken's short deadline after all network-backed rule checks,
        # immediately before AddOrder.
        submission_params = {
            "clientOrderId": client_order_id,
            "deadline": _submission_deadline(),
            # Charge the trading fee from the purchased base asset so the GBP
            # cash debit remains bounded by the user-owned quote-cost budget.
            "oflags": "fcib",
        }
    except Exception as preparation_error:
        raise KrakenPreSubmissionError(
            f"Kraken order preparation failed before AddOrder: {preparation_error}"
        ) from preparation_error

    try:
        order = exchange.create_market_buy_order_with_cost(
            symbol, precise_cost, submission_params
        )
    except Exception as submission_error:
        matching_order = _reconcile_after_submission_error(
            exchange, symbol, client_order_id
        )
        if matching_order is None:
            raise KrakenOrderStateUnknown(
                "Kraken order submission outcome is unknown and no matching order "
                f"was found for client ID {client_order_id}"
            ) from submission_error
        return _poll_known_order(
            exchange,
            symbol,
            str(matching_order["id"]),
            client_order_id,
            initial_order=matching_order,
        )

    if not isinstance(order, dict):
        order = {}
    order_id = order.get("id")
    if not order_id:
        matching_order = _reconcile_after_submission_error(
            exchange, symbol, client_order_id
        )
        if matching_order is None:
            raise KrakenOrderStateUnknown(
                "Kraken accepted an order request without returning an order ID, "
                f"and no order matched client ID {client_order_id}"
            )
        order_id = matching_order["id"]
        order = matching_order

    return _poll_known_order(
        exchange,
        symbol,
        str(order_id),
        client_order_id,
        initial_order=order,
    )

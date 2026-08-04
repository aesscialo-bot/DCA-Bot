"""Kraken spot client used by the DCA trade executor."""

import os
import time

import ccxt
import requests


QUOTE_CURRENCY = os.environ.get("KRAKEN_QUOTE_CURRENCY", "GBP").upper()
SUPPORTED_QUOTE_CURRENCIES = {"GBP", "USD", "EUR"}


def get_thb_quote_rate(quote_currency: str = QUOTE_CURRENCY) -> float:
    """Return units of *quote_currency* per THB."""
    quote_currency = quote_currency.upper()
    if quote_currency not in SUPPORTED_QUOTE_CURRENCIES:
        raise ValueError("KRAKEN_QUOTE_CURRENCY must be GBP, USD, or EUR")

    try:
        response = requests.get(
            f"https://api.frankfurter.app/latest?from=THB&to={quote_currency}",
            timeout=5,
        )
        response.raise_for_status()
        return float(response.json()["rates"][quote_currency])
    except Exception as primary_error:
        try:
            response = requests.get(
                "https://open.er-api.com/v6/latest/THB", timeout=5
            )
            response.raise_for_status()
            return float(response.json()["rates"][quote_currency])
        except Exception as secondary_error:
            raise RuntimeError(
                f"Unable to convert THB to {quote_currency}: "
                f"{primary_error}; {secondary_error}"
            ) from secondary_error


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
    """Convert BTC_THB/BTC/USDT-style configuration keys to BTC/GBP."""
    base = config_symbol.replace("/", "_").split("_")[0].upper()
    return f"{base}/{QUOTE_CURRENCY}"


def validate_kraken_credentials() -> dict:
    """Authenticate without trading and return non-sensitive availability data."""
    exchange = get_kraken_exchange()
    balance = exchange.fetch_balance()
    return {
        "exchange": exchange.id,
        "quote_currency": QUOTE_CURRENCY,
        "quote_available": float(balance.get("free", {}).get(QUOTE_CURRENCY, 0)),
    }


def place_market_buy(config_symbol: str, amount_thb: float) -> dict:
    """Place a Kraken market buy using a THB-denominated strategy budget."""
    exchange = get_kraken_exchange()
    symbol = to_kraken_symbol(config_symbol)
    exchange.load_markets()
    if symbol not in exchange.markets:
        raise ValueError(f"Kraken spot market is unavailable: {symbol}")

    thb_quote_rate = get_thb_quote_rate()
    quote_budget = float(amount_thb) * thb_quote_rate
    market = exchange.market(symbol)
    limits = market.get("limits", {})
    minimum_cost = float((limits.get("cost", {}) or {}).get("min") or 0)
    ticker = exchange.fetch_ticker(symbol)
    ask = float(ticker.get("ask") or ticker.get("last") or 0)
    minimum_amount = float((limits.get("amount", {}) or {}).get("min") or 0)
    effective_minimum = max(minimum_cost, minimum_amount * ask)
    if quote_budget < effective_minimum:
        raise ValueError(
            f"DCA budget converts to {quote_budget:.2f} {QUOTE_CURRENCY}, below "
            f"Kraken's current minimum of approximately {effective_minimum:.2f} "
            f"{QUOTE_CURRENCY} for {symbol}."
        )

    precise_cost = float(exchange.cost_to_precision(symbol, quote_budget))
    if exchange.has.get("createMarketBuyOrderWithCost"):
        order = exchange.create_market_buy_order_with_cost(symbol, precise_cost)
    else:
        if ask <= 0:
            raise RuntimeError(f"Kraken returned no usable ask price for {symbol}")
        base_amount = float(exchange.amount_to_precision(symbol, precise_cost / ask))
        order = exchange.create_order(symbol, "market", "buy", base_amount)

    order_id = order.get("id")
    if not order_id:
        raise RuntimeError("Kraken did not return an order ID")

    time.sleep(5)
    filled_order = exchange.fetch_order(order_id, symbol)
    received = float(filled_order.get("filled") or order.get("filled") or 0)
    spent_quote = float(filled_order.get("cost") or order.get("cost") or 0)
    if received <= 0 or spent_quote <= 0:
        raise RuntimeError(
            f"Kraken order {order_id} has no confirmed fill yet; status="
            f"{filled_order.get('status', 'unknown')}"
        )

    timestamp_ms = filled_order.get("timestamp") or order.get("timestamp")
    return {
        "order_id": order_id,
        "pair": symbol,
        "quote_currency": QUOTE_CURRENCY,
        "thb_quote_rate": thb_quote_rate,
        "spent_quote": spent_quote,
        "spent_thb": spent_quote / thb_quote_rate,
        "received": received,
        "timestamp": int(timestamp_ms / 1000) if timestamp_ms else int(time.time()),
    }

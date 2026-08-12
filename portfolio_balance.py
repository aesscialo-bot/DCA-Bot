"""Read-only Kraken portfolio reporting for active and retained DCA assets.

The production crypto markets are quoted in GBP, matching the user's budgets
and portfolio view. Retired HYPE remains report-only on its historical USD
market so an existing holding never disappears from the authoritative Kraken
total. Any residual USD cash is converted with Kraken's live GBP/USD ticker.
Kraken is the source of truth; Ghostfolio is an optional post-fill mirror and
is never required for this report.
"""

from __future__ import annotations

import json
import math
import os
import re
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import requests

from dca_config import validate_rules_map
from kraken_client import get_kraken_exchange


DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")
DCA_TARGET_MAP_JSON = os.environ.get("DCA_TARGET_MAP", "{}")
SHORT_REPORT = os.environ.get("SHORT_REPORT", "true").lower() == "true"
TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)

REPORT_CUTOFF_DAY = 5
REPORT_CUTOFF_HOUR = 7
TRADE_PAGE_SIZE = 100
DISCORD_DESCRIPTION_LIMIT = 3900
GBP_USD_SYMBOL = "GBP/USD"
LEGACY_REPORTING_SYMBOLS = ("HYPE/USD",)


def _gha_mask(value: str) -> None:
    """Hide an account value when this module runs in GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def _positive_number(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive finite number") from exc
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{name} must be a positive finite number")
    return number


def extract_market_symbols(target_map: dict[str, Any]) -> list[str]:
    """Return unique Kraken pair symbols from canonical target keys."""
    symbols: list[str] = []
    for raw_key in target_map:
        if not isinstance(raw_key, str) or not re.fullmatch(
            r"[A-Z0-9]+_(?:GBP|USD)", raw_key
        ):
            raise ValueError(
                f"Invalid DCA market {raw_key!r}; expected a BASE_GBP or BASE_USD key"
            )
        base, quote = raw_key.rsplit("_", 1)
        symbol = f"{base}/{quote}"
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def reporting_market_symbols(target_map: dict[str, Any]) -> list[str]:
    """Return active DCA markets plus immutable report-only legacy holdings."""
    symbols = extract_market_symbols(target_map)
    for symbol in LEGACY_REPORTING_SYMBOLS:
        if symbol not in symbols:
            symbols.append(symbol)
    return symbols


def previous_month(value: datetime) -> datetime:
    """Move an aware datetime to the same day and time one month earlier."""
    if value.month == 1:
        return value.replace(year=value.year - 1, month=12)
    return value.replace(month=value.month - 1)


def monthly_reporting_window(now: datetime | None = None) -> tuple[datetime, datetime]:
    """Return the most recently completed local-time 5th-to-5th window."""
    now = now or datetime.now(SELECTED_TZ)
    if now.tzinfo is None:
        now = now.replace(tzinfo=SELECTED_TZ)

    end = now.replace(
        day=REPORT_CUTOFF_DAY,
        hour=REPORT_CUTOFF_HOUR,
        minute=0,
        second=0,
        microsecond=0,
    )
    if now < end:
        end = previous_month(end)
    return previous_month(end), end


def get_portfolio_balances(exchange: Any, symbols: list[str]) -> dict[str, float]:
    """Fetch configured crypto totals plus GBP and USD cash from Kraken."""
    response = exchange.fetch_balance()
    totals = response.get("total", {}) or {}
    balances: dict[str, float] = {}

    for symbol in symbols:
        base = symbol.split("/", 1)[0]
        raw_balance = totals.get(base)
        if raw_balance is None:
            asset_entry = response.get(base, {}) or {}
            if isinstance(asset_entry, dict):
                raw_balance = asset_entry.get("total", asset_entry.get("free", 0))
            else:
                raw_balance = asset_entry
        balances[base] = float(raw_balance or 0)

    balances["GBP"] = float(totals.get("GBP") or 0)
    balances["USD"] = float(totals.get("USD") or 0)
    return balances


def _ticker_price(exchange: Any, symbol: str) -> float:
    ticker = exchange.fetch_ticker(symbol)
    return _positive_number(
        ticker.get("last") or ticker.get("close") or ticker.get("bid"),
        f"Kraken {symbol} ticker",
    )


def get_market_prices(exchange: Any, symbols: list[str]) -> dict[str, float]:
    """Fetch the latest usable quote price for each configured Kraken market."""
    prices: dict[str, float] = {}
    for symbol in symbols:
        try:
            prices[symbol] = _ticker_price(exchange, symbol)
        except Exception as exc:
            print(f"Warning: Kraken ticker unavailable for {symbol}: {exc}")
            prices[symbol] = 0.0
    return prices


def get_live_gbp_usd_rate(exchange: Any) -> float:
    """Return live USD received for one GBP from Kraken's GBP/USD market."""
    return _ticker_price(exchange, GBP_USD_SYMBOL)


def _normalise_buy_trade(trade: dict[str, Any]) -> dict[str, Any] | None:
    timestamp_ms = trade.get("timestamp")
    if timestamp_ms is None or str(trade.get("side", "")).lower() != "buy":
        return None

    amount = float(trade.get("amount") or 0)
    price = float(trade.get("price") or 0)
    cost = float(trade.get("cost") or (amount * price))
    if amount <= 0 or price <= 0 or cost <= 0:
        return None

    symbol = str(trade.get("symbol") or "")
    if "/" not in symbol:
        return None
    quote_currency = symbol.split("/", 1)[1].upper()
    fee = trade.get("fee") or {}
    fee_quote = 0.0
    if str(fee.get("currency", "")).upper() == quote_currency:
        fee_quote = float(fee.get("cost") or 0)

    return {
        "trade_id": str(trade.get("id") or ""),
        "order_id": str(trade.get("order") or trade.get("id") or "N/A"),
        "amount_crypto": amount,
        "amount_quote": cost,
        "fee_quote": fee_quote,
        "rate_quote": price,
        "quote_currency": quote_currency,
        "timestamp": int(timestamp_ms) / 1000,
    }


def aggregate_buy_trades(
    exchange: Any,
    symbols: list[str],
    start_ts: int,
    end_ts: int,
) -> dict[str, list[dict[str, Any]]]:
    """Fetch all configured Kraken-market buy fills in ``[start, end)``."""
    start_ms = int(start_ts * 1000)
    end_ms = int(end_ts * 1000)
    result: dict[str, list[dict[str, Any]]] = {}
    allowed_symbols = set(symbols)
    seen: set[tuple[Any, ...]] = set()
    offset = 0

    # Kraken's private trade-history endpoint is account-wide. Paginate once,
    # then filter CCXT's unified symbols locally.
    while True:
        page = exchange.fetch_my_trades(
            None,
            since=start_ms,
            limit=TRADE_PAGE_SIZE,
            params={
                "ofs": offset,
                "end": int(end_ts),
                "limit": TRADE_PAGE_SIZE,
            },
        )
        if not page:
            break

        for trade in page:
            symbol = trade.get("symbol")
            timestamp_ms = int(trade.get("timestamp") or 0)
            if symbol not in allowed_symbols or not (start_ms <= timestamp_ms < end_ms):
                continue
            normalised = _normalise_buy_trade(trade)
            if not normalised:
                continue
            identity = (
                normalised["trade_id"],
                normalised["timestamp"],
                normalised["amount_crypto"],
                normalised["rate_quote"],
            )
            if identity in seen:
                continue
            seen.add(identity)
            base = symbol.split("/", 1)[0]
            result.setdefault(base, []).append(normalised)

        if len(page) < TRADE_PAGE_SIZE:
            break
        offset += len(page)

    for trades in result.values():
        trades.sort(key=lambda item: item["timestamp"], reverse=True)
    return result


def _format_holding(
    base: str,
    balance: float,
    quote_price: float,
    quote_currency: str,
    gbp_usd_rate: float,
) -> tuple[str, float]:
    _gha_mask(f"{balance:.8f}")
    if quote_price <= 0:
        return (
            f"**{base}**\n"
            f"  Amount: `{balance:.8f}`\n"
            "  Market price and GBP value unavailable",
            0.0,
        )

    if quote_currency == "GBP":
        gbp_price = quote_price
        price_text = f"£{quote_price:,.2f}"
    elif quote_currency == "USD":
        gbp_price = quote_price / gbp_usd_rate
        price_text = f"${quote_price:,.2f} (£{gbp_price:,.2f})"
    else:
        raise ValueError(f"Unsupported configured quote currency: {quote_currency}")
    value_gbp = balance * gbp_price
    for value in (quote_price, gbp_price, value_gbp):
        _gha_mask(f"{value:,.2f}")
    return (
        f"**{base}**\n"
        f"  Amount: `{balance:.8f}`\n"
        f"  Price: {price_text}\n"
        f"  Value: £{value_gbp:,.2f}",
        value_gbp,
    )


def build_portfolio_report(
    exchange: Any,
    symbols: list[str],
    short_report: bool = True,
    now: datetime | None = None,
) -> str:
    """Build a GBP-valued Kraken report for active and retained markets."""
    balances = get_portfolio_balances(exchange, symbols)
    prices = get_market_prices(exchange, symbols)
    gbp_usd_rate = get_live_gbp_usd_rate(exchange)
    _gha_mask(f"{gbp_usd_rate:.8f}")
    lines = [
        "**📊 TRACKED KRAKEN HOLDINGS — GBP VALUATION**",
        f"_Live Kraken FX: £1 = ${gbp_usd_rate:.4f}_",
        "",
    ]
    total_value_gbp = 0.0
    holding_count = 0

    for symbol in sorted(symbols):
        base = symbol.split("/", 1)[0]
        balance = balances.get(base, 0)
        if balance <= 0:
            continue
        holding_count += 1
        quote_currency = symbol.split("/", 1)[1].upper()
        holding_text, value = _format_holding(
            base,
            balance,
            prices.get(symbol, 0),
            quote_currency,
            gbp_usd_rate,
        )
        total_value_gbp += value
        lines.extend([holding_text, ""])

    if not holding_count:
        lines.extend(["_No balances found for the tracked markets._", ""])

    cash_gbp = balances.get("GBP", 0)
    cash_usd = balances.get("USD", 0)
    cash_usd_gbp = cash_usd / gbp_usd_rate
    for value in (cash_gbp, cash_usd, cash_usd_gbp):
        _gha_mask(f"{value:,.2f}")
    lines.extend(
        [
            "**Cash on Kraken**",
            f"  GBP: £{cash_gbp:,.2f}",
            f"  USD: ${cash_usd:,.2f} (£{cash_usd_gbp:,.2f})",
            "",
        ]
    )
    total_value_gbp += cash_gbp + cash_usd_gbp

    _gha_mask(f"{total_value_gbp:,.2f}")
    lines.extend(
        [
            "**💷 Tracked Assets + GBP/USD Cash**",
            f"£{total_value_gbp:,.2f}",
            "_Kraken is the source of truth. Ghostfolio is an optional mirror._",
            "_HYPE is retained for reporting only; other Kraken crypto assets are excluded._",
        ]
    )

    if short_report:
        return "\n".join(lines)

    start_dt, end_dt = monthly_reporting_window(now)
    start_ts, end_ts = int(start_dt.timestamp()), int(end_dt.timestamp())
    report_label = f"{start_dt.strftime('%d %b %Y')} → {end_dt.strftime('%d %b %Y')}"
    history = aggregate_buy_trades(exchange, symbols, start_ts, end_ts)
    lines.extend(["", "═" * 40, f"**📈 KRAKEN BUY HISTORY ({report_label})**", ""])

    if not history:
        lines.append("_No tracked Kraken-market buys in this period._")
        return "\n".join(lines)

    for base in sorted(history):
        trades = history[base]
        total_crypto = sum(trade["amount_crypto"] for trade in trades)
        quote_currency = trades[0]["quote_currency"]
        if any(trade["quote_currency"] != quote_currency for trade in trades):
            raise ValueError(f"Mixed quote currencies found for {base}")
        total_quote = sum(trade["amount_quote"] for trade in trades)
        total_fees_quote = sum(trade.get("fee_quote", 0) for trade in trades)
        total_gbp = (
            total_quote
            if quote_currency == "GBP"
            else total_quote / gbp_usd_rate
        )
        currency_prefix = "£" if quote_currency == "GBP" else "$"
        for value in (total_crypto, total_quote, total_fees_quote, total_gbp):
            _gha_mask(f"{value:,.8f}")
        lines.append(
            f"**{base}** ({len(trades)} buy{'s' if len(trades) != 1 else ''}) — "
            f"`{total_crypto:.8f}` acquired — {currency_prefix}{total_quote:,.2f} "
            + (f"(£{total_gbp:,.2f}) " if quote_currency != "GBP" else "")
            + f"cost — {currency_prefix}{total_fees_quote:,.2f} quote fees"
        )

        for trade in trades:
            order_id = trade["order_id"]
            for value in (
                order_id,
                f"{trade['amount_crypto']:.8f}",
                f"{trade['amount_quote']:,.2f}",
                f"{trade['rate_quote']:,.2f}",
                f"{trade.get('fee_quote', 0):,.2f}",
            ):
                _gha_mask(str(value))
            traded_at = datetime.fromtimestamp(trade["timestamp"], tz=SELECTED_TZ)
            lines.append(
                f"• {traded_at.strftime('%Y-%m-%d %H:%M %Z')} — "
                f"{trade['amount_crypto']:.8f} {base} at "
                f"{currency_prefix}{trade['rate_quote']:,.2f} — "
                f"cost {currency_prefix}{trade['amount_quote']:,.2f} — "
                f"quote fee {currency_prefix}{trade.get('fee_quote', 0):,.2f} — "
                f"order `{order_id}`"
            )
        lines.append("")

    return "\n".join(lines).rstrip()


def split_discord_message(message: str) -> list[str]:
    """Split a report on line boundaries below Discord's embed limit."""
    if len(message) <= DISCORD_DESCRIPTION_LIMIT:
        return [message]

    chunks: list[str] = []
    current: list[str] = []
    current_length = 0
    for line in message.splitlines():
        pieces = [
            line[index : index + DISCORD_DESCRIPTION_LIMIT]
            for index in range(0, max(len(line), 1), DISCORD_DESCRIPTION_LIMIT)
        ]
        for piece in pieces:
            added_length = len(piece) + (1 if current else 0)
            if current and current_length + added_length > DISCORD_DESCRIPTION_LIMIT:
                chunks.append("\n".join(current))
                current = []
                current_length = 0
                added_length = len(piece)
            current.append(piece)
            current_length += added_length
    if current:
        chunks.append("\n".join(current))
    return chunks


def send_discord_notification(message: str) -> int:
    """Send the report as one or more Discord embeds; return chunks sent."""
    if not DISCORD_WEBHOOK_URL:
        print("Warning: no Discord webhook URL configured")
        return 0

    chunks = split_discord_message(message)
    for index, chunk in enumerate(chunks):
        embed: dict[str, Any] = {
            "description": chunk,
            "color": 3447003,
            "timestamp": datetime.now(SELECTED_TZ).isoformat(),
        }
        if index == 0:
            embed["title"] = "💼 Kraken DCA Portfolio Report"
        if index == len(chunks) - 1:
            embed["footer"] = {"text": "Kraken is the authoritative portfolio"}

        response = requests.post(
            DISCORD_WEBHOOK_URL,
            json={"embeds": [embed]},
            timeout=10,
        )
        response.raise_for_status()
        if index < len(chunks) - 1:
            time.sleep(0.25)
    print(f"Discord portfolio report sent ({len(chunks)} message(s))")
    return len(chunks)


def main() -> None:
    """Fetch Kraken data, build the selected report, and notify Discord."""
    print("--- Kraken DCA Portfolio Balance Check (GBP valuation) ---")
    try:
        target_map = validate_rules_map(json.loads(DCA_TARGET_MAP_JSON))
        symbols = reporting_market_symbols(target_map)
        exchange = get_kraken_exchange()
        report = build_portfolio_report(exchange, symbols, SHORT_REPORT)
        print(report)
        send_discord_notification(report)
    except Exception as exc:
        error_message = f"❌ Kraken portfolio check failed: {exc}"
        print(error_message)
        try:
            send_discord_notification(error_message)
        finally:
            raise


if __name__ == "__main__":
    main()

"""Reporting-only PortfolioEventV3 importer for local Ghostfolio."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
import re
import sys
import time
from zoneinfo import ZoneInfo
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


EVENT_FILE = "kraken_usd_dca_ghostfolio_events.jsonl"
RECEIPT_FILE = "ghostfolio_sync_receipts.jsonl"
HOLDINGS_SNAPSHOT_FILE = "kraken_holdings_snapshot_v1.json"
HOLDINGS_RECEIPT_FILE = "ghostfolio_holdings_receipts.jsonl"
STATE_PATH = Path("/receipts/state.json")
HOLDINGS_INTENT_PATH = Path("/receipts/holdings-intent.json")
ASSET_PROFILES = {
    "BTC_GBP": {"symbol": "bitcoin", "data_source": "COINGECKO"},
    # Ghostfolio 3.43.0 returns Hyperliquid in lookup results for both
    # providers, but its CoinGecko importer rejects that asset. The supported
    # Yahoo crypto profile is therefore the audited local identifier.
    "HYPE_USD": {"symbol": "HYPE32196USD", "data_source": "YAHOO"},
    "SOL_GBP": {"symbol": "solana", "data_source": "COINGECKO"},
}
KRAKEN_HOLDINGS_CONTRACT = {
    "BTC_GBP": {"asset": "BTC", "pair": "BTC/GBP", "quote_currency": "GBP"},
    "HYPE_USD": {"asset": "HYPE", "pair": "HYPE/USD", "quote_currency": "USD"},
    "SOL_GBP": {"asset": "SOL", "pair": "SOL/GBP", "quote_currency": "GBP"},
}
PORTFOLIO_EVENT_FIELDS = {
    "event_version",
    "event_id",
    "occurred_at",
    "target",
    "base_currency",
    "quote_currency",
    "budget_currency",
    "funding_order_id",
    "crypto_order_id",
    "gbp_debit",
    "gbp_usd_rate",
    "funded_usd",
    "route",
    "crypto_cost_quote",
    "crypto_quantity",
    "unit_price_quote",
    "funding_fee_quote",
    "crypto_fee_quote",
    "canonical_hash",
}
PORTFOLIO_EVENT_DECIMAL_FIELDS = (
    "gbp_debit",
    "gbp_usd_rate",
    "funded_usd",
    "crypto_cost_quote",
    "crypto_quantity",
    "unit_price_quote",
    "funding_fee_quote",
    "crypto_fee_quote",
)
PORTFOLIO_EVENT_CONTRACT = {
    "BTC_GBP": {
        "base_currency": "BTC",
        "quote_currency": "GBP",
        "route": "DIRECT_GBP",
    },
    "HYPE_USD": {
        "base_currency": "HYPE",
        "quote_currency": "USD",
        "route": "GBP_TO_USD",
    },
    "SOL_GBP": {
        "base_currency": "SOL",
        "quote_currency": "GBP",
        "route": "DIRECT_GBP",
    },
}
SYMBOLS = {target: profile["symbol"] for target, profile in ASSET_PROFILES.items()}
QUANTITY_TOLERANCE = {"BTC_GBP": 1e-10, "HYPE_USD": 1e-8, "SOL_GBP": 1e-8}
REPORTING_ACCOUNT_NAMES = ("Kraken DCA", "Bitkub Legacy")
REPORTING_CURRENCY = "GBP"
REQUIRED_REPORTING_CURRENCIES = ("GBP", "USD")
GHOSTFOLIO_FX_DATA_SOURCE = "YAHOO"
GHOSTFOLIO_FX_SYMBOL = "USDGBP"
YAHOO_FX_SYMBOL = "USDGBP=X"
YAHOO_FX_SYMBOL_ALIASES = {YAHOO_FX_SYMBOL, "GBP=X"}
YAHOO_FX_RANGE = "1mo"
FX_MINIMUM_ROWS = 5
FX_MAXIMUM_AGE_DAYS = 4
HOLDINGS_SNAPSHOT_MAX_AGE_SECONDS = 7200
HOLDINGS_SNAPSHOT_MAX_FUTURE_SECONDS = 300
BANGKOK = ZoneInfo("Asia/Bangkok")


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def request_json(url, *, method="GET", token=None, payload=None):
    headers = {"Accept": "application/json"}
    body = None
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = canonical(payload).encode("utf-8")
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with urlopen(request, timeout=20) as response:
            raw = response.read()
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as error:
        raw = error.read()
        try:
            detail = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            detail = {"error": "non-JSON response"}
        return error.code, detail


def request_text(url, *, token=None):
    headers = {"Accept": "text/plain"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    with urlopen(Request(url, headers=headers), timeout=20) as response:
        return response.status, response.read().decode("utf-8")


def gist():
    gist_id = os.environ["GIST_ID"]
    token = os.environ["GIST_TOKEN"]
    status, payload = request_json(
        f"https://api.github.com/gists/{gist_id}", token=token
    )
    if status != 200:
        raise RuntimeError(f"Gist read failed with HTTP {status}")
    return payload


def file_content(payload, name):
    info = payload.get("files", {}).get(name)
    if not info:
        return ""
    if info.get("truncated"):
        status, raw = request_text(info["raw_url"], token=os.environ["GIST_TOKEN"])
        if status != 200:
            raise RuntimeError(f"Gist raw read failed with HTTP {status}")
        return raw
    content = info.get("content", "")
    if not isinstance(content, str):
        raise RuntimeError(f"{name} is not text")
    return content


def parse_events(content):
    events = []
    seen = set()
    for number, line in enumerate(content.splitlines(), start=1):
        if not line.strip():
            continue
        event = json.loads(line)
        _validate_portfolio_event(event, number)
        supplied = event.get("canonical_hash")
        unhashed = {key: value for key, value in event.items() if key != "canonical_hash"}
        actual = hashlib.sha256(canonical(unhashed).encode("utf-8")).hexdigest()
        if supplied != actual:
            raise RuntimeError(f"event line {number} has an invalid append-only hash")
        event_id = event.get("event_id")
        if event_id in seen:
            raise RuntimeError(f"event line {number} duplicates {event_id}")
        seen.add(event_id)
        events.append(event)
    return events


def _safe_order_id(value):
    return isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", value
    ) is not None


def _event_decimal(event, field, number):
    raw = event.get(field)
    if not isinstance(raw, str):
        raise RuntimeError(
            f"event line {number} field {field} is not a decimal string"
        )
    try:
        value = Decimal(raw)
    except InvalidOperation as error:
        raise RuntimeError(
            f"event line {number} field {field} is not a decimal string"
        ) from error
    if not value.is_finite() or value < 0:
        raise RuntimeError(f"event line {number} field {field} is invalid")
    return value


def _validate_portfolio_event(event, number):
    if not isinstance(event, dict) or set(event) != PORTFOLIO_EVENT_FIELDS:
        raise RuntimeError(f"event line {number} does not match PortfolioEventV3")
    if event.get("event_version") != 3 or isinstance(
        event.get("event_version"), bool
    ):
        raise RuntimeError(f"event line {number} has an invalid event version")
    event_id = event.get("event_id")
    if not _safe_order_id(event_id) or event.get("crypto_order_id") != event_id:
        raise RuntimeError(f"event line {number} has invalid order identifiers")
    occurred_at = event.get("occurred_at")
    if not isinstance(occurred_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
        occurred_at,
    ):
        raise RuntimeError(f"event line {number} has a non-canonical UTC timestamp")
    _parse_timestamp(occurred_at, f"event line {number}")
    target = event.get("target")
    contract = (
        PORTFOLIO_EVENT_CONTRACT.get(target)
        if isinstance(target, str)
        else None
    )
    if contract is None or any(
        event.get(key) != value for key, value in contract.items()
    ) or event.get("budget_currency") != "GBP":
        raise RuntimeError(f"event line {number} has an invalid target route or currency")
    funding_order_id = event.get("funding_order_id")
    if contract["route"] == "GBP_TO_USD":
        if not _safe_order_id(funding_order_id):
            raise RuntimeError(f"event line {number} has an invalid funding order ID")
    elif funding_order_id is not None:
        raise RuntimeError(f"event line {number} direct GBP funding ID must be null")
    values = {
        field: _event_decimal(event, field, number)
        for field in PORTFOLIO_EVENT_DECIMAL_FIELDS
    }
    for field in (
        "gbp_debit",
        "crypto_cost_quote",
        "crypto_quantity",
        "unit_price_quote",
    ):
        if values[field] <= 0:
            raise RuntimeError(f"event line {number} field {field} must be positive")
    if contract["route"] == "GBP_TO_USD":
        if values["gbp_usd_rate"] <= 0 or values["funded_usd"] <= 0:
            raise RuntimeError(f"event line {number} has invalid USD funding values")
    elif values["gbp_usd_rate"] != 0 or values["funded_usd"] != 0:
        raise RuntimeError(f"event line {number} direct GBP funding values must be zero")


def parse_event_receipts(content, events):
    event_by_id = {event["event_id"]: event for event in events}
    receipts = set()
    expected_fields = {
        "order_id",
        "event_hash",
        "ghostfolio_activity_id",
        "imported_at",
    }
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            receipt = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("Ghostfolio event receipt ledger is malformed") from error
        if not isinstance(receipt, dict) or set(receipt) != expected_fields:
            raise RuntimeError("Ghostfolio event receipt ledger is malformed")
        order_id = receipt.get("order_id")
        event = event_by_id.get(order_id)
        if event is None or receipt.get("event_hash") != event["canonical_hash"]:
            raise RuntimeError("Ghostfolio event receipt does not match its event")
        if order_id in receipts:
            raise RuntimeError("Ghostfolio event receipt ledger has duplicates")
        activity_id = receipt.get("ghostfolio_activity_id")
        if not isinstance(activity_id, str) or not activity_id:
            raise RuntimeError("Ghostfolio event receipt ledger is malformed")
        _parse_timestamp(receipt.get("imported_at"), "Ghostfolio event receipt")
        receipts.add(order_id)
    return receipts


def parse_holdings_snapshot(content):
    if not content.strip():
        return None
    snapshot = json.loads(content)
    if not isinstance(snapshot, dict) or set(snapshot) != {
        "version",
        "as_of",
        "holdings",
        "unsupported_nonzero_assets",
        "canonical_hash",
    }:
        raise RuntimeError("Kraken holdings snapshot schema is invalid")
    supplied = snapshot.get("canonical_hash")
    unhashed = {key: value for key, value in snapshot.items() if key != "canonical_hash"}
    actual = hashlib.sha256(canonical(unhashed).encode("utf-8")).hexdigest()
    if (
        supplied != actual
        or snapshot.get("version") != 1
        or isinstance(snapshot.get("version"), bool)
        or not isinstance(supplied, str)
        or len(supplied) != 64
        or supplied.lower() != supplied
        or any(character not in "0123456789abcdef" for character in supplied)
    ):
        raise RuntimeError("Kraken holdings snapshot has an invalid hash or version")
    unsupported = snapshot.get("unsupported_nonzero_assets")
    if (
        not isinstance(unsupported, list)
        or any(not isinstance(asset, str) or not asset for asset in unsupported)
        or len(unsupported) != len(set(unsupported))
    ):
        raise RuntimeError("Kraken holdings snapshot unsupported assets are invalid")
    if unsupported:
        raise RuntimeError(
            "Kraken has non-zero crypto assets without a Ghostfolio mapping: "
            + ", ".join(unsupported)
        )
    holdings = snapshot.get("holdings")
    if not isinstance(holdings, dict) or set(holdings) != set(SYMBOLS):
        raise RuntimeError("Kraken holdings snapshot does not contain the exact target set")
    for target, contract in KRAKEN_HOLDINGS_CONTRACT.items():
        item = holdings.get(target)
        if not isinstance(item, dict) or set(item) != {
            "asset",
            "pair",
            "quote_currency",
            "quantity",
            "unit_price_quote",
        }:
            raise RuntimeError(f"Kraken holdings snapshot item is invalid for {target}")
        if any(item.get(key) != value for key, value in contract.items()):
            raise RuntimeError(f"Kraken holdings snapshot identity is invalid for {target}")
        _finite_number(
            item.get("quantity"),
            f"Kraken holdings snapshot quantity for {target}",
            minimum=0,
        )
        _finite_number(
            item.get("unit_price_quote"),
            f"Kraken holdings snapshot price for {target}",
            minimum=0,
            inclusive=False,
        )
    _parse_timestamp(snapshot.get("as_of"), "holdings snapshot")
    return snapshot


def _finite_number(value, label, *, minimum=None, inclusive=True):
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RuntimeError(f"{label} is invalid")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{label} is invalid") from error
    if not math.isfinite(number):
        raise RuntimeError(f"{label} is invalid")
    if minimum is not None and (
        number < minimum if inclusive else number <= minimum
    ):
        raise RuntimeError(f"{label} is invalid")
    return number


def _parse_timestamp(value, label):
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{label} has no valid ISO timestamp")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise RuntimeError(f"{label} has no valid ISO timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise RuntimeError(f"{label} timestamp has no timezone")
    return parsed.astimezone(timezone.utc)


def validate_snapshot_event_order(snapshot, events):
    """Block a synthetic adjustment based on a snapshot older than a fill."""
    if snapshot is None:
        return
    snapshot_time = _parse_timestamp(snapshot.get("as_of"), "holdings snapshot")
    for event in events:
        event_id = event.get("event_id") or "unknown"
        event_time = _parse_timestamp(
            event.get("occurred_at"), f"portfolio event {event_id}"
        )
        if event_time > snapshot_time:
            raise RuntimeError(
                "Kraken holdings snapshot predates portfolio event " + str(event_id)
            )


def validate_snapshot_freshness(snapshot, *, now=None):
    if snapshot is None:
        return None
    snapshot_time = _parse_timestamp(snapshot.get("as_of"), "holdings snapshot")
    reference = now or datetime.now(timezone.utc)
    if not isinstance(reference, datetime) or reference.tzinfo is None:
        raise RuntimeError("holdings snapshot clock must be timezone-aware")
    reference = reference.astimezone(timezone.utc)
    age_seconds = (reference - snapshot_time).total_seconds()
    maximum_age = int(
        os.environ.get(
            "HOLDINGS_SNAPSHOT_MAX_AGE_SECONDS",
            str(HOLDINGS_SNAPSHOT_MAX_AGE_SECONDS),
        )
    )
    if maximum_age < 900 or maximum_age > 86400:
        raise RuntimeError("HOLDINGS_SNAPSHOT_MAX_AGE_SECONDS is invalid")
    if age_seconds < -HOLDINGS_SNAPSHOT_MAX_FUTURE_SECONDS:
        raise RuntimeError("Kraken holdings snapshot is future-dated")
    if age_seconds > maximum_age:
        raise RuntimeError("Kraken holdings snapshot is stale")
    return snapshot_time


def validate_snapshot_monotonicity(snapshot):
    if snapshot is None or not STATE_PATH.is_file():
        return
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("Ghostfolio sync state is unreadable") from error
    prior_as_of = state.get("holdings_snapshot_as_of")
    prior_hash = state.get("holdings_snapshot_hash")
    # Accept one migration pass from the prior state schema, then write both.
    if prior_as_of is None and prior_hash is None:
        return
    if not isinstance(prior_hash, str) or len(prior_hash) != 64:
        raise RuntimeError("Ghostfolio sync state has invalid snapshot identity")
    prior_time = _parse_timestamp(prior_as_of, "prior holdings snapshot")
    current_time = _parse_timestamp(snapshot.get("as_of"), "holdings snapshot")
    if current_time < prior_time:
        raise RuntimeError("Kraken holdings snapshot moved backwards")
    if current_time == prior_time and snapshot.get("canonical_hash") != prior_hash:
        raise RuntimeError("Kraken holdings snapshot changed at the same timestamp")


def ghostfolio_token():
    status, payload = request_json(
        os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/auth/anonymous",
        method="POST",
        payload={"accessToken": os.environ["GHOSTFOLIO_SECURITY_TOKEN"]},
    )
    if status not in {200, 201} or not payload.get("authToken"):
        raise RuntimeError(f"Ghostfolio authentication failed with HTTP {status}")
    return payload["authToken"]


def _ghostfolio_url(path):
    return os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + path


def _admin_currencies(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("settings"), dict):
        raise RuntimeError("Ghostfolio admin response has no settings object")
    currencies = payload["settings"].get("CURRENCIES", [])
    if isinstance(currencies, str):
        try:
            currencies = json.loads(currencies)
        except json.JSONDecodeError as error:
            raise RuntimeError("Ghostfolio CURRENCIES setting is malformed") from error
    if not isinstance(currencies, list) or not all(
        isinstance(currency, str) and currency.strip() for currency in currencies
    ):
        raise RuntimeError("Ghostfolio CURRENCIES setting is malformed")
    if len(set(currencies)) != len(currencies):
        raise RuntimeError("Ghostfolio CURRENCIES setting contains duplicates")
    return currencies


def ensure_ghostfolio_reporting_currency(token):
    """Ensure GBP reporting and USD source pricing without dropping settings."""
    admin_url = _ghostfolio_url("/api/v1/admin")
    status, payload = request_json(admin_url, token=token)
    if status != 200:
        raise RuntimeError(f"Ghostfolio admin read failed with HTTP {status}")
    currencies = _admin_currencies(payload)
    missing = [
        currency
        for currency in REQUIRED_REPORTING_CURRENCIES
        if currency not in currencies
    ]
    if not missing:
        return currencies

    updated = [*currencies, *missing]
    status, _ = request_json(
        _ghostfolio_url("/api/v1/admin/settings/CURRENCIES"),
        method="PUT",
        token=token,
        payload={"value": canonical(updated)},
    )
    if status not in {200, 201}:
        raise RuntimeError(
            f"Ghostfolio CURRENCIES update failed with HTTP {status}"
        )

    # Read back the value. A successful status without a visible GBP setting
    # must not allow the portfolio audit to continue.
    verify_status, verify_payload = request_json(admin_url, token=token)
    if verify_status != 200:
        raise RuntimeError(
            f"Ghostfolio admin verification failed with HTTP {verify_status}"
        )
    verified = _admin_currencies(verify_payload)
    if any(currency not in verified for currency in REQUIRED_REPORTING_CURRENCIES):
        raise RuntimeError(
            "Ghostfolio CURRENCIES update did not persist GBP and USD"
        )
    return verified


def verify_ghostfolio_reporting_accounts(token, *, require_bitkub=True):
    """Verify the Kraken account, and the legacy account for full reporting audits."""
    status, payload = request_json(_ghostfolio_url("/api/v1/account"), token=token)
    if status != 200:
        raise RuntimeError(f"Ghostfolio account read failed with HTTP {status}")
    accounts = payload.get("accounts") if isinstance(payload, dict) else None
    if not isinstance(accounts, list) or not all(
        isinstance(account, dict) for account in accounts
    ):
        raise RuntimeError("Ghostfolio account response is malformed")

    verified = {}
    required_names = REPORTING_ACCOUNT_NAMES if require_bitkub else ("Kraken DCA",)
    for name in required_names:
        matches = [account for account in accounts if account.get("name") == name]
        if len(matches) != 1:
            raise RuntimeError(
                f"Ghostfolio requires exactly one {name} custody account"
            )
        account = matches[0]
        if account.get("currency") != REPORTING_CURRENCY:
            raise RuntimeError(f"Ghostfolio account {name} is not GBP-denominated")
        account_id = account.get("id")
        if not isinstance(account_id, str) or not account_id:
            raise RuntimeError(f"Ghostfolio account {name} has no valid ID")
        verified[name] = account_id
    if len(set(verified.values())) != len(verified):
        raise RuntimeError("Ghostfolio custody accounts must have distinct IDs")
    return verified


def ghostfolio_account_map():
    try:
        accounts = json.loads(os.environ.get("GHOSTFOLIO_ACCOUNT_MAP", "{}"))
    except json.JSONDecodeError as error:
        raise RuntimeError("GHOSTFOLIO_ACCOUNT_MAP is malformed") from error
    if not isinstance(accounts, dict):
        raise RuntimeError("GHOSTFOLIO_ACCOUNT_MAP is malformed")
    return accounts


def verify_ghostfolio_account_map(reporting_accounts, *, require_bitkub=True):
    """Keep Kraken reconciliation isolated from the legacy custody account."""
    kraken_account_id = reporting_accounts.get("Kraken DCA")
    bitkub_account_id = reporting_accounts.get("Bitkub Legacy")
    if not kraken_account_id or (require_bitkub and not bitkub_account_id):
        raise RuntimeError("Ghostfolio reporting account IDs are incomplete")
    if require_bitkub and kraken_account_id == bitkub_account_id:
        raise RuntimeError("Ghostfolio custody accounts must have distinct IDs")
    accounts = ghostfolio_account_map()
    mismatched = [
        target for target in SYMBOLS if accounts.get(target) != kraken_account_id
    ]
    if mismatched:
        raise RuntimeError(
            "Ghostfolio Kraken account mapping is invalid for: "
            + ", ".join(sorted(mismatched))
        )
    if require_bitkub and accounts.get("BITKUB_LEGACY") != bitkub_account_id:
        raise RuntimeError("Ghostfolio Bitkub Legacy account mapping is invalid")
    return kraken_account_id


def yahoo_usdgbp_market_data(*, now=None):
    """Fetch and strictly normalize Yahoo USD/GBP daily closes for Ghostfolio."""
    now = now or datetime.now(timezone.utc)
    if (
        not isinstance(now, datetime)
        or now.tzinfo is None
        or now.utcoffset() is None
    ):
        raise RuntimeError("FX bridge clock must be timezone-aware")
    bangkok_today = now.astimezone(BANGKOK).date()
    url = (
        "https://query1.finance.yahoo.com/v8/finance/chart/"
        + quote(YAHOO_FX_SYMBOL, safe="")
        + "?"
        + urlencode({
            "range": YAHOO_FX_RANGE,
            "interval": "1d",
            "events": "history",
            "includePrePost": "false",
        })
    )
    status, payload = request_json(url)
    if status != 200:
        raise RuntimeError(f"Yahoo USDGBP chart read failed with HTTP {status}")
    chart = payload.get("chart") if isinstance(payload, dict) else None
    if not isinstance(chart, dict) or chart.get("error") is not None:
        raise RuntimeError("Yahoo USDGBP chart response contains an error")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError("Yahoo USDGBP chart response has no unique result")
    result = results[0]
    if not isinstance(result, dict):
        raise RuntimeError("Yahoo USDGBP chart result is malformed")
    meta = result.get("meta")
    if (
        not isinstance(meta, dict)
        or meta.get("symbol") not in YAHOO_FX_SYMBOL_ALIASES
        or meta.get("currency") != REPORTING_CURRENCY
    ):
        raise RuntimeError("Yahoo USDGBP chart metadata is invalid")
    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    quotes = indicators.get("quote") if isinstance(indicators, dict) else None
    closes = (
        quotes[0].get("close")
        if isinstance(quotes, list)
        and len(quotes) == 1
        and isinstance(quotes[0], dict)
        else None
    )
    if (
        not isinstance(timestamps, list)
        or not isinstance(closes, list)
        or len(timestamps) != len(closes)
    ):
        raise RuntimeError("Yahoo USDGBP chart has missing daily data")

    market_data = []
    seen_dates = set()
    previous_timestamp = None
    for timestamp, close in zip(timestamps, closes):
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, int)
            or (previous_timestamp is not None and timestamp <= previous_timestamp)
        ):
            raise RuntimeError("Yahoo USDGBP chart timestamps are invalid")
        previous_timestamp = timestamp
        # Yahoo includes a current-session timestamp with a null close. It is
        # not a price and must never be written as zero, but the complete
        # historical closes in the same response remain valid.
        if close is None:
            continue
        if (
            isinstance(close, bool)
            or not isinstance(close, (int, float))
            or not math.isfinite(close)
            or close <= 0
        ):
            raise RuntimeError("Yahoo USDGBP chart contains an invalid close")
        try:
            bangkok_date = datetime.fromtimestamp(
                timestamp, timezone.utc
            ).astimezone(BANGKOK).date()
        except (OSError, OverflowError, ValueError) as error:
            raise RuntimeError("Yahoo USDGBP chart timestamp is out of range") from error
        if bangkok_date in seen_dates:
            raise RuntimeError("Yahoo USDGBP chart contains duplicate Bangkok dates")
        seen_dates.add(bangkok_date)
        market_data.append({
            "date": f"{bangkok_date.isoformat()}T00:00:00.000Z",
            "marketPrice": float(close),
        })

    if len(market_data) < FX_MINIMUM_ROWS:
        raise RuntimeError("Yahoo USDGBP chart has missing daily data")
    latest_date = max(seen_dates)
    age_days = (bangkok_today - latest_date).days
    if age_days < 0 or age_days > FX_MAXIMUM_AGE_DAYS:
        raise RuntimeError("Yahoo USDGBP chart is stale or future-dated")
    return market_data


def publish_ghostfolio_usdgbp_market_data(token, market_data):
    if not isinstance(market_data, list) or len(market_data) < FX_MINIMUM_ROWS:
        raise RuntimeError("USDGBP market data is incomplete")
    status, payload = request_json(
        _ghostfolio_url(
            f"/api/v1/market-data/{GHOSTFOLIO_FX_DATA_SOURCE}/{GHOSTFOLIO_FX_SYMBOL}"
        ),
        method="POST",
        token=token,
        payload={"marketData": market_data},
    )
    if status not in {200, 201}:
        raise RuntimeError(f"Ghostfolio USDGBP import failed with HTTP {status}")
    if not isinstance(payload, list) or len(payload) != len(market_data):
        raise RuntimeError("Ghostfolio USDGBP import acknowledgement is incomplete")
    return len(payload)


def ghostfolio_usdgbp_is_current(token, *, now=None):
    """Use Ghostfolio's stored FX rows until a refresh is actually due."""
    now = now or datetime.now(timezone.utc)
    if not isinstance(now, datetime) or now.tzinfo is None:
        raise RuntimeError("FX bridge clock must be timezone-aware")
    bangkok_today = now.astimezone(BANGKOK).date()
    for age_days in range(FX_MAXIMUM_AGE_DAYS + 1):
        candidate = bangkok_today - timedelta(days=age_days)
        status, payload = request_json(
            _ghostfolio_url(
                f"/api/v1/exchange-rate/USD-GBP/{candidate.isoformat()}"
            ),
            token=token,
        )
        if status == 404:
            continue
        if status != 200:
            raise RuntimeError(
                f"Ghostfolio USDGBP verification failed with HTTP {status}"
            )
        value = payload.get("marketPrice") if isinstance(payload, dict) else None
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value <= 0
        ):
            raise RuntimeError("Ghostfolio USDGBP verification is malformed")
        return True
    return False


def flush_ghostfolio_cache(token):
    status, _ = request_json(
        _ghostfolio_url("/api/v1/cache/flush"),
        method="POST",
        token=token,
    )
    if status not in {200, 201, 204}:
        raise RuntimeError(f"Ghostfolio cache flush failed with HTTP {status}")


def prepare_ghostfolio_fx_bridge(token, *, now=None, force_refresh=False):
    """Prepare reporting-only FX state before any portfolio calculation audit."""
    ensure_ghostfolio_reporting_currency(token)
    reporting_accounts = verify_ghostfolio_reporting_accounts(token)
    kraken_account_id = verify_ghostfolio_account_map(reporting_accounts)
    imported = 0
    if force_refresh or not ghostfolio_usdgbp_is_current(token, now=now):
        market_data = yahoo_usdgbp_market_data(now=now)
        imported = publish_ghostfolio_usdgbp_market_data(token, market_data)
    flush_ghostfolio_cache(token)
    return {
        "fx_rows": imported,
        "kraken_account_id": kraken_account_id,
    }


def import_payload(event):
    accounts = ghostfolio_account_map()
    account_id = accounts.get(event["target"])
    if not account_id:
        raise RuntimeError(f"no local custody account configured for {event['target']}")
    profile = ASSET_PROFILES[event["target"]]
    return {
        "activities": [{
            "accountId": account_id,
            "comment": (
                f"Kraken orders funding={event['funding_order_id']} crypto={event['crypto_order_id']}; "
                f"route={event['route']}; funding fee {event['quote_currency']} "
                f"{event['funding_fee_quote']}; crypto fee {event['quote_currency']} "
                f"{event['crypto_fee_quote']}"
            ),
            "currency": event["quote_currency"],
            "dataSource": profile["data_source"],
            "date": event["occurred_at"],
            "fee": float(event["crypto_fee_quote"]),
            "quantity": float(event["crypto_quantity"]),
            "symbol": profile["symbol"],
            "type": "BUY",
            "unitPrice": float(event["unit_price_quote"]),
        }]
    }


def ghostfolio_quantities(token, kraken_account_id):
    if not isinstance(kraken_account_id, str) or not kraken_account_id:
        raise RuntimeError("Kraken DCA account ID is invalid")
    status, payload = request_json(
        _ghostfolio_url("/api/v1/portfolio/holdings")
        + "?"
        + urlencode({"accounts": kraken_account_id}),
        token=token,
    )
    if status != 200:
        raise RuntimeError(f"Ghostfolio holdings read failed with HTTP {status}")
    holdings = payload.get("holdings") if isinstance(payload, dict) else None
    if not isinstance(holdings, list) or not all(
        isinstance(holding, dict) for holding in holdings
    ):
        raise RuntimeError("Ghostfolio holdings response is malformed")
    result = {target: 0.0 for target in SYMBOLS}
    reverse = {symbol: target for target, symbol in SYMBOLS.items()}
    seen = set()
    for holding in holdings:
        profile = holding.get("assetProfile")
        if not isinstance(profile, dict):
            continue
        symbol = profile.get("symbol")
        if not isinstance(symbol, str):
            raise RuntimeError("Ghostfolio holdings response is malformed")
        target = reverse.get(symbol)
        if target:
            if target in seen:
                raise RuntimeError(f"Ghostfolio holdings duplicate {target}")
            seen.add(target)
            result[target] = _finite_number(
                holding.get("quantity"),
                f"Ghostfolio holding quantity for {target}",
                minimum=0,
            )
    return result


def ghostfolio_portfolio_calculation(token):
    status, payload = request_json(
        os.environ.get("GHOSTFOLIO_URL", "http://app:3333")
        + "/api/v1/portfolio/details?withMarkets=true",
        token=token,
    )
    if status != 200:
        raise RuntimeError(
            f"Ghostfolio portfolio calculation read failed with HTTP {status}"
        )
    has_error = payload.get("hasError")
    if not isinstance(has_error, bool):
        raise RuntimeError(
            "Ghostfolio portfolio calculation response has no boolean hasError"
        )
    return {
        "portfolio_calculation_status": "ERROR" if has_error else "OK",
        "portfolio_calculation_has_error": has_error,
    }


def ghostfolio_portfolio_calculation_with_fx_repair(
    token, reporting, *, now=None
):
    """Retry one calculation after a forced historical USD/GBP backfill."""
    calculation = ghostfolio_portfolio_calculation(token)
    if (
        calculation["portfolio_calculation_has_error"]
        and reporting.get("fx_rows") == 0
    ):
        repaired = prepare_ghostfolio_fx_bridge(
            token, now=now, force_refresh=True
        )
        reporting.update(repaired)
        calculation = ghostfolio_portfolio_calculation(token)
    return calculation


def holdings_drift(snapshot, actual):
    if not isinstance(actual, dict):
        raise RuntimeError("Ghostfolio holdings quantities are invalid")
    drift = {}
    for target in SYMBOLS:
        expected = _finite_number(
            snapshot["holdings"][target]["quantity"],
            f"expected holding quantity for {target}",
            minimum=0,
        )
        observed = _finite_number(
            actual.get(target),
            f"Ghostfolio holding quantity for {target}",
            minimum=0,
        )
        difference = expected - observed
        if abs(difference) > QUANTITY_TOLERANCE[target]:
            drift[target] = difference
    return drift


def holdings_import_payload(snapshot, target, difference):
    accounts = ghostfolio_account_map()
    account_id = accounts.get(target)
    if not account_id:
        raise RuntimeError(f"no local custody account configured for {target}")
    item = snapshot["holdings"][target]
    profile = ASSET_PROFILES[target]
    return {
        "activities": [{
            "accountId": account_id,
            "comment": (
                "Kraken opening-balance reconciliation; "
                f"snapshot={snapshot['canonical_hash']}; target={target}"
            ),
            "currency": item["quote_currency"],
            "dataSource": profile["data_source"],
            "date": snapshot["as_of"],
            "fee": 0,
            "quantity": abs(difference),
            "symbol": profile["symbol"],
            "type": "BUY" if difference > 0 else "SELL",
            "unitPrice": float(item["unit_price_quote"]),
        }]
    }


def is_exact_duplicate(payload):
    messages = payload.get("message", []) if isinstance(payload, dict) else []
    if isinstance(messages, str):
        messages = [messages]
    return bool(messages) and all("duplicate activity" in message.lower() for message in messages)


def append_receipt(gist_payload, receipt):
    existing = file_content(gist_payload, RECEIPT_FILE)
    for line in existing.splitlines():
        row = json.loads(line)
        if row.get("order_id") == receipt["order_id"]:
            return row == receipt
    updated = existing + ("" if not existing or existing.endswith("\n") else "\n") + canonical(receipt) + "\n"
    status, _ = request_json(
        f"https://api.github.com/gists/{os.environ['GIST_ID']}",
        method="PATCH",
        token=os.environ["GIST_TOKEN"],
        payload={"files": {RECEIPT_FILE: {"content": updated}}},
    )
    if status != 200:
        raise RuntimeError(f"receipt publish failed with HTTP {status}")
    return True


def append_named_receipt(gist_payload, filename, identity_field, receipt):
    existing = file_content(gist_payload, filename)
    for line in existing.splitlines():
        row = json.loads(line)
        if row.get(identity_field) == receipt[identity_field]:
            return row == receipt
    updated = existing + ("" if not existing or existing.endswith("\n") else "\n") + canonical(receipt) + "\n"
    status, _ = request_json(
        f"https://api.github.com/gists/{os.environ['GIST_ID']}",
        method="PATCH",
        token=os.environ["GIST_TOKEN"],
        payload={"files": {filename: {"content": updated}}},
    )
    if status != 200:
        raise RuntimeError(f"{filename} publish failed with HTTP {status}")
    return True


def holdings_receipt_for_snapshot(gist_payload, snapshot_hash):
    match = None
    for line in file_content(gist_payload, HOLDINGS_RECEIPT_FILE).splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("holdings receipt ledger is malformed") from error
        _validate_holdings_receipt(row, "holdings receipt ledger")
        if row.get("snapshot_hash") == snapshot_hash:
            if match is not None:
                raise RuntimeError("holdings receipt ledger has duplicate snapshot hashes")
            match = row
    return match


def _validate_holdings_receipt(receipt, label):
    if (
        not isinstance(receipt, dict)
        or set(receipt) != {"snapshot_hash", "reconciled_at", "adjustments"}
        or not isinstance(receipt.get("snapshot_hash"), str)
        or len(receipt["snapshot_hash"]) != 64
        or receipt["snapshot_hash"].lower() != receipt["snapshot_hash"]
        or any(
            character not in "0123456789abcdef"
            for character in receipt["snapshot_hash"]
        )
        or not isinstance(receipt.get("adjustments"), list)
    ):
        raise RuntimeError(f"{label} is malformed")
    _parse_timestamp(receipt.get("reconciled_at"), label)
    seen = set()
    for adjustment in receipt["adjustments"]:
        if (
            not isinstance(adjustment, dict)
            or set(adjustment) != {"target", "quantity_delta"}
            or adjustment.get("target") not in SYMBOLS
            or adjustment["target"] in seen
        ):
            raise RuntimeError(f"{label} is malformed")
        seen.add(adjustment["target"])
        delta = _finite_number(
            adjustment.get("quantity_delta"),
            f"{label} adjustment for {adjustment['target']}",
        )
        if delta == 0:
            raise RuntimeError(f"{label} is malformed")
    return receipt


def load_holdings_intent():
    if not HOLDINGS_INTENT_PATH.is_file():
        return None
    try:
        intent = json.loads(HOLDINGS_INTENT_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("local holdings reconciliation intent is unreadable") from error
    if not isinstance(intent, dict) or set(intent) != {
        "version",
        "snapshot",
        "receipt",
    } or intent.get("version") != 1 or isinstance(intent.get("version"), bool):
        raise RuntimeError("local holdings reconciliation intent is malformed")
    snapshot_value = intent.get("snapshot")
    if not isinstance(snapshot_value, dict):
        raise RuntimeError("local holdings reconciliation intent is malformed")
    snapshot = parse_holdings_snapshot(canonical(snapshot_value))
    receipt = intent.get("receipt")
    try:
        _validate_holdings_receipt(receipt, "local holdings reconciliation intent")
    except RuntimeError as error:
        raise RuntimeError("local holdings reconciliation intent is malformed") from error
    if receipt.get("snapshot_hash") != snapshot["canonical_hash"]:
        raise RuntimeError("local holdings reconciliation intent is malformed")
    return intent


def save_holdings_intent(intent):
    HOLDINGS_INTENT_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = HOLDINGS_INTENT_PATH.with_suffix(".new")
    temporary_path.write_text(canonical(intent), encoding="utf-8")
    os.replace(temporary_path, HOLDINGS_INTENT_PATH)


def clear_holdings_intent():
    try:
        HOLDINGS_INTENT_PATH.unlink()
    except FileNotFoundError:
        pass


def _finalize_holdings_intent(intent, token, reporting, *, now=None):
    calculation = ghostfolio_portfolio_calculation_with_fx_repair(
        token, reporting, now=now
    )
    if calculation["portfolio_calculation_has_error"]:
        raise RuntimeError("Ghostfolio portfolio calculation is incomplete")
    fresh_payload = gist()
    receipt = intent["receipt"]
    existing = holdings_receipt_for_snapshot(
        fresh_payload, receipt["snapshot_hash"]
    )
    if existing is not None:
        if existing != receipt:
            raise RuntimeError("holdings receipt conflict")
    elif not append_named_receipt(
        fresh_payload,
        HOLDINGS_RECEIPT_FILE,
        "snapshot_hash",
        receipt,
    ):
        raise RuntimeError("holdings receipt conflict")
    clear_holdings_intent()
    return calculation


def reconcile_holdings_snapshot(
    *, commit=False, token=None, now=None, gist_payload=None
):
    # One sync cycle must use one immutable outbox view. Receipt publication
    # deliberately rereads only at the append boundary to preserve concurrent
    # append-only records.
    payload = gist_payload if gist_payload is not None else gist()
    snapshot = parse_holdings_snapshot(file_content(payload, HOLDINGS_SNAPSHOT_FILE))
    events = parse_events(file_content(payload, EVENT_FILE))
    intent = load_holdings_intent()
    current_validated = False
    if intent is None:
        validate_snapshot_freshness(snapshot, now=now)
        validate_snapshot_monotonicity(snapshot)
        validate_snapshot_event_order(snapshot, events)
        current_validated = True
    token = token or ghostfolio_token()
    reporting = prepare_ghostfolio_fx_bridge(token, now=now)
    kraken_account_id = reporting["kraken_account_id"]
    if snapshot is None and intent is None:
        calculation = ghostfolio_portfolio_calculation_with_fx_repair(
            token, reporting, now=now
        )
        return {
            "status": "NO_SNAPSHOT",
            "drift": {},
            "snapshot_as_of": None,
            "snapshot_hash": None,
            **calculation,
        }
    actual = ghostfolio_quantities(token, kraken_account_id)
    recovered_intent = False
    resume_intent = False

    if intent is not None:
        intent_snapshot = intent["snapshot"]
        intent_hash = intent_snapshot["canonical_hash"]
        matching_receipt = holdings_receipt_for_snapshot(payload, intent_hash)
        if matching_receipt is not None:
            if matching_receipt != intent["receipt"]:
                raise RuntimeError("holdings receipt conflict")
            if commit:
                clear_holdings_intent()
            intent = None
        else:
            intent_drift = holdings_drift(intent_snapshot, actual)
            same_snapshot = (
                snapshot is not None
                and intent_hash == snapshot["canonical_hash"]
            )
            # An exact match to the durable transaction watermark proves that
            # every old adjustment landed, even when the current Gist now has
            # a newer signed snapshot. Finalize the old receipt first.
            can_finalize = not intent_drift
            if commit and can_finalize:
                _finalize_holdings_intent(
                    intent, token, reporting, now=now
                )
                recovered_intent = True
                intent = None
                resume_intent = same_snapshot
            elif same_snapshot:
                # The intent was persisted only after freshness, event-order,
                # and every dry-run check passed. It may therefore resume even
                # if the same snapshot aged while Ghostfolio was unavailable.
                resume_intent = True
            elif commit:
                raise RuntimeError(
                    "pending holdings reconciliation cannot be proven against "
                    "the latest snapshot"
                )

    if not current_validated:
        if not resume_intent:
            validate_snapshot_freshness(snapshot, now=now)
        validate_snapshot_monotonicity(snapshot)
        validate_snapshot_event_order(snapshot, events)
    if snapshot is None:
        calculation = ghostfolio_portfolio_calculation_with_fx_repair(
            token, reporting, now=now
        )
        return {
            "status": "NO_SNAPSHOT",
            "drift": {},
            "snapshot_as_of": None,
            "snapshot_hash": None,
            **calculation,
        }
    drift = holdings_drift(snapshot, actual)
    existing_receipt = holdings_receipt_for_snapshot(
        payload, snapshot["canonical_hash"]
    )
    if existing_receipt is not None and drift:
        raise RuntimeError("previously receipted holdings snapshot has new drift")
    if not commit or not drift:
        calculation = ghostfolio_portfolio_calculation_with_fx_repair(
            token, reporting, now=now
        )
        return {
            "status": "DRIFT"
            if drift
            else ("RECONCILED" if recovered_intent else "IN_SYNC"),
            "drift": drift,
            "snapshot_as_of": snapshot["as_of"],
            "snapshot_hash": snapshot["canonical_hash"],
            **calculation,
        }

    base = os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/import"
    prepared = []
    # Preflight every adjustment before persisting an intent or mutating the
    # local portfolio. A later retry can safely repeat these dry-runs.
    for target, difference in drift.items():
        activity = holdings_import_payload(snapshot, target, difference)
        dry_status, dry_result = request_json(
            base + "?" + urlencode({"dryRun": "true"}),
            method="POST", token=token, payload=activity,
        )
        duplicate = dry_status == 400 and is_exact_duplicate(dry_result)
        if dry_status not in {200, 201} and not duplicate:
            raise RuntimeError(f"Ghostfolio holdings dry-run conflict for {target}")
        prepared.append((target, difference, activity, duplicate))

    if intent is None:
        intent = {
            "version": 1,
            "snapshot": snapshot,
            "receipt": {
                "snapshot_hash": snapshot["canonical_hash"],
                "reconciled_at": time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                ),
                "adjustments": [
                    {
                        "target": target,
                        "quantity_delta": format(difference, ".16g"),
                    }
                    for target, difference in drift.items()
                ],
            },
        }
        save_holdings_intent(intent)
    for target, _difference, activity, duplicate in prepared:
        if not duplicate:
            status, result = request_json(base, method="POST", token=token, payload=activity)
            if status not in {200, 201} and not (
                status == 400 and is_exact_duplicate(result)
            ):
                raise RuntimeError(f"Ghostfolio holdings import failed for {target}")

    # Imports do not emit Ghostfolio's portfolio-changed event. Flush before
    # convergence so the quantity read cannot be served from the old cache.
    flush_ghostfolio_cache(token)
    remaining_drift = holdings_drift(
        snapshot, ghostfolio_quantities(token, kraken_account_id)
    )
    if remaining_drift:
        raise RuntimeError(
            "Ghostfolio holdings reconciliation did not converge for: "
            + ", ".join(sorted(remaining_drift))
        )

    calculation = ghostfolio_portfolio_calculation_with_fx_repair(
        token, reporting, now=now
    )
    if calculation["portfolio_calculation_has_error"]:
        raise RuntimeError("Ghostfolio portfolio calculation is incomplete")
    receipt = intent["receipt"]
    if not append_named_receipt(
        gist(), HOLDINGS_RECEIPT_FILE, "snapshot_hash", receipt
    ):
        raise RuntimeError("holdings receipt conflict")
    clear_holdings_intent()
    return {
        "status": "RECONCILED",
        "drift": drift,
        "snapshot_as_of": snapshot["as_of"],
        "snapshot_hash": snapshot["canonical_hash"],
        **calculation,
    }


def recover_holdings_intent_before_events(
    gist_payload, events, event_receipts, token, *, now=None
):
    """Complete an older local holdings transaction before newer event imports."""
    intent = load_holdings_intent()
    if intent is None:
        return None
    intent_snapshot = intent["snapshot"]
    intent_hash = intent_snapshot["canonical_hash"]
    existing = holdings_receipt_for_snapshot(gist_payload, intent_hash)
    if existing is not None:
        if existing != intent["receipt"]:
            raise RuntimeError("holdings receipt conflict")
        clear_holdings_intent()
        return {"status": "RECEIPT_CONFIRMED"}

    intent_time = _parse_timestamp(
        intent_snapshot["as_of"], "holdings intent snapshot"
    )
    prior_events = []
    for event in events:
        event_time = _parse_timestamp(
            event["occurred_at"], f"portfolio event {event['event_id']}"
        )
        was_imported = event["event_id"] in event_receipts
        if event_time <= intent_time:
            if not was_imported:
                raise RuntimeError(
                    "portfolio event at or before the pending holdings intent "
                    "has no Ghostfolio receipt"
                )
            prior_events.append(event)
        elif was_imported:
            raise RuntimeError(
                "a newer PortfolioEvent was imported before the pending "
                "holdings intent could be recovered"
            )

    recovery_payload = dict(gist_payload)
    recovery_files = dict(gist_payload.get("files", {}))
    recovery_files[HOLDINGS_SNAPSHOT_FILE] = {
        "content": canonical(intent_snapshot)
    }
    recovery_files[EVENT_FILE] = {
        "content": "".join(canonical(event) + "\n" for event in prior_events)
    }
    recovery_payload["files"] = recovery_files
    result = reconcile_holdings_snapshot(
        commit=True,
        token=token,
        now=now,
        gist_payload=recovery_payload,
    )
    if HOLDINGS_INTENT_PATH.is_file():
        raise RuntimeError("pending holdings intent did not recover")
    return result


def sync_once():
    payload = gist()
    events = parse_events(file_content(payload, EVENT_FILE))
    receipts = parse_event_receipts(
        file_content(payload, RECEIPT_FILE), events
    )
    token = ghostfolio_token()
    recover_holdings_intent_before_events(payload, events, receipts, token)
    reporting_accounts = verify_ghostfolio_reporting_accounts(
        token, require_bitkub=False
    )
    verify_ghostfolio_account_map(reporting_accounts, require_bitkub=False)
    for event in events:
        if event["event_id"] in receipts:
            continue
        activity = import_payload(event)
        base = os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/import"
        dry_status, dry_result = request_json(
            base + "?" + urlencode({"dryRun": "true"}),
            method="POST",
            token=token,
            payload=activity,
        )
        duplicate = dry_status == 400 and is_exact_duplicate(dry_result)
        if dry_status not in {200, 201} and not duplicate:
            raise RuntimeError(f"Ghostfolio dry-run conflict for {event['event_id']}")
        activity_id = "exact-duplicate"
        if not duplicate:
            status, result = request_json(base, method="POST", token=token, payload=activity)
            if status not in {200, 201}:
                if not (status == 400 and is_exact_duplicate(result)):
                    raise RuntimeError(f"Ghostfolio import failed for {event['event_id']}")
            else:
                imported = result.get("activities") if isinstance(result, dict) else None
                imported_id = (
                    imported[0].get("id")
                    if isinstance(imported, list)
                    and imported
                    and isinstance(imported[0], dict)
                    else None
                )
                if not isinstance(imported_id, str) or not imported_id:
                    raise RuntimeError(
                        f"Ghostfolio import acknowledgement is incomplete for "
                        f"{event['event_id']}"
                    )
                activity_id = imported_id
        receipt = {
            "order_id": event["event_id"],
            "event_hash": event["canonical_hash"],
            "ghostfolio_activity_id": activity_id,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not append_receipt(gist(), receipt):
            raise RuntimeError(f"receipt conflict for {event['event_id']}")
        receipts.add(event["event_id"])
    holdings = reconcile_holdings_snapshot(
        commit=True, token=token, gist_payload=payload
    )
    STATE_PATH.write_text(
        canonical({
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_count": len(events),
            "receipt_count": len(receipts),
            "holdings_status": holdings["status"],
            "holdings_drift_targets": sorted(holdings["drift"]),
            "holdings_snapshot_as_of": holdings["snapshot_as_of"],
            "holdings_snapshot_hash": holdings["snapshot_hash"],
            "portfolio_calculation_status": holdings[
                "portfolio_calculation_status"
            ],
            "portfolio_calculation_has_error": holdings[
                "portfolio_calculation_has_error"
            ],
        }),
        encoding="utf-8",
    )


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "health":
        if not STATE_PATH.is_file():
            return 1
        if HOLDINGS_INTENT_PATH.is_file():
            return 1
        maximum_age = max(900, int(os.environ.get("SYNC_INTERVAL_SECONDS", "300")) * 3)
        if time.time() - STATE_PATH.stat().st_mtime > maximum_age:
            return 1
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return 1
        if state.get("holdings_status") not in {"IN_SYNC", "RECONCILED"}:
            return 1
        if state.get("portfolio_calculation_has_error") is not False:
            return 1
        snapshot_hash = state.get("holdings_snapshot_hash")
        if not isinstance(snapshot_hash, str) or len(snapshot_hash) != 64:
            return 1
        try:
            validate_snapshot_freshness({"as_of": state["holdings_snapshot_as_of"]})
        except (KeyError, RuntimeError, ValueError):
            return 1
        return 0
    if command == "once":
        sync_once()
        return 0
    if command == "reconcile-holdings":
        result = reconcile_holdings_snapshot(commit=True)
        print(canonical({
            "status": result["status"],
            "targets": sorted(result["drift"]),
        }))
        return 0
    interval = int(os.environ.get("SYNC_INTERVAL_SECONDS", "300"))
    while True:
        try:
            sync_once()
        except (KeyError, ValueError, RuntimeError, URLError) as error:
            print(f"sync blocked: {type(error).__name__}: {error}", flush=True)
        time.sleep(max(60, interval))


if __name__ == "__main__":
    raise SystemExit(main())

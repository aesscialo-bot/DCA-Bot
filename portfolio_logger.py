"""Best-effort Ghostfolio logging for Kraken trades settled in GBP.

Kraken remains the source of truth for the GBP trade. Ghostfolio's crypto
profiles are USD-denominated, so this module alone converts the GBP unit price
to USD immediately before importing an optional portfolio activity.
"""

import math
import os
import time
from datetime import datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

import requests


GHOSTFOLIO_URL = os.environ.get("GHOSTFOLIO_URL", "https://ghostfol.io")
GHOSTFOLIO_TOKEN = os.environ.get("GHOSTFOLIO_TOKEN")

# Some ticker strings identify the wrong asset through Yahoo. These mappings
# deliberately use Ghostfolio's exact provider identifier.
SYMBOL_DATASOURCE_OVERRIDES = {
    "HYPE": {"dataSource": "COINGECKO", "symbol": "hyperliquid"},
    "SUI": {"dataSource": "COINGECKO", "symbol": "sui"},
}
AMBIGUOUS_SYMBOLS = {"HYPE"}

IMPORT_RETRY_ATTEMPTS = 3
IMPORT_RETRY_DELAY_SECONDS = 2
RETRYABLE_IMPORT_STATUS_CODES = {408, 425, 429}
ROI_LOOKUP_TIMEOUT_SECONDS = 10
FX_LOOKUP_TIMEOUT_SECONDS = 5

TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)


def get_account_id(symbol, portfolio_map):
    """Return a symbol-specific Ghostfolio account, then DEFAULT if present."""
    if not portfolio_map:
        print("No PORTFOLIO_ACCOUNT_MAP configured.")
        return None

    account_id = portfolio_map.get(symbol.upper())
    if not account_id:
        account_id = portfolio_map.get("DEFAULT")
        if account_id:
            print(f"   Using DEFAULT account for {symbol.upper()}")
    return account_id


def authenticate_ghostfolio(base_url, access_token, timeout=30, retries=3, delay=2):
    """Exchange a Ghostfolio access token for a short-lived bearer token."""
    url = f"{base_url}/api/v1/auth/anonymous"
    payload = {"accessToken": access_token}

    for attempt in range(1, retries + 1):
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            if response.status_code != 201:
                print(
                    "Ghostfolio auth failed "
                    f"(HTTP {response.status_code}): "
                    f"{_safe_response_body(response, [access_token])}"
                )
                return None

            token = response.json().get("authToken")
            if not token:
                print("Ghostfolio auth response did not include authToken.")
                return None
            return token
        except requests.exceptions.Timeout:
            print(f"Ghostfolio auth timed out (attempt {attempt}/{retries}).")
        except (
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
        ) as error:
            print(
                "Ghostfolio auth connection error "
                f"(attempt {attempt}/{retries}): {error}"
            )
        except Exception as error:
            print(f"Ghostfolio authentication error: {error}")
            return None

        if attempt < retries:
            time.sleep(delay)

    print(f"Ghostfolio authentication failed after {retries} attempts.")
    return None


def _validate_gbp_pair(symbol, exchange_pair):
    """Reject a non-GBP exchange pair before portfolio data can be written."""
    if not exchange_pair:
        return

    normalized_pair = exchange_pair.strip().upper().replace("_", "/")
    expected_pair = f"{symbol.strip().upper()}/GBP"
    if normalized_pair != expected_pair:
        raise ValueError(
            f"Expected Kraken GBP pair {expected_pair}, got {exchange_pair}"
        )


def resolve_ghostfolio_asset(symbol, exchange_pair=None):
    """Resolve a Kraken base ticker to Ghostfolio's USD provider profile."""
    base_symbol = symbol.strip().upper()
    _validate_gbp_pair(base_symbol, exchange_pair)
    override = SYMBOL_DATASOURCE_OVERRIDES.get(base_symbol)

    if override:
        resolution = {
            "dataSource": override["dataSource"],
            "symbol": override["symbol"],
            "providerIdentifier": override["symbol"],
            "usedExplicitMapping": True,
        }
    elif base_symbol in AMBIGUOUS_SYMBOLS:
        raise ValueError(
            f"Ambiguous Ghostfolio asset ticker {base_symbol} has no explicit mapping"
        )
    else:
        provider_symbol = f"{base_symbol}USD"
        resolution = {
            "dataSource": "YAHOO",
            "symbol": provider_symbol,
            "providerIdentifier": provider_symbol,
            "usedExplicitMapping": False,
        }

    print(
        "   Ghostfolio asset resolution: "
        f"pair={exchange_pair or 'unknown'}, base={base_symbol}, "
        f"requested_symbol={resolution['symbol']}, "
        f"data_source={resolution['dataSource']}, "
        f"method={'explicit_mapping' if resolution['usedExplicitMapping'] else 'fallback'}"
    )
    return resolution


def _positive_finite_number(value, field_name):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise ValueError(f"{field_name} must be a positive finite number")
    return number


def get_gbp_usd_rate(trade_timestamp=None):
    """Return an execution-date GBP-to-USD rate for Ghostfolio only."""
    if trade_timestamp is not None:
        trade_date = datetime.fromtimestamp(
            float(trade_timestamp), tz=dt_timezone.utc
        ).date()
    else:
        trade_date = datetime.now(dt_timezone.utc).date()

    if trade_date < datetime.now(dt_timezone.utc).date():
        sources = (
            (
                "Frankfurter historical",
                f"https://api.frankfurter.app/{trade_date.isoformat()}?from=GBP&to=USD",
            ),
        )
    else:
        sources = (
            ("Frankfurter", "https://api.frankfurter.app/latest?from=GBP&to=USD"),
            ("ExchangeRate-API", "https://open.er-api.com/v6/latest/GBP"),
        )

    for source_name, url in sources:
        try:
            response = requests.get(url, timeout=FX_LOOKUP_TIMEOUT_SECONDS)
            response.raise_for_status()
            rate = _positive_finite_number(
                response.json()["rates"]["USD"], "GBP-to-USD rate"
            )
            return rate
        except (
            KeyError,
            TypeError,
            ValueError,
            requests.RequestException,
        ) as error:
            print(f"{source_name} GBP-to-USD lookup failed: {error}")
        except Exception as error:
            print(f"{source_name} GBP-to-USD lookup failed: {error}")

    print("GBP-to-USD conversion unavailable; skipping optional Ghostfolio log.")
    return None


def get_asset_roi_percent(symbol, account_id, exchange_pair=None, bearer_token=None):
    """Return Ghostfolio's account-scoped ROI percentage for one asset."""
    if not GHOSTFOLIO_TOKEN and not bearer_token:
        print("GHOSTFOLIO_TOKEN not set. Cannot fetch Ghostfolio asset ROI.")
        return None
    if not account_id:
        print("No account ID provided. Cannot fetch Ghostfolio asset ROI.")
        return None

    try:
        if not bearer_token:
            bearer_token = authenticate_ghostfolio(
                GHOSTFOLIO_URL,
                GHOSTFOLIO_TOKEN,
                timeout=ROI_LOOKUP_TIMEOUT_SECONDS,
                retries=1,
            )
            if not bearer_token:
                return None

        resolution = resolve_ghostfolio_asset(symbol, exchange_pair=exchange_pair)
        response = requests.get(
            f"{GHOSTFOLIO_URL}/api/v1/portfolio/holdings",
            headers={"Authorization": f"Bearer {bearer_token}"},
            params={
                "accounts": account_id,
                "dataSource": resolution["dataSource"],
                "range": "max",
                "symbol": resolution["symbol"],
            },
            timeout=ROI_LOOKUP_TIMEOUT_SECONDS,
        )
        if response.status_code != 200:
            print(
                "Ghostfolio asset ROI lookup failed "
                f"(HTTP {response.status_code}): "
                f"{_safe_response_body(response, [f'Bearer {bearer_token}'])}"
            )
            return None

        holdings = response.json().get("holdings")
        if not isinstance(holdings, list):
            print("Ghostfolio asset ROI response has no valid holdings list.")
            return None

        matching_holdings = [
            holding
            for holding in holdings
            if holding.get("dataSource") == resolution["dataSource"]
            and holding.get("symbol") == resolution["symbol"]
        ]
        if len(matching_holdings) != 1:
            print(
                "Ghostfolio asset ROI lookup returned "
                f"{len(matching_holdings)} matching holdings."
            )
            return None

        holding = matching_holdings[0]
        net_performance = _finite_number(
            holding.get("netPerformanceWithCurrencyEffect"), "net performance"
        )
        investment = _positive_finite_number(
            holding.get("investment"), "investment"
        )
        roi_percent = (net_performance / investment) * 100
        print(
            "   Ghostfolio asset ROI: "
            f"asset={resolution['dataSource']}/{resolution['symbol']}, "
            f"roi={roi_percent:.2f}%"
        )
        return roi_percent
    except (
        requests.exceptions.Timeout,
        requests.exceptions.ConnectionError,
        requests.exceptions.SSLError,
    ) as error:
        print(f"Ghostfolio asset ROI request failed: {error}")
    except (TypeError, ValueError) as error:
        print(f"Ghostfolio asset ROI returned invalid data: {error}")
    except Exception as error:
        print(f"Ghostfolio asset ROI lookup failed: {error}")
    return None


def _finite_number(value, field_name):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{field_name} must be finite")
    return number


def build_ghostfolio_activity(
    trade_data,
    symbol,
    account_id,
    exchange_pair=None,
    gbp_usd_rate=None,
):
    """Build a USD-provider activity from an authoritative GBP Kraken trade."""
    timestamp = _finite_number(trade_data["ts"], "trade timestamp")
    quantity = _positive_finite_number(
        trade_data["amount_crypto"], "crypto amount"
    )
    amount_gbp = _positive_finite_number(trade_data["amount_gbp"], "GBP spend")
    cost_gbp = _positive_finite_number(trade_data["cost_gbp"], "GBP order cost")
    fee_gbp = _finite_number(trade_data["fee_gbp"], "GBP fee")
    gbp_fee_debit = _finite_number(
        trade_data["gbp_fee_debit"], "GBP fee debit"
    )
    if fee_gbp < 0 or gbp_fee_debit < 0:
        raise ValueError("GBP fee cannot be negative")
    if abs(amount_gbp - (cost_gbp + gbp_fee_debit)) > 0.01:
        raise ValueError("GBP spend must equal order cost plus GBP fee debit")
    gbp_price = _positive_finite_number(
        trade_data["gbp_price_per_unit"], "GBP unit price"
    )
    fx_rate = _positive_finite_number(gbp_usd_rate, "GBP-to-USD rate")
    resolution = resolve_ghostfolio_asset(symbol, exchange_pair=exchange_pair)

    date = datetime.fromtimestamp(timestamp, tz=SELECTED_TZ)
    date_text = date.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    order_id = str(trade_data.get("order_id", "unknown")).replace("\n", " ")

    return {
        "accountId": account_id,
        "comment": (
            f"GBP {amount_gbp:.2f} total on Kraken "
            f"(cost {cost_gbp:.2f} + GBP debit fee {gbp_fee_debit:.2f}; "
            f"fee equivalent {fee_gbp:.2f}) | order {order_id}"
        ),
        "currency": "USD",
        "dataSource": resolution["dataSource"],
        "date": date_text,
        # Ghostfolio's fee is an economic cost. For a base-asset fee, quantity
        # is already net of that fee, so adding its GBP equivalent reconstructs
        # the confirmed gross order cost without claiming an extra GBP cash debit.
        "fee": round(fee_gbp * fx_rate, 4),
        "quantity": float(f"{quantity:.8f}"),
        "symbol": resolution["symbol"],
        "type": "BUY",
        "unitPrice": round(gbp_price * fx_rate, 4),
    }


def _safe_response_body(response, redacted_values=()):
    """Return a bounded response body with credentials removed."""
    body = response.text[:1000]
    for value in (GHOSTFOLIO_TOKEN, *redacted_values):
        if value:
            body = body.replace(value, "[redacted]")
    return body


def _response_messages(response):
    try:
        response_data = response.json()
    except ValueError:
        return []

    messages = response_data.get("message", [])
    if isinstance(messages, str):
        return [messages]
    if isinstance(messages, list):
        return [str(message) for message in messages]
    return []


def _is_retryable_import_response(response):
    if response.status_code in RETRYABLE_IMPORT_STATUS_CODES:
        return True
    if response.status_code >= 500:
        return True
    if response.status_code != 400:
        return False
    return any(
        "is not valid for the specified data source" in message.lower()
        for message in _response_messages(response)
    )


def _post_import(url, headers, payload, stage):
    """Post an import request with bounded retries for transient failures."""
    for attempt in range(1, IMPORT_RETRY_ATTEMPTS + 1):
        try:
            response = requests.post(url, headers=headers, json=payload, timeout=30)
        except (
            requests.exceptions.Timeout,
            requests.exceptions.ConnectionError,
            requests.exceptions.SSLError,
        ) as error:
            retryable = True
            failure = f"request error: {error}"
        else:
            if response.status_code == 201:
                return response

            retryable = _is_retryable_import_response(response)
            failure = (
                f"HTTP {response.status_code}: "
                f"{_safe_response_body(response, [headers.get('Authorization')])}"
            )
            if not retryable:
                print(f"Ghostfolio {stage} failed ({failure}).")
                return response

        print(
            f"Ghostfolio {stage} failed "
            f"(attempt {attempt}/{IMPORT_RETRY_ATTEMPTS}; {failure})."
        )
        if retryable and attempt < IMPORT_RETRY_ATTEMPTS:
            time.sleep(IMPORT_RETRY_DELAY_SECONDS)

    print(
        f"Ghostfolio {stage} failed after "
        f"{IMPORT_RETRY_ATTEMPTS} attempts: {failure}."
    )
    return None


def validate_ghostfolio_resolution(activity, dry_run_response):
    """Ensure Ghostfolio resolved the exact provider identity requested."""
    activities = dry_run_response.get("activities", [])
    if len(activities) != 1:
        raise ValueError(
            f"Ghostfolio dry run returned {len(activities)} activities; expected 1"
        )

    result = activities[0]
    result_error = result.get("error")
    if result_error:
        if isinstance(result_error, dict) and result_error.get("code") == "IS_DUPLICATE":
            return "duplicate"
        raise ValueError(f"Ghostfolio asset resolution failed: {result_error}")

    profile = result.get("SymbolProfile") or {}
    selected_data_source = profile.get("dataSource")
    selected_symbol = profile.get("symbol")
    if (
        selected_data_source != activity["dataSource"]
        or selected_symbol != activity["symbol"]
    ):
        raise ValueError(
            "Ghostfolio asset resolution mismatch: "
            f"requested {activity['dataSource']}/{activity['symbol']}, "
            f"selected {selected_data_source}/{selected_symbol}"
        )
    return "valid"


def log_to_ghostfolio(
    trade_data, symbol, account_id, exchange_pair=None, bearer_token=None
):
    """Optionally mirror one completed Kraken GBP purchase into Ghostfolio.

    Expected trade fields are ``ts``, ``amount_crypto``, ``cost_gbp``,
    ``fee_gbp`` (economic GBP equivalent), ``gbp_fee_debit``, ``amount_gbp``
    (total GBP cash debit), ``gbp_price_per_unit``, and ``order_id``. Every
    failure returns ``False``;
    portfolio logging never changes the outcome of the Kraken trade.
    """
    if not GHOSTFOLIO_TOKEN and not bearer_token:
        print("GHOSTFOLIO_TOKEN not set. Skipping Ghostfolio logging.")
        return False
    if not account_id:
        print("No account ID provided. Skipping Ghostfolio logging.")
        return False

    try:
        if not bearer_token:
            bearer_token = authenticate_ghostfolio(
                GHOSTFOLIO_URL, GHOSTFOLIO_TOKEN, timeout=30
            )
            if not bearer_token:
                return False

        fx_rate = get_gbp_usd_rate(trade_data["ts"])
        if fx_rate is None:
            return False

        activity = build_ghostfolio_activity(
            trade_data,
            symbol,
            account_id,
            exchange_pair=exchange_pair,
            gbp_usd_rate=fx_rate,
        )
        quantity = activity["quantity"]
        url = f"{GHOSTFOLIO_URL}/api/v1/import"
        headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Content-Type": "application/json",
        }
        payload = {"activities": [activity]}

        dry_run = _post_import(
            f"{url}?dryRun=true", headers, payload, "asset-resolution dry run"
        )
        if dry_run is None or dry_run.status_code != 201:
            return False
        dry_run_state = validate_ghostfolio_resolution(activity, dry_run.json())
        if dry_run_state == "duplicate":
            print(
                "Ghostfolio activity already exists: "
                f"{quantity:.8f} {symbol.upper()} at USD {activity['unitPrice']:.4f}."
            )
            return True

        response = _post_import(url, headers, payload, "import")
        if response is None or response.status_code != 201:
            return False
        if validate_ghostfolio_resolution(activity, response.json()) != "valid":
            print("Ghostfolio import unexpectedly returned a duplicate activity.")
            return False

        print(
            "Successfully logged to Ghostfolio: "
            f"{quantity:.8f} {symbol.upper()} at USD {activity['unitPrice']:.4f} "
            f"(source spend GBP {float(trade_data['amount_gbp']):.2f})."
        )
        return True
    except Exception as error:
        print(f"Ghostfolio logging error: {error}")
        return False


if __name__ == "__main__":
    print("This module is called by crypto_dca.py after a completed Kraken order.")

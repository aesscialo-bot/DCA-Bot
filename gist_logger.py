"""Best-effort audit logging for Kraken GBP-funded USD-market purchases."""

import math
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_FILENAME = "kraken_usd_dca_trade_log.md"
GIST_REQUEST_TIMEOUT_SECONDS = 10

TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)

TABLE_HEADER = (
    "# Kraken USD DCA Trade Log\n\n"
    "Kraken is the source of truth. Ghostfolio is an optional mirror.\n\n"
    "| Date | GBP funding debit | GBP/USD | USD funded | USD crypto cost | "
    "Kraken fees | Target price (USD) | Crypto received | Funding order | "
    "Crypto order | Ghostfolio mirror |\n"
    "| --- | ---: | ---: | ---: | ---: | --- | ---: | ---: | --- | --- | :---: |\n"
)


def _clean_table_value(value):
    """Keep external identifiers from breaking the Markdown table."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _finite(value, name, *, allow_zero=False):
    number = float(value)
    if not math.isfinite(number) or number < 0 or (number == 0 and not allow_zero):
        qualifier = "non-negative" if allow_zero else "positive"
        raise ValueError(f"{name} must be a {qualifier} finite number")
    return number


def _validated_trade_values(trade_data):
    """Extract and validate the authoritative two-leg Kraken fill fields."""
    quote_currency = str(trade_data.get("quote_currency", "")).upper()
    if quote_currency != "USD":
        raise ValueError("Gist logger requires a Kraken USD-market trade")

    values = {
        "timestamp": _finite(trade_data["ts"], "trade timestamp"),
        "amount_gbp": _finite(trade_data["amount_gbp"], "GBP funding debit"),
        "gbp_usd_rate": _finite(trade_data["gbp_usd_rate"], "GBP/USD rate"),
        "funded_usd": _finite(trade_data["funded_usd"], "funded USD"),
        "cost_usd": _finite(trade_data["cost_usd"], "USD crypto cost"),
        "fee_usd": _finite(
            trade_data.get("fee_usd", 0), "USD crypto fee", allow_zero=True
        ),
        "amount_crypto": _finite(trade_data["amount_crypto"], "crypto amount"),
        "usd_price": _finite(
            trade_data["usd_price_per_unit"], "USD unit price"
        ),
    }
    usd_fee_debit = _finite(
        trade_data.get("usd_fee_debit", 0), "USD fee debit", allow_zero=True
    )
    if values["cost_usd"] + usd_fee_debit > values["funded_usd"] + 0.01:
        raise ValueError("USD crypto debit cannot exceed confirmed funded USD")

    fee_details = trade_data.get("fee_details") or []
    if not isinstance(fee_details, list):
        raise ValueError("Kraken fee details must be a list")
    if fee_details:
        fee_text = ", ".join(
            f"{float(item['amount']):.8f} {_clean_table_value(item['currency'])} "
            f"(GBP equiv {float(item['gbp_equivalent']):.2f})"
            for item in fee_details
        )
    else:
        fee_gbp = _finite(
            trade_data.get("fee_gbp", 0), "GBP fee equivalent", allow_zero=True
        )
        fee_text = f"GBP equivalent {fee_gbp:.2f}"
    values["fee_text"] = fee_text
    return values


def update_gist_log(trade_data, symbol="BTC", saved_to_ghostfolio=False):
    """Append a confirmed Kraken two-leg purchase to the optional audit Gist.

    Logging is deliberately best-effort: a Gist outage never changes a Kraken
    order's success. ``True`` means the Gist PATCH succeeded; all skipped or
    failed attempts return ``False``.
    """
    if not GIST_ID or not GIST_TOKEN:
        print("GIST_ID or GIST_TOKEN not set. Skipping optional Gist audit log.")
        return False

    try:
        values = _validated_trade_values(trade_data)
        headers = {
            "Authorization": f"Bearer {GIST_TOKEN}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        url = f"https://api.github.com/gists/{GIST_ID}"

        response = requests.get(
            url, headers=headers, timeout=GIST_REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        gist = response.json()
        current_content = (
            gist.get("files", {}).get(GIST_FILENAME, {}).get("content", "")
        )

        if not current_content.strip():
            current_content = TABLE_HEADER
        elif not current_content.endswith("\n"):
            current_content += "\n"

        timestamp_text = datetime.fromtimestamp(
            values["timestamp"], tz=SELECTED_TZ
        ).strftime("%Y-%m-%d %H:%M %Z")
        crypto_text = (
            f"{values['amount_crypto']:.8f} {_clean_table_value(symbol.upper())}"
        )
        funding_order_id = _clean_table_value(
            trade_data.get("funding_order_id", "unknown")
        )
        order_id = _clean_table_value(trade_data.get("order_id", "unknown"))
        mirrored = "yes" if saved_to_ghostfolio else "optional/not saved"
        row = (
            f"| {timestamp_text} | GBP {values['amount_gbp']:.2f} | "
            f"{values['gbp_usd_rate']:.6f} | USD {values['funded_usd']:.4f} | "
            f"USD {values['cost_usd']:.4f} | {values['fee_text']} | "
            f"USD {values['usd_price']:,.4f} | {crypto_text} | "
            f"{funding_order_id} | {order_id} | {mirrored} |\n"
        )

        response = requests.patch(
            url,
            headers=headers,
            json={"files": {GIST_FILENAME: {"content": current_content + row}}},
            timeout=GIST_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(f"Optional Gist audit log updated for {symbol.upper()}.")
        return True
    except (KeyError, TypeError, ValueError, requests.RequestException) as error:
        print(f"Failed to update optional Gist audit ({type(error).__name__}).")
    except Exception as error:
        # Optional logging must remain nonblocking for unexpected response data.
        print(f"Failed to update optional Gist audit ({type(error).__name__}).")

    return False


if __name__ == "__main__":
    print("This optional logger is called after a completed Kraken order.")

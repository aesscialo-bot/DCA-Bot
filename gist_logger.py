"""Best-effort logging of Kraken GBP purchases to a GitHub Gist."""

import os
from datetime import datetime
from zoneinfo import ZoneInfo

import requests


GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_FILENAME = "kraken_gbp_trade_log.md"
GIST_REQUEST_TIMEOUT_SECONDS = 10

TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)

TABLE_HEADER = (
    "# Kraken GBP Trade Log\n\n"
    "| Date | Order cost | Kraken fee | Total GBP debit | Price (GBP) | Crypto received | Kraken order | Ghostfolio |\n"
    "| --- | ---: | ---: | ---: | ---: | ---: | --- | :---: |\n"
)


def _clean_table_value(value):
    """Keep external identifiers from breaking the Markdown table."""
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def _validated_trade_values(trade_data):
    """Extract the GBP-native fields required by the Gist row."""
    timestamp = float(trade_data["ts"])
    cost_gbp = float(trade_data["cost_gbp"])
    fee_gbp = float(trade_data["fee_gbp"])
    gbp_fee_debit = float(trade_data["gbp_fee_debit"])
    total_gbp = float(trade_data["amount_gbp"])
    amount_crypto = float(trade_data["amount_crypto"])
    gbp_price = float(trade_data["gbp_price_per_unit"])

    if cost_gbp <= 0 or fee_gbp < 0 or gbp_fee_debit < 0 or total_gbp <= 0:
        raise ValueError("GBP cost and total must be positive; fee cannot be negative")
    if amount_crypto <= 0 or gbp_price <= 0:
        raise ValueError("Crypto amount and GBP price must be positive")
    if abs(total_gbp - (cost_gbp + gbp_fee_debit)) > 0.01:
        raise ValueError("Total GBP debit must equal order cost plus GBP fee debit")

    fee_details = trade_data.get("fee_details") or []
    if not isinstance(fee_details, list) or not fee_details:
        raise ValueError("Kraken fee details are required")
    fee_text = ", ".join(
        f"{float(item['amount']):.8f} {_clean_table_value(item['currency'])} "
        f"(GBP equiv {float(item['gbp_equivalent']):.2f})"
        for item in fee_details
    )

    return timestamp, cost_gbp, fee_text, total_gbp, amount_crypto, gbp_price


def update_gist_log(trade_data, symbol="BTC", saved_to_ghostfolio=False):
    """Append one GBP purchase to the dedicated Gist file.

    Logging is deliberately best-effort: a Gist outage must never affect an
    already completed Kraken order. ``True`` means the Gist PATCH succeeded;
    all skipped or failed attempts return ``False``.
    """
    if not GIST_ID or not GIST_TOKEN:
        print("GIST_ID or GIST_TOKEN not set. Skipping Gist log.")
        return False

    try:
        (
            timestamp,
            cost_gbp,
            fee_text,
            total_gbp,
            amount_crypto,
            gbp_price,
        ) = _validated_trade_values(trade_data)
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
            timestamp, tz=SELECTED_TZ
        ).strftime("%Y-%m-%d %H:%M %Z")
        crypto_text = f"{amount_crypto:.8f} {_clean_table_value(symbol.upper())}"
        order_id = _clean_table_value(trade_data.get("order_id", "unknown"))
        saved = "yes" if saved_to_ghostfolio else "no"
        row = (
            f"| {timestamp_text} | GBP {cost_gbp:.2f} | {fee_text} | "
            f"GBP {total_gbp:.2f} | GBP {gbp_price:,.4f} | {crypto_text} | "
            f"{order_id} | {saved} |\n"
        )

        response = requests.patch(
            url,
            headers=headers,
            json={"files": {GIST_FILENAME: {"content": current_content + row}}},
            timeout=GIST_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(f"Gist log updated for {symbol.upper()} in {GIST_FILENAME}.")
        return True
    except (KeyError, TypeError, ValueError, requests.RequestException) as error:
        print(f"Failed to update Gist: {error}")
    except Exception as error:
        # Gist logging must remain nonblocking even for unexpected response data.
        print(f"Failed to update Gist: {error}")

    return False


if __name__ == "__main__":
    print("This module is called by crypto_dca.py after a completed Kraken order.")

"""Best-effort audit logging for Kraken GBP-funded USD-market purchases."""

import hashlib
import json
import math
import os
from datetime import datetime, timezone
from decimal import Decimal
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import requests


GIST_ID = os.environ.get("GIST_ID")
GIST_TOKEN = os.environ.get("GIST_TOKEN")
GIST_FILENAME = "kraken_usd_dca_trade_log.md"
GHOSTFOLIO_EVENTS_FILENAME = "kraken_usd_dca_ghostfolio_events.jsonl"
GIST_REQUEST_TIMEOUT_SECONDS = 10
GIST_DELIVERY_VERSION = 2
MAX_GIST_FILE_BYTES = 8_000_000

_DELIVERY_KEYS = (
    "version",
    "delivery_id",
    "created_at",
    "symbol",
    "row",
    "row_sha256",
    "event",
    "event_sha256",
)
_CRYPTO_ORDER_COLUMN = 9

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
    return (
        str(value)
        .replace("\\", "\\\\")
        .replace("|", "\\|")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


def _canonical_external_identifier(value, name):
    """Return a stable identifier while retaining Markdown-escapable pipes."""
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a non-empty string")
    identifier = value.replace("\r", " ").replace("\n", " ").strip()
    if not identifier or identifier.lower() in {"unknown", "none", "null"}:
        raise ValueError(f"{name} must be a confirmed external order ID")
    if any(
        ord(character) < 32 and character != "\t"
        for character in identifier
    ):
        raise ValueError(f"{name} contains an invalid control character")
    return identifier


def _canonical_symbol(value):
    if not isinstance(value, str):
        raise ValueError("symbol must be a non-empty string")
    symbol = value.replace("\r", " ").replace("\n", " ").strip().upper()
    if not symbol:
        raise ValueError("symbol must be a non-empty string")
    return symbol


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
        "funding_fee_usd": _finite(
            trade_data.get("funding_fee_usd", 0),
            "USD funding fee",
            allow_zero=True,
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


def _decimal_text(value):
    """Return a stable non-exponent decimal string for signed ledger data."""
    text = format(Decimal(str(value)).normalize(), "f")
    return "0" if text in {"-0", ""} else text


def _split_markdown_row(line):
    """Split one Markdown table row without treating escaped pipes as columns."""
    if not isinstance(line, str) or not line.startswith("|"):
        return None

    cells = []
    current = []
    escaped = False
    for character in line:
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)

        if character == "\\":
            escaped = not escaped
        else:
            escaped = False

    if current or not line.endswith("|") or len(cells) < 2 or cells[0]:
        return None
    return cells[1:]


def _unescape_table_value(value):
    result = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            next_character = value[index + 1]
            if next_character in {"\\", "|"}:
                result.append(next_character)
                index += 2
                continue
        result.append(value[index])
        index += 1
    return "".join(result)


def _delivery_row_status(content, delivery):
    """Return ``missing``, ``duplicate``, or ``conflict`` for a delivery ID."""
    target_line = delivery["row"].removesuffix("\n")
    matching_lines = []
    for line in content.splitlines():
        cells = _split_markdown_row(line)
        if not cells or len(cells) != 11:
            continue
        existing_order_id = _unescape_table_value(
            cells[_CRYPTO_ORDER_COLUMN]
        )
        if existing_order_id == delivery["delivery_id"]:
            matching_lines.append(line)

    if not matching_lines:
        return "missing"
    if all(line == target_line for line in matching_lines):
        return "duplicate"
    return "conflict"


def _delivery_event_status(content, delivery):
    """Return ``missing``, ``duplicate``, or ``conflict`` for a JSONL event."""
    expected = json.dumps(
        delivery["event"], sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    matches = []
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as error:
            raise ValueError("Ghostfolio event Gist contains malformed JSONL") from error
        if event.get("event_id") == delivery["delivery_id"]:
            matches.append(line)
    if not matches:
        return "missing"
    if all(line == expected for line in matches):
        return "duplicate"
    return "conflict"


def _safe_raw_gist_url(value):
    """Accept only GitHub's authenticated raw-Gist host."""
    if not isinstance(value, str):
        raise ValueError("truncated Gist file is missing its raw URL")
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "gist.githubusercontent.com"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("truncated Gist file returned an unsafe raw URL")
    return value


def _response_text(response):
    value = response.text
    if not isinstance(value, str):
        raise ValueError("Gist file content must be text")
    if len(value.encode("utf-8")) > MAX_GIST_FILE_BYTES:
        raise ValueError("Gist file exceeds the supported audit-ledger size")
    return value


def _gist_file_content(gist, headers, filename=GIST_FILENAME):
    """Return the complete file, fetching raw content when REST truncates it."""
    files = gist.get("files", {})
    if not isinstance(files, dict):
        raise ValueError("Gist files payload must be an object")
    file_info = files.get(filename)
    if file_info is None:
        return ""
    if not isinstance(file_info, dict):
        raise ValueError("Gist file payload must be an object")
    if file_info.get("truncated") is True:
        raw_url = _safe_raw_gist_url(file_info.get("raw_url"))
        raw_response = requests.get(
            raw_url,
            headers=headers,
            timeout=GIST_REQUEST_TIMEOUT_SECONDS,
        )
        raw_response.raise_for_status()
        return _response_text(raw_response)
    content = file_info.get("content", "")
    if not isinstance(content, str):
        raise ValueError("Gist file content must be text")
    if len(content.encode("utf-8")) > MAX_GIST_FILE_BYTES:
        raise ValueError("Gist file exceeds the supported audit-ledger size")
    return content


def build_gist_delivery(trade_data, symbol, saved_to_ghostfolio=False):
    """Build an immutable, retry-safe Gist delivery from a confirmed purchase."""
    if not isinstance(trade_data, dict):
        raise ValueError("trade data must be a dictionary")

    values = _validated_trade_values(trade_data)
    canonical_symbol = _canonical_symbol(symbol)
    funding_order_id = _canonical_external_identifier(
        trade_data.get("funding_order_id"), "funding order ID"
    )
    order_id = _canonical_external_identifier(
        trade_data.get("order_id"), "crypto order ID"
    )

    timestamp = datetime.fromtimestamp(values["timestamp"], tz=SELECTED_TZ)
    timestamp_text = timestamp.strftime("%Y-%m-%d %H:%M %Z")
    created_at = datetime.fromtimestamp(
        values["timestamp"], tz=timezone.utc
    ).isoformat().replace("+00:00", "Z")
    crypto_text = (
        f"{values['amount_crypto']:.8f} {_clean_table_value(canonical_symbol)}"
    )
    mirrored = "yes" if saved_to_ghostfolio else "optional/not saved"
    row = (
        f"| {timestamp_text} | GBP {values['amount_gbp']:.2f} | "
        f"{values['gbp_usd_rate']:.6f} | USD {values['funded_usd']:.4f} | "
        f"USD {values['cost_usd']:.4f} | {values['fee_text']} | "
        f"USD {values['usd_price']:,.4f} | {crypto_text} | "
        f"{_clean_table_value(funding_order_id)} | "
        f"{_clean_table_value(order_id)} | {mirrored} |\n"
    )

    target = f"{canonical_symbol}_USD"
    event = {
        "event_version": GIST_DELIVERY_VERSION,
        "event_id": order_id,
        "occurred_at": created_at,
        "target": target,
        "base_currency": canonical_symbol,
        "quote_currency": "USD",
        "budget_currency": "GBP",
        "funding_order_id": funding_order_id,
        "crypto_order_id": order_id,
        "gbp_debit": _decimal_text(values["amount_gbp"]),
        "gbp_usd_rate": _decimal_text(values["gbp_usd_rate"]),
        "funded_usd": _decimal_text(values["funded_usd"]),
        "crypto_cost_usd": _decimal_text(values["cost_usd"]),
        "crypto_quantity": _decimal_text(values["amount_crypto"]),
        "unit_price_usd": _decimal_text(values["usd_price"]),
        "funding_fee_usd": _decimal_text(values["funding_fee_usd"]),
        "crypto_fee_usd": _decimal_text(values["fee_usd"]),
    }
    event["canonical_hash"] = hashlib.sha256(
        json.dumps(
            event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
    ).hexdigest()
    canonical_event = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )

    return {
        "version": GIST_DELIVERY_VERSION,
        "delivery_id": order_id,
        "created_at": created_at,
        "symbol": canonical_symbol,
        "row": row,
        "row_sha256": hashlib.sha256(row.encode("utf-8")).hexdigest(),
        "event": event,
        "event_sha256": hashlib.sha256(canonical_event.encode("utf-8")).hexdigest(),
    }


def _validated_delivery(delivery):
    if not isinstance(delivery, dict) or set(delivery) != set(_DELIVERY_KEYS):
        raise ValueError("Gist delivery must use the exact version 2 schema")
    if type(delivery["version"]) is not int or delivery["version"] != 2:
        raise ValueError("unsupported Gist delivery version")

    delivery_id = _canonical_external_identifier(
        delivery["delivery_id"], "delivery ID"
    )
    if delivery_id != delivery["delivery_id"]:
        raise ValueError("delivery ID must already be canonical")
    symbol = _canonical_symbol(delivery["symbol"])
    if symbol != delivery["symbol"]:
        raise ValueError("delivery symbol must already be canonical")

    created_at = delivery["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise ValueError("delivery created_at must be a UTC ISO timestamp")
    try:
        parsed_created_at = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("delivery created_at must be a UTC ISO timestamp") from error
    if parsed_created_at.utcoffset() != timezone.utc.utcoffset(None):
        raise ValueError("delivery created_at must be a UTC ISO timestamp")

    row = delivery["row"]
    if not isinstance(row, str) or not row.endswith("\n") or "\r" in row:
        raise ValueError("delivery row must be one newline-terminated Markdown row")
    if "\n" in row[:-1]:
        raise ValueError("delivery row must be one newline-terminated Markdown row")
    cells = _split_markdown_row(row.removesuffix("\n"))
    if not cells or len(cells) != 11:
        raise ValueError("delivery row must contain exactly 11 Markdown columns")
    row_order_id = _unescape_table_value(cells[_CRYPTO_ORDER_COLUMN])
    if row_order_id != delivery_id:
        raise ValueError("delivery ID does not match the crypto order column")

    row_sha256 = delivery["row_sha256"]
    expected_hash = hashlib.sha256(row.encode("utf-8")).hexdigest()
    if (
        not isinstance(row_sha256, str)
        or len(row_sha256) != 64
        or row_sha256 != expected_hash
    ):
        raise ValueError("delivery row SHA-256 does not match its content")

    event = delivery["event"]
    if not isinstance(event, dict) or event.get("event_version") != 2:
        raise ValueError("delivery event must use PortfolioEventV2")
    if event.get("event_id") != delivery_id or event.get("crypto_order_id") != delivery_id:
        raise ValueError("delivery event identifiers must match delivery ID")
    if event.get("occurred_at") != created_at or event.get("base_currency") != symbol:
        raise ValueError("delivery event metadata does not match the delivery")
    canonical_event = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    event_sha256 = delivery["event_sha256"]
    if event_sha256 != hashlib.sha256(canonical_event).hexdigest():
        raise ValueError("delivery event SHA-256 does not match its content")

    return {key: delivery[key] for key in _DELIVERY_KEYS}


def _coerce_delivery(value, symbol, saved_to_ghostfolio):
    if isinstance(value, dict) and (
        "delivery_id" in value or "row_sha256" in value or "row" in value
    ):
        return _validated_delivery(value)
    return build_gist_delivery(value, symbol, saved_to_ghostfolio)


def update_gist_log(trade_data, symbol="BTC", saved_to_ghostfolio=False):
    """Deliver a confirmed Kraken two-leg purchase to the optional audit Gist.

    Logging is deliberately best-effort: a Gist outage never changes a Kraken
    order's success. ``True`` means the exact immutable row is present (whether
    newly appended or already delivered); skipped, conflicting, and failed
    attempts return ``False``. ``trade_data`` may also be a delivery returned
    by :func:`build_gist_delivery`.
    """
    try:
        delivery = _coerce_delivery(
            trade_data, symbol, saved_to_ghostfolio
        )
    except (KeyError, TypeError, ValueError) as error:
        print(f"Failed to update optional Gist audit ({type(error).__name__}).")
        return False

    if not GIST_ID or not GIST_TOKEN:
        print("GIST_ID or GIST_TOKEN not set. Skipping optional Gist audit log.")
        return False

    try:
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
        current_content = _gist_file_content(gist, headers, GIST_FILENAME)
        current_events = _gist_file_content(
            gist, headers, GHOSTFOLIO_EVENTS_FILENAME
        )

        row_status = _delivery_row_status(current_content, delivery)
        event_status = _delivery_event_status(current_events, delivery)
        if row_status == "duplicate" and event_status == "duplicate":
            print(
                f"Optional Gist audit already contains "
                f"{delivery['symbol']} delivery."
            )
            return True
        if "conflict" in {row_status, event_status}:
            print("Failed to update optional Gist audit (delivery conflict).")
            return False

        if not current_content.strip():
            current_content = TABLE_HEADER
        elif not current_content.endswith("\n"):
            current_content += "\n"
        updated_content = (
            current_content + delivery["row"]
            if row_status == "missing"
            else current_content
        )
        if len(updated_content.encode("utf-8")) > MAX_GIST_FILE_BYTES:
            raise ValueError("Gist file would exceed the supported audit-ledger size")
        event_line = json.dumps(
            delivery["event"],
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ) + "\n"
        if current_events and not current_events.endswith("\n"):
            current_events += "\n"
        updated_events = (
            current_events + event_line
            if event_status == "missing"
            else current_events
        )
        if len(updated_events.encode("utf-8")) > MAX_GIST_FILE_BYTES:
            raise ValueError("Ghostfolio event file would exceed the supported size")

        response = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    GIST_FILENAME: {"content": updated_content},
                    GHOSTFOLIO_EVENTS_FILENAME: {"content": updated_events},
                }
            },
            timeout=GIST_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        print(
            f"Optional Gist audit log updated for {delivery['symbol']}."
        )
        return True
    except (KeyError, TypeError, ValueError, requests.RequestException) as error:
        print(f"Failed to update optional Gist audit ({type(error).__name__}).")
    except Exception as error:
        # Optional logging must remain nonblocking for unexpected response data.
        print(f"Failed to update optional Gist audit ({type(error).__name__}).")

    return False


if __name__ == "__main__":
    print("This optional logger is called after a completed Kraken order.")

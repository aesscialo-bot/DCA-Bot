"""Recover one confirmed Kraken fill into the PortfolioEventV3 outbox.

This command is deliberately incident-specific.  It can only reconcile the
confirmed HYPE/USD purchase funded with GBP on 2026-08-07.  Kraken is queried
through ``reconcile_only=True`` using the bot's deterministic client IDs, so a
missing order leg can never be submitted by this command.

The default mode is preview.  Publishing requires a second, reviewed run with
both the event and Markdown-row hashes from preview.  Recovery requires the
exact Markdown row to exist already and can append only the missing JSONL
event; it never creates or changes the human-readable ledger row.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import secrets
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import gist_logger
import requests
from gist_logger import build_gist_delivery
from kraken_client import build_client_order_id, place_gbp_funded_market_buy


INCIDENT_TARGET = "HYPE_USD"
INCIDENT_PAIR = "HYPE/USD"
INCIDENT_TRADE_DATE = date(2026, 8, 7)
INCIDENT_BUDGET_GBP = Decimal("12.50")
INCIDENT_ROUTE = "GBP_TO_USD"
INCIDENT_QUOTE_CURRENCY = "USD"
INCIDENT_TIMEZONE = ZoneInfo("Asia/Bangkok")

# Keep this identical to ghostfolio_sync._safe_order_id so an event accepted
# here can never be rejected later by the local sidecar's provenance checks.
_ORDER_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RecoveryRefused(RuntimeError):
    """Raised when historical evidence does not exactly match the incident."""


@dataclass(frozen=True)
class RecoveryRequest:
    """Explicit evidence supplied by the operator for one recovery attempt."""

    target: str
    trade_date: date
    budget_gbp: Decimal
    expected_crypto_order_id: str
    expected_funding_order_id: str


def _parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise argparse.ArgumentTypeError("must be an ISO date such as 2026-08-07") from error


def _parse_gbp(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except (InvalidOperation, TypeError) as error:
        raise argparse.ArgumentTypeError("must be an exact decimal GBP amount") from error
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("must be a positive finite GBP amount")
    return amount


def _validated_order_id(value: str, name: str) -> str:
    if not isinstance(value, str) or not _ORDER_ID_PATTERN.fullmatch(value):
        raise RecoveryRefused(
            f"{name} must be a non-empty Kraken order ID containing only safe characters"
        )
    return value


def _validate_request(request: RecoveryRequest) -> None:
    """Fail before Kraken access unless all incident coordinates are exact."""
    if request.target != INCIDENT_TARGET:
        raise RecoveryRefused(f"target must be exactly {INCIDENT_TARGET}")
    if request.trade_date != INCIDENT_TRADE_DATE:
        raise RecoveryRefused(
            f"trade date must be exactly {INCIDENT_TRADE_DATE.isoformat()}"
        )
    if request.budget_gbp != INCIDENT_BUDGET_GBP:
        raise RecoveryRefused(
            f"GBP budget must be exactly {format(INCIDENT_BUDGET_GBP, '.2f')}"
        )
    _validated_order_id(request.expected_crypto_order_id, "crypto order ID")
    _validated_order_id(request.expected_funding_order_id, "funding order ID")
    if request.expected_crypto_order_id == request.expected_funding_order_id:
        raise RecoveryRefused("crypto and funding order IDs must be distinct")


def _finite_number(value, name: str, *, allow_zero: bool = False) -> float:
    if isinstance(value, bool):
        raise RecoveryRefused(f"Kraken {name} is not a valid number")
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise RecoveryRefused(f"Kraken {name} is not a valid number") from error
    if (
        not math.isfinite(number)
        or number < 0
        or (number == 0 and not allow_zero)
    ):
        qualifier = "non-negative" if allow_zero else "positive"
        raise RecoveryRefused(f"Kraken {name} must be {qualifier} and finite")
    return number


def _exact_identifier(order_data: dict, key: str, expected: str) -> str:
    actual = _validated_order_id(order_data.get(key), key.replace("_", " "))
    if not secrets.compare_digest(actual, expected):
        raise RecoveryRefused(f"Kraken {key.replace('_', ' ')} does not match expected evidence")
    return actual


def _authoritative_trade_data(
    order_data: dict,
    request: RecoveryRequest,
    *,
    client_order_id: str,
    funding_client_order_id: str,
) -> dict:
    """Validate reconciled fill evidence and mirror crypto_dca's trade schema."""
    if not isinstance(order_data, dict):
        raise RecoveryRefused("Kraken reconciliation returned an invalid fill")

    order_id = _exact_identifier(
        order_data, "order_id", request.expected_crypto_order_id
    )
    funding_order_id = _exact_identifier(
        order_data, "funding_order_id", request.expected_funding_order_id
    )
    _exact_identifier(order_data, "client_order_id", client_order_id)
    _exact_identifier(
        order_data, "funding_client_order_id", funding_client_order_id
    )

    if order_data.get("pair") != INCIDENT_PAIR:
        raise RecoveryRefused(f"Kraken pair must be exactly {INCIDENT_PAIR}")
    if order_data.get("quote_currency") != INCIDENT_QUOTE_CURRENCY:
        raise RecoveryRefused(
            f"Kraken quote currency must be exactly {INCIDENT_QUOTE_CURRENCY}"
        )

    spent_gbp = _finite_number(order_data.get("spent_gbp"), "GBP debit")
    if Decimal(str(spent_gbp)) != INCIDENT_BUDGET_GBP:
        raise RecoveryRefused("confirmed Kraken GBP debit does not match the exact budget")

    execution_timestamp = order_data.get("timestamp")
    if isinstance(execution_timestamp, bool) or not isinstance(execution_timestamp, int):
        raise RecoveryRefused("Kraken fill timestamp must be integer epoch seconds")
    try:
        occurred_at = datetime.fromtimestamp(
            execution_timestamp, tz=timezone.utc
        ).astimezone(INCIDENT_TIMEZONE)
    except (OSError, OverflowError, ValueError) as error:
        raise RecoveryRefused("Kraken fill timestamp is outside the supported range") from error
    if occurred_at.date() != request.trade_date:
        raise RecoveryRefused("Kraken fill timestamp does not fall on the expected Bangkok date")

    fee_details = order_data.get("fee_details")
    if not isinstance(fee_details, list):
        raise RecoveryRefused("Kraken fee details must be a list")

    # Keep this field set aligned with crypto_dca.execute_trade.  Every value is
    # reconstructed from the connector's normalized, confirmed two-leg fill.
    trade_data = {
        "ts": execution_timestamp,
        "amount_crypto": _finite_number(order_data.get("received"), "crypto received"),
        "amount_gbp": spent_gbp,
        "cost_gbp": _finite_number(order_data.get("cost_gbp"), "GBP crypto cost"),
        "fee_gbp": _finite_number(
            order_data.get("fee_gbp"), "GBP fee equivalent", allow_zero=True
        ),
        "gbp_fee_debit": _finite_number(
            order_data.get("gbp_fee_debit"), "GBP fee debit", allow_zero=True
        ),
        "fee_details": fee_details,
        "order_id": order_id,
        "gbp_price_per_unit": _finite_number(
            order_data.get("market_gbp_price_per_unit"), "market GBP unit price"
        ),
        "effective_gbp_price_per_unit": _finite_number(
            order_data.get("effective_gbp_price_per_unit"),
            "effective GBP unit price",
        ),
        "exchange_pair": INCIDENT_PAIR,
        "decision_id": f"historical-recovery-{client_order_id}",
        "route": INCIDENT_ROUTE,
        "quote_currency": INCIDENT_QUOTE_CURRENCY,
        "cost_quote": _finite_number(order_data.get("cost_usd"), "USD crypto cost"),
        "fee_quote": _finite_number(
            order_data.get("crypto_fee_usd"), "USD crypto fee", allow_zero=True
        ),
        "funding_fee_quote": _finite_number(
            order_data.get("funding_fee_usd"), "USD funding fee", allow_zero=True
        ),
        "quote_fee_debit": _finite_number(
            order_data.get("usd_fee_debit"), "USD fee debit", allow_zero=True
        ),
        "funded_usd": _finite_number(order_data.get("funded_usd"), "funded USD"),
        "gbp_usd_rate": _finite_number(order_data.get("gbp_usd_rate"), "GBP/USD rate"),
        "unit_price_quote": _finite_number(
            order_data.get("market_usd_price_per_unit"), "market USD unit price"
        ),
        "effective_price_quote": _finite_number(
            order_data.get("effective_usd_price_per_unit"),
            "effective USD unit price",
        ),
        "funding_order_id": funding_order_id,
    }
    return trade_data


def _validate_delivery(delivery: dict, request: RecoveryRequest) -> tuple[str, str]:
    if not isinstance(delivery, dict) or not isinstance(delivery.get("event"), dict):
        raise RecoveryRefused("Portfolio event builder returned an invalid delivery")
    event = delivery["event"]
    if delivery.get("version") != 3 or event.get("event_version") != 3:
        raise RecoveryRefused("recovery requires PortfolioEventV3")
    if event.get("target") != INCIDENT_TARGET or event.get("route") != INCIDENT_ROUTE:
        raise RecoveryRefused("Portfolio event target or funding route does not match")
    if event.get("crypto_order_id") != request.expected_crypto_order_id:
        raise RecoveryRefused("Portfolio event crypto order ID does not match")
    if event.get("funding_order_id") != request.expected_funding_order_id:
        raise RecoveryRefused("Portfolio event funding order ID does not match")
    if Decimal(str(event.get("gbp_debit"))) != INCIDENT_BUDGET_GBP:
        raise RecoveryRefused("Portfolio event GBP debit does not match")
    event_hash = event.get("canonical_hash")
    if not isinstance(event_hash, str) or not _SHA256_PATTERN.fullmatch(event_hash):
        raise RecoveryRefused("Portfolio event is missing its canonical SHA-256")
    row = delivery.get("row")
    row_sha256 = delivery.get("row_sha256")
    if not isinstance(row, str) or not row.endswith("\n"):
        raise RecoveryRefused("Portfolio delivery is missing its exact Markdown row")
    calculated_row_hash = hashlib.sha256(row.encode("utf-8")).hexdigest()
    if (
        not isinstance(row_sha256, str)
        or not _SHA256_PATTERN.fullmatch(row_sha256)
        or not secrets.compare_digest(row_sha256, calculated_row_hash)
    ):
        raise RecoveryRefused("Portfolio delivery Markdown-row SHA-256 is invalid")
    return event_hash, row_sha256


def _gist_credentials() -> tuple[str, str]:
    gist_id = os.environ.get("GIST_ID", "").strip()
    gist_token = os.environ.get("GIST_TOKEN", "").strip()
    if not gist_id or not gist_token:
        raise RecoveryRefused(
            "Gist credentials are required to prove the exact Markdown row exists"
        )
    return gist_id, gist_token


def _event_line(delivery: dict) -> str:
    return json.dumps(
        delivery["event"],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ) + "\n"


def _gist_contents(gist: dict, headers: dict) -> tuple[str, str]:
    markdown = gist_logger._gist_file_content(  # noqa: SLF001 - shared contract
        gist, headers, gist_logger.GIST_FILENAME
    )
    events = gist_logger._gist_file_content(  # noqa: SLF001 - shared contract
        gist, headers, gist_logger.GHOSTFOLIO_EVENTS_FILENAME
    )
    return markdown, events


def _exact_gist_status(markdown: str, events: str, delivery: dict) -> tuple[str, str]:
    row_status = gist_logger._delivery_row_status(  # noqa: SLF001
        markdown, delivery
    )
    event_status = gist_logger._delivery_event_status(  # noqa: SLF001
        events, delivery
    )
    if row_status == "missing":
        raise RecoveryRefused(
            "exact Markdown row is missing; recovery is not permitted to create it"
        )
    if row_status == "conflict":
        raise RecoveryRefused(
            "Markdown row conflicts with reconstructed Kraken evidence"
        )
    if event_status == "conflict":
        raise RecoveryRefused(
            "Portfolio event conflicts with the existing order-ID record"
        )
    if row_status != "duplicate" or event_status not in {"missing", "duplicate"}:
        raise RecoveryRefused("Gist delivery state is not safely recoverable")
    return row_status, event_status


def _inspect_and_maybe_publish_event(delivery: dict, *, publish: bool) -> str:
    """Require the exact row and optionally append only the missing event.

    The PATCH payload contains only the JSONL filename.  The response is then
    checked for both the unchanged exact row and the exact event before success
    is reported.  An exact existing event is an idempotent success.
    """
    gist_id, gist_token = _gist_credentials()
    headers = {
        "Authorization": f"Bearer {gist_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/gists/{gist_id}"

    try:
        response = requests.get(
            url,
            headers=headers,
            timeout=gist_logger.GIST_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        markdown, events = _gist_contents(response.json(), headers)
        _row_status, event_status = _exact_gist_status(
            markdown, events, delivery
        )

        if event_status == "duplicate":
            return "exact_event_present"
        if not publish:
            return "event_missing"

        if events and not events.endswith("\n"):
            events += "\n"
        updated_events = events + _event_line(delivery)
        if len(updated_events.encode("utf-8")) > gist_logger.MAX_GIST_FILE_BYTES:
            raise RecoveryRefused(
                "Ghostfolio event file would exceed the supported size"
            )

        response = requests.patch(
            url,
            headers=headers,
            json={
                "files": {
                    gist_logger.GHOSTFOLIO_EVENTS_FILENAME: {
                        "content": updated_events
                    }
                }
            },
            timeout=gist_logger.GIST_REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        verified_markdown, verified_events = _gist_contents(
            response.json(), headers
        )
        _verified_row, verified_event = _exact_gist_status(
            verified_markdown, verified_events, delivery
        )
        if verified_event != "duplicate":
            raise RecoveryRefused(
                "Gist did not confirm the exact appended Portfolio event"
            )
        return "event_appended"
    except RecoveryRefused:
        raise
    except Exception as error:
        raise RecoveryRefused(
            f"Gist recovery failed safely ({type(error).__name__})"
        ) from error


def recover_portfolio_event(
    request: RecoveryRequest,
    *,
    publish: bool = False,
    expected_event_hash: str | None = None,
    expected_row_sha256: str | None = None,
) -> dict:
    """Reconcile, validate, and optionally publish the historical event.

    The returned dictionary is intentionally a small, safe operator summary;
    neither the normalized Kraken payload nor credentials are returned.
    """
    _validate_request(request)
    if expected_event_hash is not None and not _SHA256_PATTERN.fullmatch(
        expected_event_hash
    ):
        raise RecoveryRefused("expected event hash must be 64 lowercase hex characters")
    if expected_row_sha256 is not None and not _SHA256_PATTERN.fullmatch(
        expected_row_sha256
    ):
        raise RecoveryRefused(
            "expected row SHA-256 must be 64 lowercase hex characters"
        )
    if publish and (expected_event_hash is None or expected_row_sha256 is None):
        raise RecoveryRefused(
            "--publish requires both reviewed event and Markdown-row hashes"
        )

    client_order_id = build_client_order_id(
        request.target, request.trade_date, purpose="buy"
    )
    funding_client_order_id = build_client_order_id(
        request.target, request.trade_date, purpose="funding"
    )
    try:
        order_data = place_gbp_funded_market_buy(
            request.target,
            float(request.budget_gbp),
            client_order_id=client_order_id,
            funding_client_order_id=funding_client_order_id,
            reconcile_only=True,
        )
    except Exception as error:
        raise RecoveryRefused(
            f"Kraken reconciliation failed safely ({type(error).__name__}); no event was published"
        ) from error

    trade_data = _authoritative_trade_data(
        order_data,
        request,
        client_order_id=client_order_id,
        funding_client_order_id=funding_client_order_id,
    )
    try:
        delivery = build_gist_delivery(
            trade_data, "HYPE", saved_to_ghostfolio=False
        )
    except Exception as error:
        raise RecoveryRefused(
            f"Portfolio event construction failed safely ({type(error).__name__})"
        ) from error
    event_hash, row_sha256 = _validate_delivery(delivery, request)

    if expected_event_hash is not None and not secrets.compare_digest(
        event_hash, expected_event_hash
    ):
        raise RecoveryRefused("reviewed event hash does not match current Kraken evidence")
    if expected_row_sha256 is not None and not secrets.compare_digest(
        row_sha256, expected_row_sha256
    ):
        raise RecoveryRefused(
            "reviewed Markdown-row hash does not match current Kraken evidence"
        )

    gist_status = _inspect_and_maybe_publish_event(delivery, publish=publish)

    return {
        "mode": "publish" if publish else "preview",
        "target": INCIDENT_TARGET,
        "trade_date": request.trade_date.isoformat(),
        "timestamp": delivery["created_at"],
        "crypto_order_id": request.expected_crypto_order_id,
        "funding_order_id": request.expected_funding_order_id,
        "client_order_id": client_order_id,
        "funding_client_order_id": funding_client_order_id,
        "event_hash": event_hash,
        "row_sha256": row_sha256,
        "gist_status": gist_status,
    }


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", required=True)
    parser.add_argument("--trade-date", required=True, type=_parse_date)
    parser.add_argument("--budget-gbp", required=True, type=_parse_gbp)
    parser.add_argument("--expected-crypto-order-id", required=True)
    parser.add_argument("--expected-funding-order-id", required=True)
    parser.add_argument(
        "--publish",
        action="store_true",
        help="append the exact delivery to the Gist after all checks pass",
    )
    parser.add_argument(
        "--expected-event-hash",
        help="canonical hash printed by a reviewed preview; mandatory with --publish",
    )
    parser.add_argument(
        "--expected-row-sha256",
        help="Markdown-row hash printed by a reviewed preview; mandatory with --publish",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    request = RecoveryRequest(
        target=args.target,
        trade_date=args.trade_date,
        budget_gbp=args.budget_gbp,
        expected_crypto_order_id=args.expected_crypto_order_id,
        expected_funding_order_id=args.expected_funding_order_id,
    )
    try:
        summary = recover_portfolio_event(
            request,
            publish=args.publish,
            expected_event_hash=args.expected_event_hash,
            expected_row_sha256=args.expected_row_sha256,
        )
    except RecoveryRefused as error:
        print(f"Recovery refused: {error}", file=sys.stderr)
        return 2
    except Exception as error:  # Defensive: never print raw third-party payloads.
        print(
            f"Recovery failed safely ({type(error).__name__}); no event was published",
            file=sys.stderr,
        )
        return 2

    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

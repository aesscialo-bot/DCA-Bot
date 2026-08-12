"""Reporting-only PortfolioEventV3 importer for local Ghostfolio."""

from __future__ import annotations

import base64
import binascii
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
# Keep the repository filename stable: snapshot schema evolution is identified
# by the signed top-level ``version`` field, and historical receipts bind to the
# content hash rather than to a renamed path.
HOLDINGS_SNAPSHOT_FILE = "kraken_holdings_snapshot_v1.json"
HOLDINGS_RECEIPT_FILE = "ghostfolio_holdings_receipts.jsonl"
PROVENANCE_RECLASSIFICATION_RECEIPT_FILE = (
    "ghostfolio_provenance_reclassification_receipts.jsonl"
)
REPOSITORY_OWNER_ENV = "DCA_OUTBOX_REPOSITORY_OWNER"
REPOSITORY_NAME_ENV = "DCA_OUTBOX_REPOSITORY_NAME"
REPOSITORY_BRANCH_ENV = "DCA_OUTBOX_REPOSITORY_BRANCH"
REPOSITORY_TOKEN_ENV = "DCA_OUTBOX_REPOSITORY_TOKEN"
REPOSITORY_EVENT_PATH_ENV = "DCA_OUTBOX_EVENT_PATH"
REPOSITORY_HOLDINGS_PATH_ENV = "DCA_OUTBOX_HOLDINGS_PATH"
REPOSITORY_EVENT_RECEIPT_PATH_ENV = (
    "DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH"
)
REPOSITORY_HOLDINGS_RECEIPT_PATH_ENV = (
    "DCA_OUTBOX_GHOSTFOLIO_HOLDINGS_RECEIPT_PATH"
)
REPOSITORY_PROVENANCE_RECEIPT_PATH_ENV = (
    "DCA_OUTBOX_GHOSTFOLIO_PROVENANCE_RECEIPT_PATH"
)
REPOSITORY_PATH_ENV_BY_FILE = {
    EVENT_FILE: REPOSITORY_EVENT_PATH_ENV,
    HOLDINGS_SNAPSHOT_FILE: REPOSITORY_HOLDINGS_PATH_ENV,
    RECEIPT_FILE: REPOSITORY_EVENT_RECEIPT_PATH_ENV,
    HOLDINGS_RECEIPT_FILE: REPOSITORY_HOLDINGS_RECEIPT_PATH_ENV,
    PROVENANCE_RECLASSIFICATION_RECEIPT_FILE: (
        REPOSITORY_PROVENANCE_RECEIPT_PATH_ENV
    ),
}
REQUIRED_REPOSITORY_FILES = {EVENT_FILE, HOLDINGS_SNAPSHOT_FILE}
GITHUB_API_ROOT = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
MAX_REPOSITORY_FILE_BYTES = 8_000_000
MAX_REPOSITORY_WRITE_ATTEMPTS = 3
REPOSITORY_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
REPOSITORY_BRANCH_PATTERN = re.compile(r"[A-Za-z0-9._/-]{1,255}")
GIT_OBJECT_SHA_PATTERN = re.compile(r"[0-9a-f]{40,64}")
STATE_PATH = Path("/receipts/state.json")
HOLDINGS_INTENT_PATH = Path("/receipts/holdings-intent.json")
PROVENANCE_RECLASSIFICATION_INTENT_PATH = Path(
    "/receipts/hype-provenance-reclassification-intent.json"
)
ASSET_PROFILES = {
    "BTC_GBP": {"symbol": "bitcoin", "data_source": "COINGECKO"},
    # Ghostfolio 3.43.0 returns Hyperliquid in lookup results for both
    # providers, but its CoinGecko importer rejects that asset. The supported
    # Yahoo crypto profile is therefore the audited local identifier.
    "HYPE_USD": {"symbol": "HYPE32196USD", "data_source": "YAHOO"},
    "ETH_GBP": {"symbol": "ethereum", "data_source": "COINGECKO"},
    "SOL_GBP": {"symbol": "solana", "data_source": "COINGECKO"},
}
LEGACY_KRAKEN_HOLDINGS_CONTRACT_V1 = {
    "BTC_GBP": {"asset": "BTC", "pair": "BTC/GBP", "quote_currency": "GBP"},
    "HYPE_USD": {"asset": "HYPE", "pair": "HYPE/USD", "quote_currency": "USD"},
    "SOL_GBP": {"asset": "SOL", "pair": "SOL/GBP", "quote_currency": "GBP"},
}
KRAKEN_HOLDINGS_CONTRACT = {
    "BTC_GBP": {"asset": "BTC", "pair": "BTC/GBP", "quote_currency": "GBP"},
    # HYPE is retained in reporting snapshots and historical recovery even
    # though it is no longer an active DCA target.
    "HYPE_USD": {"asset": "HYPE", "pair": "HYPE/USD", "quote_currency": "USD"},
    "ETH_GBP": {"asset": "ETH", "pair": "ETH/GBP", "quote_currency": "GBP"},
    "SOL_GBP": {"asset": "SOL", "pair": "SOL/GBP", "quote_currency": "GBP"},
}
KRAKEN_HOLDINGS_CONTRACTS = {
    1: LEGACY_KRAKEN_HOLDINGS_CONTRACT_V1,
    2: KRAKEN_HOLDINGS_CONTRACT,
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
    "ETH_GBP": {
        "base_currency": "ETH",
        "quote_currency": "GBP",
        "route": "DIRECT_GBP",
    },
    "SOL_GBP": {
        "base_currency": "SOL",
        "quote_currency": "GBP",
        "route": "DIRECT_GBP",
    },
}
SYMBOLS = {target: profile["symbol"] for target, profile in ASSET_PROFILES.items()}
QUANTITY_TOLERANCE = {
    "BTC_GBP": 1e-10,
    "HYPE_USD": 1e-8,
    "ETH_GBP": 1e-10,
    "SOL_GBP": 1e-8,
}
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

# One narrow migration for the private, hash-allowlisted HYPE fill.
HYPE_RECOVERY_TARGET = "HYPE_USD"
HYPE_RECOVERY_EVENT_ID_ENV = "GHOSTFOLIO_RECOVERY_CRYPTO_ORDER_ID"
HYPE_RECOVERY_FUNDING_ORDER_ID_ENV = "GHOSTFOLIO_RECOVERY_FUNDING_ORDER_ID"
HYPE_RECOVERY_EVENT_HASH_ENV = "GHOSTFOLIO_RECOVERY_EVENT_HASH"
HYPE_OPENING_COMMENT_PATTERN = re.compile(
    r"^Kraken opening-balance reconciliation; "
    r"snapshot=([0-9a-f]{64}); target=HYPE_USD$"
)
PROVENANCE_RECLASSIFICATION_PHASES = {
    "PREPARED",
    "OPENING_REDUCED",
    "EVENT_IMPORTED",
    "RECEIPTS_PUBLISHED",
}


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _recovery_evidence(*, required):
    evidence = {
        "event_id": os.environ.get(HYPE_RECOVERY_EVENT_ID_ENV),
        "funding_order_id": os.environ.get(HYPE_RECOVERY_FUNDING_ORDER_ID_ENV),
        "event_hash": os.environ.get(HYPE_RECOVERY_EVENT_HASH_ENV),
    }
    if not any(evidence.values()) and not required:
        return None
    if (
        not _safe_order_id(evidence["event_id"])
        or not _safe_order_id(evidence["funding_order_id"])
        or evidence["event_id"] == evidence["funding_order_id"]
        or not isinstance(evidence["event_hash"], str)
        or re.fullmatch(r"[0-9a-f]{64}", evidence["event_hash"]) is None
    ):
        raise RuntimeError("local HYPE recovery evidence is missing or invalid")
    return evidence


def _recovery_residual_comment(snapshot_hash, event_id):
    if (
        re.fullmatch(r"[0-9a-f]{64}", str(snapshot_hash)) is None
        or not _safe_order_id(event_id)
    ):
        raise RuntimeError("HYPE recovery event ID is invalid")
    return (
        "Kraken opening-balance residual after PortfolioEvent recovery; "
        f"snapshot={snapshot_hash}; target=HYPE_USD; "
        f"event={event_id}"
    )


def request_json(
    url, *, method="GET", token=None, payload=None, request_headers=None
):
    headers = {"Accept": "application/json"}
    if request_headers:
        headers.update(request_headers)
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
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("HTTP request failed") from None


def request_bytes(url, *, token=None, request_headers=None):
    headers = {"Accept": "application/octet-stream"}
    if request_headers:
        headers.update(request_headers)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    try:
        with urlopen(Request(url, headers=headers), timeout=20) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()
    except (URLError, TimeoutError, OSError):
        raise RuntimeError("HTTP request failed") from None


def _required_repository_environment(name):
    value = os.environ.get(name)
    if value is None or not value.strip() or value != value.strip():
        raise RuntimeError(f"required private repository setting {name} is invalid")
    return value


def _validated_repository_path(value, setting):
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > 1024
        or value.startswith("/")
        or value.endswith("/")
        or "\\" in value
        or any(ord(character) < 32 for character in value)
        or any(component in {"", ".", ".."} for component in value.split("/"))
    ):
        raise RuntimeError(f"required private repository setting {setting} is invalid")
    return value


def repository_configuration():
    owner = _required_repository_environment(REPOSITORY_OWNER_ENV)
    name = _required_repository_environment(REPOSITORY_NAME_ENV)
    branch = _required_repository_environment(REPOSITORY_BRANCH_ENV)
    token = _required_repository_environment(REPOSITORY_TOKEN_ENV)
    if REPOSITORY_COMPONENT_PATTERN.fullmatch(owner) is None:
        raise RuntimeError("private repository owner is invalid")
    if REPOSITORY_COMPONENT_PATTERN.fullmatch(name) is None:
        raise RuntimeError("private repository name is invalid")
    if (
        REPOSITORY_BRANCH_PATTERN.fullmatch(branch) is None
        or branch.startswith("/")
        or branch.endswith("/")
        or "//" in branch
        or any(component in {".", ".."} for component in branch.split("/"))
    ):
        raise RuntimeError("private repository branch is invalid")
    paths = {
        filename: _validated_repository_path(
            _required_repository_environment(environment), environment
        )
        for filename, environment in REPOSITORY_PATH_ENV_BY_FILE.items()
    }
    if len(set(paths.values())) != len(paths):
        raise RuntimeError("private repository artifact paths must be distinct")
    return {
        "owner": owner,
        "name": name,
        "branch": branch,
        "token": token,
        "paths": paths,
    }


def _repository_url(configuration):
    return (
        f"{GITHUB_API_ROOT}/repos/"
        f"{quote(configuration['owner'], safe='')}/"
        f"{quote(configuration['name'], safe='')}"
    )


def _github_headers(*, raw=False):
    return {
        "Accept": (
            "application/vnd.github.raw+json"
            if raw
            else "application/vnd.github+json"
        ),
        "X-GitHub-Api-Version": GITHUB_API_VERSION,
    }


def _repository_json(configuration, url, *, method="GET", payload=None):
    status, value = request_json(
        url,
        method=method,
        token=configuration["token"],
        payload=payload,
        request_headers=_github_headers(),
    )
    if status in {401, 403}:
        raise RuntimeError("private repository authentication or authorization failed")
    return status, value


def _verify_private_repository(configuration):
    status, metadata = _repository_json(
        configuration, _repository_url(configuration)
    )
    if status != 200:
        raise RuntimeError("private repository identity request failed")
    expected = f"{configuration['owner']}/{configuration['name']}".casefold()
    if (
        not isinstance(metadata, dict)
        or metadata.get("private") is not True
        or str(metadata.get("full_name", "")).casefold() != expected
    ):
        raise RuntimeError("configured canonical repository is not verified private")


def _repository_commit(configuration):
    branch = quote(configuration["branch"], safe="")
    status, metadata = _repository_json(
        configuration, f"{_repository_url(configuration)}/commits/{branch}"
    )
    commit_sha = metadata.get("sha") if isinstance(metadata, dict) else None
    if status != 200 or not isinstance(commit_sha, str) or not re.fullmatch(
        r"[0-9a-f]{40}", commit_sha
    ):
        raise RuntimeError("private repository branch could not be pinned")
    return commit_sha


def _repository_file(configuration, path, reference, *, required):
    encoded_path = quote(path, safe="/")
    url = (
        f"{_repository_url(configuration)}/contents/{encoded_path}?"
        + urlencode({"ref": reference})
    )
    status, metadata = _repository_json(configuration, url)
    if status == 404 and not required:
        return {"content": "", "path": path, "sha": None, "exists": False}
    if status != 200 or not isinstance(metadata, dict):
        raise RuntimeError(f"private repository file read failed for {path}")
    sha = metadata.get("sha")
    size = metadata.get("size")
    if (
        metadata.get("type") != "file"
        or metadata.get("path") != path
        or not isinstance(sha, str)
        or GIT_OBJECT_SHA_PATTERN.fullmatch(sha) is None
        or type(size) is not int
        or size < 0
        or size > MAX_REPOSITORY_FILE_BYTES
    ):
        raise RuntimeError(f"private repository file metadata is invalid for {path}")
    if metadata.get("encoding") == "base64":
        encoded = metadata.get("content")
        if not isinstance(encoded, str):
            raise RuntimeError(f"private repository file content is invalid for {path}")
        try:
            raw = base64.b64decode("".join(encoded.split()), validate=True)
        except (ValueError, binascii.Error):
            raise RuntimeError(
                f"private repository file content is invalid for {path}"
            ) from None
    elif metadata.get("encoding") == "none" and size > 1_000_000:
        raw_status, raw = request_bytes(
            url,
            token=configuration["token"],
            request_headers=_github_headers(raw=True),
        )
        if raw_status != 200:
            raise RuntimeError(f"private repository raw read failed for {path}")
    else:
        raise RuntimeError(f"private repository file encoding is invalid for {path}")
    if not isinstance(raw, bytes) or len(raw) != size:
        raise RuntimeError(f"private repository file size is invalid for {path}")
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise RuntimeError(f"private repository file is not UTF-8 for {path}") from None
    return {"content": content, "path": path, "sha": sha, "exists": True}


def repository_snapshot():
    """Read one immutable, commit-pinned canonical source and receipt view."""
    configuration = repository_configuration()
    _verify_private_repository(configuration)
    commit_sha = _repository_commit(configuration)
    files = {
        filename: _repository_file(
            configuration,
            path,
            commit_sha,
            required=filename in REQUIRED_REPOSITORY_FILES,
        )
        for filename, path in configuration["paths"].items()
    }
    return {"repository_commit_sha": commit_sha, "files": files}


def file_content(payload, name):
    info = payload.get("files", {}).get(name)
    if not info:
        return ""
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


def _strict_decimal(value, label, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, float, Decimal)):
        raise RuntimeError(f"{label} is not a decimal value")
    try:
        number = Decimal(str(value))
    except InvalidOperation as error:
        raise RuntimeError(f"{label} is not a decimal value") from error
    if not number.is_finite() or number < 0 or (positive and number <= 0):
        raise RuntimeError(f"{label} is invalid")
    return number


def _decimal_text(value):
    number = _strict_decimal(value, "decimal")
    rendered = format(number.normalize(), "f")
    return "0" if rendered in {"", "-0"} else rendered


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


def _jsonl_rows(content, label):
    rows = []
    try:
        rows = [json.loads(line) for line in content.splitlines() if line.strip()]
    except json.JSONDecodeError as error:
        raise RuntimeError(f"{label} is malformed") from error
    return rows


def _event_receipt_rows(content, events):
    event_by_id, receipts = {row["event_id"]: row for row in events}, {}
    fields = {"order_id", "event_hash", "ghostfolio_activity_id", "imported_at"}
    for receipt in _jsonl_rows(content, "Ghostfolio event receipt ledger"):
        event = event_by_id.get(receipt.get("order_id")) if isinstance(receipt, dict) else None
        if event is None or receipt.get("event_hash") != event["canonical_hash"]:
            raise RuntimeError("Ghostfolio event receipt does not match its event")
        if (
            not isinstance(receipt, dict) or set(receipt) != fields
            or not isinstance(receipt["ghostfolio_activity_id"], str)
            or not receipt["ghostfolio_activity_id"]
            or receipt["order_id"] in receipts
        ):
            raise RuntimeError("Ghostfolio event receipt ledger is malformed")
        _parse_timestamp(receipt["imported_at"], "Ghostfolio event receipt")
        receipts[receipt["order_id"]] = receipt
    return receipts


def parse_event_receipts(content, events):
    return set(_event_receipt_rows(content, events))


def _validate_hype_recovery_event(event, evidence=None):
    if any((
        event.get("target") != HYPE_RECOVERY_TARGET,
        event.get("base_currency") != "HYPE",
        event.get("quote_currency") != "USD",
        event.get("budget_currency") != "GBP",
        event.get("route") != "GBP_TO_USD",
    )):
        raise RuntimeError("HYPE recovery event has the wrong contract")
    if evidence and any((
        event.get("event_id") != evidence["event_id"],
        event.get("crypto_order_id") != evidence["event_id"],
        event.get("funding_order_id") != evidence["funding_order_id"],
        event.get("canonical_hash") != evidence["event_hash"],
    )):
        raise RuntimeError("HYPE recovery event does not match local evidence")
    return event


def _validate_provenance_reclassification_receipt(receipt, events, label):
    fields = {
        "version", "event_id", "event_hash", "opening_snapshot_hash",
        "opening_activity_id", "original_quantity", "residual_quantity",
        "reclassified_quantity", "completed_at",
    }
    event = next((
        row for row in events
        if isinstance(receipt, dict) and row["event_id"] == receipt.get("event_id")
    ), None)
    if (
        not isinstance(receipt, dict) or set(receipt) != fields
        or receipt.get("version") != 1 or isinstance(receipt.get("version"), bool)
        or event is None or receipt.get("event_hash") != event["canonical_hash"]
        or re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("opening_snapshot_hash"))) is None
        or not _safe_order_id(receipt.get("opening_activity_id"))
        or re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z",
            str(receipt.get("completed_at")),
        )
        is None
    ):
        raise RuntimeError(f"{label} is malformed")
    _validate_hype_recovery_event(event)
    quantities = [
        _strict_decimal(receipt[field], f"{label} {field}")
        for field in ("original_quantity", "residual_quantity", "reclassified_quantity")
        if isinstance(receipt[field], str)
    ]
    if (
        len(quantities) != 3 or quantities[0] <= 0 or quantities[1] <= 0
        or quantities[2] != _event_decimal(event, "crypto_quantity", 1)
        or quantities[1] + quantities[2] != quantities[0]
    ):
        raise RuntimeError(f"{label} quantities do not reconcile")
    _parse_timestamp(receipt["completed_at"], label)
    return receipt


def parse_provenance_reclassification_receipts(content, events):
    receipts = {}
    for number, receipt in enumerate(
        _jsonl_rows(content, "provenance receipt ledger"), start=1
    ):
        _validate_provenance_reclassification_receipt(
            receipt, events, f"provenance receipt line {number}"
        )
        if receipt["event_id"] in receipts:
            raise RuntimeError("provenance receipt ledger has duplicates")
        receipts[receipt["event_id"]] = receipt
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
    version = snapshot.get("version")
    contract = (
        KRAKEN_HOLDINGS_CONTRACTS.get(version)
        if isinstance(version, int) and not isinstance(version, bool)
        else None
    )
    if (
        supplied != actual
        or contract is None
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
    if not isinstance(holdings, dict) or set(holdings) != set(contract):
        raise RuntimeError("Kraken holdings snapshot does not contain the exact target set")
    for target, identity in contract.items():
        item = holdings.get(target)
        if not isinstance(item, dict) or set(item) != {
            "asset",
            "pair",
            "quote_currency",
            "quantity",
            "unit_price_quote",
        }:
            raise RuntimeError(f"Kraken holdings snapshot item is invalid for {target}")
        if any(item.get(key) != value for key, value in identity.items()):
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


def _canonical_utc_timestamp(value, label):
    return _parse_timestamp(value, label).isoformat(
        timespec="microseconds"
    ).replace("+00:00", "Z")


def ghostfolio_hype_activities(token, account_id):
    query = urlencode({
        "accounts": account_id,
        "dataSource": ASSET_PROFILES[HYPE_RECOVERY_TARGET]["data_source"],
        "symbol": ASSET_PROFILES[HYPE_RECOVERY_TARGET]["symbol"],
        "take": 100, "sortColumn": "date", "sortDirection": "asc",
    })
    status, payload = request_json(
        _ghostfolio_url("/api/v1/activities") + "?" + query, token=token
    )
    activities = payload.get("activities") if isinstance(payload, dict) else None
    count = payload.get("count") if isinstance(payload, dict) else None
    if status != 200 or not isinstance(activities, list) or (
        not isinstance(count, int) or isinstance(count, bool)
        or count != len(activities) or count > 100
        or not all(isinstance(row, dict) for row in activities)
    ):
        raise RuntimeError("Ghostfolio HYPE activity response is incomplete")
    return activities


def _stable_hype_activity(activity, account_id):
    profile, tags = activity.get("assetProfile"), activity.get("tags", [])
    if not isinstance(profile, dict) or not isinstance(tags, list):
        raise RuntimeError("Ghostfolio HYPE activity identity is malformed")
    tag_ids = [tag.get("id") if isinstance(tag, dict) else None for tag in tags]
    if any(not isinstance(tag, str) or not tag for tag in tag_ids) or (
        len(tag_ids) != len(set(tag_ids))
    ):
        raise RuntimeError("Ghostfolio HYPE activity tags are malformed")
    if (
        not _safe_order_id(activity.get("id"))
        or activity.get("accountId") != account_id
        or not isinstance(activity.get("comment"), str)
        or activity.get("currency") != "USD" or activity.get("type") != "BUY"
        or profile.get("dataSource") != ASSET_PROFILES[HYPE_RECOVERY_TARGET]["data_source"]
        or profile.get("symbol") != ASSET_PROFILES[HYPE_RECOVERY_TARGET]["symbol"]
    ):
        raise RuntimeError("Ghostfolio HYPE activity identity is malformed")
    decimal_fields = {
        "fee": (activity.get("fee"), False),
        "quantity": (activity.get("quantity"), True),
        "unit_price": (activity.get("unitPrice"), True),
    }
    result = {
        "id": activity["id"], "account_id": account_id,
        "comment": activity["comment"], "currency": "USD",
        "data_source": profile["dataSource"],
        "date": _canonical_utc_timestamp(activity.get("date"), "HYPE activity"),
        "symbol": profile["symbol"], "tags": sorted(tag_ids), "type": "BUY",
    }
    result.update({
        field: _decimal_text(_strict_decimal(value, f"HYPE activity {field}", positive=positive))
        for field, (value, positive) in decimal_fields.items()
    })
    return result


def _opening_marker(identity, event_id):
    original = HYPE_OPENING_COMMENT_PATTERN.fullmatch(identity.get("comment", ""))
    if original:
        return original.group(1), False
    residual = re.fullmatch(
        r"Kraken opening-balance residual after PortfolioEvent recovery; "
        r"snapshot=([0-9a-f]{64}); target=HYPE_USD; event=" + re.escape(event_id),
        identity.get("comment", ""),
    )
    return (residual.group(1), True) if residual else None


def _opening_activity_matches(
    identity, event, snapshot_hash, quantity, *, residual, original=None
):
    if (
        _opening_marker(identity, event["event_id"]) != (snapshot_hash, residual)
        or identity.get("fee") != "0" or identity.get("tags") != []
        or _strict_decimal(identity.get("quantity"), "HYPE opening") != quantity
        or _parse_timestamp(identity["date"], "HYPE opening")
        <= _parse_timestamp(event["occurred_at"], "HYPE event")
    ):
        return False
    if original is None:
        return True
    expected = {
        **original,
        "comment": (
            _recovery_residual_comment(snapshot_hash, event["event_id"])
            if residual else original["comment"]
        ),
        "quantity": _decimal_text(quantity),
    }
    return identity == expected


def _event_activity_matches(identity, event):
    activity = import_payload(event)["activities"][0]
    return all((
        identity.get("comment") == activity["comment"],
        identity.get("date") == _canonical_utc_timestamp(activity["date"], "HYPE event"),
        identity.get("fee") == _decimal_text(activity["fee"]),
        identity.get("quantity") == _decimal_text(activity["quantity"]),
        identity.get("unit_price") == _decimal_text(activity["unitPrice"]),
        identity.get("tags") == [],
    ))


def _hype_activity_topology(
    activities,
    account_id,
    event,
    *,
    expected_opening_marker=None,
    require_recovered=False,
    ignore_other_after=None,
):
    identities = [_stable_hype_activity(row, account_id) for row in activities]
    classified = [
        (
            row,
            _opening_marker(row, event["event_id"]),
            _event_activity_matches(row, event),
        )
        for row in identities
    ]
    opening = [
        row
        for row, marker, _ in classified
        if marker is not None
        and (expected_opening_marker is None or marker == expected_opening_marker)
    ]
    recovered = [row for row, _, matches in classified if matches]
    other = [
        row
        for row, marker, matches in classified
        if not matches
        and not (
            marker is not None
            and (
                expected_opening_marker is None
                or marker == expected_opening_marker
            )
        )
    ]
    other_is_invalid = bool(other)
    if ignore_other_after is not None:
        completed_at = _parse_timestamp(
            ignore_other_after, "completed HYPE provenance"
        )
        other_is_invalid = any(
            _parse_timestamp(row["date"], "unrelated HYPE activity")
            <= completed_at
            for row in other
        )
    recovered_is_invalid = (
        len(recovered) != 1 if require_recovered else len(recovered) > 1
    )
    if (
        other_is_invalid
        or len(opening) != 1
        or recovered_is_invalid
    ):
        raise RuntimeError("Ghostfolio HYPE recovery activity topology is ambiguous")
    return opening[0], recovered[0] if recovered else None


def _opening_update_payload(identity, residual, event, snapshot_hash):
    return {
        "accountId": identity["account_id"],
        "comment": _recovery_residual_comment(snapshot_hash, event["event_id"]),
        "currency": identity["currency"], "dataSource": identity["data_source"],
        "date": identity["date"], "fee": float(Decimal(identity["fee"])),
        "id": identity["id"], "quantity": float(residual),
        "symbol": identity["symbol"], "tags": identity["tags"],
        "type": identity["type"], "unitPrice": float(Decimal(identity["unit_price"])),
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
    for target in snapshot["holdings"]:
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


def _receipt_identity(content, identity_field, identity_value):
    match = None
    for line in content.splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            raise RuntimeError("private repository receipt ledger is malformed") from error
        if not isinstance(row, dict):
            raise RuntimeError("private repository receipt ledger is malformed")
        if row.get(identity_field) == identity_value:
            if match is not None:
                raise RuntimeError("private repository receipt identity is duplicated")
            match = row
    return match


def _append_repository_receipt(filename, identity_field, receipt):
    configuration = repository_configuration()
    _verify_private_repository(configuration)
    if filename not in {
        RECEIPT_FILE,
        HOLDINGS_RECEIPT_FILE,
        PROVENANCE_RECLASSIFICATION_RECEIPT_FILE,
    }:
        raise RuntimeError("private repository receipt filename is invalid")
    if not isinstance(receipt, dict) or identity_field not in receipt:
        raise RuntimeError("private repository receipt identity is invalid")
    path = configuration["paths"][filename]
    identity_value = receipt[identity_field]
    wanted_line = canonical(receipt)
    for _attempt in range(MAX_REPOSITORY_WRITE_ATTEMPTS):
        current = _repository_file(
            configuration, path, configuration["branch"], required=False
        )
        existing = _receipt_identity(
            current["content"], identity_field, identity_value
        )
        if existing is not None:
            return existing == receipt
        separator = (
            ""
            if not current["content"] or current["content"].endswith("\n")
            else "\n"
        )
        updated = current["content"] + separator + wanted_line + "\n"
        if len(updated.encode("utf-8")) > MAX_REPOSITORY_FILE_BYTES:
            raise RuntimeError("private repository receipt ledger is too large")
        payload = {
            "message": f"Append Ghostfolio receipt to {filename}",
            "content": base64.b64encode(updated.encode("utf-8")).decode("ascii"),
            "branch": configuration["branch"],
        }
        if current["sha"] is not None:
            payload["sha"] = current["sha"]
        url = (
            f"{_repository_url(configuration)}/contents/"
            f"{quote(path, safe='/')}"
        )
        try:
            status, _metadata = _repository_json(
                configuration, url, method="PUT", payload=payload
            )
        except RuntimeError as error:
            if str(error) != "HTTP request failed":
                raise
            continue
        if status in {409, 422}:
            continue
        if status not in {200, 201}:
            raise RuntimeError(f"private repository receipt write failed for {filename}")
        verified = _repository_file(
            configuration, path, configuration["branch"], required=True
        )
        durable = _receipt_identity(
            verified["content"], identity_field, identity_value
        )
        if durable == receipt:
            return True
        if durable is not None:
            return False
    raise RuntimeError("private repository receipt write conflicted after bounded retries")


def append_receipt(repository_payload, receipt):
    existing = _receipt_identity(
        file_content(repository_payload, RECEIPT_FILE),
        "order_id",
        receipt["order_id"],
    )
    if existing is not None:
        return existing == receipt
    return _append_repository_receipt(RECEIPT_FILE, "order_id", receipt)


def append_named_receipt(repository_payload, filename, identity_field, receipt):
    existing = _receipt_identity(
        file_content(repository_payload, filename),
        identity_field,
        receipt[identity_field],
    )
    if existing is not None:
        return existing == receipt
    return _append_repository_receipt(filename, identity_field, receipt)


def holdings_receipt_for_snapshot(repository_payload, snapshot_hash):
    match = None
    for line in file_content(repository_payload, HOLDINGS_RECEIPT_FILE).splitlines():
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


def _intent_economics(intent):
    event, opening = intent["event"], intent["opening_original"]
    snapshot_hash = intent["opening_snapshot_hash"]
    fields = {"id", "account_id", "comment", "currency", "data_source", "date",
              "fee", "quantity", "symbol", "tags", "type", "unit_price"}
    if not isinstance(opening, dict):
        raise RuntimeError("local provenance opening identity is malformed")
    profile = ASSET_PROFILES[HYPE_RECOVERY_TARGET]
    original = _strict_decimal(opening.get("quantity"), "HYPE opening", positive=True)
    if (
        set(opening) != fields or not _safe_order_id(opening.get("id"))
        or not _safe_order_id(opening.get("account_id"))
        or (opening.get("currency"), opening.get("data_source"), opening.get("symbol"))
        != ("USD", profile["data_source"], profile["symbol"])
        or opening.get("type") != "BUY"
        or not _opening_activity_matches(
            opening, event, snapshot_hash, original, residual=False
        )
    ):
        raise RuntimeError("local provenance opening identity is malformed")
    reclassified = _event_decimal(event, "crypto_quantity", 1)
    residual = original - reclassified
    snapshot = parse_holdings_snapshot(canonical(intent["snapshot"]))
    if snapshot is None:
        raise RuntimeError("local provenance snapshot is missing")
    if residual <= 0:
        raise RuntimeError("HYPE recovery residual is not positive")
    signed = _strict_decimal(
        snapshot["holdings"][HYPE_RECOVERY_TARGET]["quantity"], "signed HYPE"
    )
    if signed != original:
        raise RuntimeError("HYPE recovery economics do not reconcile")
    return snapshot, original, residual, reclassified


def _validate_provenance_reclassification_intent(intent):
    fields = {"version", "phase", "event", "snapshot", "baseline_event_ids",
              "opening_snapshot_hash", "opening_original", "event_activity_id",
              "completed_at"}
    valid_header = (
        isinstance(intent, dict) and set(intent) == fields
        and intent.get("version") == 1 and not isinstance(intent.get("version"), bool)
        and intent.get("phase") in PROVENANCE_RECLASSIFICATION_PHASES
        and re.fullmatch(r"[0-9a-f]{64}", str(intent.get("opening_snapshot_hash")))
    )
    if not valid_header:
        raise RuntimeError("local provenance reclassification intent is malformed")
    parsed, baseline = parse_events(canonical(intent["event"])), intent["baseline_event_ids"]
    if (
        len(parsed) != 1 or not isinstance(baseline, list)
        or any(not _safe_order_id(event_id) for event_id in baseline)
        or baseline != sorted(set(baseline)) or parsed[0]["event_id"] not in baseline
    ):
        raise RuntimeError("local provenance reclassification intent is malformed")
    _validate_hype_recovery_event(parsed[0])
    _intent_economics(intent)
    completed = intent["phase"] in {"EVENT_IMPORTED", "RECEIPTS_PUBLISHED"}
    if completed:
        if not _safe_order_id(intent["event_activity_id"]):
            raise RuntimeError("local provenance reclassification intent is malformed")
        _parse_timestamp(intent["completed_at"], "local provenance intent")
    elif intent["event_activity_id"] is not None or intent["completed_at"] is not None:
        raise RuntimeError("local provenance reclassification intent is malformed")
    return intent


def load_provenance_reclassification_intent():
    if not PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file():
        return None
    try:
        return _validate_provenance_reclassification_intent(json.loads(
            PROVENANCE_RECLASSIFICATION_INTENT_PATH.read_text(encoding="utf-8")
        ))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError("local provenance intent is unreadable") from error


def save_provenance_reclassification_intent(intent):
    _validate_provenance_reclassification_intent(intent)
    path = PROVENANCE_RECLASSIFICATION_INTENT_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".new")
    temporary.write_text(canonical(intent), encoding="utf-8")
    os.replace(temporary, path)


def clear_provenance_reclassification_intent():
    try:
        PROVENANCE_RECLASSIFICATION_INTENT_PATH.unlink()
    except FileNotFoundError:
        pass


def _set_provenance_phase(intent, phase, **changes):
    updated = {**intent, **changes, "phase": phase}
    save_provenance_reclassification_intent(updated)
    return updated


def _require_hype_source_receipt(payload, snapshot_hash, quantity):
    receipt = holdings_receipt_for_snapshot(payload, snapshot_hash)
    matches = [] if receipt is None else [
        row for row in receipt["adjustments"] if row["target"] == HYPE_RECOVERY_TARGET
    ]
    if len(matches) != 1 or _strict_decimal(
        matches[0]["quantity_delta"], "HYPE source adjustment"
    ) != quantity:
        raise RuntimeError("HYPE recovery source holdings receipt does not match")


def _recovery_snapshot(payload, events, event, opening, *, now=None):
    marker = _opening_marker(opening, event["event_id"])
    if marker is None or marker[1]:
        raise RuntimeError("HYPE opening activity is not an original reconciliation")
    provisional = {
        "event": event,
        "opening_original": opening,
        "opening_snapshot_hash": marker[0],
        "snapshot": parse_holdings_snapshot(file_content(payload, HOLDINGS_SNAPSHOT_FILE)),
    }
    if provisional["snapshot"] is None:
        raise RuntimeError("HYPE recovery requires a signed Kraken snapshot")
    snapshot, original, residual, _ = _intent_economics(provisional)
    _require_hype_source_receipt(payload, marker[0], original)
    validate_snapshot_freshness(snapshot, now=now)
    validate_snapshot_monotonicity(snapshot)
    validate_snapshot_event_order(snapshot, events)
    return snapshot, original, residual, marker[0]


def _require_recovery_quantities(snapshot, actual, hype_quantity):
    # Historical HYPE recovery evidence is bound to a three-target v1 snapshot.
    # Compare exactly the targets signed by that snapshot; a later ETH holding
    # must neither invalidate nor be mutated by recovery of the older artifact.
    for target in snapshot["holdings"]:
        expected = hype_quantity if target == HYPE_RECOVERY_TARGET else _strict_decimal(
            snapshot["holdings"][target]["quantity"], f"signed {target}"
        )
        observed = _strict_decimal(actual.get(target), f"Ghostfolio {target}")
        if abs(float(expected - observed)) > QUANTITY_TOLERANCE[target]:
            raise RuntimeError(f"Ghostfolio holdings drift during HYPE recovery for {target}")


def _preflight_recovery_event(event, token):
    status, result = request_json(
        _ghostfolio_url("/api/v1/import") + "?dryRun=true",
        method="POST", token=token, payload=import_payload(event),
    )
    if status in {200, 201}:
        return "missing"
    if status == 400 and is_exact_duplicate(result):
        return "duplicate"
    raise RuntimeError("Ghostfolio HYPE recovery dry-run conflict")


def _put_residual_opening_activity(identity, residual, event, snapshot_hash, token):
    status, _ = request_json(
        _ghostfolio_url("/api/v1/activities/") + quote(identity["id"], safe=""),
        method="PUT", token=token,
        payload=_opening_update_payload(identity, residual, event, snapshot_hash),
    )
    if status not in {200, 201}:
        raise RuntimeError(f"Ghostfolio HYPE opening update failed with HTTP {status}")


def _post_recovery_event(event, token):
    status, result = request_json(
        _ghostfolio_url("/api/v1/import"),
        method="POST", token=token, payload=import_payload(event),
    )
    activities = result.get("activities") if isinstance(result, dict) else None
    activity_id = (
        activities[0].get("id") if status in {200, 201}
        and isinstance(activities, list) and len(activities) == 1
        and isinstance(activities[0], dict) else None
    )
    if not _safe_order_id(activity_id):
        raise RuntimeError("Ghostfolio HYPE recovery acknowledgement is incomplete")
    return activity_id


def _migration_receipts(event, intent):
    _, original, residual, reclassified = _intent_economics(intent)
    common = {"event_id": event["event_id"], "event_hash": event["canonical_hash"]}
    return (
        {
            "order_id": event["event_id"], "event_hash": event["canonical_hash"],
            "ghostfolio_activity_id": intent["event_activity_id"],
            "imported_at": intent["completed_at"],
        },
        {
            "version": 1, **common,
            "opening_snapshot_hash": intent["opening_snapshot_hash"],
            "opening_activity_id": intent["opening_original"]["id"],
            "original_quantity": _decimal_text(original),
            "residual_quantity": _decimal_text(residual),
            "reclassified_quantity": _decimal_text(reclassified),
            "completed_at": intent["completed_at"],
        },
    )


def _receipt_pair(payload, events, event_id):
    return (
        _event_receipt_rows(file_content(payload, RECEIPT_FILE), events).get(event_id),
        parse_provenance_reclassification_receipts(
            file_content(payload, PROVENANCE_RECLASSIFICATION_RECEIPT_FILE), events
        ).get(event_id),
    )


def _publish_recovery_receipts(intent):
    event, event_id = intent["event"], intent["event"]["event_id"]
    wanted = _migration_receipts(event, intent)
    fresh = repository_snapshot()
    fresh_events = parse_events(file_content(fresh, EVENT_FILE))
    if sorted(row["event_id"] for row in fresh_events) != intent["baseline_event_ids"]:
        raise RuntimeError("newer PortfolioEvents appeared during HYPE recovery")
    existing = _receipt_pair(fresh, fresh_events, event_id)
    if any(old is not None and old != new for old, new in zip(existing, wanted)):
        raise RuntimeError("HYPE recovery receipt conflict")
    for filename, old, receipt in zip(
        (RECEIPT_FILE, PROVENANCE_RECLASSIFICATION_RECEIPT_FILE), existing, wanted
    ):
        identity_field = "order_id" if filename == RECEIPT_FILE else "event_id"
        if old is None and not append_named_receipt(
            fresh, filename, identity_field, receipt
        ):
            raise RuntimeError("HYPE recovery receipt conflict")
    verified = repository_snapshot()
    verified_events = parse_events(file_content(verified, EVENT_FILE))
    if (
        sorted(row["event_id"] for row in verified_events) != intent["baseline_event_ids"]
        or _receipt_pair(verified, verified_events, event_id) != wanted
    ):
        raise RuntimeError("HYPE recovery receipts were not durably verified")


def _migration_status(event, snapshot_hash, *, clear_intent, complete):
    return {
        "status": "COMPLETE" if complete else "NOT_REQUIRED",
        "event_id": event["event_id"] if event else None,
        "event_hash": event["canonical_hash"] if event else None,
        "opening_snapshot_hash": snapshot_hash,
        "receipt_present": complete,
        "clear_intent_after_state": clear_intent,
    }


def _completed_reclassification_status(
    payload, events, event_receipts, provenance, token, account_id
):
    event = next(row for row in events if row["event_id"] == provenance["event_id"])
    event_receipt, _ = _receipt_pair(payload, events, event["event_id"])
    original = _strict_decimal(provenance["original_quantity"], "HYPE original")
    residual = _strict_decimal(provenance["residual_quantity"], "HYPE residual")
    snapshot_hash = provenance["opening_snapshot_hash"]
    _require_hype_source_receipt(payload, snapshot_hash, original)
    opening, recovered = _hype_activity_topology(
        ghostfolio_hype_activities(token, account_id),
        account_id,
        event,
        expected_opening_marker=(snapshot_hash, True),
        require_recovered=True,
        ignore_other_after=provenance["completed_at"],
    )
    if (
        event["event_id"] not in event_receipts or event_receipt is None
        or not _opening_activity_matches(
            opening, event, snapshot_hash, residual, residual=True
        )
        or recovered is None or recovered["id"] != event_receipt["ghostfolio_activity_id"]
        or provenance["opening_activity_id"] != opening["id"]
    ):
        raise RuntimeError("completed HYPE provenance topology is invalid")
    return _migration_status(event, snapshot_hash, clear_intent=False, complete=True)


def process_hype_provenance_reclassification(
    payload, events, event_receipts, token, account_id, *, now=None
):
    provenance_rows = parse_provenance_reclassification_receipts(
        file_content(payload, PROVENANCE_RECLASSIFICATION_RECEIPT_FILE), events
    )
    intent = load_provenance_reclassification_intent()
    if len(provenance_rows) > 1:
        raise RuntimeError("HYPE provenance receipt ledger is ambiguous")
    completed_id = next(iter(provenance_rows), None)
    if completed_id:
        event_id, evidence = completed_id, None
    elif intent:
        event_id, evidence = intent["event"]["event_id"], _recovery_evidence(required=True)
    else:
        evidence = _recovery_evidence(required=False)
        pending_hype = [
            row for row in events if row["target"] == HYPE_RECOVERY_TARGET
            and row["event_id"] not in event_receipts
        ]
        if evidence is None:
            if pending_hype:
                raise RuntimeError("local HYPE recovery evidence is required")
            return _migration_status(None, None, clear_intent=False, complete=False)
        event_id = evidence["event_id"]
    matches = [row for row in events if row["event_id"] == event_id]
    if not matches:
        if intent or provenance_rows or any(
            row["target"] == HYPE_RECOVERY_TARGET for row in events
        ):
            raise RuntimeError("configured HYPE recovery event is not in the outbox")
        return _migration_status(None, None, clear_intent=False, complete=False)
    event = _validate_hype_recovery_event(matches[0], evidence)
    completed = provenance_rows.get(event_id)
    if completed and not intent:
        return _completed_reclassification_status(
            payload, events, event_receipts, completed, token, account_id
        )
    if event_id in event_receipts and not intent:
        raise RuntimeError("HYPE event was imported without a reclassification intent")

    if not intent:
        if any(row["target"] == HYPE_RECOVERY_TARGET and row["event_id"] != event_id
               for row in events):
            raise RuntimeError("another HYPE PortfolioEvent makes recovery ambiguous")
        if any(row["event_id"] != event_id and row["event_id"] not in event_receipts
               for row in events):
            raise RuntimeError("an unreceipted PortfolioEvent blocks HYPE recovery")
        opening, recovered = _hype_activity_topology(
            ghostfolio_hype_activities(token, account_id), account_id, event
        )
        if recovered:
            raise RuntimeError("HYPE recovery event exists before reclassification")
        snapshot, original, residual, snapshot_hash = _recovery_snapshot(
            payload, events, event, opening, now=now
        )
        _require_recovery_quantities(
            snapshot, ghostfolio_quantities(token, account_id), original
        )
        if _preflight_recovery_event(event, token) != "missing":
            raise RuntimeError("HYPE recovery event already exists")
        intent = {
            "version": 1, "phase": "PREPARED", "event": event, "snapshot": snapshot,
            "baseline_event_ids": sorted(row["event_id"] for row in events),
            "opening_snapshot_hash": snapshot_hash, "opening_original": opening,
            "event_activity_id": None, "completed_at": None,
        }
        save_provenance_reclassification_intent(intent)
    else:
        if (
            canonical(intent["event"]) != canonical(event)
            or sorted(row["event_id"] for row in events) != intent["baseline_event_ids"]
        ):
            raise RuntimeError("newer or changed PortfolioEvents block HYPE recovery")
        snapshot, original, residual, _ = _intent_economics(intent)
        snapshot_hash = intent["opening_snapshot_hash"]
        _require_hype_source_receipt(payload, snapshot_hash, original)

    opening, recovered = _hype_activity_topology(
        ghostfolio_hype_activities(token, account_id), account_id, event
    )
    original_match = _opening_activity_matches(
        opening, event, snapshot_hash, original, residual=False,
        original=intent["opening_original"],
    )
    residual_match = _opening_activity_matches(
        opening, event, snapshot_hash, residual, residual=True,
        original=intent["opening_original"],
    )
    if not original_match and not residual_match:
        raise RuntimeError("HYPE opening activity changed during recovery")
    event_receipt, _ = _receipt_pair(payload, events, event_id)
    actual = ghostfolio_quantities(token, account_id)
    if original_match:
        if recovered or event_receipt or _preflight_recovery_event(event, token) != "missing":
            raise RuntimeError("HYPE event appeared before the opening was reduced")
        _require_recovery_quantities(snapshot, actual, original)
        _put_residual_opening_activity(opening, residual, event, snapshot_hash, token)
        opening, recovered = _hype_activity_topology(
            ghostfolio_hype_activities(token, account_id), account_id, event
        )
        if recovered or not _opening_activity_matches(
            opening, event, snapshot_hash, residual, residual=True,
            original=intent["opening_original"],
        ):
            raise RuntimeError("Ghostfolio did not persist the HYPE residual opening")
        intent = _set_provenance_phase(intent, "OPENING_REDUCED")
        flush_ghostfolio_cache(token)
        _require_recovery_quantities(
            snapshot, ghostfolio_quantities(token, account_id), residual
        )
    elif not recovered:
        _require_recovery_quantities(snapshot, actual, residual)

    if event_receipt:
        if recovered is None or recovered["id"] != event_receipt["ghostfolio_activity_id"]:
            raise RuntimeError("HYPE event receipt does not match Ghostfolio")
        activity_id, completed_at = recovered["id"], event_receipt["imported_at"]
    else:
        preflight = _preflight_recovery_event(event, token)
        if recovered is None:
            if preflight != "missing":
                raise RuntimeError("HYPE recovery duplicate could not be identified")
            activity_id = _post_recovery_event(event, token)
            opening, recovered = _hype_activity_topology(
                ghostfolio_hype_activities(token, account_id), account_id, event
            )
            if recovered is None or recovered["id"] != activity_id:
                raise RuntimeError("HYPE recovery import identity did not persist")
        elif preflight == "duplicate":
            activity_id = recovered["id"]
        else:
            raise RuntimeError("HYPE recovery activity conflicts with its dry-run")
        completed_at = intent["completed_at"] or time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
        )
    if (
        intent["phase"] not in {"EVENT_IMPORTED", "RECEIPTS_PUBLISHED"}
        or (intent["event_activity_id"], intent["completed_at"])
        != (activity_id, completed_at)
    ):
        intent = _set_provenance_phase(
            intent, "EVENT_IMPORTED",
            event_activity_id=activity_id, completed_at=completed_at,
        )
    flush_ghostfolio_cache(token)
    _require_recovery_quantities(
        snapshot, ghostfolio_quantities(token, account_id), original
    )
    calculation = ghostfolio_portfolio_calculation_with_fx_repair(
        token, {"kraken_account_id": account_id}, now=now
    )
    if calculation["portfolio_calculation_has_error"]:
        raise RuntimeError("Ghostfolio calculation failed during HYPE recovery")
    _publish_recovery_receipts(intent)
    event_receipts.add(event_id)
    if intent["phase"] != "RECEIPTS_PUBLISHED":
        _set_provenance_phase(intent, "RECEIPTS_PUBLISHED")
    return _migration_status(event, snapshot_hash, clear_intent=True, complete=True)


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
    fresh_payload = repository_snapshot()
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
    *,
    commit=False,
    token=None,
    now=None,
    repository_payload=None,
    allow_provenance_intent=False,
):
    if (
        PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file()
        and not allow_provenance_intent
    ):
        raise RuntimeError(
            "pending HYPE provenance reclassification blocks holdings reconciliation"
        )
    # One sync cycle must use one immutable outbox view. Receipt publication
    # deliberately rereads only at the append boundary to preserve concurrent
    # append-only records.
    payload = (
        repository_payload
        if repository_payload is not None
        else repository_snapshot()
    )
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
            # every old adjustment landed, even when the repository now has
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
        repository_snapshot(), HOLDINGS_RECEIPT_FILE, "snapshot_hash", receipt
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
    repository_payload, events, event_receipts, token, *, now=None
):
    """Complete an older local holdings transaction before newer event imports."""
    intent = load_holdings_intent()
    if intent is None:
        return None
    intent_snapshot = intent["snapshot"]
    intent_hash = intent_snapshot["canonical_hash"]
    existing = holdings_receipt_for_snapshot(repository_payload, intent_hash)
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

    recovery_payload = dict(repository_payload)
    recovery_files = dict(repository_payload.get("files", {}))
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
        repository_payload=recovery_payload,
    )
    if HOLDINGS_INTENT_PATH.is_file():
        raise RuntimeError("pending holdings intent did not recover")
    return result


def sync_once():
    payload = repository_snapshot()
    events = parse_events(file_content(payload, EVENT_FILE))
    receipts = parse_event_receipts(
        file_content(payload, RECEIPT_FILE), events
    )
    token = ghostfolio_token()
    if (
        HOLDINGS_INTENT_PATH.is_file()
        and PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file()
    ):
        raise RuntimeError(
            "holdings and HYPE provenance intents cannot be recovered concurrently"
        )
    recover_holdings_intent_before_events(payload, events, receipts, token)
    reporting_accounts = verify_ghostfolio_reporting_accounts(
        token, require_bitkub=False
    )
    kraken_account_id = verify_ghostfolio_account_map(
        reporting_accounts, require_bitkub=False
    )
    provenance = process_hype_provenance_reclassification(
        payload,
        events,
        receipts,
        token,
        kraken_account_id,
    )
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
        if not append_receipt(repository_snapshot(), receipt):
            raise RuntimeError(f"receipt conflict for {event['event_id']}")
        receipts.add(event["event_id"])
    holdings_kwargs = {
        # While the provenance intent still exists, the final holdings pass is
        # a read-only convergence check.  Creating a holdings intent here
        # could leave both durable intents after a crash and require manual
        # recovery.  Normal mutation resumes on the next cycle, after the
        # provenance state is durable and its intent has been cleared.
        "commit": not provenance["clear_intent_after_state"],
        "token": token,
        "repository_payload": payload,
    }
    if provenance["clear_intent_after_state"]:
        holdings_kwargs["allow_provenance_intent"] = True
    holdings = reconcile_holdings_snapshot(**holdings_kwargs)
    if provenance["clear_intent_after_state"] and holdings["drift"]:
        raise RuntimeError(
            "holdings drift blocks completion of HYPE provenance reclassification"
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
            "provenance_reclassification_status": provenance["status"],
            "provenance_reclassification_event_id": provenance["event_id"],
            "provenance_reclassification_event_hash": provenance["event_hash"],
            "provenance_reclassification_opening_snapshot_hash": provenance[
                "opening_snapshot_hash"
            ],
            "provenance_reclassification_receipt_present": provenance[
                "receipt_present"
            ],
        }),
        encoding="utf-8",
    )
    if provenance["clear_intent_after_state"]:
        clear_provenance_reclassification_intent()


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "health":
        if not STATE_PATH.is_file():
            return 1
        if (
            HOLDINGS_INTENT_PATH.is_file()
            or PROVENANCE_RECLASSIFICATION_INTENT_PATH.is_file()
        ):
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
        provenance_status = state.get("provenance_reclassification_status")
        if provenance_status == "COMPLETE":
            if (
                not _safe_order_id(
                    state.get("provenance_reclassification_event_id")
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(state.get("provenance_reclassification_event_hash", "")),
                )
                or not re.fullmatch(
                    r"[0-9a-f]{64}",
                    str(
                        state.get(
                            "provenance_reclassification_opening_snapshot_hash"
                        )
                    ),
                )
                or state.get("provenance_reclassification_receipt_present") is not True
            ):
                return 1
        elif provenance_status == "NOT_REQUIRED":
            if (
                state.get("provenance_reclassification_event_id") is not None
                or state.get("provenance_reclassification_event_hash") is not None
                or state.get(
                    "provenance_reclassification_opening_snapshot_hash"
                )
                is not None
                or state.get("provenance_reclassification_receipt_present") is not False
            ):
                return 1
        else:
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

"""Reporting-only PortfolioEventV3 importer for local Ghostfolio."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


EVENT_FILE = "kraken_usd_dca_ghostfolio_events.jsonl"
RECEIPT_FILE = "ghostfolio_sync_receipts.jsonl"
HOLDINGS_SNAPSHOT_FILE = "kraken_holdings_snapshot_v1.json"
HOLDINGS_RECEIPT_FILE = "ghostfolio_holdings_receipts.jsonl"
STATE_PATH = Path("/receipts/state.json")
SYMBOLS = {"BTC_GBP": "bitcoin", "HYPE_USD": "hyperliquid", "SOL_GBP": "solana"}
QUANTITY_TOLERANCE = {"BTC_GBP": 1e-10, "HYPE_USD": 1e-8, "SOL_GBP": 1e-8}


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


def parse_holdings_snapshot(content):
    if not content.strip():
        return None
    snapshot = json.loads(content)
    supplied = snapshot.get("canonical_hash")
    unhashed = {key: value for key, value in snapshot.items() if key != "canonical_hash"}
    actual = hashlib.sha256(canonical(unhashed).encode("utf-8")).hexdigest()
    if supplied != actual or snapshot.get("version") != 1:
        raise RuntimeError("Kraken holdings snapshot has an invalid hash or version")
    if snapshot.get("unsupported_nonzero_assets"):
        raise RuntimeError(
            "Kraken has non-zero crypto assets without a Ghostfolio mapping: "
            + ", ".join(snapshot["unsupported_nonzero_assets"])
        )
    holdings = snapshot.get("holdings")
    if not isinstance(holdings, dict) or set(holdings) != set(SYMBOLS):
        raise RuntimeError("Kraken holdings snapshot does not contain the exact target set")
    return snapshot


def ghostfolio_token():
    status, payload = request_json(
        os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/auth/anonymous",
        method="POST",
        payload={"accessToken": os.environ["GHOSTFOLIO_SECURITY_TOKEN"]},
    )
    if status not in {200, 201} or not payload.get("authToken"):
        raise RuntimeError(f"Ghostfolio authentication failed with HTTP {status}")
    return payload["authToken"]


def import_payload(event):
    accounts = json.loads(os.environ.get("GHOSTFOLIO_ACCOUNT_MAP", "{}"))
    account_id = accounts.get(event["target"])
    if not account_id:
        raise RuntimeError(f"no local custody account configured for {event['target']}")
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
            "dataSource": "COINGECKO",
            "date": event["occurred_at"],
            "fee": float(event["crypto_fee_quote"]),
            "quantity": float(event["crypto_quantity"]),
            "symbol": SYMBOLS[event["target"]],
            "type": "BUY",
            "unitPrice": float(event["unit_price_quote"]),
        }]
    }


def ghostfolio_quantities(token):
    status, payload = request_json(
        os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/portfolio/holdings",
        token=token,
    )
    if status != 200:
        raise RuntimeError(f"Ghostfolio holdings read failed with HTTP {status}")
    result = {target: 0.0 for target in SYMBOLS}
    reverse = {symbol: target for target, symbol in SYMBOLS.items()}
    for holding in payload.get("holdings", []):
        symbol = str((holding.get("assetProfile") or {}).get("symbol") or "")
        target = reverse.get(symbol)
        if target:
            result[target] = float(holding.get("quantity") or 0)
    return result


def holdings_drift(snapshot, actual):
    drift = {}
    for target in SYMBOLS:
        expected = float(snapshot["holdings"][target]["quantity"])
        difference = expected - float(actual.get(target, 0))
        if abs(difference) > QUANTITY_TOLERANCE[target]:
            drift[target] = difference
    return drift


def holdings_import_payload(snapshot, target, difference):
    accounts = json.loads(os.environ.get("GHOSTFOLIO_ACCOUNT_MAP", "{}"))
    account_id = accounts.get(target)
    if not account_id:
        raise RuntimeError(f"no local custody account configured for {target}")
    item = snapshot["holdings"][target]
    return {
        "activities": [{
            "accountId": account_id,
            "comment": (
                "Kraken opening-balance reconciliation; "
                f"snapshot={snapshot['canonical_hash']}; target={target}"
            ),
            "currency": item["quote_currency"],
            "dataSource": "COINGECKO",
            "date": snapshot["as_of"],
            "fee": 0,
            "quantity": abs(difference),
            "symbol": SYMBOLS[target],
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


def reconcile_holdings_snapshot(*, commit=False):
    payload = gist()
    snapshot = parse_holdings_snapshot(file_content(payload, HOLDINGS_SNAPSHOT_FILE))
    if snapshot is None:
        return {"status": "NO_SNAPSHOT", "drift": {}}
    token = ghostfolio_token()
    drift = holdings_drift(snapshot, ghostfolio_quantities(token))
    if not commit or not drift:
        return {"status": "DRIFT" if drift else "IN_SYNC", "drift": drift}

    base = os.environ.get("GHOSTFOLIO_URL", "http://app:3333") + "/api/v1/import"
    adjustments = []
    for target, difference in drift.items():
        activity = holdings_import_payload(snapshot, target, difference)
        dry_status, dry_result = request_json(
            base + "?" + urlencode({"dryRun": "true"}),
            method="POST", token=token, payload=activity,
        )
        duplicate = dry_status == 400 and is_exact_duplicate(dry_result)
        if dry_status not in {200, 201} and not duplicate:
            raise RuntimeError(f"Ghostfolio holdings dry-run conflict for {target}")
        if not duplicate:
            status, result = request_json(base, method="POST", token=token, payload=activity)
            if status not in {200, 201} and not (
                status == 400 and is_exact_duplicate(result)
            ):
                raise RuntimeError(f"Ghostfolio holdings import failed for {target}")
        adjustments.append({"target": target, "quantity_delta": format(difference, ".16g")})

    receipt = {
        "snapshot_hash": snapshot["canonical_hash"],
        "reconciled_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "adjustments": adjustments,
    }
    if not append_named_receipt(
        gist(), HOLDINGS_RECEIPT_FILE, "snapshot_hash", receipt
    ):
        raise RuntimeError("holdings receipt conflict")
    return {"status": "RECONCILED", "drift": drift}


def sync_once():
    payload = gist()
    events = parse_events(file_content(payload, EVENT_FILE))
    receipts = {
        json.loads(line)["order_id"]
        for line in file_content(payload, RECEIPT_FILE).splitlines()
        if line.strip()
    }
    token = ghostfolio_token()
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
                imported = result.get("activities", [])
                if imported and isinstance(imported[0], dict):
                    activity_id = str(imported[0].get("id") or "created")
        receipt = {
            "order_id": event["event_id"],
            "event_hash": event["canonical_hash"],
            "ghostfolio_activity_id": activity_id,
            "imported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if not append_receipt(gist(), receipt):
            raise RuntimeError(f"receipt conflict for {event['event_id']}")
        receipts.add(event["event_id"])
    holdings = reconcile_holdings_snapshot(commit=False)
    STATE_PATH.write_text(
        canonical({
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_count": len(events),
            "receipt_count": len(receipts),
            "holdings_status": holdings["status"],
            "holdings_drift_targets": sorted(holdings["drift"]),
        }),
        encoding="utf-8",
    )


def main():
    command = sys.argv[1] if len(sys.argv) > 1 else "run"
    if command == "health":
        if not STATE_PATH.is_file():
            return 1
        maximum_age = max(900, int(os.environ.get("SYNC_INTERVAL_SECONDS", "300")) * 3)
        return 0 if time.time() - STATE_PATH.stat().st_mtime <= maximum_age else 1
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

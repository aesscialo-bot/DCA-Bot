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
STATE_PATH = Path("/receipts/state.json")
SYMBOLS = {"BTC_GBP": "bitcoin", "HYPE_USD": "hyperliquid", "SOL_GBP": "solana"}


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
    STATE_PATH.write_text(
        canonical({
            "checked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "event_count": len(events),
            "receipt_count": len(receipts),
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
    interval = int(os.environ.get("SYNC_INTERVAL_SECONDS", "300"))
    while True:
        try:
            sync_once()
        except (KeyError, ValueError, RuntimeError, URLError) as error:
            print(f"sync blocked: {type(error).__name__}: {error}", flush=True)
        time.sleep(max(60, interval))


if __name__ == "__main__":
    raise SystemExit(main())

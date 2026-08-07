"""Publish a signed, read-only Kraken crypto holdings snapshot to the outbox Gist."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone

import requests

from kraken_client import get_kraken_exchange


FILENAME = "kraken_holdings_snapshot_v1.json"
TARGETS = {
    "BTC_GBP": ("BTC", "BTC/GBP", "GBP"),
    "HYPE_USD": ("HYPE", "HYPE/USD", "USD"),
    "SOL_GBP": ("SOL", "SOL/GBP", "GBP"),
}
FIAT = frozenset({"GBP", "USD", "EUR"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def build_snapshot(exchange, *, now=None):
    reference = now or datetime.now(timezone.utc)
    balances = exchange.fetch_balance().get("total", {}) or {}
    holdings = {}
    for target, (asset, pair, quote) in TARGETS.items():
        quantity = float(balances.get(asset) or 0)
        ticker = exchange.fetch_ticker(pair)
        price = float(ticker.get("last") or ticker.get("close") or 0)
        if not math.isfinite(quantity) or quantity < 0 or not math.isfinite(price) or price <= 0:
            raise RuntimeError(f"invalid Kraken holding or ticker for {target}")
        holdings[target] = {
            "asset": asset,
            "pair": pair,
            "quote_currency": quote,
            "quantity": format(quantity, ".16g"),
            "unit_price_quote": format(price, ".16g"),
        }
    supported_assets = {item[0] for item in TARGETS.values()}
    unsupported = sorted(
        asset for asset, value in balances.items()
        if asset not in supported_assets | FIAT and float(value or 0) > 0
    )
    snapshot = {
        "version": 1,
        "as_of": reference.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "holdings": holdings,
        "unsupported_nonzero_assets": unsupported,
    }
    snapshot["canonical_hash"] = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
    return snapshot


def publish(snapshot):
    gist_id = os.environ["GIST_ID"]
    token = os.environ["GIST_TOKEN"]
    response = requests.patch(
        f"https://api.github.com/gists/{gist_id}",
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
        json={"files": {FILENAME: {"content": json.dumps(snapshot, sort_keys=True, indent=2) + "\n"}}},
        timeout=20,
    )
    response.raise_for_status()


def main():
    snapshot = build_snapshot(get_kraken_exchange())
    publish(snapshot)
    print(
        "Published signed Kraken holdings snapshot for "
        + ", ".join(sorted(snapshot["holdings"]))
        + f"; unsupported non-zero assets={len(snapshot['unsupported_nonzero_assets'])}."
    )


if __name__ == "__main__":
    main()

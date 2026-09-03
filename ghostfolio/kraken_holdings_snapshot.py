"""Publish a signed Kraken holdings snapshot to the private repository outbox."""

from __future__ import annotations

import hashlib
import json
import math
import os
from datetime import datetime, timezone

from dca_config import validate_execution_state
from github_contents import (
    GitHubContentsClient,
    GitHubContentsError,
    configured_outbox_paths,
)
from kraken_client import get_kraken_exchange


SNAPSHOT_VERSION = 3
TARGETS = {
    "BTC_GBP": ("BTC", "BTC/GBP", "GBP"),
    "HYPE_USD": ("HYPE", "HYPE/USD", "USD"),
    "ETH_GBP": ("ETH", "ETH/GBP", "GBP"),
    "SOL_GBP": ("SOL", "SOL/GBP", "GBP"),
    "DOGE_GBP": ("DOGE", "DOGE/GBP", "GBP"),
}
FIAT = frozenset({"GBP", "USD", "EUR"})


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def ensure_snapshot_safe_execution_state(value):
    """Refuse a balance watermark while a fill or outbox event is unresolved."""
    state = validate_execution_state(value)
    blockers = []
    for target, entry in state.items():
        if entry.get("PENDING_ORDER") is not None:
            blockers.append(f"{target}:pending-order")
        if entry.get("PENDING_GIST_DELIVERIES"):
            blockers.append(f"{target}:pending-delivery")
    if blockers:
        raise RuntimeError(
            "Kraken holdings snapshot blocked by unresolved DCA state: "
            + ", ".join(sorted(blockers))
        )
    return state


def build_snapshot(exchange, *, now=None):
    reference = now or datetime.now(timezone.utc)
    if (
        not isinstance(reference, datetime)
        or reference.tzinfo is None
        or reference.utcoffset() is None
    ):
        raise RuntimeError("Kraken holdings snapshot time must include a timezone")
    balance_response = exchange.fetch_balance()
    balances = (
        balance_response.get("total")
        if isinstance(balance_response, dict)
        else None
    )
    if not isinstance(balances, dict):
        raise RuntimeError("Kraken balance response is malformed")
    normalized_balances = {}
    for asset, value in balances.items():
        if not isinstance(asset, str) or not asset or isinstance(value, bool):
            raise RuntimeError("Kraken balance response is malformed")
        try:
            quantity = float(value or 0)
        except (TypeError, ValueError) as error:
            raise RuntimeError(f"invalid Kraken balance for {asset}") from error
        if not math.isfinite(quantity) or quantity < 0:
            raise RuntimeError(f"invalid Kraken balance for {asset}")
        normalized_balances[asset] = quantity
    holdings = {}
    for target, (asset, pair, quote) in TARGETS.items():
        quantity = normalized_balances.get(asset, 0.0)
        ticker = exchange.fetch_ticker(pair)
        if not isinstance(ticker, dict):
            raise RuntimeError(f"invalid Kraken holding or ticker for {target}")
        price_value = ticker.get("last") or ticker.get("close") or 0
        if isinstance(price_value, bool):
            raise RuntimeError(f"invalid Kraken holding or ticker for {target}")
        try:
            price = float(price_value)
        except (TypeError, ValueError) as error:
            raise RuntimeError(
                f"invalid Kraken holding or ticker for {target}"
            ) from error
        if not math.isfinite(price) or price <= 0:
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
        asset for asset, value in normalized_balances.items()
        if asset not in supported_assets | FIAT and value > 0
    )
    snapshot = {
        "version": SNAPSHOT_VERSION,
        "as_of": reference.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        "holdings": holdings,
        "unsupported_nonzero_assets": unsupported,
    }
    snapshot["canonical_hash"] = hashlib.sha256(canonical(snapshot).encode()).hexdigest()
    return snapshot


def publish(snapshot, *, client=None):
    """Replace the snapshot with optimistic-SHA protection and verify content."""
    paths = configured_outbox_paths()
    repository = client or GitHubContentsClient.from_env()
    content = json.dumps(snapshot, sort_keys=True, indent=2) + "\n"
    result = repository.replace_text(
        paths.holdings,
        content,
        message="Update signed Kraken holdings snapshot",
    )
    if result.content != content:
        raise GitHubContentsError(
            "private repository did not confirm the holdings snapshot"
        )


def main():
    ensure_snapshot_safe_execution_state(os.environ["DCA_EXECUTION_STATE"])
    snapshot = build_snapshot(get_kraken_exchange())
    publish(snapshot)
    print(
        "Published signed Kraken holdings snapshot for "
        + ", ".join(sorted(snapshot["holdings"]))
        + f"; unsupported non-zero assets={len(snapshot['unsupported_nonzero_assets'])}."
    )


if __name__ == "__main__":
    main()

"""Fail-closed migration from all-USD DCA keys to mixed Kraken markets."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from dca_config import empty_analysis_state, validate_execution_state, validate_rules_map


KEY_MAP = {
    "BTC_USD": "BTC_GBP",
    "HYPE_USD": "HYPE_USD",
    "SOL_USD": "SOL_GBP",
}


def _object(raw: str, label: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def migrate(rules_raw: str, execution_raw: str, *, now: datetime | None = None) -> dict:
    """Return fully validated mixed-market state without performing network writes."""
    old_rules = _object(rules_raw, "DCA_TARGET_MAP")
    if set(old_rules) != set(KEY_MAP):
        raise ValueError("source rules must contain exactly BTC_USD, HYPE_USD, SOL_USD")
    old_execution = _object(execution_raw or "{}", "DCA_EXECUTION_STATE")
    unsupported = set(old_execution) - set(KEY_MAP)
    if unsupported:
        raise ValueError("source execution state contains unsupported targets")

    for old_key in ("BTC_USD", "SOL_USD"):
        entry = old_execution.get(old_key, {})
        if entry.get("PENDING_ORDER") is not None:
            raise ValueError(f"{old_key} has a pending Kraken intent; reconcile before migration")
        if entry.get("PENDING_GIST_DELIVERIES"):
            raise ValueError(f"{old_key} has pending ledger delivery; deliver before migration")

    new_rules = {KEY_MAP[key]: value for key, value in old_rules.items()}
    new_rules = validate_rules_map(new_rules)
    new_execution = {}
    for old_key, new_key in KEY_MAP.items():
        if old_key not in old_execution:
            continue
        entry = dict(old_execution[old_key])
        new_execution[new_key] = entry
    new_execution = validate_execution_state(new_execution)
    generated = now or datetime.now(timezone.utc)
    new_analysis = empty_analysis_state(
        new_rules,
        now=generated,
        reason="Market keys migrated; fresh verified mixed-market history analysis required",
    )
    return {
        "DCA_TARGET_MAP": new_rules,
        "DCA_EXECUTION_STATE": new_execution,
        "DCA_ANALYSIS_STATE": new_analysis,
        "DCA_CANARY_SYMBOL": "SOL_GBP",
        "DCA_TRADING_MODE": "shadow",
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", required=True)
    parser.add_argument("--execution", default="{}")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = migrate(args.rules, args.execution)
    args.output.write_text(
        json.dumps(result, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

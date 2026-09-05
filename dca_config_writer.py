"""Serialized, fail-closed updates for the user-owned DCA rules variable."""

from __future__ import annotations

import argparse
import copy
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from dca_config import (
    ALLOWED_TARGETS,
    ConfigError,
    empty_analysis_state,
    global_rules_hash,
    rules_hash,
    validate_enabled_market_minimums,
    validate_analysis_state,
    validate_execution_state,
    validate_rules_map,
)


def global_rules_pre_state_hash(current_rules) -> str:
    """Compatibility name for the shared global rules fingerprint."""

    return global_rules_hash(current_rules)


def prepare_enable_analysis_invalidation(
    updated_rules, analysis_state, *, symbol: str, now: datetime | None = None
) -> dict:
    """Invalidate only the enabled target, retaining every unrelated decision.

    The workflow holds both rule-writer and analysis-writer locks and persists
    this document BEFORE enabling rules. A failed enable therefore only blocks
    analysis; it can never revive a formerly enabled same-day decision.
    """

    rules = validate_rules_map(updated_rules)
    if symbol not in ALLOWED_TARGETS or rules[symbol]["BUY_ENABLED"] is not True:
        raise ConfigError("Analysis invalidation requires a valid enabled target")
    try:
        state = validate_analysis_state(analysis_state)
    except Exception:
        raise ConfigError(
            "Cannot safely invalidate analysis; run !dca analyze all before enabling"
        ) from None
    reason = "Enable request invalidated the prior decision; successful analysis is required"
    replacement = empty_analysis_state(rules, now=now, reason=reason)["TARGETS"][symbol]
    replacement["ANALYSIS_STATUS"] = "ERROR"
    replacement["HISTORY"] = {"STATUS": "ERROR"}
    # Other targets keep their original envelope date, including at midnight.
    replacement["ANALYSIS_DATE"] = state["ANALYSIS_DATE"]
    # An invalidation is not an override release. Preserve visible audit fields.
    replacement["SIGNALS"] = {
        **copy.deepcopy(state["TARGETS"][symbol]["SIGNALS"]), "ERROR": reason
    }
    state["TARGETS"][symbol] = replacement
    return validate_analysis_state(state)


def _json_number(value: str, label: str) -> int | float:
    if not value:
        raise ConfigError(f"{label} is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} must be a JSON number") from exc
    if isinstance(parsed, bool) or not isinstance(parsed, (int, float)):
        raise ConfigError(f"{label} must be a JSON number")
    return parsed


def _json_boolean(value: str, label: str) -> bool:
    if not value:
        raise ConfigError(f"{label} is required")
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{label} must be true or false") from exc
    if not isinstance(parsed, bool):
        raise ConfigError(f"{label} must be true or false")
    return parsed


def _minimum_value(
    provider: Mapping[str, float] | Callable[[str], float | Mapping[str, float]],
    symbol: str,
) -> float:
    result = provider(symbol) if callable(provider) else provider.get(symbol)
    if isinstance(result, Mapping):
        result = result.get("effective_minimum_gbp")
    if isinstance(result, bool) or not isinstance(result, (int, float)):
        raise ConfigError(f"Could not prove Kraken's current minimum for {symbol}")
    return float(result)


def apply_change(
    current_rules,
    analysis_state=None,
    execution_state=None,
    *,
    action: str,
    symbol: str,
    low_amount_gbp_json: str = "",
    mid_amount_gbp_json: str = "",
    up_amount_gbp_json: str = "",
    enabled_json: str = "",
    expected_rules_hash: str = "",
    expected_decision_id: str = "",
    expected_global_rules_hash: str = "",
    market_minimum_provider=None,
    now: datetime | None = None,
) -> tuple[dict, bool]:
    """Return the validated replacement map and whether it should be written."""

    rules = validate_rules_map(current_rules)
    if symbol not in ALLOWED_TARGETS:
        raise ConfigError(f"Unknown production target: {symbol}")
    if action not in {"set_amounts", "set_enabled", "dry_run"}:
        raise ConfigError(f"Unsupported configuration action: {action}")

    if action in {"set_amounts", "dry_run"}:
        if rules[symbol]["BUY_ENABLED"]:
            raise ConfigError(f"Disable {symbol} before editing its budgets")
        low = _json_number(low_amount_gbp_json, "low_amount_gbp_json")
        up = _json_number(up_amount_gbp_json, "up_amount_gbp_json")
        mid = (
            _json_number(mid_amount_gbp_json, "mid_amount_gbp_json")
            if mid_amount_gbp_json
            else None
        )
        candidate = {key: dict(value) for key, value in rules.items()}
        amounts = {"LOW": low, "UP": up}
        if mid is not None:
            amounts["MID"] = mid
        candidate[symbol] = {
            "REGIME_AMOUNTS_GBP": amounts,
            "BUY_ENABLED": False,
        }
        normalized = validate_rules_map(candidate)
        return normalized, action != "dry_run"

    enabled = _json_boolean(enabled_json, "enabled_json")
    candidate = {key: dict(value) for key, value in rules.items()}
    candidate[symbol] = {
        "REGIME_AMOUNTS_GBP": dict(rules[symbol]["REGIME_AMOUNTS_GBP"]),
        "BUY_ENABLED": enabled,
    }
    if not enabled:
        return validate_rules_map(candidate), True

    if not expected_rules_hash or not expected_global_rules_hash:
        raise ConfigError(
            "Enabling requires the exact reviewed rules and global state"
        )
    if expected_global_rules_hash != global_rules_pre_state_hash(rules):
        raise ConfigError("Global DCA rules changed after the enable review")
    execution = validate_execution_state(execution_state)
    pending_symbols = [
        target
        for target, entry in execution.items()
        if isinstance(entry.get("PENDING_ORDER"), Mapping)
    ]
    if pending_symbols:
        raise ConfigError(
            "Cannot enable while Kraken order reconciliation is pending for "
            + ", ".join(pending_symbols)
        )
    live_hash = rules_hash(symbol, rules[symbol])
    if expected_rules_hash != live_hash:
        raise ConfigError("Budgets changed after the enable review")
    if market_minimum_provider is None:
        raise ConfigError("A fresh Kraken market-minimum check is required")
    minimum = _minimum_value(market_minimum_provider, symbol)
    updated = validate_enabled_market_minimums(candidate, {symbol: minimum})
    prepare_enable_analysis_invalidation(updated, analysis_state, symbol=symbol, now=now)
    return updated, True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--low-amount-gbp-json", default="")
    parser.add_argument("--mid-amount-gbp-json", default="")
    parser.add_argument("--up-amount-gbp-json", default="")
    parser.add_argument("--enabled-json", default="")
    parser.add_argument("--expected-rules-hash", default="")
    parser.add_argument("--expected-decision-id", default="")
    parser.add_argument("--expected-global-rules-hash", default="")
    parser.add_argument("--output", required=True)
    parser.add_argument("--analysis-output", default="")
    args = parser.parse_args(argv)

    rules = os.environ.get("DCA_TARGET_MAP", "")
    state = os.environ.get("DCA_ANALYSIS_STATE", "")
    # An explicitly persisted `{}` means no pending intents. A missing value is
    # not equivalent and must fail closed when enabling.
    execution_state = os.environ.get("DCA_EXECUTION_STATE", "")
    minimum_provider = None
    if args.action == "set_enabled" and args.enabled_json == "true":
        from kraken_client import get_market_minimum_gbp

        minimum_provider = get_market_minimum_gbp

    updated, should_write = apply_change(
        rules,
        state,
        execution_state,
        action=args.action,
        symbol=args.symbol,
        low_amount_gbp_json=args.low_amount_gbp_json,
        mid_amount_gbp_json=args.mid_amount_gbp_json,
        up_amount_gbp_json=args.up_amount_gbp_json,
        enabled_json=args.enabled_json,
        expected_rules_hash=args.expected_rules_hash,
        expected_decision_id=args.expected_decision_id,
        expected_global_rules_hash=args.expected_global_rules_hash,
        market_minimum_provider=minimum_provider,
    )
    if args.action == "set_enabled" and args.enabled_json == "true":
        if not args.analysis_output:
            raise ConfigError("Enable requires a separate pre-write analysis invalidation output")
        invalidated = prepare_enable_analysis_invalidation(updated, state, symbol=args.symbol)
        Path(args.analysis_output).write_text(
            json.dumps(invalidated, separators=(",", ":")), encoding="utf-8"
        )
    Path(args.output).write_text(
        json.dumps(updated, separators=(",", ":")), encoding="utf-8"
    )
    print(
        f"Validated {args.action} for {args.symbol}; "
        f"repository write={'required' if should_write else 'suppressed (dry run)'}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

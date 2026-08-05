"""Serialized, fail-closed updates for the user-owned DCA rules variable."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
from typing import Callable, Mapping

from dca_config import (
    ALLOWED_TARGETS,
    ConfigError,
    global_rules_hash,
    parse_utc_iso,
    rules_hash,
    validate_analysis_state,
    validate_enabled_market_minimums,
    validate_execution_state,
    validate_rules_map,
)


def global_rules_pre_state_hash(current_rules) -> str:
    """Compatibility name for the shared global rules fingerprint."""

    return global_rules_hash(current_rules)


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
        candidate = {key: dict(value) for key, value in rules.items()}
        candidate[symbol] = {
            "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": up},
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

    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    if (
        not expected_rules_hash
        or not expected_decision_id
        or not expected_global_rules_hash
    ):
        raise ConfigError(
            "Enabling requires the exact reviewed rules, global state, and decision"
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
    # Validate the document globally, then bind only the target being enabled.
    # A disabled asset whose budgets were edited and not yet re-analyzed must
    # not prevent an unrelated asset with a current decision from being enabled.
    state = validate_analysis_state(analysis_state, now=reference)
    decision = state["TARGETS"][symbol]
    live_hash = rules_hash(symbol, rules[symbol])
    if decision["STATUS"] != "READY":
        raise ConfigError(f"{symbol} does not have a READY analysis decision")
    if expected_rules_hash != live_hash or decision["RULES_HASH"] != live_hash:
        raise ConfigError("Budgets changed after the enable review")
    if expected_decision_id != decision["DECISION_ID"]:
        raise ConfigError("Analysis changed after the enable review")
    if reference.astimezone(timezone.utc) > parse_utc_iso(decision["VALID_UNTIL"]):
        raise ConfigError("The reviewed analysis decision is stale")
    if market_minimum_provider is None:
        raise ConfigError("A fresh Kraken market-minimum check is required")
    minimum = _minimum_value(market_minimum_provider, symbol)
    return validate_enabled_market_minimums(candidate, {symbol: minimum}), True


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--action", required=True)
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--low-amount-gbp-json", default="")
    parser.add_argument("--up-amount-gbp-json", default="")
    parser.add_argument("--enabled-json", default="")
    parser.add_argument("--expected-rules-hash", default="")
    parser.add_argument("--expected-decision-id", default="")
    parser.add_argument("--expected-global-rules-hash", default="")
    parser.add_argument("--output", required=True)
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
        up_amount_gbp_json=args.up_amount_gbp_json,
        enabled_json=args.enabled_json,
        expected_rules_hash=args.expected_rules_hash,
        expected_decision_id=args.expected_decision_id,
        expected_global_rules_hash=args.expected_global_rules_hash,
        market_minimum_provider=minimum_provider,
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

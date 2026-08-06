"""Shared schemas and safety checks for the Kraken USD-market DCA service.

The module deliberately contains no network calls.  GitHub Actions, Railway,
Discord, analysis, and order execution can therefore validate exactly the same
configuration before performing any side effect.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import math
import re
from typing import Any, Callable, Mapping


TARGET_KEYS = ("BTC_USD", "HYPE_USD", "SOL_USD")
ALLOWED_TARGETS = TARGET_KEYS
TARGET_SYMBOLS = {key: key.replace("_", "/") for key in TARGET_KEYS}
RULE_FIELDS = frozenset({"REGIME_AMOUNTS_GBP", "BUY_ENABLED"})
REGIME_AMOUNT_FIELDS = frozenset({"LOW", "UP"})
ANALYSIS_STATE_FIELDS = frozenset({"VERSION", "GENERATED_AT", "TARGETS"})
ANALYSIS_DECISION_FIELDS = frozenset(
    {
        "STATUS",
        "REGIME",
        "AMOUNT_TIER",
        "EXECUTE_AT",
        "VALID_UNTIL",
        "DECISION_ID",
        "RULES_HASH",
        "SIGNALS",
        "TIMING",
    }
)
ANALYSIS_STATE_VERSION = 2
AMOUNT_POLICY_VERSION = 2
MIN_ENABLED_AMOUNT_GBP = 5.0
MAX_AMOUNT_GBP = 1_000.0
READY_STATUS = "READY"
ERROR_STATUS = "ERROR"
REGIMES = frozenset({"UPTREND", "DOWNTREND", "SIDEWAYS"})
AMOUNT_TIERS = frozenset({"LOW", "MID", "HIGH"})
REGIME_AMOUNT_TIERS = {
    "DOWNTREND": "HIGH",
    "SIDEWAYS": "MID",
    "UPTREND": "LOW",
}


class ConfigError(ValueError):
    """Raised when persisted DCA configuration fails closed."""


def _json_object(value: str | Mapping[str, Any], label: str) -> dict[str, Any]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ConfigError(f"{label} is not valid JSON") from exc
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be a JSON object")
    return dict(value)


def _unexpected_fields(value: Mapping[str, Any], expected: frozenset[str], label: str) -> None:
    present = set(value)
    missing = expected - present
    extra = present - expected
    if missing:
        raise ConfigError(f"{label} is missing: {', '.join(sorted(missing))}")
    if extra:
        raise ConfigError(f"{label} contains unsupported fields: {', '.join(sorted(extra))}")


def _amount(value: Any, label: str) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{label} must be a finite number")
    number = float(value)
    if not math.isfinite(number):
        raise ConfigError(f"{label} must be a finite number")
    if number < 0 or number > MAX_AMOUNT_GBP:
        raise ConfigError(f"{label} must be between £0 and £{MAX_AMOUNT_GBP:,.0f}")
    decimal_value = Decimal(str(value))
    if decimal_value != decimal_value.quantize(Decimal("0.01")):
        raise ConfigError(f"{label} must have no more than two decimal places")
    return int(number) if number.is_integer() else number


def _minimum_for_target(
    target: str,
    minimums: Mapping[str, float] | Callable[[str], float | None] | None,
) -> float | None:
    if minimums is None:
        return None
    raw = minimums(target) if callable(minimums) else minimums.get(
        target, minimums.get(TARGET_SYMBOLS[target])
    )
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, (int, float)) or not math.isfinite(float(raw)):
        raise ConfigError(f"Market minimum for {target} must be a finite GBP amount")
    return float(raw)


def validate_target_map(
    value: str | Mapping[str, Any],
    market_minimums_gbp: Mapping[str, float] | Callable[[str], float | None] | None = None,
    *,
    require_all: bool = True,
) -> dict[str, dict[str, Any]]:
    """Validate and normalize user-owned DCA rules.

    Disabled targets may use zero as an explicit unconfigured placeholder.
    Enabled targets require both endpoints to be within £5–£1,000 and not below a
    supplied live Kraken market minimum. Only the three production USD market
    keys and the final two-field rule schema are accepted. Budget policy values
    remain GBP-denominated even though Kraken executes the target pairs in USD.
    """

    raw_map = _json_object(value, "DCA_TARGET_MAP")
    keys = set(raw_map)
    unsupported = keys - set(TARGET_KEYS)
    missing = set(TARGET_KEYS) - keys
    if unsupported:
        raise ConfigError(
            "DCA_TARGET_MAP contains unsupported targets: " + ", ".join(sorted(unsupported))
        )
    if require_all and missing:
        raise ConfigError(
            "DCA_TARGET_MAP is missing production targets: " + ", ".join(sorted(missing))
        )
    if not raw_map:
        raise ConfigError("DCA_TARGET_MAP must not be empty")

    normalized: dict[str, dict[str, Any]] = {}
    for target in TARGET_KEYS:
        if target not in raw_map:
            continue
        entry = raw_map[target]
        if not isinstance(entry, Mapping):
            raise ConfigError(f"DCA_TARGET_MAP.{target} must be an object")
        entry = dict(entry)
        _unexpected_fields(entry, RULE_FIELDS, f"DCA_TARGET_MAP.{target}")
        enabled = entry["BUY_ENABLED"]
        if not isinstance(enabled, bool):
            raise ConfigError(f"DCA_TARGET_MAP.{target}.BUY_ENABLED must be boolean")
        amounts = entry["REGIME_AMOUNTS_GBP"]
        if not isinstance(amounts, Mapping):
            raise ConfigError(
                f"DCA_TARGET_MAP.{target}.REGIME_AMOUNTS_GBP must be an object"
            )
        amounts = dict(amounts)
        _unexpected_fields(
            amounts,
            REGIME_AMOUNT_FIELDS,
            f"DCA_TARGET_MAP.{target}.REGIME_AMOUNTS_GBP",
        )
        low = _amount(amounts["LOW"], f"{target}.REGIME_AMOUNTS_GBP.LOW")
        up = _amount(amounts["UP"], f"{target}.REGIME_AMOUNTS_GBP.UP")
        if low > up:
            raise ConfigError(
                f"{target}.REGIME_AMOUNTS_GBP.LOW must not exceed UP; "
                "LOW is the lower endpoint and UP is the upper endpoint"
            )
        for tier, amount in (("LOW", low), ("UP", up)):
            if 0 < amount < MIN_ENABLED_AMOUNT_GBP:
                raise ConfigError(
                    f"{target}.{tier} must be £0 while unconfigured or at least "
                    f"£{MIN_ENABLED_AMOUNT_GBP:.0f}"
                )
        if enabled:
            for tier, amount in (("LOW", low), ("UP", up)):
                if amount < MIN_ENABLED_AMOUNT_GBP:
                    raise ConfigError(
                        f"{target}.{tier} must be at least £{MIN_ENABLED_AMOUNT_GBP:.0f} before enabling"
                    )
            market_minimum = _minimum_for_target(target, market_minimums_gbp)
            if market_minimum is not None:
                for tier, amount in (("LOW", low), ("UP", up)):
                    if amount < market_minimum:
                        raise ConfigError(
                            f"{target}.{tier} £{amount:g} is below Kraken's current "
                            f"£{market_minimum:g} market minimum"
                        )
        normalized[target] = {
            "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": up},
            "BUY_ENABLED": enabled,
        }
    return normalized


def validate_rules_map(
    obj: str | Mapping[str, Any], *, require_all: bool = True
) -> dict[str, dict[str, Any]]:
    """Integration-friendly alias for final rule validation."""

    return validate_target_map(obj, require_all=require_all)


def rules_hash(target: str, rule: Mapping[str, Any]) -> str:
    """Return a stable fingerprint of spend-affecting rules.

    ``BUY_ENABLED`` is intentionally excluded: toggling execution does not alter
    the analysis decision. Changing either endpoint or the amount-policy version
    invalidates that decision.
    """

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    normalized = validate_target_map({target: rule}, require_all=False)[target]
    payload = {
        "TARGET": target,
        "AMOUNT_POLICY_VERSION": AMOUNT_POLICY_VERSION,
        "REGIME_AMOUNTS_GBP": normalized["REGIME_AMOUNTS_GBP"],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return sha256(encoded).hexdigest()


rules_hash_for_target = rules_hash


def global_rules_hash(value: str | Mapping[str, Any]) -> str:
    """Fingerprint every budget and enable flag in canonical form."""

    normalized = validate_rules_map(value)
    encoded = json.dumps(
        normalized, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def amount_tier_for_regime(regime: str) -> str:
    """Return the counter-cyclical amount tier for a market regime."""

    if regime not in REGIMES:
        raise ConfigError(f"Unsupported market regime: {regime}")
    return REGIME_AMOUNT_TIERS[regime]


def amount_for_tier_gbp(rule: Mapping[str, Any], tier: str) -> int | float:
    """Return a lower, midpoint, or higher GBP budget.

    The persisted ``UP`` field is retained as the upper configured endpoint for
    backwards compatibility; it no longer means that an uptrend selects it.
    ``MID`` is the arithmetic midpoint rounded to the nearest penny with
    conventional half-up currency rounding.
    """

    if tier not in AMOUNT_TIERS:
        raise ConfigError(f"Unsupported amount tier: {tier}")
    entry = dict(rule)
    normalized = validate_target_map(
        {TARGET_KEYS[0]: entry}, require_all=False
    )[TARGET_KEYS[0]]
    amounts = normalized["REGIME_AMOUNTS_GBP"]
    if tier == "LOW":
        return amounts["LOW"]
    if tier == "HIGH":
        return amounts["UP"]
    midpoint = (
        (Decimal(str(amounts["LOW"])) + Decimal(str(amounts["UP"])))
        / Decimal("2")
    ).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    number = float(midpoint)
    return int(number) if number.is_integer() else number


def effective_amount_gbp(rule: Mapping[str, Any], regime: str) -> int | float:
    """Select the deterministic counter-cyclical budget for a market regime."""

    return amount_for_tier_gbp(rule, amount_tier_for_regime(regime))


def effective_amount(rule: Mapping[str, Any], decision: Mapping[str, Any] | str) -> float:
    """Return the GBP amount selected by an analysis decision or regime string."""

    if isinstance(decision, Mapping):
        regime = decision.get("REGIME")
        tier = decision.get("AMOUNT_TIER")
        expected_tier = REGIME_AMOUNT_TIERS.get(regime)
        if regime not in REGIMES or tier != expected_tier:
            raise ConfigError("Analysis decision does not contain a valid regime/tier pair")
    else:
        regime = decision
    return float(effective_amount_gbp(rule, regime))


def maximum_daily_exposure_gbp(value: str | Mapping[str, Any]) -> int | float:
    """Return worst-case aggregate daily spend across enabled targets."""

    target_map = validate_target_map(value)
    total = sum(
        max(entry["REGIME_AMOUNTS_GBP"].values())
        for entry in target_map.values()
        if entry["BUY_ENABLED"]
    )
    return int(total) if float(total).is_integer() else total


def parse_iso_datetime(value: Any, label: str) -> datetime:
    """Parse an aware ISO-8601 datetime and normalize it to UTC."""

    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{label} must be an ISO-8601 datetime")
    candidate = value.strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise ConfigError(f"{label} must be an ISO-8601 datetime") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ConfigError(f"{label} must include a timezone")
    return parsed.astimezone(timezone.utc)


def parse_utc_iso(value: Any) -> datetime:
    """Parse an aware ISO timestamp and normalize it to UTC."""

    return parse_iso_datetime(value, "timestamp")


def validate_analysis_decision(target: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one deterministic per-target analysis result."""

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    if not isinstance(value, Mapping):
        raise ConfigError(f"DCA_ANALYSIS_STATE.TARGETS.{target} must be an object")
    decision = dict(value)
    label = f"DCA_ANALYSIS_STATE.TARGETS.{target}"
    _unexpected_fields(decision, ANALYSIS_DECISION_FIELDS, label)
    status = decision["STATUS"]
    if status not in {READY_STATUS, ERROR_STATUS}:
        raise ConfigError(f"{label}.STATUS must be READY or ERROR")
    decision_id = decision["DECISION_ID"]
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ConfigError(f"{label}.DECISION_ID must be a non-empty string")
    fingerprint = decision["RULES_HASH"]
    if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        raise ConfigError(f"{label}.RULES_HASH must be a lowercase SHA-256 hash")
    if not isinstance(decision["SIGNALS"], Mapping):
        raise ConfigError(f"{label}.SIGNALS must be an object")
    if not isinstance(decision["TIMING"], Mapping):
        raise ConfigError(f"{label}.TIMING must be an object")
    timing = dict(decision["TIMING"])
    if "ANALYZED_AT" not in timing:
        raise ConfigError(f"{label}.TIMING.ANALYZED_AT is required")
    analyzed_at = parse_iso_datetime(
        timing["ANALYZED_AT"], f"{label}.TIMING.ANALYZED_AT"
    )

    if status == READY_STATUS:
        if decision["REGIME"] not in REGIMES:
            raise ConfigError(f"{label}.REGIME is invalid")
        if decision["AMOUNT_TIER"] not in AMOUNT_TIERS:
            raise ConfigError(f"{label}.AMOUNT_TIER must be LOW, MID, or HIGH")
        expected_tier = amount_tier_for_regime(decision["REGIME"])
        if decision["AMOUNT_TIER"] != expected_tier:
            raise ConfigError(f"{label}.AMOUNT_TIER does not match REGIME")
        execute_at = parse_iso_datetime(decision["EXECUTE_AT"], f"{label}.EXECUTE_AT")
        valid_until = parse_iso_datetime(decision["VALID_UNTIL"], f"{label}.VALID_UNTIL")
        if execute_at < analyzed_at + timedelta(minutes=30):
            raise ConfigError(
                f"{label}.EXECUTE_AT must be at least 30 minutes after ANALYZED_AT"
            )
        if valid_until != execute_at + timedelta(minutes=60):
            raise ConfigError(f"{label}.VALID_UNTIL must be exactly 60 minutes after EXECUTE_AT")
    else:
        if decision["REGIME"] is not None or decision["AMOUNT_TIER"] is not None:
            raise ConfigError(f"{label} ERROR decisions cannot select a regime or amount")
        if decision["EXECUTE_AT"] is not None or decision["VALID_UNTIL"] is not None:
            raise ConfigError(f"{label} ERROR decisions cannot select an execution time")

    return {
        **decision,
        "SIGNALS": dict(decision["SIGNALS"]),
        "TIMING": timing,
    }


def validate_analysis_state(
    obj: str | Mapping[str, Any],
    rules_map: str | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    require_all: bool = True,
) -> dict[str, Any]:
    """Validate the complete persisted ``DCA_ANALYSIS_STATE`` document.

    When live rules are supplied, READY decisions must be bound to the current
    budget fingerprint. ``now`` is accepted for all integration callers and is
    checked for timezone safety; staleness remains a status concern rather than
    making the historical state structurally invalid.
    """

    state = _json_object(obj, "DCA_ANALYSIS_STATE")
    _unexpected_fields(state, ANALYSIS_STATE_FIELDS, "DCA_ANALYSIS_STATE")
    if state["VERSION"] != ANALYSIS_STATE_VERSION:
        raise ConfigError(
            f"DCA_ANALYSIS_STATE.VERSION must be {ANALYSIS_STATE_VERSION}"
        )
    generated_at = parse_iso_datetime(state["GENERATED_AT"], "DCA_ANALYSIS_STATE.GENERATED_AT")
    targets = state["TARGETS"]
    if not isinstance(targets, Mapping):
        raise ConfigError("DCA_ANALYSIS_STATE.TARGETS must be an object")
    unsupported = set(targets) - set(TARGET_KEYS)
    missing = set(TARGET_KEYS) - set(targets)
    if unsupported:
        raise ConfigError(
            "DCA_ANALYSIS_STATE contains unsupported targets: "
            + ", ".join(sorted(unsupported))
        )
    if require_all and missing:
        raise ConfigError(
            "DCA_ANALYSIS_STATE is missing production targets: "
            + ", ".join(sorted(missing))
        )
    normalized_targets = {
        key: validate_analysis_decision(key, targets[key])
        for key in TARGET_KEYS
        if key in targets
    }
    if rules_map is not None:
        normalized_rules = validate_rules_map(rules_map, require_all=require_all)
        for target, decision in normalized_targets.items():
            if decision["STATUS"] == READY_STATUS:
                expected = rules_hash(target, normalized_rules[target])
                if decision["RULES_HASH"] != expected:
                    raise ConfigError(
                        f"DCA_ANALYSIS_STATE.TARGETS.{target}.RULES_HASH does not "
                        "match the live budgets"
                    )
    if now is not None and (now.tzinfo is None or now.utcoffset() is None):
        raise ConfigError("now must include a timezone")
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_at.isoformat().replace("+00:00", "Z"),
        "TARGETS": normalized_targets,
    }


def decision_is_usable(
    decision: Mapping[str, Any],
    *,
    target: str,
    expected_rules_hash: str,
    now: datetime,
    early_minutes: int = 5,
) -> tuple[bool, str]:
    """Check freshness, rule binding, and the -5/+60 execution window."""

    try:
        normalized = validate_analysis_decision(target, decision)
    except ConfigError as exc:
        return False, str(exc)
    if normalized["STATUS"] != READY_STATUS:
        return False, "analysis status is ERROR"
    if normalized["RULES_HASH"] != expected_rules_hash:
        return False, "analysis rules hash does not match live budgets"
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    now_utc = now.astimezone(timezone.utc)
    execute_at = parse_iso_datetime(normalized["EXECUTE_AT"], "EXECUTE_AT")
    valid_until = parse_iso_datetime(normalized["VALID_UNTIL"], "VALID_UNTIL")
    if now_utc < execute_at - timedelta(minutes=early_minutes):
        return False, "execution window has not opened"
    if now_utc > valid_until:
        return False, "analysis decision is stale or its execution window was missed"
    return True, "ready"


def decision_age_minutes(
    decision: Mapping[str, Any], now: datetime | None = None
) -> float:
    """Return minutes since the decision's deterministic analysis timestamp."""

    timing = decision.get("TIMING") if isinstance(decision, Mapping) else None
    analyzed_at = timing.get("ANALYZED_AT") if isinstance(timing, Mapping) else None
    if analyzed_at is None:
        raise ConfigError("Decision TIMING.ANALYZED_AT is required to calculate age")
    reference = now or datetime.now(timezone.utc)
    if reference.tzinfo is None or reference.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    return (reference.astimezone(timezone.utc) - parse_utc_iso(analyzed_at)).total_seconds() / 60


def decision_analyzed_on_or_after(
    decision: Mapping[str, Any], start_date: date | None, selected_tz
) -> bool:
    """Return whether a decision was produced on/after the local start date.

    The scheduled 04:00 Bangkok analysis occurs on the previous UTC calendar
    date.  Comparing in the configured local timezone prevents a pre-rollout
    decision from becoming executable just after local midnight.
    """

    if start_date is None:
        return True
    if not isinstance(start_date, date) or isinstance(start_date, datetime):
        raise ConfigError("start_date must be a calendar date")
    timing = decision.get("TIMING") if isinstance(decision, Mapping) else None
    analyzed_at = timing.get("ANALYZED_AT") if isinstance(timing, Mapping) else None
    if analyzed_at is None:
        raise ConfigError("Decision TIMING.ANALYZED_AT is required for the start-date gate")
    return parse_utc_iso(analyzed_at).astimezone(selected_tz).date() >= start_date


def is_execution_window(
    now: datetime,
    execute_at: str | datetime,
    valid_until: str | datetime | None = None,
) -> bool:
    """Return whether ``now`` is inside the inclusive -5/+60 minute window."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    execution = (
        parse_utc_iso(execute_at)
        if isinstance(execute_at, str)
        else execute_at.astimezone(timezone.utc)
    )
    maximum_expiry = execution + timedelta(minutes=60)
    supplied_expiry = (
        maximum_expiry
        if valid_until is None
        else parse_utc_iso(valid_until)
        if isinstance(valid_until, str)
        else valid_until.astimezone(timezone.utc)
    )
    expiry = min(supplied_expiry, maximum_expiry)
    current = now.astimezone(timezone.utc)
    return execution - timedelta(minutes=5) <= current <= expiry


def validate_execution_state(value: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate durable buy dates and pending order intents.

    Pending intents must carry the analysis ``DECISION_ID`` that originated the
    order, allowing recovery to reject an intent from an obsolete decision.
    """

    state = _json_object(value, "DCA_EXECUTION_STATE")
    unsupported = set(state) - set(TARGET_KEYS)
    if unsupported:
        raise ConfigError(
            "DCA_EXECUTION_STATE contains unsupported targets: "
            + ", ".join(sorted(unsupported))
        )
    normalized: dict[str, Any] = {}
    for target, raw_entry in state.items():
        if not isinstance(raw_entry, Mapping):
            raise ConfigError(f"DCA_EXECUTION_STATE.{target} must be an object")
        entry = dict(raw_entry)
        extra = set(entry) - {"LAST_BUY_DATE", "PENDING_ORDER"}
        if extra:
            raise ConfigError(
                f"DCA_EXECUTION_STATE.{target} contains unsupported fields: "
                + ", ".join(sorted(extra))
            )
        last_buy = entry.get("LAST_BUY_DATE", "")
        if not isinstance(last_buy, str):
            raise ConfigError(f"DCA_EXECUTION_STATE.{target}.LAST_BUY_DATE must be a string")
        if last_buy:
            try:
                if date.fromisoformat(last_buy).isoformat() != last_buy:
                    raise ValueError
            except ValueError as exc:
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.LAST_BUY_DATE must be YYYY-MM-DD"
                ) from exc
        pending = entry.get("PENDING_ORDER")
        if pending is not None:
            if not isinstance(pending, Mapping):
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER must be an object"
                )
            pending = dict(pending)
            expected_pending = frozenset(
                {
                    "client_order_id",
                    "funding_client_order_id",
                    "trade_date",
                    "amount_gbp",
                    "decision_id",
                    "created_at",
                }
            )
            _unexpected_fields(
                pending,
                expected_pending,
                f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER",
            )
            client_order_id = pending["client_order_id"]
            if not isinstance(client_order_id, str) or not re.fullmatch(
                r"dca-[0-9a-f]{14}", client_order_id
            ):
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.client_order_id "
                    "must match dca-[0-9a-f]{14}"
                )
            funding_client_order_id = pending["funding_client_order_id"]
            if not isinstance(funding_client_order_id, str) or not re.fullmatch(
                r"dca-[0-9a-f]{14}", funding_client_order_id
            ):
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.funding_client_order_id "
                    "must match dca-[0-9a-f]{14}"
                )
            if funding_client_order_id == client_order_id:
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.funding_client_order_id "
                    "must differ from client_order_id"
                )
            trade_date = pending["trade_date"]
            if not isinstance(trade_date, str):
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.trade_date must be YYYY-MM-DD"
                )
            try:
                if date.fromisoformat(trade_date).isoformat() != trade_date:
                    raise ValueError
            except ValueError as exc:
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.trade_date must be YYYY-MM-DD"
                ) from exc
            pending_amount = _amount(
                pending["amount_gbp"],
                f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.amount_gbp",
            )
            if pending_amount < MIN_ENABLED_AMOUNT_GBP:
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.amount_gbp must be "
                    f"between £{MIN_ENABLED_AMOUNT_GBP:.0f} and £{MAX_AMOUNT_GBP:,.0f}"
                )
            decision_id = pending["decision_id"]
            if not isinstance(decision_id, str) or not decision_id.strip():
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER requires decision_id"
                )
            parse_iso_datetime(
                pending["created_at"],
                f"DCA_EXECUTION_STATE.{target}.PENDING_ORDER.created_at",
            )
            pending["amount_gbp"] = pending_amount
        result_entry: dict[str, Any] = {"LAST_BUY_DATE": last_buy}
        if pending is not None:
            result_entry["PENDING_ORDER"] = pending
        normalized[target] = result_entry
    return normalized


def validate_enabled_market_minimums(
    rules_map: str | Mapping[str, Any],
    market_minimums_gbp: Mapping[str, float] | Callable[[str], float | None],
) -> dict[str, dict[str, Any]]:
    """Revalidate enabled budgets against freshly queried Kraken minimums."""

    return validate_target_map(rules_map, market_minimums_gbp)


def default_rules_map() -> dict[str, dict[str, Any]]:
    """Return the safe three-target bootstrap configuration."""

    return {
        target: {
            "REGIME_AMOUNTS_GBP": {"LOW": 0, "UP": 0},
            "BUY_ENABLED": False,
        }
        for target in TARGET_KEYS
    }


def empty_analysis_state(
    rules_map: str | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
    reason: str = "Analysis has not run",
) -> dict[str, Any]:
    """Return a complete fail-closed state with three fresh ERROR decisions."""

    rules = validate_rules_map(rules_map or default_rules_map())
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    generated_text = generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    targets = {}
    for target in TARGET_KEYS:
        digest = rules_hash(target, rules[target])
        targets[target] = {
            "STATUS": ERROR_STATUS,
            "REGIME": None,
            "AMOUNT_TIER": None,
            "EXECUTE_AT": None,
            "VALID_UNTIL": None,
            "DECISION_ID": f"bootstrap-{target.lower()}-{digest[:12]}",
            "RULES_HASH": digest,
            "SIGNALS": {"ERROR": reason},
            "TIMING": {"ANALYZED_AT": generated_text, "ERROR": reason},
        }
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_text,
        "TARGETS": targets,
    }


__all__ = [
    "ALLOWED_TARGETS",
    "AMOUNT_POLICY_VERSION",
    "AMOUNT_TIERS",
    "ANALYSIS_STATE_VERSION",
    "ConfigError",
    "ERROR_STATUS",
    "MAX_AMOUNT_GBP",
    "MIN_ENABLED_AMOUNT_GBP",
    "READY_STATUS",
    "REGIMES",
    "TARGET_KEYS",
    "TARGET_SYMBOLS",
    "amount_for_tier_gbp",
    "amount_tier_for_regime",
    "decision_is_usable",
    "decision_age_minutes",
    "decision_analyzed_on_or_after",
    "default_rules_map",
    "effective_amount",
    "effective_amount_gbp",
    "empty_analysis_state",
    "global_rules_hash",
    "is_execution_window",
    "maximum_daily_exposure_gbp",
    "parse_iso_datetime",
    "parse_utc_iso",
    "rules_hash",
    "rules_hash_for_target",
    "validate_analysis_decision",
    "validate_analysis_state",
    "validate_execution_state",
    "validate_enabled_market_minimums",
    "validate_rules_map",
    "validate_target_map",
]

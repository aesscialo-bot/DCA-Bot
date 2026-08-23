"""Shared schemas and safety checks for the GBP-market Kraken DCA service.

The module deliberately contains no network calls.  GitHub Actions, Railway,
Discord, analysis, and order execution can therefore validate exactly the same
configuration before performing any side effect.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from hashlib import sha256
import json
import math
import re
from typing import Any, Callable, Mapping


TARGET_KEYS = ("BTC_GBP", "ETH_GBP", "SOL_GBP")
ALLOWED_TARGETS = TARGET_KEYS
TARGET_SYMBOLS = {
    "BTC_GBP": "BTC/GBP",
    "ETH_GBP": "ETH/GBP",
    "SOL_GBP": "SOL/GBP",
}
TARGET_ROUTES = {
    "BTC_GBP": "DIRECT_GBP",
    "ETH_GBP": "DIRECT_GBP",
    "SOL_GBP": "DIRECT_GBP",
}
RULE_FIELDS = frozenset({"REGIME_AMOUNTS_GBP", "BUY_ENABLED"})
REGIME_AMOUNT_FIELDS = frozenset({"LOW", "UP"})
TIMING_POLICY_VERSION = (
    "sma150-10-consecutive-closes-v1+multi-window-3-5-7-14-30-45-60-v2"
)
UPTREND_OVERRIDE_STATE_VERSION = 1
UPTREND_OVERRIDE_STATE_FIELDS = frozenset({"VERSION", "TARGETS"})
UPTREND_OVERRIDE_ENTRY_FIELDS = frozenset(
    {"ACTIVE", "ACTIVATED_AT", "RELEASED_AT", "REASON"}
)
MAX_UPTREND_OVERRIDE_REASON_CHARS = 200
UPTREND_CONFIRMATION_CANDLES = 10
# Classifier metrics are persisted after round(..., 8), while their booleans
# are computed from full-precision values. A pair of rounded metrics can move
# their difference by at most 1e-8, so only larger contradictions are invalid.
ANALYSIS_SIGNAL_ROUNDING_TOLERANCE = 1e-8
ANALYSIS_READY_SIGNAL_FIELDS = frozenset(
    {
        "DAILY_LAST_COMPLETE",
        "DAILY_CLOSE",
        "DAILY_PREVIOUS_CLOSE",
        "DAILY_SMA150",
        "DAILY_PREVIOUS_SMA150",
        "DAILY_EMA20",
        "DAILY_EMA50",
        "DAILY_PREVIOUS_EMA20",
        "DAILY_PREVIOUS_EMA50",
        "WEEKLY_LAST_COMPLETE",
        "WEEKLY_CLOSE",
        "WEEKLY_EMA20",
        "SMA150_SLOPE_20D",
        "TWO_DAY_ABOVE",
        "TWO_DAY_BELOW",
        "WEEKLY_ABOVE",
        "WEEKLY_BELOW",
        "SLOPE_POSITIVE",
        "SLOPE_NEGATIVE",
        "UPTREND_CONFIRMATION_REQUIRED",
        "UPTREND_CONFIRMATION_COUNT",
        "UPTREND_CONFIRMED",
        "REGIME_WITHOUT_OVERRIDE",
        "UPTREND_OVERRIDE_ACTIVE",
        "UPTREND_OVERRIDE_APPLIED",
        "UPTREND_OVERRIDE_REASON",
        "UPTREND_OVERRIDE_ACTIVATED_AT",
        "UPTREND_OVERRIDE_RELEASED_AT",
        "UPTREND_OVERRIDE_AUTO_RELEASED",
    }
)
UPTREND_OVERRIDE_AUDIT_SIGNAL_FIELDS = frozenset(
    {
        "REGIME_WITHOUT_OVERRIDE",
        "UPTREND_OVERRIDE_ACTIVE",
        "UPTREND_OVERRIDE_APPLIED",
        "UPTREND_OVERRIDE_REASON",
        "UPTREND_OVERRIDE_ACTIVATED_AT",
        "UPTREND_OVERRIDE_RELEASED_AT",
        "UPTREND_OVERRIDE_AUTO_RELEASED",
    }
)
DAILY_ANALYSIS_EXPECTED_BY = time(4, 20)
ANALYSIS_STATE_FIELDS = frozenset(
    {"VERSION", "GENERATED_AT", "POLICY_VERSION", "ANALYSIS_DATE", "TARGETS"}
)
ANALYSIS_DECISION_FIELDS = frozenset(
    {
        "ENABLED",
        "ANALYSIS_STATUS",
        "EXECUTION_STATUS",
        "REGIME",
        "AMOUNT_TIER",
        "SELECTED_AT",
        "EXECUTE_AT",
        "VALID_UNTIL",
        "CATCHUP_APPLIED",
        "DECISION_ID",
        "RULES_HASH",
        "POLICY_VERSION",
        "ANALYSIS_DATE",
        "HISTORY",
        "SIGNALS",
        "TIMING",
        "ERROR",
    }
)
ANALYSIS_STATE_VERSION = 3
AMOUNT_POLICY_VERSION = 2
GIST_DELIVERY_VERSION = 3
MAX_PENDING_GIST_DELIVERIES = 16
MAX_GIST_DELIVERY_ROW_BYTES = 2_048
MAX_EXECUTION_STATE_JSON_BYTES = 40_000
# A validated row contains no control characters and is serialized with
# ``ensure_ascii=False``.  Quotes and backslashes are therefore its worst JSON
# expansion (2x), with the remaining bytes covering the envelope, queue field,
# and target entry.  Keeping this separate from the 40 KB hard budget lets the
# executor reserve space for fill evidence before Kraken can receive AddOrder.
GIST_DELIVERY_RESERVED_JSON_BYTES = 5_632
GIST_DELIVERY_FIELDS = frozenset(
    {
        "version",
        "delivery_id",
        "created_at",
        "symbol",
        "row",
        "row_sha256",
        "event",
        "event_sha256",
    }
)
PORTFOLIO_EVENT_FIELDS = frozenset(
    {
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
)
PORTFOLIO_EVENT_V2_FIELDS = frozenset(
    {
        "event_version", "event_id", "occurred_at", "target",
        "base_currency", "quote_currency", "budget_currency",
        "funding_order_id", "crypto_order_id", "gbp_debit",
        "gbp_usd_rate", "funded_usd", "crypto_cost_usd",
        "crypto_quantity", "unit_price_usd", "funding_fee_usd",
        "crypto_fee_usd", "canonical_hash",
    }
)
MIN_ENABLED_AMOUNT_GBP = 5.0
MAX_AMOUNT_GBP = 1_000.0
READY_STATUS = "READY"
ERROR_STATUS = "ERROR"
ANALYSIS_STATUSES = frozenset(
    {"HISTORY_NOT_READY", "AWAITING_ANALYSIS", READY_STATUS, ERROR_STATUS}
)
EXECUTION_STATUSES = frozenset(
    {"DISABLED", "SHADOW", "ARMED", "DUE", "EXECUTED", "EXPIRED", "BLOCKED"}
)
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
        supplied live Kraken market minimum. Only the three production GBP-market
        keys and the final two-field rule schema are accepted. All budget values
        remain GBP-denominated.
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


def _canonical_utc_timestamp(value: Any, label: str) -> str:
    parsed = parse_iso_datetime(value, label)
    normalized = parsed.isoformat().replace("+00:00", "Z")
    if value != normalized:
        raise ConfigError(f"{label} must be a canonical UTC ISO timestamp")
    return normalized


def _valid_override_reason(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or value != value.strip()
        or len(value) > MAX_UPTREND_OVERRIDE_REASON_CHARS
        or any(ord(character) < 32 for character in value)
    ):
        raise ConfigError(
            f"{label} must be a trimmed, non-empty string of at most "
            f"{MAX_UPTREND_OVERRIDE_REASON_CHARS} characters without controls"
        )
    return value


def validate_uptrend_override_state(
    value: str | Mapping[str, Any],
) -> dict[str, Any]:
    """Validate the optional, per-target emergency UPTREND override state."""

    state = _json_object(value, "DCA_UPTREND_OVERRIDE_STATE")
    _unexpected_fields(
        state, UPTREND_OVERRIDE_STATE_FIELDS, "DCA_UPTREND_OVERRIDE_STATE"
    )
    if (
        type(state["VERSION"]) is not int
        or state["VERSION"] != UPTREND_OVERRIDE_STATE_VERSION
    ):
        raise ConfigError(
            "DCA_UPTREND_OVERRIDE_STATE.VERSION must be "
            f"{UPTREND_OVERRIDE_STATE_VERSION}"
        )
    targets = state["TARGETS"]
    if not isinstance(targets, Mapping):
        raise ConfigError("DCA_UPTREND_OVERRIDE_STATE.TARGETS must be an object")
    unsupported = set(targets) - set(TARGET_KEYS)
    if unsupported:
        raise ConfigError(
            "DCA_UPTREND_OVERRIDE_STATE contains unsupported targets: "
            + ", ".join(sorted(unsupported))
        )

    normalized_targets: dict[str, dict[str, Any]] = {}
    for target in TARGET_KEYS:
        if target not in targets:
            continue
        label = f"DCA_UPTREND_OVERRIDE_STATE.TARGETS.{target}"
        raw_entry = targets[target]
        if not isinstance(raw_entry, Mapping):
            raise ConfigError(f"{label} must be an object")
        entry = dict(raw_entry)
        _unexpected_fields(entry, UPTREND_OVERRIDE_ENTRY_FIELDS, label)
        if type(entry["ACTIVE"]) is not bool:
            raise ConfigError(f"{label}.ACTIVE must be a boolean")
        activated_at = _canonical_utc_timestamp(
            entry["ACTIVATED_AT"], f"{label}.ACTIVATED_AT"
        )
        reason = _valid_override_reason(entry["REASON"], f"{label}.REASON")

        released_value = entry["RELEASED_AT"]
        if entry["ACTIVE"]:
            if released_value is not None:
                raise ConfigError(f"{label}.RELEASED_AT must be null while active")
            released_at = None
        else:
            if released_value is None:
                raise ConfigError(f"{label}.RELEASED_AT is required when inactive")
            released_at = _canonical_utc_timestamp(
                released_value, f"{label}.RELEASED_AT"
            )
            if parse_iso_datetime(released_at, f"{label}.RELEASED_AT") < parse_iso_datetime(
                activated_at, f"{label}.ACTIVATED_AT"
            ):
                raise ConfigError(
                    f"{label}.RELEASED_AT must not precede ACTIVATED_AT"
                )
        normalized_targets[target] = {
            "ACTIVE": entry["ACTIVE"],
            "ACTIVATED_AT": activated_at,
            "RELEASED_AT": released_at,
            "REASON": reason,
        }

    return {
        "VERSION": UPTREND_OVERRIDE_STATE_VERSION,
        "TARGETS": normalized_targets,
    }


def analysis_decision_matches_uptrend_override(
    target: str,
    decision: Mapping[str, Any],
    override_state: str | Mapping[str, Any],
) -> bool:
    """Bind a persisted decision to the exact live per-target override entry."""

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    if not isinstance(decision, Mapping):
        return False
    signals = decision.get("SIGNALS")
    if not isinstance(signals, Mapping):
        return False
    normalized = validate_uptrend_override_state(override_state)
    entry = normalized["TARGETS"].get(target)
    if entry is None:
        return (
            signals.get("UPTREND_OVERRIDE_ACTIVE") is False
            and signals.get("UPTREND_OVERRIDE_APPLIED") is False
            and signals.get("UPTREND_OVERRIDE_REASON") is None
            and signals.get("UPTREND_OVERRIDE_ACTIVATED_AT") is None
            and signals.get("UPTREND_OVERRIDE_RELEASED_AT") is None
            and signals.get("UPTREND_OVERRIDE_AUTO_RELEASED") is False
        )
    return (
        signals.get("UPTREND_OVERRIDE_ACTIVE") is entry["ACTIVE"]
        and signals.get("UPTREND_OVERRIDE_REASON") == entry["REASON"]
        and signals.get("UPTREND_OVERRIDE_ACTIVATED_AT")
        == entry["ACTIVATED_AT"]
        and signals.get("UPTREND_OVERRIDE_RELEASED_AT")
        == entry["RELEASED_AT"]
        and (entry["ACTIVE"] is not True or decision.get("REGIME") == "UPTREND")
    )


def _split_markdown_row(line: str) -> list[str] | None:
    """Split one Markdown row without treating escaped pipes as separators."""

    if not isinstance(line, str) or not line.startswith("|"):
        return None
    cells: list[str] = []
    current: list[str] = []
    escaped = False
    for character in line:
        if character == "|" and not escaped:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(character)
        escaped = not escaped if character == "\\" else False
    if current or not line.endswith("|") or len(cells) < 2 or cells[0]:
        return None
    return cells[1:]


def _unescape_markdown_value(value: str) -> str:
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            next_character = value[index + 1]
            if next_character in {"\\", "|"}:
                result.append(next_character)
                index += 2
                continue
        result.append(value[index])
        index += 1
    return "".join(result)


def validate_gist_delivery(value: Mapping[str, Any], target: str) -> dict[str, Any]:
    """Validate one immutable Portfolio Compass Gist delivery.

    The Kraken crypto order identifier is the durable delivery identity.  The
    Markdown row is kept byte-for-byte so its digest remains stable across
    execution-state reads and writes.
    """

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    label = f"DCA_EXECUTION_STATE.{target}.PENDING_GIST_DELIVERIES[]"
    if not isinstance(value, Mapping):
        raise ConfigError(f"{label} must be an object")
    delivery = dict(value)
    _unexpected_fields(delivery, GIST_DELIVERY_FIELDS, label)

    version = delivery["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in {2, GIST_DELIVERY_VERSION}
    ):
        raise ConfigError(f"{label}.version must be 2 or {GIST_DELIVERY_VERSION}")

    delivery_id = delivery["delivery_id"]
    if not isinstance(delivery_id, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}", delivery_id
    ):
        raise ConfigError(
            f"{label}.delivery_id must be a safe nonempty Kraken order identifier"
        )

    created_at = delivery["created_at"]
    if not isinstance(created_at, str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?Z",
        created_at,
    ):
        raise ConfigError(f"{label}.created_at must be a canonical UTC ISO timestamp")
    if parse_iso_datetime(created_at, f"{label}.created_at").utcoffset() != timedelta(0):
        raise ConfigError(f"{label}.created_at must be a canonical UTC ISO timestamp")

    expected_symbol = TARGET_SYMBOLS[target].split("/", 1)[0]
    if not isinstance(delivery["symbol"], str) or delivery["symbol"] != expected_symbol:
        raise ConfigError(f"{label}.symbol must be {expected_symbol}")

    row = delivery["row"]
    if (
        not isinstance(row, str)
        or not row.endswith("\n")
        or row.count("\n") != 1
        or "\r" in row
        or not row.startswith("|")
        or not row.endswith("|\n")
    ):
        raise ConfigError(
            f"{label}.row must be one Markdown data line ending with a newline"
        )
    try:
        row_bytes = row.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ConfigError(f"{label}.row must be valid UTF-8 text") from exc
    if len(row_bytes) > MAX_GIST_DELIVERY_ROW_BYTES:
        raise ConfigError(
            f"{label}.row must be at most {MAX_GIST_DELIVERY_ROW_BYTES} UTF-8 bytes"
        )
    if any(ord(character) < 32 for character in row[:-1]):
        raise ConfigError(f"{label}.row must not contain control characters")
    cells = _split_markdown_row(row.removesuffix("\n"))
    if not cells or len(cells) != 11:
        raise ConfigError(f"{label}.row must contain exactly 11 Markdown columns")
    if _unescape_markdown_value(cells[9]) != delivery_id:
        raise ConfigError(f"{label}.delivery_id must match the crypto order column")

    row_sha256 = delivery["row_sha256"]
    if not isinstance(row_sha256, str) or not re.fullmatch(
        r"[0-9a-f]{64}", row_sha256
    ):
        raise ConfigError(f"{label}.row_sha256 must be a lowercase SHA-256 digest")
    if row_sha256 != sha256(row_bytes).hexdigest():
        raise ConfigError(f"{label}.row_sha256 does not match row")
    event = delivery["event"]
    if not isinstance(event, Mapping):
        raise ConfigError(f"{label}.event must be an object")
    event = dict(event)
    event_version = event.get("event_version")
    expected_event_fields = (
        PORTFOLIO_EVENT_V2_FIELDS if event_version == 2 else PORTFOLIO_EVENT_FIELDS
    )
    _unexpected_fields(event, expected_event_fields, f"{label}.event")
    if event_version != version:
        raise ConfigError(f"{label}.event.event_version must match delivery version")
    if event["event_id"] != delivery_id or event["crypto_order_id"] != delivery_id:
        raise ConfigError(f"{label}.event identifiers must match delivery_id")
    if event["target"] != target:
        raise ConfigError(f"{label}.event.target must be {target}")
    if event["base_currency"] != expected_symbol:
        raise ConfigError(f"{label}.event.base_currency must be {expected_symbol}")
    if event_version == 2:
        if event["quote_currency"] != "USD" or event["budget_currency"] != "GBP":
            raise ConfigError(f"{label}.event v2 currencies must be GBP-funded USD")
        quote_currency = "USD"
        expected_route = "GBP_TO_USD"
    else:
        quote_currency = TARGET_SYMBOLS[target].split("/", 1)[1]
        expected_route = TARGET_ROUTES[target]
    if event["quote_currency"] != quote_currency or event["budget_currency"] != "GBP":
        raise ConfigError(f"{label}.event currencies do not match {TARGET_SYMBOLS[target]}")
    if event_version == 3 and event["route"] != expected_route:
        raise ConfigError(f"{label}.event.route must be {expected_route}")
    if event["occurred_at"] != created_at:
        raise ConfigError(f"{label}.event.occurred_at must match created_at")
    decimal_fields = (
        (
            "gbp_debit", "gbp_usd_rate", "funded_usd", "crypto_cost_usd",
            "crypto_quantity", "unit_price_usd", "funding_fee_usd", "crypto_fee_usd",
        )
        if event_version == 2
        else (
        "gbp_debit",
        "gbp_usd_rate",
        "funded_usd",
        "crypto_cost_quote",
        "crypto_quantity",
        "unit_price_quote",
        "funding_fee_quote",
        "crypto_fee_quote",
        )
    )
    for field in decimal_fields:
        raw = event[field]
        if not isinstance(raw, str):
            raise ConfigError(f"{label}.event.{field} must be a decimal string")
        try:
            number = Decimal(raw)
        except Exception as exc:
            raise ConfigError(f"{label}.event.{field} must be a decimal string") from exc
        if not number.is_finite() or number < 0:
            raise ConfigError(f"{label}.event.{field} must be non-negative")
    funding_order_id = event["funding_order_id"]
    if event_version == 2 or expected_route == "GBP_TO_USD":
        if not isinstance(funding_order_id, str) or not funding_order_id.strip():
            raise ConfigError(f"{label}.event.funding_order_id must be non-empty")
    elif funding_order_id is not None:
        raise ConfigError(f"{label}.event.funding_order_id must be null for direct GBP")
    canonical_hash = event["canonical_hash"]
    event_without_hash = {key: value for key, value in event.items() if key != "canonical_hash"}
    expected_canonical_hash = sha256(
        json.dumps(
            event_without_hash,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    if canonical_hash != expected_canonical_hash:
        raise ConfigError(f"{label}.event.canonical_hash does not match event")
    canonical_event = json.dumps(
        event, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    event_sha256 = delivery["event_sha256"]
    if not isinstance(event_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", event_sha256):
        raise ConfigError(f"{label}.event_sha256 must be a lowercase SHA-256 digest")
    if event_sha256 != sha256(canonical_event).hexdigest():
        raise ConfigError(f"{label}.event_sha256 does not match event")
    delivery["event"] = event
    return delivery


def _analysis_signal_number(
    signals: Mapping[str, Any],
    field: str,
    label: str,
    *,
    positive: bool = False,
) -> float:
    raw = signals[field]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ConfigError(f"{label}.{field} must be a finite number")
    number = float(raw)
    if not math.isfinite(number):
        raise ConfigError(f"{label}.{field} must be a finite number")
    if positive and number <= 0:
        raise ConfigError(f"{label}.{field} must be greater than zero")
    return number


def _validate_override_signal_audit(
    signals: Mapping[str, Any],
    label: str,
    *,
    analyzed_at: datetime,
    require_all: bool,
) -> dict[str, Any] | None:
    """Validate override audit metadata shared by ready and error decisions."""

    present = set(signals) & set(UPTREND_OVERRIDE_AUDIT_SIGNAL_FIELDS)
    if not present:
        return None
    if not require_all and "UPTREND_OVERRIDE_ACTIVE" not in signals:
        raise ConfigError(
            f"{label}.UPTREND_OVERRIDE_ACTIVE is required with override audit fields"
        )

    boolean_fields = (
        "UPTREND_OVERRIDE_ACTIVE",
        "UPTREND_OVERRIDE_APPLIED",
        "UPTREND_OVERRIDE_AUTO_RELEASED",
    )
    for field in boolean_fields:
        if field in signals and type(signals[field]) is not bool:
            raise ConfigError(f"{label}.{field} must be a boolean")

    active = signals.get("UPTREND_OVERRIDE_ACTIVE")
    applied = signals.get("UPTREND_OVERRIDE_APPLIED", False)
    auto_released = signals.get("UPTREND_OVERRIDE_AUTO_RELEASED", False)
    normal_regime = signals.get("REGIME_WITHOUT_OVERRIDE")
    if normal_regime is not None and normal_regime not in REGIMES:
        raise ConfigError(f"{label}.REGIME_WITHOUT_OVERRIDE is invalid")

    reason = signals.get("UPTREND_OVERRIDE_REASON")
    activated_value = signals.get("UPTREND_OVERRIDE_ACTIVATED_AT")
    released_value = signals.get("UPTREND_OVERRIDE_RELEASED_AT")
    if reason is not None:
        reason = _valid_override_reason(
            reason, f"{label}.UPTREND_OVERRIDE_REASON"
        )
    activated_at = (
        _canonical_utc_timestamp(
            activated_value, f"{label}.UPTREND_OVERRIDE_ACTIVATED_AT"
        )
        if activated_value is not None
        else None
    )
    released_at = (
        _canonical_utc_timestamp(
            released_value, f"{label}.UPTREND_OVERRIDE_RELEASED_AT"
        )
        if released_value is not None
        else None
    )

    if (reason is None) != (activated_at is None):
        raise ConfigError(
            f"{label} override REASON and ACTIVATED_AT must both be null or populated"
        )
    if active is True:
        if reason is None:
            raise ConfigError(
                f"{label} active override requires REASON and ACTIVATED_AT"
            )
        if released_at is not None:
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_RELEASED_AT must be null while active"
            )
        if auto_released:
            raise ConfigError(
                f"{label} active override cannot be marked auto-released"
            )
    elif active is False:
        if applied:
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_APPLIED requires an active override"
            )
        if reason is None and released_at is not None:
            raise ConfigError(
                f"{label} released override requires REASON and ACTIVATED_AT"
            )
        if reason is not None and released_at is None:
            raise ConfigError(
                f"{label} inactive override with lifecycle metadata requires RELEASED_AT"
            )
    else:
        raise ConfigError(f"{label}.UPTREND_OVERRIDE_ACTIVE must be a boolean")

    if applied:
        if normal_regime is None:
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_APPLIED requires REGIME_WITHOUT_OVERRIDE"
            )
        if normal_regime == "UPTREND":
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_APPLIED must be false for natural UPTREND"
            )
    if auto_released and released_at is None:
        raise ConfigError(
            f"{label}.UPTREND_OVERRIDE_AUTO_RELEASED requires RELEASED_AT"
        )

    activated_dt = (
        parse_iso_datetime(activated_at, f"{label}.UPTREND_OVERRIDE_ACTIVATED_AT")
        if activated_at is not None
        else None
    )
    released_dt = (
        parse_iso_datetime(released_at, f"{label}.UPTREND_OVERRIDE_RELEASED_AT")
        if released_at is not None
        else None
    )
    if activated_dt is not None and activated_dt > analyzed_at:
        raise ConfigError(
            f"{label}.UPTREND_OVERRIDE_ACTIVATED_AT cannot follow ANALYZED_AT"
        )
    if released_dt is not None:
        if activated_dt is None or released_dt < activated_dt:
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_RELEASED_AT cannot precede ACTIVATED_AT"
            )
        if released_dt > analyzed_at:
            raise ConfigError(
                f"{label}.UPTREND_OVERRIDE_RELEASED_AT cannot follow ANALYZED_AT"
            )

    return {
        "active": active,
        "applied": applied,
        "auto_released": auto_released,
        "normal_regime": normal_regime,
        "activated_at": activated_dt,
        "released_at": released_dt,
    }


def _validate_ready_analysis_signals(
    decision: Mapping[str, Any],
    analyzed_at: datetime,
    label: str,
) -> None:
    signals = dict(decision["SIGNALS"])
    signal_label = f"{label}.SIGNALS"
    _unexpected_fields(signals, ANALYSIS_READY_SIGNAL_FIELDS, signal_label)

    completed_at = _canonical_utc_timestamp(
        signals["DAILY_LAST_COMPLETE"], f"{signal_label}.DAILY_LAST_COMPLETE"
    )
    weekly_completed_at = _canonical_utc_timestamp(
        signals["WEEKLY_LAST_COMPLETE"], f"{signal_label}.WEEKLY_LAST_COMPLETE"
    )
    daily_open = parse_iso_datetime(
        completed_at, f"{signal_label}.DAILY_LAST_COMPLETE"
    )
    weekly_open = parse_iso_datetime(
        weekly_completed_at, f"{signal_label}.WEEKLY_LAST_COMPLETE"
    )
    if daily_open + timedelta(days=1) > analyzed_at:
        raise ConfigError(
            f"{signal_label}.DAILY_LAST_COMPLETE must identify a completed candle"
        )
    if weekly_open + timedelta(days=7) > analyzed_at:
        raise ConfigError(
            f"{signal_label}.WEEKLY_LAST_COMPLETE must identify a completed candle"
        )

    positive_fields = (
        "DAILY_CLOSE",
        "DAILY_PREVIOUS_CLOSE",
        "DAILY_SMA150",
        "DAILY_PREVIOUS_SMA150",
        "DAILY_EMA20",
        "DAILY_EMA50",
        "DAILY_PREVIOUS_EMA20",
        "DAILY_PREVIOUS_EMA50",
        "WEEKLY_CLOSE",
        "WEEKLY_EMA20",
    )
    numbers = {
        field: _analysis_signal_number(
            signals, field, signal_label, positive=True
        )
        for field in positive_fields
    }
    numbers["SMA150_SLOPE_20D"] = _analysis_signal_number(
        signals, "SMA150_SLOPE_20D", signal_label
    )

    classifier_boolean_fields = (
        "TWO_DAY_ABOVE",
        "TWO_DAY_BELOW",
        "WEEKLY_ABOVE",
        "WEEKLY_BELOW",
        "SLOPE_POSITIVE",
        "SLOPE_NEGATIVE",
        "UPTREND_CONFIRMED",
    )
    for field in classifier_boolean_fields:
        if type(signals[field]) is not bool:
            raise ConfigError(f"{signal_label}.{field} must be a boolean")

    required = signals["UPTREND_CONFIRMATION_REQUIRED"]
    if type(required) is not int or required != UPTREND_CONFIRMATION_CANDLES:
        raise ConfigError(
            f"{signal_label}.UPTREND_CONFIRMATION_REQUIRED must be "
            f"{UPTREND_CONFIRMATION_CANDLES}"
        )
    count = signals["UPTREND_CONFIRMATION_COUNT"]
    if type(count) is not int or not 0 <= count <= required:
        raise ConfigError(
            f"{signal_label}.UPTREND_CONFIRMATION_COUNT must be an integer "
            f"between 0 and {required}"
        )
    confirmed = signals["UPTREND_CONFIRMED"]
    if confirmed is not (count == required):
        raise ConfigError(
            f"{signal_label}.UPTREND_CONFIRMED must match confirmation count"
        )

    mutually_exclusive_pairs = (
        ("TWO_DAY_ABOVE", "TWO_DAY_BELOW"),
        ("WEEKLY_ABOVE", "WEEKLY_BELOW"),
        ("SLOPE_POSITIVE", "SLOPE_NEGATIVE"),
    )
    for first, second in mutually_exclusive_pairs:
        if signals[first] and signals[second]:
            raise ConfigError(
                f"{signal_label}.{first} and {second} cannot both be true"
            )

    tolerance = ANALYSIS_SIGNAL_ROUNDING_TOLERANCE
    two_day_deltas = (
        numbers["DAILY_CLOSE"] - numbers["DAILY_SMA150"],
        numbers["DAILY_EMA20"] - numbers["DAILY_EMA50"],
        numbers["DAILY_PREVIOUS_CLOSE"]
        - numbers["DAILY_PREVIOUS_SMA150"],
        numbers["DAILY_PREVIOUS_EMA20"]
        - numbers["DAILY_PREVIOUS_EMA50"],
    )
    if signals["TWO_DAY_ABOVE"] and any(
        delta < -tolerance for delta in two_day_deltas
    ):
        raise ConfigError(
            f"{signal_label}.TWO_DAY_ABOVE contradicts the persisted daily metrics"
        )
    if not signals["TWO_DAY_ABOVE"] and all(
        delta > tolerance for delta in two_day_deltas
    ):
        raise ConfigError(
            f"{signal_label}.TWO_DAY_ABOVE must be true for the persisted daily metrics"
        )
    if signals["TWO_DAY_BELOW"] and any(
        delta > tolerance for delta in two_day_deltas
    ):
        raise ConfigError(
            f"{signal_label}.TWO_DAY_BELOW contradicts the persisted daily metrics"
        )
    if not signals["TWO_DAY_BELOW"] and all(
        delta < -tolerance for delta in two_day_deltas
    ):
        raise ConfigError(
            f"{signal_label}.TWO_DAY_BELOW must be true for the persisted daily metrics"
        )

    weekly_delta = numbers["WEEKLY_CLOSE"] - numbers["WEEKLY_EMA20"]
    if weekly_delta > tolerance and not signals["WEEKLY_ABOVE"]:
        raise ConfigError(
            f"{signal_label}.WEEKLY_ABOVE contradicts the persisted weekly metrics"
        )
    if weekly_delta < -tolerance and not signals["WEEKLY_BELOW"]:
        raise ConfigError(
            f"{signal_label}.WEEKLY_BELOW contradicts the persisted weekly metrics"
        )
    if signals["WEEKLY_ABOVE"] and weekly_delta < -tolerance:
        raise ConfigError(
            f"{signal_label}.WEEKLY_ABOVE contradicts the persisted weekly metrics"
        )
    if signals["WEEKLY_BELOW"] and weekly_delta > tolerance:
        raise ConfigError(
            f"{signal_label}.WEEKLY_BELOW contradicts the persisted weekly metrics"
        )

    slope = numbers["SMA150_SLOPE_20D"]
    if slope > tolerance and not signals["SLOPE_POSITIVE"]:
        raise ConfigError(
            f"{signal_label}.SLOPE_POSITIVE contradicts SMA150_SLOPE_20D"
        )
    if slope < -tolerance and not signals["SLOPE_NEGATIVE"]:
        raise ConfigError(
            f"{signal_label}.SLOPE_NEGATIVE contradicts SMA150_SLOPE_20D"
        )
    if signals["SLOPE_POSITIVE"] and slope < -tolerance:
        raise ConfigError(
            f"{signal_label}.SLOPE_POSITIVE contradicts SMA150_SLOPE_20D"
        )
    if signals["SLOPE_NEGATIVE"] and slope > tolerance:
        raise ConfigError(
            f"{signal_label}.SLOPE_NEGATIVE contradicts SMA150_SLOPE_20D"
        )

    if signals["TWO_DAY_ABOVE"] and count < 2:
        raise ConfigError(
            f"{signal_label}.TWO_DAY_ABOVE requires at least two confirming closes"
        )
    if signals["TWO_DAY_BELOW"] and count > 0:
        raise ConfigError(
            f"{signal_label}.TWO_DAY_BELOW requires zero confirming closes"
        )

    audit = _validate_override_signal_audit(
        signals,
        signal_label,
        analyzed_at=analyzed_at,
        require_all=True,
    )
    if audit is None:
        raise ConfigError(f"{signal_label} override audit fields are required")
    normal_regime = audit["normal_regime"]
    downtrend_confirmed = bool(
        signals["TWO_DAY_BELOW"]
        and signals["WEEKLY_BELOW"]
        and signals["SLOPE_NEGATIVE"]
    )
    expected_normal_regime = (
        "UPTREND"
        if confirmed
        else "DOWNTREND"
        if downtrend_confirmed
        else "SIDEWAYS"
    )
    if normal_regime != expected_normal_regime:
        raise ConfigError(
            f"{signal_label}.REGIME_WITHOUT_OVERRIDE does not match classifier signals"
        )

    active = audit["active"]
    expected_regime = "UPTREND" if active else normal_regime
    if decision["REGIME"] != expected_regime:
        raise ConfigError(
            f"{label}.REGIME does not match classifier and override signals"
        )
    expected_applied = active and normal_regime != "UPTREND"
    if audit["applied"] is not expected_applied:
        raise ConfigError(
            f"{signal_label}.UPTREND_OVERRIDE_APPLIED does not match override state"
        )
    if active and confirmed:
        raise ConfigError(
            f"{signal_label} confirmed UPTREND override must be auto-released"
        )
    if audit["auto_released"]:
        if not confirmed or normal_regime != "UPTREND":
            raise ConfigError(
                f"{signal_label} auto-release requires a naturally confirmed UPTREND"
            )
        if audit["released_at"] != analyzed_at:
            raise ConfigError(
                f"{signal_label}.UPTREND_OVERRIDE_RELEASED_AT must match ANALYZED_AT "
                "when auto-released"
            )


def validate_analysis_decision(target: str, value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one deterministic per-target v3 analysis result."""

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    if not isinstance(value, Mapping):
        raise ConfigError(f"DCA_ANALYSIS_STATE.TARGETS.{target} must be an object")
    decision = dict(value)
    label = f"DCA_ANALYSIS_STATE.TARGETS.{target}"
    _unexpected_fields(decision, ANALYSIS_DECISION_FIELDS, label)
    if type(decision["ENABLED"]) is not bool:
        raise ConfigError(f"{label}.ENABLED must be a boolean")
    analysis_status = decision["ANALYSIS_STATUS"]
    if analysis_status not in ANALYSIS_STATUSES:
        raise ConfigError(f"{label}.ANALYSIS_STATUS is invalid")
    if decision["EXECUTION_STATUS"] not in EXECUTION_STATUSES:
        raise ConfigError(f"{label}.EXECUTION_STATUS is invalid")
    if decision["POLICY_VERSION"] != TIMING_POLICY_VERSION:
        raise ConfigError(f"{label}.POLICY_VERSION must be {TIMING_POLICY_VERSION}")
    analysis_date = decision["ANALYSIS_DATE"]
    if not isinstance(analysis_date, str):
        raise ConfigError(f"{label}.ANALYSIS_DATE must be YYYY-MM-DD")
    try:
        date.fromisoformat(analysis_date)
    except ValueError as exc:
        raise ConfigError(f"{label}.ANALYSIS_DATE must be YYYY-MM-DD") from exc
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
    if not isinstance(decision["HISTORY"], Mapping):
        raise ConfigError(f"{label}.HISTORY must be an object")
    timing = dict(decision["TIMING"])
    if "ANALYZED_AT" not in timing:
        raise ConfigError(f"{label}.TIMING.ANALYZED_AT is required")
    analyzed_at = parse_iso_datetime(
        timing["ANALYZED_AT"], f"{label}.TIMING.ANALYZED_AT"
    )

    if analysis_status == READY_STATUS:
        if decision["REGIME"] not in REGIMES:
            raise ConfigError(f"{label}.REGIME is invalid")
        if decision["AMOUNT_TIER"] not in AMOUNT_TIERS:
            raise ConfigError(f"{label}.AMOUNT_TIER must be LOW, MID, or HIGH")
        expected_tier = amount_tier_for_regime(decision["REGIME"])
        if decision["AMOUNT_TIER"] != expected_tier:
            raise ConfigError(f"{label}.AMOUNT_TIER does not match REGIME")
        selected_at = parse_iso_datetime(decision["SELECTED_AT"], f"{label}.SELECTED_AT")
        execute_at = parse_iso_datetime(decision["EXECUTE_AT"], f"{label}.EXECUTE_AT")
        valid_until = parse_iso_datetime(decision["VALID_UNTIL"], f"{label}.VALID_UNTIL")
        if valid_until != execute_at + timedelta(minutes=60):
            raise ConfigError(f"{label}.VALID_UNTIL must be exactly 60 minutes after EXECUTE_AT")
        if type(decision["CATCHUP_APPLIED"]) is not bool:
            raise ConfigError(f"{label}.CATCHUP_APPLIED must be a boolean")
        if decision["CATCHUP_APPLIED"] and selected_at > analyzed_at:
            raise ConfigError(f"{label}.CATCHUP_APPLIED requires an already-missed selected time")
        if not decision["CATCHUP_APPLIED"] and execute_at != selected_at:
            raise ConfigError(f"{label}.EXECUTE_AT must match SELECTED_AT without catch-up")
        history = dict(decision["HISTORY"])
        if history.get("STATUS") != "READY":
            raise ConfigError(f"{label}.HISTORY must be READY")
        history_hash = history.get("HASH")
        if not isinstance(history_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", history_hash):
            raise ConfigError(f"{label}.HISTORY.HASH must be a lowercase SHA-256 hash")
        if decision["ERROR"] is not None:
            raise ConfigError(f"{label}.ERROR must be null for READY analysis")
        _validate_ready_analysis_signals(decision, analyzed_at, label)
    else:
        if decision["REGIME"] is not None or decision["AMOUNT_TIER"] is not None:
            raise ConfigError(f"{label} non-ready decisions cannot select a regime or amount")
        if any(decision[field] is not None for field in ("SELECTED_AT", "EXECUTE_AT", "VALID_UNTIL")):
            raise ConfigError(f"{label} non-ready decisions cannot select an execution time")
        if decision["CATCHUP_APPLIED"] is not False:
            raise ConfigError(f"{label}.CATCHUP_APPLIED must be false when not ready")
        if not isinstance(decision["ERROR"], str) or not decision["ERROR"].strip():
            raise ConfigError(f"{label}.ERROR must describe why analysis is not ready")
        _validate_override_signal_audit(
            decision["SIGNALS"],
            f"{label}.SIGNALS",
            analyzed_at=analyzed_at,
            require_all=False,
        )

    return {
        **decision,
        "HISTORY": dict(decision["HISTORY"]),
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
    if state["POLICY_VERSION"] != TIMING_POLICY_VERSION:
        raise ConfigError(
            f"DCA_ANALYSIS_STATE.POLICY_VERSION must be {TIMING_POLICY_VERSION}"
        )
    analysis_date = state["ANALYSIS_DATE"]
    if not isinstance(analysis_date, str):
        raise ConfigError("DCA_ANALYSIS_STATE.ANALYSIS_DATE must be YYYY-MM-DD")
    try:
        date.fromisoformat(analysis_date)
    except ValueError as exc:
        raise ConfigError("DCA_ANALYSIS_STATE.ANALYSIS_DATE must be YYYY-MM-DD") from exc
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
    for target, decision in normalized_targets.items():
        if decision["ANALYSIS_DATE"] != analysis_date:
            raise ConfigError(
                f"DCA_ANALYSIS_STATE.TARGETS.{target}.ANALYSIS_DATE must match "
                "DCA_ANALYSIS_STATE.ANALYSIS_DATE"
            )
    if rules_map is not None:
        normalized_rules = validate_rules_map(rules_map, require_all=require_all)
        for target, decision in normalized_targets.items():
            if decision["ANALYSIS_STATUS"] == READY_STATUS:
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
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": analysis_date,
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
    if normalized["ANALYSIS_STATUS"] != READY_STATUS:
        return False, f"analysis status is {normalized['ANALYSIS_STATUS']}"
    if normalized["RULES_HASH"] != expected_rules_hash:
        return False, "analysis rules hash does not match live budgets"
    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    now_utc = now.astimezone(timezone.utc)
    if normalized["EXECUTION_STATUS"] in {"DISABLED", "BLOCKED", "EXPIRED"}:
        return False, f"execution status is {normalized['EXECUTION_STATUS']}"
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


def awaiting_daily_analysis_refresh(
    analysis: Mapping[str, Any],
    now: datetime,
    selected_tz,
    *,
    expected_by: time = DAILY_ANALYSIS_EXPECTED_BY,
) -> bool:
    """Return whether a healthy prior-day state is awaiting today's analysis.

    New orders must remain blocked at local midnight because yesterday's
    decisions cannot be reused. Until the bounded 04:20 recovery threshold,
    however, one complete prior-day state is the normal input to the scheduled
    04:07 analysis rather than an operational failure.
    """

    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("daily analysis refresh check requires an aware timestamp")
    if not isinstance(expected_by, time) or expected_by.tzinfo is not None:
        raise ConfigError("daily analysis expected-by time must be timezone-naive")
    local_now = now.astimezone(selected_tz)
    if local_now.time() >= expected_by:
        return False
    prior_date = (local_now.date() - timedelta(days=1)).isoformat()
    if (
        not isinstance(analysis, Mapping)
        or analysis.get("POLICY_VERSION") != TIMING_POLICY_VERSION
        or analysis.get("ANALYSIS_DATE") != prior_date
    ):
        return False
    targets = analysis.get("TARGETS")
    if not isinstance(targets, Mapping):
        return False
    for target in TARGET_KEYS:
        decision = targets.get(target)
        if not isinstance(decision, Mapping):
            return False
        history = decision.get("HISTORY")
        if (
            decision.get("POLICY_VERSION") != TIMING_POLICY_VERSION
            or decision.get("ANALYSIS_DATE") != prior_date
            or decision.get("ANALYSIS_STATUS") != READY_STATUS
            or not isinstance(history, Mapping)
            or history.get("STATUS") != READY_STATUS
        ):
            return False
    return True


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


def _execution_state_json_bytes(value: Mapping[str, Any]) -> int:
    return len(
        json.dumps(value, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def validate_execution_state(value: str | Mapping[str, Any]) -> dict[str, Any]:
    """Validate durable buy dates, order intents, and Gist deliveries.

    Pending intents must carry the analysis ``DECISION_ID`` that originated the
    order, allowing recovery to reject an intent from an obsolete decision.
    Pending Gist deliveries retain list order as their FIFO delivery order.
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
        extra = set(entry) - {
            "LAST_BUY_DATE",
            "PENDING_ORDER",
            "PENDING_GIST_DELIVERIES",
        }
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
        raw_deliveries = entry.get("PENDING_GIST_DELIVERIES", [])
        if not isinstance(raw_deliveries, list):
            raise ConfigError(
                f"DCA_EXECUTION_STATE.{target}.PENDING_GIST_DELIVERIES must be an array"
            )
        if len(raw_deliveries) > MAX_PENDING_GIST_DELIVERIES:
            raise ConfigError(
                f"DCA_EXECUTION_STATE.{target}.PENDING_GIST_DELIVERIES must contain at most "
                f"{MAX_PENDING_GIST_DELIVERIES} deliveries"
            )
        deliveries: list[dict[str, Any]] = []
        delivery_ids: set[str] = set()
        for raw_delivery in raw_deliveries:
            delivery = validate_gist_delivery(raw_delivery, target)
            delivery_id = delivery["delivery_id"]
            if delivery_id in delivery_ids:
                raise ConfigError(
                    f"DCA_EXECUTION_STATE.{target}.PENDING_GIST_DELIVERIES contains "
                    f"duplicate delivery_id: {delivery_id}"
                )
            delivery_ids.add(delivery_id)
            deliveries.append(delivery)
        result_entry: dict[str, Any] = {"LAST_BUY_DATE": last_buy}
        if pending is not None:
            result_entry["PENDING_ORDER"] = pending
        if deliveries:
            result_entry["PENDING_GIST_DELIVERIES"] = deliveries
        normalized[target] = result_entry
    if _execution_state_json_bytes(normalized) > MAX_EXECUTION_STATE_JSON_BYTES:
        raise ConfigError(
            "DCA_EXECUTION_STATE exceeds the protected GitHub variable size budget"
        )
    return normalized


def ensure_gist_delivery_capacity(
    value: str | Mapping[str, Any], target: str
) -> dict[str, Any]:
    """Reserve worst-case durable fill evidence before a new Kraken order.

    Every already-persisted pending order also retains a reservation.  Callers
    reconciling an old intent do not need to invoke this gate: reconciliation
    must remain possible even if unrelated state later consumes the headroom.
    """

    if target not in TARGET_KEYS:
        raise ConfigError(f"Unsupported production target: {target}")
    state = validate_execution_state(value)
    reservation_targets = {
        key
        for key, entry in state.items()
        if entry.get("PENDING_ORDER") is not None
    }
    reservation_targets.add(target)
    for reserved_target in reservation_targets:
        deliveries = state.get(reserved_target, {}).get(
            "PENDING_GIST_DELIVERIES", []
        )
        if len(deliveries) >= MAX_PENDING_GIST_DELIVERIES:
            raise ConfigError(
                "No durable Portfolio Compass delivery slot is available for "
                f"{reserved_target}"
            )
    projected_bytes = _execution_state_json_bytes(state) + (
        len(reservation_targets) * GIST_DELIVERY_RESERVED_JSON_BYTES
    )
    if projected_bytes > MAX_EXECUTION_STATE_JSON_BYTES:
        raise ConfigError(
            "DCA_EXECUTION_STATE lacks reserved space for durable Portfolio "
            "Compass delivery evidence"
        )
    return state


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
    """Return a complete fail-closed v3 state awaiting deterministic analysis."""

    rules = validate_rules_map(rules_map or default_rules_map())
    generated = now or datetime.now(timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ConfigError("now must include a timezone")
    generated_text = generated.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    selected_tz = timezone(timedelta(hours=7))
    analysis_date = generated.astimezone(selected_tz).date().isoformat()
    targets = {}
    for target in TARGET_KEYS:
        digest = rules_hash(target, rules[target])
        targets[target] = {
            "ENABLED": bool(rules[target]["BUY_ENABLED"]),
            "ANALYSIS_STATUS": "AWAITING_ANALYSIS",
            "EXECUTION_STATUS": "DISABLED" if not rules[target]["BUY_ENABLED"] else "BLOCKED",
            "REGIME": None,
            "AMOUNT_TIER": None,
            "SELECTED_AT": None,
            "EXECUTE_AT": None,
            "VALID_UNTIL": None,
            "CATCHUP_APPLIED": False,
            "DECISION_ID": f"bootstrap-{target.lower()}-{digest[:12]}",
            "RULES_HASH": digest,
            "POLICY_VERSION": TIMING_POLICY_VERSION,
            "ANALYSIS_DATE": analysis_date,
            "HISTORY": {"STATUS": "HISTORY_NOT_READY"},
            "SIGNALS": {"ERROR": reason},
            "TIMING": {"ANALYZED_AT": generated_text, "ERROR": reason},
            "ERROR": reason,
        }
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_text,
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": analysis_date,
        "TARGETS": targets,
    }


__all__ = [
    "ALLOWED_TARGETS",
    "AMOUNT_POLICY_VERSION",
    "AMOUNT_TIERS",
    "ANALYSIS_READY_SIGNAL_FIELDS",
    "ANALYSIS_STATE_VERSION",
    "ANALYSIS_STATUSES",
    "ConfigError",
    "DAILY_ANALYSIS_EXPECTED_BY",
    "ERROR_STATUS",
    "EXECUTION_STATUSES",
    "GIST_DELIVERY_FIELDS",
    "GIST_DELIVERY_RESERVED_JSON_BYTES",
    "GIST_DELIVERY_VERSION",
    "MAX_AMOUNT_GBP",
    "MAX_EXECUTION_STATE_JSON_BYTES",
    "MAX_GIST_DELIVERY_ROW_BYTES",
    "MAX_PENDING_GIST_DELIVERIES",
    "MAX_UPTREND_OVERRIDE_REASON_CHARS",
    "MIN_ENABLED_AMOUNT_GBP",
    "PORTFOLIO_EVENT_FIELDS",
    "READY_STATUS",
    "REGIMES",
    "TARGET_KEYS",
    "TARGET_SYMBOLS",
    "TIMING_POLICY_VERSION",
    "UPTREND_OVERRIDE_ENTRY_FIELDS",
    "UPTREND_CONFIRMATION_CANDLES",
    "UPTREND_OVERRIDE_AUDIT_SIGNAL_FIELDS",
    "UPTREND_OVERRIDE_STATE_FIELDS",
    "UPTREND_OVERRIDE_STATE_VERSION",
    "analysis_decision_matches_uptrend_override",
    "amount_for_tier_gbp",
    "amount_tier_for_regime",
    "awaiting_daily_analysis_refresh",
    "decision_is_usable",
    "decision_age_minutes",
    "decision_analyzed_on_or_after",
    "default_rules_map",
    "effective_amount",
    "effective_amount_gbp",
    "ensure_gist_delivery_capacity",
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
    "validate_gist_delivery",
    "validate_enabled_market_minimums",
    "validate_rules_map",
    "validate_target_map",
    "validate_uptrend_override_state",
]

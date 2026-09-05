"""Guarded one-time migration from HYPE/USD to native ETH/GBP DCA.

The migration is deliberately offline: it validates complete GitHub variable
values and produces one fail-closed replacement document without performing
network writes.  The workflow that invokes it separately proves that trading
is paused and that Kraken has no unresolved bot orders.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from dca_config import (
    AMOUNT_POLICY_VERSION,
    ANALYSIS_STATE_VERSION,
    TIMING_POLICY_VERSION,
    empty_analysis_state,
    parse_iso_datetime,
    validate_analysis_decision,
    validate_analysis_state,
    validate_execution_state,
    validate_rules_map,
)


SOURCE_TARGETS = ("BTC_GBP", "HYPE_USD", "SOL_GBP")
TARGET_MAP = {
    "BTC_GBP": "BTC_GBP",
    "HYPE_USD": "ETH_GBP",
    "SOL_GBP": "SOL_GBP",
}
RETIRED_STATE_VERSION = 2
MIGRATION_ID = "HYPE_USD_TO_ETH_GBP"
STATE_VARIABLES = (
    "DCA_TARGET_MAP",
    "DCA_ANALYSIS_STATE",
    "DCA_EXECUTION_STATE",
)


def _object(raw: str | Mapping[str, Any], label: str) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        value = dict(raw)
    else:
        try:
            value = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


def _source_rules_hash(target: str, rule: Mapping[str, Any]) -> str:
    payload = {
        "TARGET": target,
        "AMOUNT_POLICY_VERSION": AMOUNT_POLICY_VERSION,
        "REGIME_AMOUNTS_GBP": rule["REGIME_AMOUNTS_GBP"],
    }
    return _canonical_hash(payload)


def _source_execution_entry(value: Any, target: str) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError(f"source execution state {target} must be an object")
    entry = dict(value)
    extra = set(entry) - {
        "LAST_BUY_DATE",
        "PENDING_ORDER",
        "PENDING_GIST_DELIVERIES",
    }
    if extra:
        raise ValueError(
            f"source execution state {target} contains unsupported fields: "
            + ", ".join(sorted(extra))
        )
    if entry.get("PENDING_ORDER") is not None:
        raise ValueError(
            f"{target} has a pending Kraken intent; reconcile before migration"
        )
    if entry.get("PENDING_GIST_DELIVERIES"):
        raise ValueError(
            f"{target} has pending ledger delivery; deliver before migration"
        )
    last_buy = entry.get("LAST_BUY_DATE", "")
    if not isinstance(last_buy, str):
        raise ValueError(f"source execution state {target} has an invalid buy date")
    if last_buy:
        try:
            if date.fromisoformat(last_buy).isoformat() != last_buy:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                f"source execution state {target} has an invalid buy date"
            ) from exc
    return {"LAST_BUY_DATE": last_buy}


def _source_analysis_state(
    value: str | Mapping[str, Any],
    source_rules: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    state = _object(value, "DCA_ANALYSIS_STATE")
    expected_fields = {
        "VERSION",
        "GENERATED_AT",
        "POLICY_VERSION",
        "ANALYSIS_DATE",
        "TARGETS",
    }
    if set(state) != expected_fields:
        raise ValueError("source analysis state does not match the v3 schema")
    if state["VERSION"] != ANALYSIS_STATE_VERSION:
        raise ValueError(
            f"source analysis state VERSION must be {ANALYSIS_STATE_VERSION}"
        )
    generated_at = parse_iso_datetime(
        state["GENERATED_AT"], "DCA_ANALYSIS_STATE.GENERATED_AT"
    )
    if state["POLICY_VERSION"] != TIMING_POLICY_VERSION:
        raise ValueError(
            f"source analysis state POLICY_VERSION must be {TIMING_POLICY_VERSION}"
        )
    analysis_date = state["ANALYSIS_DATE"]
    if not isinstance(analysis_date, str):
        raise ValueError("source analysis date must be YYYY-MM-DD")
    try:
        if date.fromisoformat(analysis_date).isoformat() != analysis_date:
            raise ValueError
    except ValueError as exc:
        raise ValueError("source analysis date must be YYYY-MM-DD") from exc
    targets = state["TARGETS"]
    if not isinstance(targets, Mapping) or set(targets) != set(SOURCE_TARGETS):
        raise ValueError(
            "source analysis must contain exactly BTC_GBP, HYPE_USD, SOL_GBP"
        )

    normalized_targets = {}
    for target in SOURCE_TARGETS:
        source_decision = targets[target]
        # Verify the original target binding before shared-schema validation;
        # never relabel retired HYPE history as a current ETH/DOGE fixture.
        if (
            isinstance(source_decision, Mapping)
            and source_decision.get("ANALYSIS_STATUS") == "READY"
            and source_decision.get("RULES_HASH")
            != _source_rules_hash(target, source_rules[target])
        ):
            raise ValueError(
                f"source analysis rules hash does not match {target} budgets"
            )
        validation_target = "ETH_GBP" if target == "HYPE_USD" else target
        decision = validate_analysis_decision(validation_target, source_decision)
        if decision["ANALYSIS_DATE"] != analysis_date:
            raise ValueError(
                f"source analysis decision date does not match for {target}"
            )
        normalized_targets[target] = decision
    return {
        "VERSION": ANALYSIS_STATE_VERSION,
        "GENERATED_AT": generated_at.isoformat().replace("+00:00", "Z"),
        "POLICY_VERSION": TIMING_POLICY_VERSION,
        "ANALYSIS_DATE": analysis_date,
        "TARGETS": normalized_targets,
    }


def _migration_time(value: datetime | None) -> datetime:
    generated = value or datetime.now(timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("migration time must include a timezone")
    return generated.astimezone(timezone.utc)


def _new_rules(source_rules: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate = {
        TARGET_MAP[source]: dict(source_rules[source]) for source in SOURCE_TARGETS
    }
    candidate["ETH_GBP"] = {**candidate["ETH_GBP"], "BUY_ENABLED": False}
    # This historical migration's target contract predates DOGE/GBP. Keep its
    # archived three-target replay independent from the current production set.
    return validate_rules_map(candidate, require_all=False)


def _source_execution_state(value: str | Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "DCA_EXECUTION_STATE")
    unsupported = set(source) - set(SOURCE_TARGETS)
    if unsupported:
        raise ValueError(
            "source execution state contains unsupported targets: "
            + ", ".join(sorted(unsupported))
        )
    return {
        target: _source_execution_entry(source.get(target, {}), target)
        for target in SOURCE_TARGETS
    }


def _new_execution(source: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    # HYPE and ETH occupy the same daily allocation slot across the replacement.
    # Carrying the validated date prevents a second allocation on the cutover day.
    return validate_execution_state(
        {
            "BTC_GBP": source["BTC_GBP"],
            "ETH_GBP": {
                "LAST_BUY_DATE": source["HYPE_USD"]["LAST_BUY_DATE"]
            },
            "SOL_GBP": source["SOL_GBP"],
        }
    )


def _new_analysis(
    rules: Mapping[str, Mapping[str, Any]], generated: datetime
) -> dict[str, Any]:
    return empty_analysis_state(
        rules,
        now=generated,
        require_all=False,
        reason=(
            "HYPE/USD retired and ETH/GBP added; fresh verified ETH/GBP history "
            "analysis is required"
        ),
    )


def _validate_audit_evidence(
    value: str | Mapping[str, Any],
    source_execution: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    evidence = _object(value, "migration audit evidence")
    if set(evidence) != {"audit_date", "hype_completed_flow_dates"}:
        raise ValueError("migration audit evidence schema is invalid")
    audit_date = evidence["audit_date"]
    try:
        parsed_audit_date = date.fromisoformat(audit_date)
    except (TypeError, ValueError):
        raise ValueError("migration audit date is invalid") from None
    if parsed_audit_date.isoformat() != audit_date:
        raise ValueError("migration audit date is invalid")
    flow_dates = evidence["hype_completed_flow_dates"]
    if (
        not isinstance(flow_dates, list)
        or any(not isinstance(item, str) for item in flow_dates)
        or len(set(flow_dates)) != len(flow_dates)
    ):
        raise ValueError("migration audit HYPE flow dates are invalid")
    for item in flow_dates:
        try:
            parsed_flow_date = date.fromisoformat(item)
        except ValueError:
            raise ValueError("migration audit HYPE flow date is invalid") from None
        if (
            parsed_flow_date.isoformat() != item
            or parsed_flow_date not in {parsed_audit_date, parsed_audit_date - timedelta(days=1)}
        ):
            raise ValueError("migration audit HYPE flow date is invalid")
    if len(flow_dates) > 1:
        raise ValueError("migration audit contains multiple same-day HYPE flows")
    hype_last_buy = source_execution["HYPE_USD"]["LAST_BUY_DATE"]
    if hype_last_buy and date.fromisoformat(hype_last_buy) > parsed_audit_date:
        raise ValueError("HYPE last buy date is later than the migration audit")
    if flow_dates and hype_last_buy != flow_dates[0]:
        raise ValueError(
            "HYPE last buy date does not match authenticated Kraken flow"
        )
    if hype_last_buy == audit_date and flow_dates != [audit_date]:
        raise ValueError(
            "HYPE last buy date lacks matching authenticated Kraken flow"
        )
    return {
        "audit_date": audit_date,
        "hype_completed_flow_dates": list(flow_dates),
    }


def _state_hashes(
    rules: Mapping[str, Any],
    analysis: Mapping[str, Any],
    execution: Mapping[str, Any],
) -> dict[str, str]:
    return {
        "DCA_TARGET_MAP": _canonical_hash(rules),
        "DCA_ANALYSIS_STATE": _canonical_hash(analysis),
        "DCA_EXECUTION_STATE": _canonical_hash(execution),
    }


def _retired_state(
    source_rules: Mapping[str, Mapping[str, Any]],
    source_analysis: Mapping[str, Any],
    source_execution: Mapping[str, Mapping[str, Any]],
    source_hashes: Mapping[str, str],
    target_hashes: Mapping[str, str],
    generated: datetime,
) -> dict[str, Any]:
    generated_text = generated.isoformat().replace("+00:00", "Z")
    payload = {
        "VERSION": RETIRED_STATE_VERSION,
        "MIGRATION": MIGRATION_ID,
        "RETIRED_AT": generated_text,
        "TARGET": "HYPE_USD",
        "REPLACED_BY": "ETH_GBP",
        "RULE": source_rules["HYPE_USD"],
        "ANALYSIS_STATE": {
            field: source_analysis[field]
            for field in (
                "VERSION",
                "GENERATED_AT",
                "POLICY_VERSION",
                "ANALYSIS_DATE",
            )
        },
        "ANALYSIS": source_analysis["TARGETS"]["HYPE_USD"],
        "EXECUTION": source_execution["HYPE_USD"],
        "SOURCE_STATE_HASHES": dict(source_hashes),
        "TARGET_STATE_HASHES": dict(target_hashes),
    }
    return {**payload, "CANONICAL_HASH": _canonical_hash(payload)}


def _hash_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(STATE_VARIABLES):
        raise ValueError(f"retired archive {label} is invalid")
    result = dict(value)
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in result.values()
    ):
        raise ValueError(f"retired archive {label} is invalid")
    return result


def _validate_retired_state(value: str | Mapping[str, Any]) -> dict[str, Any]:
    archive = _object(value, "DCA_RETIRED_TARGET_STATE")
    expected = {
        "VERSION",
        "MIGRATION",
        "RETIRED_AT",
        "TARGET",
        "REPLACED_BY",
        "RULE",
        "ANALYSIS_STATE",
        "ANALYSIS",
        "EXECUTION",
        "SOURCE_STATE_HASHES",
        "TARGET_STATE_HASHES",
        "CANONICAL_HASH",
    }
    if set(archive) != expected:
        raise ValueError("retired archive schema is invalid")
    supplied_hash = archive["CANONICAL_HASH"]
    payload = {key: value for key, value in archive.items() if key != "CANONICAL_HASH"}
    if supplied_hash != _canonical_hash(payload):
        raise ValueError("retired archive canonical hash is invalid")
    if (
        archive["VERSION"] != RETIRED_STATE_VERSION
        or archive["MIGRATION"] != MIGRATION_ID
        or archive["TARGET"] != "HYPE_USD"
        or archive["REPLACED_BY"] != "ETH_GBP"
    ):
        raise ValueError("retired archive identity is invalid")
    retired_at = parse_iso_datetime(
        archive["RETIRED_AT"], "DCA_RETIRED_TARGET_STATE.RETIRED_AT"
    )
    rule = validate_rules_map(
        {"ETH_GBP": archive["RULE"]}, require_all=False
    )["ETH_GBP"]
    if rule["BUY_ENABLED"] is not False:
        raise ValueError("retired HYPE rule must be disabled")
    analysis_metadata = archive["ANALYSIS_STATE"]
    if not isinstance(analysis_metadata, Mapping) or set(analysis_metadata) != {
        "VERSION",
        "GENERATED_AT",
        "POLICY_VERSION",
        "ANALYSIS_DATE",
    }:
        raise ValueError("retired archive analysis metadata is invalid")
    parse_iso_datetime(
        analysis_metadata["GENERATED_AT"],
        "DCA_RETIRED_TARGET_STATE.ANALYSIS_STATE.GENERATED_AT",
    )
    if (
        analysis_metadata["VERSION"] != ANALYSIS_STATE_VERSION
        or analysis_metadata["POLICY_VERSION"] != TIMING_POLICY_VERSION
    ):
        raise ValueError("retired archive analysis metadata is invalid")
    decision = validate_analysis_decision("ETH_GBP", archive["ANALYSIS"])
    if decision["ANALYSIS_DATE"] != analysis_metadata["ANALYSIS_DATE"]:
        raise ValueError("retired archive analysis date is inconsistent")
    if (
        decision["ANALYSIS_STATUS"] == "READY"
        and decision["RULES_HASH"] != _source_rules_hash("HYPE_USD", rule)
    ):
        raise ValueError("retired archive analysis rules hash is invalid")
    execution = _source_execution_entry(archive["EXECUTION"], "HYPE_USD")
    source_hashes = _hash_map(
        archive["SOURCE_STATE_HASHES"], "SOURCE_STATE_HASHES"
    )
    target_hashes = _hash_map(
        archive["TARGET_STATE_HASHES"], "TARGET_STATE_HASHES"
    )
    return {
        **archive,
        "RETIRED_AT": retired_at.isoformat().replace("+00:00", "Z"),
        "RULE": rule,
        "ANALYSIS_STATE": dict(analysis_metadata),
        "ANALYSIS": decision,
        "EXECUTION": execution,
        "SOURCE_STATE_HASHES": source_hashes,
        "TARGET_STATE_HASHES": target_hashes,
    }


def migrate(
    rules_raw: str | Mapping[str, Any],
    analysis_raw: str | Mapping[str, Any],
    execution_raw: str | Mapping[str, Any],
    retired_raw: str | Mapping[str, Any] | None = None,
    *,
    audit_raw: str | Mapping[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a validated, resumable HYPE-to-ETH cutover document.

    ETH inherits HYPE's two GBP budget endpoints but starts disabled. HYPE's
    validated buy date is carried to ETH solely to preserve the once-per-day
    allocation guard; BTC and SOL buy dates are retained. HYPE's final state is
    also preserved
    in a hash-bound retired-target archive, while unresolved order or delivery
    evidence blocks the migration instead of being discarded.  With an existing
    archive, each core variable may be either its exact source value or exact
    target value, but only in analysis -> execution -> rules write order.
    """

    current_rules = _object(rules_raw, "DCA_TARGET_MAP")
    current_analysis = _object(analysis_raw, "DCA_ANALYSIS_STATE")
    # An empty string is never an acceptable execution document. The caller
    # must prove that the repository variable was read successfully; an actual
    # persisted empty object remains a valid no-purchase state.
    current_execution = _object(execution_raw, "DCA_EXECUTION_STATE")
    current_hashes = _state_hashes(
        current_rules, current_analysis, current_execution
    )
    archive_present = not (
        retired_raw is None
        or isinstance(retired_raw, str)
        and not retired_raw.strip()
    )

    if not archive_present:
        source_rules = current_rules
        if set(source_rules) != set(SOURCE_TARGETS):
            raise ValueError(
                "source rules must contain exactly BTC_GBP, HYPE_USD, SOL_GBP"
            )
        if any(
            not isinstance(source_rules[target], Mapping)
            or source_rules[target].get("BUY_ENABLED") is not False
            for target in SOURCE_TARGETS
        ):
            raise ValueError("disable every DCA target before migration")
        new_rules = _new_rules(source_rules)
        normalized_source_rules = {
            "BTC_GBP": new_rules["BTC_GBP"],
            "HYPE_USD": new_rules["ETH_GBP"],
            "SOL_GBP": new_rules["SOL_GBP"],
        }
        source_analysis = _source_analysis_state(
            current_analysis, normalized_source_rules
        )
        source_execution = _source_execution_state(current_execution)
        if audit_raw is not None:
            _validate_audit_evidence(audit_raw, source_execution)
        new_execution = _new_execution(source_execution)
        generated = _migration_time(now)
        new_analysis = _new_analysis(new_rules, generated)
        source_hashes = _state_hashes(
            source_rules, current_analysis, current_execution
        )
        target_hashes = _state_hashes(new_rules, new_analysis, new_execution)
        retired_state = _retired_state(
            normalized_source_rules,
            source_analysis,
            source_execution,
            source_hashes,
            target_hashes,
            generated,
        )
        phase = "SOURCE"
    else:
        retired_state = _validate_retired_state(retired_raw)
        source_hashes = retired_state["SOURCE_STATE_HASHES"]
        target_hashes = retired_state["TARGET_STATE_HASHES"]
        statuses = {}
        for name in STATE_VARIABLES:
            digest = current_hashes[name]
            if digest == source_hashes[name]:
                statuses[name] = "SOURCE"
            elif digest == target_hashes[name]:
                statuses[name] = "TARGET"
            else:
                raise ValueError(
                    f"{name} matches neither the archived source nor target state"
                )
        phase_tuple = (
            statuses["DCA_ANALYSIS_STATE"],
            statuses["DCA_EXECUTION_STATE"],
            statuses["DCA_TARGET_MAP"],
        )
        phases = {
            ("SOURCE", "SOURCE", "SOURCE"): "ARCHIVED",
            ("TARGET", "SOURCE", "SOURCE"): "ANALYSIS_WRITTEN",
            ("TARGET", "TARGET", "SOURCE"): "EXECUTION_WRITTEN",
            ("TARGET", "TARGET", "TARGET"): "CORE_COMPLETE",
        }
        if phase_tuple not in phases:
            raise ValueError(
                "core migration state violates analysis -> execution -> rules order"
            )
        phase = phases[phase_tuple]

        if statuses["DCA_TARGET_MAP"] == "SOURCE":
            source_rules = current_rules
            if set(source_rules) != set(SOURCE_TARGETS):
                raise ValueError("archived source rules have invalid target membership")
            new_rules = _new_rules(source_rules)
        else:
            new_rules = validate_rules_map(current_rules, require_all=False)
        normalized_source_rules = {
            "BTC_GBP": new_rules["BTC_GBP"],
            "HYPE_USD": retired_state["RULE"],
            "SOL_GBP": new_rules["SOL_GBP"],
        }

        if statuses["DCA_EXECUTION_STATE"] == "SOURCE":
            source_execution = _source_execution_state(current_execution)
            new_execution = _new_execution(source_execution)
        else:
            new_execution = validate_execution_state(current_execution)

        generated = parse_iso_datetime(
            retired_state["RETIRED_AT"],
            "DCA_RETIRED_TARGET_STATE.RETIRED_AT",
        )
        if statuses["DCA_ANALYSIS_STATE"] == "SOURCE":
            _source_analysis_state(current_analysis, normalized_source_rules)
            new_analysis = _new_analysis(new_rules, generated)
        else:
            new_analysis = validate_analysis_state(
                current_analysis, new_rules, require_all=False
            )

        rebuilt_target_hashes = _state_hashes(
            new_rules, new_analysis, new_execution
        )
        if rebuilt_target_hashes != target_hashes:
            raise ValueError("retired archive target hashes do not match rebuilt state")

    return {
        "DCA_TARGET_MAP": new_rules,
        "DCA_EXECUTION_STATE": new_execution,
        "DCA_ANALYSIS_STATE": new_analysis,
        "DCA_RETIRED_TARGET_STATE": retired_state,
        "DCA_CANARY_SYMBOL": "SOL_GBP",
        "DCA_TRADING_MODE": "shadow",
        "_CURRENT_STATE_HASHES": current_hashes,
        "_ARCHIVE_PRESENT": archive_present,
        "_MIGRATION_PHASE": phase,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--audit", required=True)
    parser.add_argument("--retired", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = migrate(
        args.rules,
        args.analysis,
        args.execution,
        args.retired,
        audit_raw=args.audit,
    )
    args.output.write_text(
        json.dumps(result, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Guarded one-time migration that adds disabled DOGE/GBP to the DCA bot.

The module is deliberately offline.  It validates complete repository-variable
values and produces one fail-closed replacement document; the workflow that
invokes it owns authenticated reads, compare-and-swap writes, and the Kraken
order audit.
"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Mapping

from dca_config import (
    empty_analysis_state,
    parse_iso_datetime,
    validate_analysis_state,
    validate_execution_state,
    validate_rules_map,
)


SOURCE_TARGETS = ("BTC_GBP", "ETH_GBP", "SOL_GBP")
TARGET_TARGETS = (*SOURCE_TARGETS, "DOGE_GBP")
ADDED_TARGET = "DOGE_GBP"
MIGRATION_ID = "ADD_DOGE_GBP"
MIGRATION_STATE_VERSION = 1
MIGRATION_STATE_VARIABLE = "DCA_DOGE_GBP_MIGRATION_STATE"
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
        except (json.JSONDecodeError, TypeError) as exc:
            raise ValueError(f"{label} is not valid JSON") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _canonical(value: Mapping[str, Any]) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def _canonical_hash(value: Mapping[str, Any]) -> str:
    return sha256(_canonical(value).encode("utf-8")).hexdigest()


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


def _migration_time(value: datetime | None) -> datetime:
    generated = value or datetime.now(timezone.utc)
    if generated.tzinfo is None or generated.utcoffset() is None:
        raise ValueError("migration time must include a timezone")
    return generated.astimezone(timezone.utc)


def _source_rules(value: str | Mapping[str, Any]) -> dict[str, Any]:
    source = _object(value, "DCA_TARGET_MAP")
    if set(source) != set(SOURCE_TARGETS):
        raise ValueError(
            "source rules must contain exactly BTC_GBP, ETH_GBP, SOL_GBP"
        )
    if any(
        not isinstance(source[target], Mapping)
        or source[target].get("BUY_ENABLED") is not False
        for target in SOURCE_TARGETS
    ):
        raise ValueError("disable every source DCA target before migration")
    return validate_rules_map(source, require_all=False)


def _target_rules(source: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    candidate = {
        target: dict(source[target])
        for target in SOURCE_TARGETS
    }
    candidate[ADDED_TARGET] = {
        "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
        "BUY_ENABLED": False,
    }
    return _validate_target_rules(candidate)


def _validate_target_rules(value: str | Mapping[str, Any]) -> dict[str, Any]:
    target = validate_rules_map(value)
    if tuple(target) != TARGET_TARGETS:
        raise ValueError("target rules do not contain the canonical four targets")
    if any(target[name]["BUY_ENABLED"] is not False for name in TARGET_TARGETS):
        raise ValueError("every migrated DCA target must remain disabled")
    if target[ADDED_TARGET] != {
        "REGIME_AMOUNTS_GBP": {"LOW": 0, "MID": 0, "UP": 0},
        "BUY_ENABLED": False,
    }:
        raise ValueError("DOGE_GBP must start disabled with zero GBP budgets")
    return target


def _source_analysis(
    value: str | Mapping[str, Any],
    rules: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    source = _object(value, "DCA_ANALYSIS_STATE")
    targets = source.get("TARGETS")
    if not isinstance(targets, Mapping) or set(targets) != set(SOURCE_TARGETS):
        raise ValueError(
            "source analysis must contain exactly BTC_GBP, ETH_GBP, SOL_GBP"
        )
    return validate_analysis_state(source, rules, require_all=False)


def _last_buy_date(value: Any, target: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"source execution state {target} has an invalid buy date")
    if value:
        try:
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                f"source execution state {target} has an invalid buy date"
            ) from exc
    return value


def _source_execution(value: str | Mapping[str, Any]) -> dict[str, str]:
    source = _object(value, "DCA_EXECUTION_STATE")
    unsupported = set(source) - set(SOURCE_TARGETS)
    if unsupported:
        raise ValueError(
            "source execution state contains unsupported targets: "
            + ", ".join(sorted(unsupported))
        )
    for target, raw_entry in source.items():
        if isinstance(raw_entry, Mapping):
            if "LAST_BUY_DATE" in raw_entry:
                _last_buy_date(raw_entry["LAST_BUY_DATE"], target)
            if raw_entry.get("PENDING_ORDER") is not None:
                raise ValueError(
                    f"{target} has a pending Kraken intent; reconcile before migration"
                )
            if raw_entry.get("PENDING_GIST_DELIVERIES"):
                raise ValueError(
                    f"{target} has a pending delivery queue; deliver before migration"
                )
    normalized = validate_execution_state(source)
    last_buy_dates: dict[str, str] = {}
    for target in SOURCE_TARGETS:
        entry = normalized.get(target, {"LAST_BUY_DATE": ""})
        if entry.get("PENDING_ORDER") is not None:
            raise ValueError(
                f"{target} has a pending Kraken intent; reconcile before migration"
            )
        if entry.get("PENDING_GIST_DELIVERIES"):
            raise ValueError(
                f"{target} has a pending delivery queue; deliver before migration"
            )
        last_buy_dates[target] = _last_buy_date(
            entry.get("LAST_BUY_DATE", ""), target
        )
    return last_buy_dates


def _target_execution(last_buy_dates: Mapping[str, str]) -> dict[str, Any]:
    if set(last_buy_dates) != set(SOURCE_TARGETS):
        raise ValueError("archived source buy dates have invalid target membership")
    candidate = {
        target: {"LAST_BUY_DATE": _last_buy_date(last_buy_dates[target], target)}
        for target in SOURCE_TARGETS
    }
    candidate[ADDED_TARGET] = {"LAST_BUY_DATE": ""}
    return _validate_target_execution(candidate, last_buy_dates)


def _validate_target_execution(
    value: str | Mapping[str, Any],
    last_buy_dates: Mapping[str, str],
) -> dict[str, Any]:
    target = validate_execution_state(value)
    if set(target) != set(TARGET_TARGETS):
        raise ValueError("target execution state must contain all four targets")
    expected = {
        name: {"LAST_BUY_DATE": last_buy_dates[name]}
        for name in SOURCE_TARGETS
    }
    expected[ADDED_TARGET] = {"LAST_BUY_DATE": ""}
    if target != expected:
        raise ValueError(
            "target execution state does not preserve the archived buy dates"
        )
    return target


def _target_analysis(
    rules: Mapping[str, Mapping[str, Any]], generated: datetime
) -> dict[str, Any]:
    return empty_analysis_state(
        rules,
        now=generated,
        reason=(
            "DOGE/GBP added disabled with zero budgets; fresh complete four-target "
            "analysis is required"
        ),
    )


def _hash_map(value: Any, label: str) -> dict[str, str]:
    if not isinstance(value, Mapping) or set(value) != set(STATE_VARIABLES):
        raise ValueError(f"migration archive {label} is invalid")
    result = dict(value)
    if any(
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for digest in result.values()
    ):
        raise ValueError(f"migration archive {label} is invalid")
    return result


def _migration_archive(
    source_hashes: Mapping[str, str],
    target_hashes: Mapping[str, str],
    last_buy_dates: Mapping[str, str],
    generated: datetime,
) -> dict[str, Any]:
    payload = {
        "VERSION": MIGRATION_STATE_VERSION,
        "MIGRATION": MIGRATION_ID,
        "ARCHIVED_AT": generated.isoformat().replace("+00:00", "Z"),
        "SOURCE_TARGETS": list(SOURCE_TARGETS),
        "TARGET_TARGETS": list(TARGET_TARGETS),
        "ADDED_TARGET": ADDED_TARGET,
        "SOURCE_LAST_BUY_DATES": dict(last_buy_dates),
        "SOURCE_STATE_HASHES": dict(source_hashes),
        "TARGET_STATE_HASHES": dict(target_hashes),
    }
    return {**payload, "CANONICAL_HASH": _canonical_hash(payload)}


def _validate_migration_archive(
    value: str | Mapping[str, Any],
) -> dict[str, Any]:
    archive = _object(value, MIGRATION_STATE_VARIABLE)
    expected = {
        "VERSION",
        "MIGRATION",
        "ARCHIVED_AT",
        "SOURCE_TARGETS",
        "TARGET_TARGETS",
        "ADDED_TARGET",
        "SOURCE_LAST_BUY_DATES",
        "SOURCE_STATE_HASHES",
        "TARGET_STATE_HASHES",
        "CANONICAL_HASH",
    }
    if set(archive) != expected:
        raise ValueError("migration archive schema is invalid")
    supplied_hash = archive["CANONICAL_HASH"]
    payload = {
        key: item for key, item in archive.items() if key != "CANONICAL_HASH"
    }
    if supplied_hash != _canonical_hash(payload):
        raise ValueError("migration archive canonical hash is invalid")
    if (
        archive["VERSION"] != MIGRATION_STATE_VERSION
        or archive["MIGRATION"] != MIGRATION_ID
        or archive["SOURCE_TARGETS"] != list(SOURCE_TARGETS)
        or archive["TARGET_TARGETS"] != list(TARGET_TARGETS)
        or archive["ADDED_TARGET"] != ADDED_TARGET
    ):
        raise ValueError("migration archive identity is invalid")
    archived_at = parse_iso_datetime(
        archive["ARCHIVED_AT"], f"{MIGRATION_STATE_VARIABLE}.ARCHIVED_AT"
    )
    raw_dates = archive["SOURCE_LAST_BUY_DATES"]
    if not isinstance(raw_dates, Mapping) or set(raw_dates) != set(SOURCE_TARGETS):
        raise ValueError("migration archive source buy dates are invalid")
    last_buy_dates = {
        target: _last_buy_date(raw_dates[target], target)
        for target in SOURCE_TARGETS
    }
    return {
        **archive,
        "ARCHIVED_AT": archived_at.isoformat().replace("+00:00", "Z"),
        "SOURCE_LAST_BUY_DATES": last_buy_dates,
        "SOURCE_STATE_HASHES": _hash_map(
            archive["SOURCE_STATE_HASHES"], "SOURCE_STATE_HASHES"
        ),
        "TARGET_STATE_HASHES": _hash_map(
            archive["TARGET_STATE_HASHES"], "TARGET_STATE_HASHES"
        ),
    }


def migrate(
    rules_raw: str | Mapping[str, Any],
    analysis_raw: str | Mapping[str, Any],
    execution_raw: str | Mapping[str, Any],
    migration_state_raw: str | Mapping[str, Any] | None = None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return a validated, resumable three-to-four-target cutover document.

    Every source target must be disabled, and unresolved order intents or
    delivery queues block the migration.  Existing buy dates are retained;
    DOGE starts disabled with zero budgets and no buy date.  An existing
    hash-bound archive permits only the analysis -> execution -> rules phases.
    """

    current_rules = _object(rules_raw, "DCA_TARGET_MAP")
    current_analysis = _object(analysis_raw, "DCA_ANALYSIS_STATE")
    # The workflow must prove this variable was read.  A real persisted empty
    # object remains a valid no-purchase source state; an empty string does not.
    current_execution = _object(execution_raw, "DCA_EXECUTION_STATE")
    current_hashes = _state_hashes(
        current_rules, current_analysis, current_execution
    )
    archive_present = not (
        migration_state_raw is None
        or isinstance(migration_state_raw, str)
        and not migration_state_raw.strip()
    )

    if not archive_present:
        source_rules = _source_rules(current_rules)
        _source_analysis(current_analysis, source_rules)
        last_buy_dates = _source_execution(current_execution)
        target_rules = _target_rules(source_rules)
        target_execution = _target_execution(last_buy_dates)
        generated = _migration_time(now)
        target_analysis = _target_analysis(target_rules, generated)
        source_hashes = _state_hashes(
            current_rules, current_analysis, current_execution
        )
        target_hashes = _state_hashes(
            target_rules, target_analysis, target_execution
        )
        migration_state = _migration_archive(
            source_hashes,
            target_hashes,
            last_buy_dates,
            generated,
        )
        phase = "SOURCE"
    else:
        migration_state = _validate_migration_archive(migration_state_raw)
        source_hashes = migration_state["SOURCE_STATE_HASHES"]
        target_hashes = migration_state["TARGET_STATE_HASHES"]
        statuses: dict[str, str] = {}
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
            source_rules = _source_rules(current_rules)
            target_rules = _target_rules(source_rules)
        else:
            target_rules = _validate_target_rules(current_rules)

        last_buy_dates = migration_state["SOURCE_LAST_BUY_DATES"]
        expected_execution = _target_execution(last_buy_dates)
        if statuses["DCA_EXECUTION_STATE"] == "SOURCE":
            if _source_execution(current_execution) != last_buy_dates:
                raise ValueError(
                    "source execution dates do not match the migration archive"
                )
            target_execution = expected_execution
        else:
            target_execution = _validate_target_execution(
                current_execution, last_buy_dates
            )

        generated = parse_iso_datetime(
            migration_state["ARCHIVED_AT"],
            f"{MIGRATION_STATE_VARIABLE}.ARCHIVED_AT",
        )
        expected_analysis = _target_analysis(target_rules, generated)
        if statuses["DCA_ANALYSIS_STATE"] == "SOURCE":
            _source_analysis(current_analysis, source_rules)
            target_analysis = expected_analysis
        else:
            target_analysis = validate_analysis_state(
                current_analysis, target_rules
            )
            if target_analysis != expected_analysis:
                raise ValueError(
                    "target analysis state is not the archived complete reset"
                )

        rebuilt_target_hashes = _state_hashes(
            target_rules, target_analysis, target_execution
        )
        if rebuilt_target_hashes != target_hashes:
            raise ValueError(
                "migration archive target hashes do not match rebuilt state"
            )

    return {
        "DCA_TARGET_MAP": target_rules,
        "DCA_ANALYSIS_STATE": target_analysis,
        "DCA_EXECUTION_STATE": target_execution,
        MIGRATION_STATE_VARIABLE: migration_state,
        "DCA_TRADING_MODE": "shadow",
        "DCA_CANARY_SYMBOL": "SOL_GBP",
        "_CURRENT_STATE_HASHES": current_hashes,
        "_ARCHIVE_PRESENT": archive_present,
        "_MIGRATION_PHASE": phase,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rules", required=True)
    parser.add_argument("--analysis", required=True)
    parser.add_argument("--execution", required=True)
    parser.add_argument("--migration-state", default="")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    result = migrate(
        args.rules,
        args.analysis,
        args.execution,
        args.migration_state,
    )
    args.output.write_text(
        json.dumps(result, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

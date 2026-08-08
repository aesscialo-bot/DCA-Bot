"""Fail-closed execution of GBP-budgeted mixed spot purchases on Kraken."""

from __future__ import annotations

import json
import math
import os
import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

from dca_config import (
    ALLOWED_TARGETS,
    TARGET_ROUTES,
    ConfigError,
    MAX_PENDING_GIST_DELIVERIES,
    TIMING_POLICY_VERSION,
    decision_analyzed_on_or_after,
    decision_age_minutes,
    ensure_gist_delivery_capacity,
    effective_amount,
    is_execution_window,
    parse_utc_iso,
    rules_hash,
    validate_analysis_state,
    validate_execution_state,
    validate_gist_delivery,
    validate_rules_map,
)
from gist_logger import build_gist_delivery, update_gist_log
from kraken_client import (
    KrakenOrderNoFill,
    KrakenPreSubmissionError,
    KrakenOrderStateUnknown,
    build_client_order_id,
    place_market_buy,
    place_gbp_funded_market_buy,
)


TIMEZONE_NAME = os.environ.get("TIMEZONE", "Asia/Bangkok")
SELECTED_TZ = ZoneInfo(TIMEZONE_NAME)
MIN_DCA_GBP = 5.0
MAX_DCA_GBP = 1000.0
DCA_TARGET_MAP_JSON = os.environ.get("DCA_TARGET_MAP", "")
DCA_ANALYSIS_STATE_JSON = os.environ.get("DCA_ANALYSIS_STATE", "")
DCA_EXECUTION_STATE_JSON = os.environ.get("DCA_EXECUTION_STATE", "")
DCA_SYMBOLS_JSON = os.environ.get("DCA_SYMBOLS_JSON", "")
DCA_START_DATE = os.environ.get("DCA_START_DATE", "").strip()
DCA_TRADING_MODE = os.environ.get("DCA_TRADING_MODE", "shadow").strip().lower()
DCA_CANARY_SYMBOL = os.environ.get("DCA_CANARY_SYMBOL", "SOL_GBP").strip().upper()
GHOSTFOLIO_DIRECT_SYNC_ENABLED = (
    os.environ.get("GHOSTFOLIO_DIRECT_SYNC_ENABLED", "false").strip().lower()
    == "true"
)
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL")

RULES_VARIABLE = "DCA_TARGET_MAP"
ANALYSIS_STATE_VARIABLE = "DCA_ANALYSIS_STATE"
EXECUTION_STATE_VARIABLE = "DCA_EXECUTION_STATE"
PENDING_ORDER_FIELD = "PENDING_ORDER"
PENDING_GIST_DELIVERIES_FIELD = "PENDING_GIST_DELIVERIES"
_CLIENT_ORDER_ID_PATTERN = re.compile(r"^dca-[0-9a-f]{14}$")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _gha_mask(value: str) -> None:
    """Mask a value in GitHub Actions logs."""
    if os.environ.get("GITHUB_ACTIONS") == "true" and value:
        print(f"::add-mask::{value}", flush=True)


def _place_routed_market_buy(
    key,
    amount_gbp,
    *,
    client_order_id,
    funding_client_order_id,
    reconcile_only,
    pre_submit_check,
):
    """Route only HYPE/USD through GBP/USD; native pairs spend GBP directly."""
    if TARGET_ROUTES[key] == "DIRECT_GBP":
        return place_market_buy(
            key,
            amount_gbp,
            client_order_id=client_order_id,
            reconcile_only=reconcile_only,
            pre_submit_check=pre_submit_check,
        )
    return place_gbp_funded_market_buy(
        key,
        amount_gbp,
        client_order_id=client_order_id,
        funding_client_order_id=funding_client_order_id,
        reconcile_only=reconcile_only,
        pre_submit_check=pre_submit_check,
    )


def send_discord_alert(message, is_error=False, *, title=None, color=None):
    if not DISCORD_WEBHOOK_URL:
        return
    payload = {
        "embeds": [
            {
                "title": title
                or (
                    "🚨 DCA Bot Needs Attention"
                    if is_error
                    else "✅ DCA Bot Update"
                ),
                "description": message,
                "color": color if color is not None else (16711680 if is_error else 65280),
                "timestamp": datetime.now(SELECTED_TZ).isoformat(),
            }
        ]
    }
    try:
        response = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=5)
        response.raise_for_status()
    except requests.RequestException as error:
        print(f"Failed to send Discord alert: {type(error).__name__}", flush=True)


def _parse_amount_gbp(value) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("GBP order amount must be a JSON number")
    amount = float(value)
    if not math.isfinite(amount) or not MIN_DCA_GBP <= amount <= MAX_DCA_GBP:
        raise ValueError(
            f"GBP order amount must be between {MIN_DCA_GBP:.0f} and "
            f"{MAX_DCA_GBP:.0f}"
        )
    return amount


def _parse_trade_date(value, target: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{target}.trade_date must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{target}.trade_date must be YYYY-MM-DD") from error
    if parsed.isoformat() != value:
        raise ValueError(f"{target}.trade_date must be YYYY-MM-DD")
    return value


def _configured_start_date() -> date | None:
    """Return the optional local first trading date, failing closed if malformed."""

    if not DCA_START_DATE:
        return None
    try:
        parsed = date.fromisoformat(DCA_START_DATE)
    except ValueError as error:
        raise ValueError("DCA_START_DATE must be YYYY-MM-DD") from error
    if parsed.isoformat() != DCA_START_DATE:
        raise ValueError("DCA_START_DATE must be YYYY-MM-DD")
    return parsed


def _assert_start_date_reached(now: datetime) -> None:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("start-date check requires a timezone-aware timestamp")
    start_date = _configured_start_date()
    local_date = now.astimezone(SELECTED_TZ).date()
    if start_date is not None and local_date < start_date:
        raise KrakenPreSubmissionError(
            f"Automated trading starts on {start_date.isoformat()} "
            f"{SELECTED_TZ.key}; today is {local_date.isoformat()}"
        )


def _parse_symbol_filter(value) -> tuple[str, ...]:
    """Return an optional strict workflow-dispatch target filter.

    A missing/blank value means all production targets.  An explicit empty JSON
    array means reconcile pending intents but start no new trades.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return tuple(ALLOWED_TARGETS)
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as error:
            raise ValueError("DCA_SYMBOLS_JSON must be a JSON array") from error
    if not isinstance(value, list):
        raise ValueError("DCA_SYMBOLS_JSON must be a JSON array")
    selected = []
    for raw_symbol in value:
        if not isinstance(raw_symbol, str):
            raise ValueError("DCA_SYMBOLS_JSON entries must be target strings")
        symbol = raw_symbol.strip().upper().replace("/", "_")
        if symbol not in ALLOWED_TARGETS:
            raise ValueError(f"Unsupported DCA target filter: {raw_symbol}")
        if symbol not in selected:
            selected.append(symbol)
    return tuple(selected)


def _execution_state_for_symbol(symbol_key, execution_state):
    state = validate_execution_state(execution_state or {})
    return state.get(symbol_key, {"LAST_BUY_DATE": ""})


def _normalise_pending_order(pending, target: str) -> dict:
    if not isinstance(pending, dict):
        raise ValueError(f"{target}.PENDING_ORDER must be an object")
    expected_fields = {
        "client_order_id",
        "funding_client_order_id",
        "trade_date",
        "amount_gbp",
        "decision_id",
        "created_at",
    }
    if set(pending) != expected_fields:
        raise ValueError(
            f"{target}.PENDING_ORDER must contain exactly: "
            + ", ".join(sorted(expected_fields))
        )
    client_order_id = pending["client_order_id"]
    funding_client_order_id = pending["funding_client_order_id"]
    trade_date = pending["trade_date"]
    amount_gbp = pending["amount_gbp"]
    decision_id = pending["decision_id"]
    if (
        not isinstance(client_order_id, str)
        or not _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id)
    ):
        raise ValueError(f"{target} has an invalid pending client order ID")
    if (
        not isinstance(funding_client_order_id, str)
        or not _CLIENT_ORDER_ID_PATTERN.fullmatch(funding_client_order_id)
        or funding_client_order_id == client_order_id
    ):
        raise ValueError(f"{target} has an invalid pending funding client order ID")
    _parse_trade_date(trade_date, target)
    amount_gbp = _parse_amount_gbp(amount_gbp)
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError(f"{target}.PENDING_ORDER requires decision_id")
    normalized = {
        "client_order_id": client_order_id,
        "funding_client_order_id": funding_client_order_id,
        "trade_date": trade_date,
        "amount_gbp": amount_gbp,
        "decision_id": decision_id,
    }
    created_at = pending["created_at"]
    parse_utc_iso(created_at)
    normalized["created_at"] = created_at
    return normalized


def _pending_order_for_symbol(execution_state, symbol_key):
    entry = _execution_state_for_symbol(symbol_key, execution_state)
    pending = entry.get(PENDING_ORDER_FIELD)
    return _normalise_pending_order(pending, symbol_key) if pending else None


def _pending_gist_deliveries_for_symbol(execution_state, symbol_key):
    entry = _execution_state_for_symbol(symbol_key, execution_state)
    return list(entry.get(PENDING_GIST_DELIVERIES_FIELD, []))


def get_config_for_symbol(symbol_key, target_map, execution_state=None):
    """Return one normalized final-schema rule plus its trader-owned state."""
    rules = validate_rules_map(target_map, require_all=False)
    if symbol_key not in rules:
        raise ValueError(f"No DCA configuration exists for {symbol_key}")
    state = validate_execution_state(execution_state or {})
    state_entry = state.get(symbol_key, {"LAST_BUY_DATE": ""})
    return {
        "KEY": symbol_key,
        **rules[symbol_key],
        "LAST_BUY_DATE": state_entry.get("LAST_BUY_DATE", ""),
        "PENDING_ORDER": state_entry.get(PENDING_ORDER_FIELD),
        "PENDING_GIST_DELIVERIES": list(
            state_entry.get(PENDING_GIST_DELIVERIES_FIELD, [])
        ),
    }


def _github_variable_context(variable_name):
    token = os.environ.get("GH_PAT_FOR_VARS")
    if not token:
        raise RuntimeError(
            "GH_PAT_FOR_VARS is required to read live DCA configuration"
        )
    repository = os.environ.get("GITHUB_REPOSITORY")
    if not repository:
        raise RuntimeError(
            "GITHUB_REPOSITORY is required to read live DCA configuration"
        )
    collection_url = f"https://api.github.com/repos/{repository}/actions/variables"
    url = f"{collection_url}/{variable_name}"
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    return url, collection_url, headers


def _fetch_repo_json_variable(variable_name, *, required):
    url, _collection_url, headers = _github_variable_context(variable_name)
    response = requests.get(url, headers=headers, timeout=15)
    if response.status_code == 404 and not required:
        return {}, False
    if response.status_code != 200:
        raise RuntimeError(
            f"Live {variable_name} read failed with HTTP {response.status_code}"
        )
    try:
        value = json.loads(response.json()["value"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Live {variable_name} is not valid JSON") from error
    if not isinstance(value, dict):
        raise RuntimeError(f"Live {variable_name} must be a JSON object")
    return value, True


def _write_repo_json_variable(variable_name, value, *, exists):
    if variable_name == EXECUTION_STATE_VARIABLE:
        value = validate_execution_state(value)
    url, collection_url, headers = _github_variable_context(variable_name)
    data = {
        "name": variable_name,
        "value": json.dumps(value, separators=(",", ":"), ensure_ascii=False),
    }
    if exists:
        response = requests.patch(url, headers=headers, json=data, timeout=15)
        expected_status = 204
    else:
        response = requests.post(collection_url, headers=headers, json=data, timeout=15)
        expected_status = 201
    if response.status_code != expected_status:
        operation = "update" if exists else "create"
        raise RuntimeError(
            f"{variable_name} {operation} failed with HTTP {response.status_code}"
        )


def fetch_live_target_map():
    target_map, _exists = _fetch_repo_json_variable(RULES_VARIABLE, required=True)
    return validate_rules_map(target_map)


def fetch_live_analysis_state(target_map=None):
    analysis, _exists = _fetch_repo_json_variable(
        ANALYSIS_STATE_VARIABLE, required=True
    )
    return validate_analysis_state(analysis, target_map)


def fetch_live_execution_state():
    state, _exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=False
    )
    return validate_execution_state(state)


def _initial_rules_map():
    """Load rules without exposing complete production JSON in workflow env logs."""

    if DCA_TARGET_MAP_JSON.strip():
        return validate_rules_map(json.loads(DCA_TARGET_MAP_JSON))
    return fetch_live_target_map()


def _initial_analysis_state():
    if DCA_ANALYSIS_STATE_JSON.strip():
        return validate_analysis_state(json.loads(DCA_ANALYSIS_STATE_JSON))
    return fetch_live_analysis_state()


def _initial_execution_state():
    if DCA_EXECUTION_STATE_JSON.strip():
        return validate_execution_state(json.loads(DCA_EXECUTION_STATE_JSON))
    return fetch_live_execution_state()


def prepare_order_intent(
    symbol_key,
    client_order_id,
    funding_client_order_id,
    trade_date,
    amount_gbp,
    decision_id,
):
    """Persist an analysis-bound intent before Kraken can receive AddOrder."""
    if symbol_key not in ALLOWED_TARGETS:
        raise ValueError(f"Unsupported DCA target: {symbol_key}")
    _parse_trade_date(trade_date, symbol_key)
    amount_gbp = _parse_amount_gbp(amount_gbp)
    if not _CLIENT_ORDER_ID_PATTERN.fullmatch(client_order_id):
        raise ValueError("Invalid deterministic client order ID")
    if (
        not _CLIENT_ORDER_ID_PATTERN.fullmatch(funding_client_order_id)
        or funding_client_order_id == client_order_id
    ):
        raise ValueError("Invalid deterministic funding client order ID")
    if not isinstance(decision_id, str) or not decision_id.strip():
        raise ValueError("decision_id is required for a durable order intent")

    raw_state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=False
    )
    state = validate_execution_state(raw_state)
    entry = state.setdefault(symbol_key, {"LAST_BUY_DATE": ""})
    if entry.get("LAST_BUY_DATE") == trade_date:
        raise RuntimeError(f"{symbol_key} is already marked as bought on {trade_date}")
    pending = entry.get(PENDING_ORDER_FIELD)
    if pending is not None:
        return _normalise_pending_order(pending, symbol_key), True

    # A confirmed fill must be able to transition atomically from PENDING_ORDER
    # to a durable Portfolio Compass delivery.  Reserve that storage before the
    # Kraken client can ever receive AddOrder.
    ensure_gist_delivery_capacity(state, symbol_key)

    pending = {
        "client_order_id": client_order_id,
        "funding_client_order_id": funding_client_order_id,
        "trade_date": trade_date,
        "amount_gbp": amount_gbp,
        "decision_id": decision_id,
        "created_at": _utc_now().isoformat().replace("+00:00", "Z"),
    }
    entry[PENDING_ORDER_FIELD] = pending
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    _gha_mask(client_order_id)
    print(
        f"Persisted durable Kraken order intent for {symbol_key} "
        f"(buy={client_order_id}, funding={funding_client_order_id}).",
        flush=True,
    )
    return pending, False


def clear_order_intent(symbol_key, client_order_id, decision_id=None):
    """Clear only a matching intent after a confirmed pre-submit/no-fill failure."""
    raw_state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=True
    )
    state = validate_execution_state(raw_state)
    pending = _pending_order_for_symbol(state, symbol_key)
    if pending is None:
        return
    if pending["client_order_id"] != client_order_id:
        raise RuntimeError(f"Refusing to clear a different pending order for {symbol_key}")
    if decision_id is not None and pending["decision_id"] != decision_id:
        raise RuntimeError(f"Refusing to clear a different decision for {symbol_key}")
    state[symbol_key].pop(PENDING_ORDER_FIELD, None)
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(f"Cleared safe no-fill order intent for {symbol_key}.", flush=True)


def complete_order_intent(
    symbol_key,
    client_order_id,
    completed_date,
    decision_id=None,
    *,
    gist_delivery,
):
    """Atomically complete an order and retain immutable outbox evidence.

    The confirmed fill is never marked complete without first retaining the
    delivery evidence in the same protected execution-state write.  Repository
    availability therefore cannot cause the Kraken order to be repeated.  The
    legacy field name is preserved so existing recovery evidence is not lost.
    """
    _parse_trade_date(completed_date, symbol_key)
    raw_state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=True
    )
    state = validate_execution_state(raw_state)
    pending = _pending_order_for_symbol(state, symbol_key)
    if pending is None or pending["client_order_id"] != client_order_id:
        raise RuntimeError(
            f"Durable order intent changed or disappeared for {symbol_key}"
        )
    if decision_id is not None and pending["decision_id"] != decision_id:
        raise RuntimeError(f"Durable decision changed for {symbol_key}")
    entry = state[symbol_key]
    normalized_delivery = validate_gist_delivery(gist_delivery, symbol_key)
    deliveries = list(entry.get(PENDING_GIST_DELIVERIES_FIELD, []))
    existing = next(
        (
            queued
            for queued in deliveries
            if queued["delivery_id"] == normalized_delivery["delivery_id"]
        ),
        None,
    )
    if existing is not None and existing != normalized_delivery:
        raise RuntimeError(
            f"Different outbox evidence already uses the Kraken order ID for {symbol_key}"
        )
    if existing is None:
        if len(deliveries) >= MAX_PENDING_GIST_DELIVERIES:
            raise RuntimeError(
                f"The durable outbox delivery queue is full for {symbol_key}"
            )
        deliveries.append(normalized_delivery)
    entry[PENDING_GIST_DELIVERIES_FIELD] = deliveries

    entry["LAST_BUY_DATE"] = completed_date
    entry.pop(PENDING_ORDER_FIELD, None)
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(
        f"Completed order intent for {symbol_key}; LAST_BUY_DATE={completed_date}.",
        flush=True,
    )


def acknowledge_gist_delivery(symbol_key, delivery_id, row_sha256):
    """Remove only one exact, already-delivered outbox event."""
    raw_state, exists = _fetch_repo_json_variable(
        EXECUTION_STATE_VARIABLE, required=True
    )
    state = validate_execution_state(raw_state)
    entry = state.get(symbol_key)
    if entry is None:
        return True
    deliveries = list(entry.get(PENDING_GIST_DELIVERIES_FIELD, []))
    match = next(
        (
            queued
            for queued in deliveries
            if queued["delivery_id"] == delivery_id
        ),
        None,
    )
    if match is None:
        return True
    if match["row_sha256"] != row_sha256:
        raise RuntimeError(
            f"Refusing to acknowledge changed outbox delivery evidence for {symbol_key}"
        )
    remaining = [
        queued
        for queued in deliveries
        if queued["delivery_id"] != delivery_id
    ]
    if remaining:
        entry[PENDING_GIST_DELIVERIES_FIELD] = remaining
    else:
        entry.pop(PENDING_GIST_DELIVERIES_FIELD, None)
    _write_repo_json_variable(EXECUTION_STATE_VARIABLE, state, exists=exists)
    print(f"Acknowledged Portfolio Compass ledger delivery for {symbol_key}.", flush=True)
    return True


def _attempt_gist_delivery(symbol_key, delivery):
    """Deliver and acknowledge one immutable outbox event, if possible."""
    normalized = validate_gist_delivery(delivery, symbol_key)
    _gha_mask(normalized["delivery_id"])
    if not update_gist_log(normalized):
        return False
    try:
        return acknowledge_gist_delivery(
            symbol_key,
            normalized["delivery_id"],
            normalized["row_sha256"],
        )
    except Exception as error:
        # The remote row may already exist.  Retaining the outbox item is safe
        # because the next idempotent attempt cannot append a duplicate.
        print(
            "Portfolio Compass ledger delivery was saved but its local "
            f"acknowledgement failed ({type(error).__name__}).",
            flush=True,
        )
        return False


def retry_pending_gist_deliveries(execution_state=None):
    """Drain confirmed-purchase deliveries without touching Kraken orders."""
    state = validate_execution_state(
        execution_state if execution_state is not None else fetch_live_execution_state()
    )
    all_delivered = True
    for symbol_key in ALLOWED_TARGETS:
        for delivery in _pending_gist_deliveries_for_symbol(state, symbol_key):
            if not _attempt_gist_delivery(symbol_key, delivery):
                all_delivered = False
    return all_delivered


def _decision_snapshot(decision):
    """Return the complete canonical decision for live-state comparison."""

    return json.dumps(decision, sort_keys=True, separators=(",", ":"))


def _decision_gate(symbol, rule, decision, now):
    """Return ``(status, reason, amount)`` for one current decision."""
    if decision["ANALYSIS_STATUS"] != "READY":
        return "ERROR", f"analysis status is {decision['ANALYSIS_STATUS']}", None
    if decision["ANALYSIS_DATE"] != now.astimezone(SELECTED_TZ).date().isoformat():
        return "ERROR", "analysis decision is not for the current Bangkok date", None
    expected_hash = rules_hash(symbol, rule)
    if decision["RULES_HASH"] != expected_hash:
        return (
            "REFRESH_REQUIRED",
            "the GBP budget changed after this analysis was calculated",
            None,
        )
    try:
        execute_at = parse_utc_iso(decision["EXECUTE_AT"])
        valid_until = parse_utc_iso(decision["VALID_UNTIL"])
        age_minutes = decision_age_minutes(decision, now)
        start_date = _configured_start_date()
        analyzed_after_start = decision_analyzed_on_or_after(
            decision, start_date, SELECTED_TZ
        )
    except (ConfigError, TypeError, ValueError) as error:
        return "ERROR", f"analysis timestamps are invalid: {error}", None
    if not analyzed_after_start:
        return "ERROR", "analysis decision predates the automated trading start date", None
    if age_minutes < -1:
        return "ERROR", "analysis decision timestamp is in the future", None
    if now < execute_at - timedelta(minutes=5):
        return "NOT_DUE", "execution window has not opened", None
    if now > valid_until or not is_execution_window(now, execute_at):
        return "MISSED", "analysis decision is stale or its window was missed", None
    if DCA_TRADING_MODE == "shadow":
        return "SHADOW", "shadow mode blocks new Kraken orders", None
    if DCA_TRADING_MODE == "canary" and symbol != DCA_CANARY_SYMBOL:
        return "SHADOW", f"canary mode permits only {DCA_CANARY_SYMBOL}", None
    if DCA_TRADING_MODE not in {"canary", "live"}:
        return "ERROR", f"invalid DCA_TRADING_MODE {DCA_TRADING_MODE!r}", None
    amount_gbp = effective_amount(rule, decision)
    try:
        amount_gbp = _parse_amount_gbp(amount_gbp)
    except ValueError as error:
        return "ERROR", str(error), None
    return "READY", "ready", amount_gbp


def _global_history_gate(analysis, now):
    """Enforce the selected Kraken-only policy before any new order.

    A pair-local analysis error still remains visible and retryable, but strict
    production execution cannot proceed until all three canonical targets have
    a current, verified decision built by the same 60-day policy.
    """
    today = now.astimezone(SELECTED_TZ).date().isoformat()
    failures = []
    if analysis.get("ANALYSIS_DATE") != today:
        failures.append(f"state date is {analysis.get('ANALYSIS_DATE') or 'missing'}")
    if analysis.get("POLICY_VERSION") != TIMING_POLICY_VERSION:
        failures.append("state policy version is not current")
    for target in ALLOWED_TARGETS:
        decision = analysis["TARGETS"][target]
        history = decision.get("HISTORY") or {}
        if decision.get("ANALYSIS_DATE") != today:
            failures.append(f"{target} analysis date is not {today}")
        if decision.get("ANALYSIS_STATUS") != "READY":
            failures.append(
                f"{target} analysis is {decision.get('ANALYSIS_STATUS', 'missing')}"
            )
        if history.get("STATUS") != "READY":
            failures.append(f"{target} history is {history.get('STATUS', 'missing')}")
    if failures:
        return False, "; ".join(failures)
    return True, "all three Kraken histories and decisions are ready"


def _revalidate_trade_intent(
    symbol,
    expected_rule,
    expected_decision,
    today,
    *,
    expected_pending=None,
    reserve_gist_delivery=False,
    now=None,
):
    """Re-fetch every live state and fail if any spend-affecting value changed."""
    current_time = now or _utc_now()
    _assert_start_date_reached(current_time)
    live_rules = fetch_live_target_map()
    # Validate analysis structure globally, then bind only this asset's decision
    # below.  A disabled asset with newly edited budgets must not block an
    # unrelated enabled asset whose own decision is still current.
    live_analysis = fetch_live_analysis_state()
    live_execution = fetch_live_execution_state()
    rule = live_rules[symbol]
    decision = live_analysis["TARGETS"][symbol]
    execution = live_execution.get(symbol, {"LAST_BUY_DATE": ""})

    globally_ready, global_reason = _global_history_gate(live_analysis, current_time)
    if not globally_ready:
        raise RuntimeError(f"global Kraken history gate blocked execution: {global_reason}")

    if not rule["BUY_ENABLED"]:
        raise RuntimeError(f"{symbol} was disabled before order submission")
    if execution.get("LAST_BUY_DATE", "") == today:
        raise RuntimeError(f"{symbol} is already marked as bought on {today}")
    if rule != expected_rule:
        raise RuntimeError(f"{symbol} budgets or enable state changed during this run")
    if _decision_snapshot(decision) != _decision_snapshot(expected_decision):
        raise RuntimeError(f"{symbol} analysis decision changed during this run")
    status, reason, amount_gbp = _decision_gate(symbol, rule, decision, current_time)
    if status != "READY":
        raise RuntimeError(f"{symbol} is not executable: {reason}")

    live_pending = _pending_order_for_symbol(live_execution, symbol)
    if expected_pending is None:
        if live_pending is not None:
            raise RuntimeError(f"{symbol} acquired a pending intent during this run")
    elif live_pending != _normalise_pending_order(expected_pending, symbol):
        raise RuntimeError(f"{symbol} durable intent changed during this run")
    else:
        if live_pending["decision_id"] != decision["DECISION_ID"]:
            raise RuntimeError(f"{symbol} durable intent belongs to another decision")
        if live_pending["trade_date"] != today:
            raise RuntimeError(f"{symbol} durable intent belongs to another trade day")
        if not math.isclose(
            live_pending["amount_gbp"], amount_gbp, rel_tol=0, abs_tol=0.001
        ):
            raise RuntimeError(f"{symbol} durable intent amount changed during this run")
    if reserve_gist_delivery:
        ensure_gist_delivery_capacity(live_execution, symbol)
    return live_rules, live_analysis, live_execution, amount_gbp


def _post_trade_logs(
    trade_data,
    base_symbol,
    exchange_pair,
    *,
    symbol_key=None,
    gist_delivery=None,
):
    """Attempt optional mirrors after the confirmed fill is already durable."""
    ghostfolio_saved = False
    if GHOSTFOLIO_DIRECT_SYNC_ENABLED:
        try:
            from portfolio_logger import get_account_id, log_to_ghostfolio

            portfolio_map = json.loads(os.environ.get("PORTFOLIO_ACCOUNT_MAP") or "{}")
            account_id = get_account_id(base_symbol, portfolio_map)
            if account_id:
                ghostfolio_saved = bool(
                    log_to_ghostfolio(
                        trade_data,
                        base_symbol,
                        account_id,
                        exchange_pair=exchange_pair,
                    )
                )
        except Exception as error:
            print(f"Optional Ghostfolio logging failed: {type(error).__name__}", flush=True)
    if gist_delivery is not None and symbol_key is not None:
        try:
            delivered = _attempt_gist_delivery(symbol_key, gist_delivery)
        except Exception as error:
            print(
                f"Portfolio Compass ledger delivery failed: {type(error).__name__}",
                flush=True,
            )
            delivered = False
        if not delivered:
            warning = (
                f"Kraken purchase for {symbol_key} is confirmed, but its Portfolio "
                "Compass ledger delivery remains safely queued for retry."
            )
            print(warning, flush=True)
            send_discord_alert(warning, is_error=True)
    else:
        # Backward-compatible helper behavior for callers outside execute_trade.
        try:
            update_gist_log(
                trade_data,
                symbol=base_symbol,
                saved_to_ghostfolio=ghostfolio_saved,
            )
        except Exception as error:
            print(f"Private repository outbox failed: {type(error).__name__}", flush=True)
    return ghostfolio_saved


def execute_trade(
    symbol,
    amount_gbp,
    map_key=None,
    *,
    expected_rule=None,
    expected_decision=None,
):
    """Place/reconcile one durable analysis-bound order and record its fill."""
    key = map_key or symbol
    if key not in ALLOWED_TARGETS:
        raise ValueError(f"Unsupported DCA target: {key}")
    amount_gbp = _parse_amount_gbp(amount_gbp)
    now = _utc_now()
    today = now.astimezone(SELECTED_TZ).date().isoformat()
    intent = None
    intent_was_existing = False

    try:
        live_state = fetch_live_execution_state()
        intent = _pending_order_for_symbol(live_state, key)
        if intent is not None:
            intent_was_existing = True
            amount_gbp = intent["amount_gbp"]
            _gha_mask(intent["client_order_id"])
            _gha_mask(intent["funding_client_order_id"])
            print(
                f"Reconciling durable Kraken order intent for {key} "
                f"(buy={intent['client_order_id']}, "
                f"funding={intent['funding_client_order_id']}).",
                flush=True,
            )
        else:
            if expected_rule is None or expected_decision is None:
                raise RuntimeError(
                    f"No current analysis-bound configuration was supplied for {key}"
                )
            validated = _revalidate_trade_intent(
                key,
                expected_rule,
                expected_decision,
                today,
                reserve_gist_delivery=True,
                now=now,
            )
            validated_amount = validated[-1]
            if not math.isclose(
                amount_gbp, validated_amount, rel_tol=0, abs_tol=0.001
            ):
                raise RuntimeError(
                    f"{key} requested amount does not match the live decision tier"
                )
            decision_id = expected_decision["DECISION_ID"]
            client_order_id = build_client_order_id(key, today, purpose="buy")
            funding_client_order_id = build_client_order_id(
                key, today, purpose="funding"
            )
            intent, intent_was_existing = prepare_order_intent(
                key,
                client_order_id,
                funding_client_order_id,
                today,
                amount_gbp,
                decision_id,
            )
            if not intent_was_existing:
                try:
                    _revalidate_trade_intent(
                        key,
                        expected_rule,
                        expected_decision,
                        today,
                        expected_pending=intent,
                        reserve_gist_delivery=True,
                    )
                except Exception as validation_error:
                    raise KrakenPreSubmissionError(
                        "Live state changed after the durable intent was saved: "
                        f"{validation_error}"
                    ) from validation_error

        client_order_id = intent["client_order_id"]
        funding_client_order_id = intent["funding_client_order_id"]
        amount_gbp = _parse_amount_gbp(intent["amount_gbp"])
        _gha_mask(str(amount_gbp))
        _gha_mask(f"{amount_gbp:.2f}")
        if amount_gbp.is_integer():
            _gha_mask(str(int(amount_gbp)))
        print(f"Executing validated Kraken DCA buy for {key}.", flush=True)

        def final_live_state_check():
            if not intent_was_existing:
                _revalidate_trade_intent(
                    key,
                    expected_rule,
                    expected_decision,
                    today,
                    expected_pending=intent,
                    reserve_gist_delivery=True,
                )

        order_data = _place_routed_market_buy(
            key,
            amount_gbp,
            client_order_id=client_order_id,
            funding_client_order_id=funding_client_order_id,
            reconcile_only=intent_was_existing,
            pre_submit_check=final_live_state_check,
        )
    except KrakenOrderStateUnknown as error:
        message = (
            f"CRITICAL: Kraken order state is unknown for {key}; the durable "
            f"intent remains locked for reconciliation. {error}"
        )
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False
    except (KrakenOrderNoFill, KrakenPreSubmissionError) as error:
        if intent is not None:
            try:
                clear_order_intent(
                    key, intent["client_order_id"], intent["decision_id"]
                )
            except Exception as clear_error:
                error = RuntimeError(
                    f"{error}; durable intent cleanup also failed: {clear_error}"
                )
        message = f"DCA failed safely before a fill ({key}): {error}"
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False
    except Exception as error:
        if intent is not None:
            message = (
                f"CRITICAL: Unexpected Kraken failure for {key}; the durable "
                f"intent remains locked for reconciliation. {error}"
            )
        else:
            message = f"DCA failed before an order intent was saved ({key}): {error}"
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False

    try:
        order_id = order_data["order_id"]
        exchange_pair = order_data["pair"]
        quote_currency = order_data["quote_currency"]
        route = TARGET_ROUTES[key]
        funding_order_id = order_data.get("funding_order_id")
        cost_gbp = float(order_data["cost_gbp"])
        fee_gbp = float(order_data["fee_gbp"])
        gbp_fee_debit = float(order_data["gbp_fee_debit"])
        fee_details = order_data["fee_details"]
        spent_gbp = float(order_data["spent_gbp"])
        received_amount = float(order_data["received"])
        market_price = float(order_data["market_gbp_price_per_unit"])
        effective_price = float(order_data["effective_gbp_price_per_unit"])
        execution_timestamp = int(order_data["timestamp"])
        if quote_currency == "USD":
            funded_usd = float(order_data["funded_usd"])
            cost_quote = float(order_data["cost_usd"])
            fee_quote = float(order_data["crypto_fee_usd"])
            funding_fee_quote = float(order_data["funding_fee_usd"])
            quote_fee_debit = float(order_data["usd_fee_debit"])
            gbp_usd_rate = float(order_data["gbp_usd_rate"])
            unit_price_quote = float(order_data["market_usd_price_per_unit"])
            effective_price_quote = float(order_data["effective_usd_price_per_unit"])
        else:
            funded_usd = 0.0
            cost_quote = float(order_data["cost_quote"])
            fee_quote = float(order_data["fee_quote"])
            funding_fee_quote = 0.0
            quote_fee_debit = float(order_data["quote_fee_debit"])
            gbp_usd_rate = 0.0
            unit_price_quote = float(order_data["market_quote_price_per_unit"])
            effective_price_quote = float(order_data["effective_quote_price_per_unit"])
    except (KeyError, TypeError, ValueError) as error:
        message = (
            f"CRITICAL: Kraken returned an unrecognized fill for {key}; the durable "
            f"intent remains locked: {type(error).__name__}"
        )
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False

    _gha_mask(str(order_id))
    if funding_order_id:
        _gha_mask(str(funding_order_id))
    _gha_mask(f"{cost_gbp:.2f}")
    _gha_mask(f"{fee_gbp:.2f}")
    _gha_mask(f"{spent_gbp:.2f}")
    _gha_mask(f"{received_amount:.8f}")

    base_symbol = exchange_pair.split("/", maxsplit=1)[0]
    trade_data = {
        "ts": execution_timestamp,
        "amount_crypto": received_amount,
        "amount_gbp": spent_gbp,
        "cost_gbp": cost_gbp,
        "fee_gbp": fee_gbp,
        "gbp_fee_debit": gbp_fee_debit,
        "fee_details": fee_details,
        "order_id": order_id,
        "gbp_price_per_unit": market_price,
        "effective_gbp_price_per_unit": effective_price,
        "exchange_pair": exchange_pair,
        "decision_id": intent["decision_id"],
        "route": route,
        "quote_currency": quote_currency,
        "cost_quote": cost_quote,
        "fee_quote": fee_quote,
        "funding_fee_quote": funding_fee_quote,
        "quote_fee_debit": quote_fee_debit,
        "funded_usd": funded_usd,
        "gbp_usd_rate": gbp_usd_rate,
        "unit_price_quote": unit_price_quote,
        "effective_price_quote": effective_price_quote,
        "funding_order_id": funding_order_id,
    }
    completed_date = datetime.fromtimestamp(
        execution_timestamp, tz=SELECTED_TZ
    ).date().isoformat()
    try:
        gist_delivery = build_gist_delivery(
            trade_data,
            base_symbol,
            # The immutable evidence is persisted before optional Ghostfolio
            # work.  "not saved" is therefore the conservative durable status.
            saved_to_ghostfolio=False,
        )
        complete_order_intent(
            key,
            intent["client_order_id"],
            completed_date,
            intent["decision_id"],
            gist_delivery=gist_delivery,
        )
    except Exception as error:
        message = (
            f"CRITICAL: Kraken filled {order_id}, but execution state could not be "
            f"completed for {key}. The durable intent remains locked: {error}"
        )
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False

    _post_trade_logs(
        trade_data,
        base_symbol,
        exchange_pair,
        symbol_key=key,
        gist_delivery=gist_delivery,
    )
    execution_time = datetime.fromtimestamp(
        execution_timestamp, tz=SELECTED_TZ
    ).strftime("%Y-%m-%d %H:%M:%S")
    message = (
        "DCA buy executed and confirmed.\n"
        f"**Pair:** {exchange_pair}\n"
        f"**GBP budget/debit:** £{spent_gbp:,.2f}\n"
        f"**Funding:** {'USD ' + format(funded_usd, ',.2f') if route == 'GBP_TO_USD' else 'not required (native GBP market)'}\n"
        f"**Crypto order cost:** {quote_currency} {cost_quote:,.2f}\n"
        f"**Funding fee:** {quote_currency} {funding_fee_quote:,.4f}\n"
        f"**Crypto fee:** {quote_currency} {fee_quote:,.4f}\n"
        f"**GBP/USD execution rate:** {('$' + format(gbp_usd_rate, ',.5f') + ' per GBP 1') if route == 'GBP_TO_USD' else 'not applicable'}\n"
        f"**Total GBP debit:** £{spent_gbp:,.2f}\n"
        f"**Received:** {received_amount:.8f} {base_symbol}\n"
        f"**Market rate:** {quote_currency} {unit_price_quote:,.2f} / GBP {market_price:,.2f}\n"
        f"**Effective rate:** {quote_currency} {effective_price_quote:,.2f} / GBP {effective_price:,.2f}\n"
        f"**Regime:** {expected_decision.get('REGIME') if expected_decision else 'recovery'}\n"
        f"**Decision ID:** {intent['decision_id']}\n"
        "**Portfolio:** Saved on Kraken\n"
        f"**Time:** {execution_time}\n"
        f"**Funding order ID:** {funding_order_id or 'not required'}\n"
        f"**Crypto order ID:** {order_id}"
    )
    send_discord_alert(message, is_error=False)
    return True


def main():
    print("--- Starting Kraken GBP-budgeted mixed-market DCA execution ---", flush=True)
    try:
        execution_state = _initial_execution_state()
    except (json.JSONDecodeError, ConfigError, RuntimeError, ValueError) as error:
        message = f"Invalid DCA execution state: {error}"
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False

    try:
        ledger_deliveries_complete = retry_pending_gist_deliveries(execution_state)
    except Exception as error:
        print(
            f"Portfolio Compass ledger retry failed: {type(error).__name__}",
            flush=True,
        )
        ledger_deliveries_complete = False
    if not ledger_deliveries_complete:
        warning = (
            "One or more confirmed purchases remain in the durable Portfolio "
            "Compass delivery queue. Kraken order processing remains independent."
        )
        print(warning, flush=True)
        send_discord_alert(warning, is_error=True)

    all_succeeded = True
    # Durable intents are reconciled first and are never abandoned merely because
    # rules are invalid, a rule was disabled, or a newer analysis failed.
    for symbol in ALLOWED_TARGETS:
        pending = _pending_order_for_symbol(execution_state, symbol)
        if pending is None:
            continue
        succeeded = execute_trade(
            symbol,
            pending["amount_gbp"],
            map_key=symbol,
        )
        all_succeeded = all_succeeded and succeeded

    try:
        rules = _initial_rules_map()
        selected_symbols = _parse_symbol_filter(DCA_SYMBOLS_JSON)
    except (json.JSONDecodeError, ConfigError, RuntimeError, ValueError) as error:
        message = (
            "Pending-order reconciliation completed, but new DCA orders are "
            f"blocked by invalid rules or target selection: {error}"
        )
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False

    now = _utc_now()
    today = now.astimezone(SELECTED_TZ).date().isoformat()
    try:
        start_date = _configured_start_date()
    except ValueError as error:
        message = f"New DCA orders are blocked by invalid start date: {error}"
        print(message, flush=True)
        send_discord_alert(message, is_error=True)
        return False
    if start_date is not None and date.fromisoformat(today) < start_date:
        print(
            f"Automated trading starts on {start_date.isoformat()} "
            f"{SELECTED_TZ.key}; no new orders are permitted today ({today}).",
            flush=True,
        )
        return all_succeeded

    analysis = None
    analysis_error = None
    try:
        analysis = _initial_analysis_state()
    except (json.JSONDecodeError, ConfigError, RuntimeError, ValueError) as error:
        analysis_error = str(error)

    global_history_ready = False
    global_history_reason = analysis_error or "analysis state is unavailable"
    if analysis is not None:
        global_history_ready, global_history_reason = _global_history_gate(analysis, now)
    if not global_history_ready:
        message = (
            "New Kraken orders are globally blocked until all three pairs have "
            f"current verified 60-day decisions: {global_history_reason}."
        )
        print(message, flush=True)
        send_discord_alert(message, is_error=True)

    for symbol in selected_symbols:
        if _pending_order_for_symbol(execution_state, symbol) is not None:
            continue
        rule = rules[symbol]
        if not rule["BUY_ENABLED"]:
            print(f"{symbol} is disabled; skipping.", flush=True)
            continue
        last_buy_date = execution_state.get(symbol, {}).get("LAST_BUY_DATE", "")
        if last_buy_date == today:
            print(f"Already bought {symbol} today ({today}); skipping.", flush=True)
            continue
        if analysis is None:
            message = f"Skipping {symbol}: analysis state is invalid: {analysis_error}"
            print(message, flush=True)
            send_discord_alert(message, is_error=True)
            all_succeeded = False
            continue
        if not global_history_ready:
            all_succeeded = False
            continue

        decision = analysis["TARGETS"][symbol]
        status, reason, amount_gbp = _decision_gate(symbol, rule, decision, now)
        if status == "NOT_DUE":
            print(f"{symbol}: {reason}.", flush=True)
            continue
        if status == "SHADOW":
            print(f"{symbol}: {reason}; no Kraken order attempted.", flush=True)
            continue
        if status == "REFRESH_REQUIRED":
            pair = symbol.replace("_", "/")
            message = (
                f"🔄 **{pair} is waiting for fresh analysis**\n"
                "The GBP budget changed after today's decision was calculated.\n"
                "🛡️ **Safety:** No Kraken order was attempted.\n"
                "▶️ **Next:** A new deterministic analysis must complete before "
                "this pair can trade."
            )
            print(f"{symbol}: {reason}; no Kraken order attempted.", flush=True)
            send_discord_alert(
                message,
                is_error=False,
                title="🔄 DCA Analysis Refresh Required",
                color=16753920,
            )
            all_succeeded = False
            continue
        if status == "MISSED":
            pair = symbol.replace("_", "/")
            print(
                f"{pair}: today's safe execution window has closed; no late "
                "order will be replayed. Tomorrow's analysis will schedule the "
                "next opportunity.",
                flush=True,
            )
            # This is an expected terminal state, not an execution failure.
            # Scheduled fallbacks remain quiet instead of repeating a red alert.
            continue
        if status != "READY":
            message = f"Skipping {symbol}: {reason}."
            print(message, flush=True)
            send_discord_alert(message, is_error=True)
            all_succeeded = False
            continue

        succeeded = execute_trade(
            symbol,
            amount_gbp,
            map_key=symbol,
            expected_rule=rule,
            expected_decision=decision,
        )
        all_succeeded = all_succeeded and succeeded
    return all_succeeded


if __name__ == "__main__":
    if not main():
        raise SystemExit(1)

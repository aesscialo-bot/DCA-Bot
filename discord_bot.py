"""Discord and Railway control plane for GBP-budgeted Kraken USD DCA.

The bot never places an order.  It reads persisted GitHub variables,
dispatches serialized GitHub Actions workflows, and runs a five-minute Railway
scheduler against absolute per-asset analysis decisions.  Every write command
is exact, allowlisted, and fail-closed.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import date, datetime, time, timezone
import json
import os
import re
import sys
from time import monotonic
from typing import Any, Mapping
from zoneinfo import ZoneInfo

import discord
from discord.ext import tasks
from google import genai
import requests

from dca_config import (
    ALLOWED_TARGETS,
    ConfigError,
    amount_for_tier_gbp,
    decision_analyzed_on_or_after,
    decision_age_minutes,
    effective_amount,
    global_rules_hash,
    is_execution_window,
    maximum_daily_exposure_gbp,
    parse_utc_iso,
    rules_hash,
    validate_analysis_state,
    validate_execution_state,
    validate_rules_map,
)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GH_PAT = os.environ.get("GH_PAT", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")
GITHUB_WORKFLOW_REF = os.environ.get("GITHUB_WORKFLOW_REF", "").strip()
CHANNEL_ID = os.environ.get("DISCORD_CHANNEL_ID")
ALLOWED_USERS = os.environ.get("DISCORD_ALLOWED_USERS", "")
DCA_CRON_ENABLED = os.environ.get("DCA_CRON_ENABLED", "false").lower() == "true"
TIMEZONE = ZoneInfo(os.environ.get("TIMEZONE", "Asia/Bangkok"))

RULES_VARIABLE = "DCA_TARGET_MAP"
ANALYSIS_STATE_VARIABLE = "DCA_ANALYSIS_STATE"
EXECUTION_STATE_VARIABLE = "DCA_EXECUTION_STATE"
START_DATE_VARIABLE = "DCA_START_DATE"
CONFIG_WRITE_PREFIX = "!dca "
DCA_AMOUNT_MIN_GBP = 5.0
DCA_AMOUNT_MAX_GBP = 1_000.0
ENABLE_CONFIRMATION_TTL_SECONDS = 300
DISPATCH_RETRY_SECONDS = 30 * 60
# The daily workflow is scheduled for 04:00 Bangkok. Give GitHub Actions a
# bounded startup window before treating a missing start-day decision as an
# operational error.
START_DAY_ANALYSIS_EXPECTED_BY = time(4, 15)

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"token {GH_PAT}",
    "Accept": "application/vnd.github+json",
}

# A restart intentionally cancels pending confirmations.
_pending_enable_confirmations: dict[str, dict[str, Any]] = {}

# symbol -> absolute decision and execution-state metadata.
_dca_schedule: dict[str, dict[str, Any]] = {}
_pending_recovery_symbols: set[str] = set()
_pending_gist_delivery_symbols: set[str] = set()
_awaiting_start_day_symbols: set[str] = set()
_dca_dispatch_guard: dict[tuple[str, str], float] = {}
_schedule_error: str | None = None
_schedule_warning: str | None = None
_last_schedule_alert: str | None = None
_schedule_start_date: date | None = None


def _log(message: str) -> None:
    """Emit one immediately visible, non-secret Railway log line."""

    print(message, flush=True)


# ---------------------------------------------------------------------------
# Exact command and authorization helpers
# ---------------------------------------------------------------------------


def _allowed_user_ids() -> set[str]:
    return {item.strip() for item in ALLOWED_USERS.split(",") if item.strip()}


def _message_author_id(message: discord.Message) -> str:
    return str(getattr(getattr(message, "author", None), "id", "")).strip()


def _is_authorized_config_writer(message: discord.Message) -> bool:
    allowed = _allowed_user_ids()
    author_id = _message_author_id(message)
    return bool(allowed and author_id and author_id in allowed)


def _config_write_block_reason(
    action: str, raw_text: str, message: discord.Message
) -> str | None:
    if action not in {"set_amounts", "set_enabled", "analyze", "update_dca"}:
        return None
    if not _is_authorized_config_writer(message):
        return (
            "Configuration and analysis commands require DISCORD_ALLOWED_USERS "
            "and an explicitly allowlisted Discord user."
        )
    if not raw_text.startswith(CONFIG_WRITE_PREFIX):
        return f"Write commands must start exactly with `{CONFIG_WRITE_PREFIX}`."
    return None


_FULL_NAMES = {
    "bitcoin": "BTC",
    "hyperliquid": "HYPE",
    "hype": "HYPE",
    "solana": "SOL",
}


def _normalise_usd_key(value: str) -> str:
    raw = value.strip()
    lowered = raw.lower()
    raw = _FULL_NAMES.get(lowered, raw).upper().replace("/", "_")
    parts = raw.split("_")
    if len(parts) == 2 and parts[1] != "USD":
        raise ValueError("Only BTC/USD, HYPE/USD, and SOL/USD are supported")
    if len(parts) > 2:
        raise ValueError("Only BTC/USD, HYPE/USD, and SOL/USD are supported")
    symbol = parts[0]
    key = f"{symbol}_USD"
    if key not in ALLOWED_TARGETS:
        available = ", ".join(target.removesuffix("_USD") for target in ALLOWED_TARGETS)
        raise ValueError(f"Supported assets are {available}")
    return key


def _to_usd_pair(value: str) -> str:
    return _normalise_usd_key(value).replace("_", "/")


def _parse_amount(value: Any, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a GBP number")
    try:
        amount = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a GBP number") from exc
    # Zero is a safe unconfigured placeholder while disabled.  Values between
    # zero and the enable minimum are never accepted.
    if amount != 0 and not DCA_AMOUNT_MIN_GBP <= amount <= DCA_AMOUNT_MAX_GBP:
        raise ValueError(
            f"{label} must be £0 or between £{DCA_AMOUNT_MIN_GBP:g} "
            f"and £{DCA_AMOUNT_MAX_GBP:,.0f}"
        )
    return amount


def _display_amount(value: Any) -> str:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return "?"
    return f"£{amount:,.2f}".replace(".00", "")


def _local_timestamp(value: str | None) -> str:
    if not value:
        return "not scheduled"
    try:
        parsed = parse_utc_iso(value).astimezone(TIMEZONE)
    except (ConfigError, ValueError):
        return "invalid"
    return parsed.strftime("%Y-%m-%d %H:%M %Z")


def _parse_start_date(value: str | None) -> date | None:
    if value is None or not value.strip():
        return None
    candidate = value.strip()
    try:
        parsed = date.fromisoformat(candidate)
    except ValueError as exc:
        raise ConfigError("DCA_START_DATE must be YYYY-MM-DD") from exc
    if parsed.isoformat() != candidate:
        raise ConfigError("DCA_START_DATE must be YYYY-MM-DD")
    return parsed


def _awaiting_start_day_analysis(
    now: datetime, start_date: date | None = None
) -> bool:
    """Return whether start-day analysis is not expected to exist yet."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ConfigError("start-day analysis check requires an aware timestamp")
    configured = _schedule_start_date if start_date is None else start_date
    if configured is None:
        return False
    local_now = now.astimezone(TIMEZONE)
    if local_now.date() < configured:
        return True
    return (
        local_now.date() == configured
        and local_now.time() < START_DAY_ANALYSIS_EXPECTED_BY
    )


def _pending_start_day_analysis_symbols(
    rules: Mapping[str, Mapping[str, Any]],
    analysis: Mapping[str, Any],
    now: datetime,
    start_date: date | None = None,
) -> set[str]:
    """Return enabled targets whose latest decision predates the start day."""

    configured = _schedule_start_date if start_date is None else start_date
    if not _awaiting_start_day_analysis(now, configured):
        return set()
    return {
        symbol
        for symbol in ALLOWED_TARGETS
        if rules[symbol]["BUY_ENABLED"]
        and not decision_analyzed_on_or_after(
            analysis["TARGETS"][symbol], configured, TIMEZONE
        )
    }


def _decision_age(decision: Mapping[str, Any], now: datetime | None = None) -> str:
    try:
        age = decision_age_minutes(decision, now=now)
    except (ConfigError, ValueError, TypeError):
        return "unknown"
    if age < 0:
        return "invalid (future timestamp)"
    if age < 60:
        return f"{age:.0f} min"
    return f"{age / 60:.1f} h"


def global_rules_pre_state_hash(current_rules: Mapping[str, Any] | str) -> str:
    """Compatibility name for the shared global rules fingerprint."""

    return global_rules_hash(current_rules)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------


def trigger_workflow(workflow_file: str, inputs: dict[str, str] | None = None) -> bool:
    """Dispatch a workflow on the explicitly configured production ref."""

    if not GITHUB_WORKFLOW_REF:
        _log("ERROR workflow dispatch refused: GITHUB_WORKFLOW_REF is missing")
        return False
    url = f"{GH_API}/repos/{GITHUB_REPO}/actions/workflows/{workflow_file}/dispatches"
    body: dict[str, Any] = {"ref": GITHUB_WORKFLOW_REF}
    if inputs:
        body["inputs"] = inputs
    try:
        response = requests.post(url, json=body, headers=GH_HEADERS, timeout=15)
    except requests.RequestException as exc:
        _log(f"ERROR workflow dispatch failed: {type(exc).__name__}")
        return False
    if response.status_code != 204:
        _log(f"ERROR workflow dispatch rejected: HTTP {response.status_code}")
        return False
    return True


def get_repo_variable(name: str) -> str | None:
    url = f"{GH_API}/repos/{GITHUB_REPO}/actions/variables/{name}"
    try:
        response = requests.get(url, headers=GH_HEADERS, timeout=15)
    except requests.RequestException as exc:
        _log(f"ERROR repository variable read failed for {name}: {type(exc).__name__}")
        return None
    if response.status_code != 200:
        _log(f"ERROR repository variable read failed for {name}: HTTP {response.status_code}")
        return None
    value = response.json().get("value")
    return value if isinstance(value, str) else None


def _get_repo_variable_and_refresh(name: str) -> str | None:
    """Compatibility seam used by tests and existing callers."""

    return get_repo_variable(name)


def _load_live_state() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Fetch and validate rules, analysis, and execution state without logging JSON."""

    raw_rules = get_repo_variable(RULES_VARIABLE)
    raw_analysis = get_repo_variable(ANALYSIS_STATE_VARIABLE)
    raw_execution = get_repo_variable(EXECUTION_STATE_VARIABLE)
    missing = [
        name
        for name, value in (
            (RULES_VARIABLE, raw_rules),
            (ANALYSIS_STATE_VARIABLE, raw_analysis),
            (EXECUTION_STATE_VARIABLE, raw_execution),
        )
        if value is None
    ]
    if missing:
        raise ConfigError("GitHub did not return: " + ", ".join(missing))
    rules = validate_rules_map(raw_rules)
    # Bind decisions to rules per asset at each consumer. A disabled asset with
    # newly edited budgets must not block status, enabling, or scheduling for an
    # unrelated asset whose own decision is current.
    analysis = validate_analysis_state(raw_analysis)
    execution = validate_execution_state(raw_execution)
    return rules, analysis, execution


def _symbols_from_dca_map() -> str:
    raw = _get_repo_variable_and_refresh(RULES_VARIABLE)
    if raw is None:
        raise ValueError(f"{RULES_VARIABLE} could not be loaded")
    try:
        rules = validate_rules_map(raw)
    except ConfigError as exc:
        raise ValueError(str(exc)) from exc
    return ", ".join(target.replace("_", "/") for target in rules)


# ---------------------------------------------------------------------------
# Optional Gemini classifier for read-only conversational commands
# ---------------------------------------------------------------------------


AI_MODEL_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
VALID_ACTIONS = {"portfolio", "status", "help", "unknown"}
CLASSIFY_PROMPT = """Classify a read-only Discord command for a DCA service.
Allowed actions are portfolio, status, help, and unknown. Never classify a
configuration change, analysis request, enable, disable, or purchase. Respond
with JSON only: {"action":"...","params":{},"reply":"..."}."""


def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intent, dict) or intent.get("action") not in VALID_ACTIONS:
        return {"action": "unknown", "params": {}, "reply": ""}
    params = intent.get("params")
    return {
        "action": intent["action"],
        "params": params if isinstance(params, dict) else {},
        "reply": str(intent.get("reply", ""))[:300],
    }


async def classify_intent(text: str) -> dict[str, Any]:
    prompt = f"{CLASSIFY_PROMPT}\nUser message: {text}"
    last_error: Exception | None = None
    for model in AI_MODEL_CANDIDATES:
        try:
            def generate():
                with genai.Client(api_key=GEMINI_API_KEY) as ai_client:
                    return ai_client.models.generate_content(model=model, contents=prompt)

            response = await asyncio.to_thread(generate)
            raw = re.sub(r"^```(?:json)?\s*|\s*```$", "", response.text.strip())
            return _validate_intent(json.loads(raw))
        except Exception as exc:  # Gemini is optional and never authorizes writes.
            last_error = exc
    _log(f"WARN read-only command classifier unavailable: {type(last_error).__name__}")
    return {"action": "unknown", "params": {}, "reply": "Classifier unavailable"}


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def handle_set_amounts(
    symbol_value: str,
    low_value: Any,
    high_value: Any,
    message: discord.Message,
) -> None:
    """Atomically queue both user budgets; edits require a disabled target."""

    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
        low = _parse_amount(low_value, "lower amount")
        high = _parse_amount(high_value, "higher amount")
        if low > high:
            raise ValueError("the lower amount must not exceed the higher amount")
    except ValueError as exc:
        await message.reply(f"Invalid request: {exc}")
        return

    raw = await asyncio.to_thread(get_repo_variable, RULES_VARIABLE)
    try:
        rules = validate_rules_map(raw or "")
    except ConfigError as exc:
        await message.reply(f"Blocked: live rules are invalid ({exc}).")
        return
    if rules[symbol]["BUY_ENABLED"]:
        await message.reply(
            f"Blocked: disable **{symbol}** before changing either budget."
        )
        return

    inputs = {
        "action": "set_amounts",
        "symbol": symbol,
        "low_amount_gbp_json": json.dumps(low, separators=(",", ":")),
        # This compatibility-named workflow input stores the upper endpoint;
        # it no longer means that an uptrend selects the amount.
        "up_amount_gbp_json": json.dumps(high, separators=(",", ":")),
    }
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        midpoint = amount_for_tier_gbp(
            {
                "REGIME_AMOUNTS_GBP": {"LOW": low, "UP": high},
                "BUY_ENABLED": False,
            },
            "MID",
        )
        await message.reply(
            f"Queued atomic budgets for **{symbol}**: lower {_display_amount(low)}, "
            f"sideways midpoint {_display_amount(midpoint)}, higher "
            f"{_display_amount(high)}. Run `!dca analyze {symbol.removesuffix('_USD')}` "
            "after the workflow completes."
        )
    else:
        await message.reply("Failed to queue the budget update. No rules were changed.")


async def handle_disable(symbol_value: str, message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
    except ValueError as exc:
        await message.reply(f"Invalid request: {exc}")
        return
    inputs = {
        "action": "set_enabled",
        "symbol": symbol,
        "enabled_json": "false",
    }
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        _pending_enable_confirmations.pop(_message_author_id(message), None)
        await message.reply(
            f"Queued disable for **{symbol}**. Once applied, the trader's live "
            "pre-submit check blocks a new order; an order already accepted by "
            "Kraken will still be reconciled."
        )
    else:
        await message.reply("Failed to queue disable. Check GitHub Actions and retry.")


def _enable_review(
    symbol: str,
    rules: Mapping[str, Any],
    analysis: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    now: datetime,
) -> dict[str, Any]:
    pending_symbols = [
        target
        for target, entry in execution.items()
        if isinstance(entry.get("PENDING_ORDER"), Mapping)
    ]
    if pending_symbols:
        raise ConfigError(
            "cannot enable while Kraken order reconciliation is pending for "
            + ", ".join(pending_symbols)
        )
    rule = rules[symbol]
    if rule["BUY_ENABLED"]:
        raise ConfigError(f"{symbol} is already enabled")
    amounts = rule["REGIME_AMOUNTS_GBP"]
    for tier in ("LOW", "UP"):
        amount = float(amounts[tier])
        if not DCA_AMOUNT_MIN_GBP <= amount <= DCA_AMOUNT_MAX_GBP:
            raise ConfigError(
                f"{symbol} {tier} must be between £{DCA_AMOUNT_MIN_GBP:g} "
                f"and £{DCA_AMOUNT_MAX_GBP:,.0f} before enabling"
            )

    decision = analysis["TARGETS"][symbol]
    if decision["STATUS"] != "READY":
        raise ConfigError(f"{symbol} analysis is ERROR; run a fresh analysis")
    expected_hash = rules_hash(symbol, rule)
    if decision["RULES_HASH"] != expected_hash:
        raise ConfigError(f"{symbol} analysis does not match the live budgets")
    if parse_utc_iso(decision["VALID_UNTIL"]) < now.astimezone(timezone.utc):
        raise ConfigError(f"{symbol} analysis is stale; run a fresh analysis")
    age = decision_age_minutes(decision, now=now)
    if age < 0:
        raise ConfigError(f"{symbol} analysis timestamp is in the future")

    exposure_rules = deepcopy(dict(rules))
    exposure_rules[symbol]["BUY_ENABLED"] = True
    maximum_exposure = float(maximum_daily_exposure_gbp(exposure_rules))
    return {
        "symbol": symbol,
        "low": float(amounts["LOW"]),
        "mid": float(amount_for_tier_gbp(rule, "MID")),
        "high": float(amounts["UP"]),
        "regime": decision["REGIME"],
        "effective_amount": float(effective_amount(rule, decision)),
        "execute_at": decision["EXECUTE_AT"],
        "valid_until": decision["VALID_UNTIL"],
        "decision_id": decision["DECISION_ID"],
        "rules_hash": expected_hash,
        "global_rules_hash": global_rules_pre_state_hash(rules),
        "decision_age_minutes": age,
        "maximum_exposure": maximum_exposure,
    }


async def handle_enable(symbol_value: str, message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
        review = _enable_review(
            symbol,
            rules,
            analysis,
            execution,
            now=datetime.now(timezone.utc),
        )
    except (ValueError, ConfigError) as exc:
        await message.reply(f"Blocked: {exc}.")
        return

    command = f"!dca confirm enable {symbol}"
    author_id = _message_author_id(message)
    _pending_enable_confirmations[author_id] = {
        "command": command,
        "review": review,
        "expires_at": monotonic() + ENABLE_CONFIRMATION_TTL_SECONDS,
    }
    await message.reply(
        f"**Enable review for {symbol}**\n"
        f"UPTREND/lower: {_display_amount(review['low'])} | "
        f"SIDEWAYS/midpoint: {_display_amount(review['mid'])} | "
        f"DOWNTREND/higher: {_display_amount(review['high'])}\n"
        f"Latest regime: `{review['regime']}` | Effective amount: "
        f"{_display_amount(review['effective_amount'])}\n"
        f"Next execution: `{_local_timestamp(review['execute_at'])}` | "
        f"Decision age: `{review['decision_age_minutes']:.0f} min`\n"
        f"Maximum aggregate daily exposure after enable: "
        f"**{_display_amount(review['maximum_exposure'])}**\n"
        "The serialized workflow will re-read this decision and check Kraken's "
        "current market minimum before enabling.\n"
        f"Send exactly `{command}` within {ENABLE_CONFIRMATION_TTL_SECONDS // 60} minutes."
    )


async def _handle_enable_confirmation(message: discord.Message, raw_text: str) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: enable confirmations require an allowlisted user.")
        return
    author_id = _message_author_id(message)
    pending = _pending_enable_confirmations.get(author_id)
    if not pending:
        await message.reply("Blocked: no enable confirmation is pending for your user.")
        return
    if monotonic() > pending["expires_at"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply("Blocked: that enable confirmation expired; review again.")
        return
    if raw_text != pending["command"]:
        await message.reply(f"Blocked: send exactly `{pending['command']}`.")
        return

    expected = pending["review"]
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
        current = _enable_review(
            expected["symbol"],
            rules,
            analysis,
            execution,
            now=datetime.now(timezone.utc),
        )
    except ConfigError as exc:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(f"Blocked after live revalidation: {exc}.")
        return

    if current["global_rules_hash"] != expected["global_rules_hash"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(
            "Blocked: the global three-asset DCA rules changed after review. "
            "Run the enable command again to review current aggregate exposure."
        )
        return

    bound_fields = (
        "low",
        "mid",
        "high",
        "effective_amount",
        "execute_at",
        "decision_id",
        "rules_hash",
        "maximum_exposure",
    )
    if any(current[field] != expected[field] for field in bound_fields):
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(
            "Blocked: budgets, decision, execution time, or aggregate exposure changed. "
            "Run the enable command again to review the live state."
        )
        return

    inputs = {
        "action": "set_enabled",
        "symbol": expected["symbol"],
        "enabled_json": "true",
        "expected_rules_hash": expected["rules_hash"],
        "expected_decision_id": expected["decision_id"],
        "expected_global_rules_hash": expected["global_rules_hash"],
    }
    _pending_enable_confirmations.pop(author_id, None)
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        await message.reply(
            f"Enable validation queued for **{expected['symbol']}**. It remains disabled "
            "unless the workflow confirms the same live rules, decision, and Kraken minimum."
        )
    else:
        await message.reply("Failed to queue enable validation. The target remains disabled.")


async def handle_analyze(params: dict[str, Any], message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("Blocked: analysis requires an allowlisted Discord user.")
        return
    raw_symbol = str(params.get("symbol") or params.get("symbols") or "all").strip()
    if raw_symbol.lower() == "all":
        workflow_symbol = "all"
        label = "all three Kraken USD targets"
    else:
        try:
            workflow_symbol = _to_usd_pair(raw_symbol)
        except ValueError as exc:
            await message.reply(f"Invalid request: {exc}")
            return
        label = workflow_symbol
    inputs = {"symbol": workflow_symbol}
    if await asyncio.to_thread(trigger_workflow, "crypto_analysis.yml", inputs):
        await message.reply(
            f"Analysis queued for **{label}**. Deterministic Python selects regime, "
            "budget tier, and execution time; Gemini only explains the result."
        )
    else:
        await message.reply("Failed to queue analysis. Existing decisions will not be reused.")


async def handle_portfolio(params: dict[str, Any], message: discord.Message) -> None:
    inputs = {"short_report": "true" if params.get("short_report", True) else "false"}
    if await asyncio.to_thread(trigger_workflow, "portfolio_check.yml", inputs):
        await message.reply("Read-only Kraken portfolio check queued.")
    else:
        await message.reply("Failed to queue the portfolio check.")


def _decision_summary(
    symbol: str,
    rule: Mapping[str, Any],
    decision: Mapping[str, Any],
    execution: Mapping[str, Any],
    *,
    now: datetime,
) -> str:
    enabled = rule["BUY_ENABLED"]
    amounts = rule["REGIME_AMOUNTS_GBP"]
    status = "ENABLED" if enabled else "disabled"
    state_entry = execution.get(symbol, {})
    pending = isinstance(state_entry.get("PENDING_ORDER"), Mapping)
    pending_text = " | PENDING KRAKEN ORDER RECOVERY" if pending else ""
    delivery_count = len(state_entry.get("PENDING_GIST_DELIVERIES", []))
    delivery_text = (
        f" | PORTFOLIO LEDGER DELIVERY WARNING ({delivery_count} pending)"
        if delivery_count
        else ""
    )
    if decision["STATUS"] == "READY":
        regime = decision["REGIME"]
        amount = _display_amount(effective_amount(rule, decision))
        next_time = _local_timestamp(decision["EXECUTE_AT"])
        age = _decision_age(decision, now)
        current_hash = rules_hash(symbol, rule)
        decision_status = "READY" if decision["RULES_HASH"] == current_hash else "RULES MISMATCH"
    else:
        regime = "ERROR"
        amount = "skipped"
        next_time = "not scheduled"
        age = _decision_age(decision, now)
        decision_status = "ERROR"
    return (
        f"**{symbol}** — {status} | UPTREND/lower "
        f"{_display_amount(amounts['LOW'])} | SIDEWAYS/midpoint "
        f"{_display_amount(amount_for_tier_gbp(rule, 'MID'))} | DOWNTREND/higher "
        f"{_display_amount(amounts['UP'])}\n"
        f"  Analysis: {decision_status} | Regime: `{regime}` | Effective: {amount} | "
        f"Next: `{next_time}` | Age: `{age}`\n"
        f"  Last buy: `{state_entry.get('LAST_BUY_DATE') or 'never'}`"
        f"{pending_text}{delivery_text}"
    )


def _pending_gist_delivery_count(execution: Mapping[str, Any]) -> int:
    """Return queued Portfolio Compass ledger records, not Kraken intents."""

    return sum(
        len(entry.get("PENDING_GIST_DELIVERIES", []))
        for entry in execution.values()
    )


async def handle_status(params: dict[str, Any], message: discord.Message) -> None:
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await message.reply(
            f"**DCA status: NOT READY**\nConfiguration/state validation failed: `{exc}`\n"
            "Trading and scheduling fail closed."
        )
        return

    now = datetime.now(timezone.utc)
    lines = ["**Kraken USD-pair DCA status (GBP budgets)**"]
    for symbol in ALLOWED_TARGETS:
        lines.append(
            _decision_summary(
                symbol,
                rules[symbol],
                analysis["TARGETS"][symbol],
                execution,
                now=now,
            )
        )
    all_disabled = not any(rule["BUY_ENABLED"] for rule in rules.values())
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    if _schedule_error:
        scheduler = f"INVALID — {_schedule_error}"
    elif _schedule_warning:
        scheduler = f"running with skipped target(s) — {_schedule_warning}"
    elif not DCA_CRON_ENABLED:
        scheduler = "paused by DCA_CRON_ENABLED=false"
    elif awaiting_symbols:
        scheduler = (
            "armed; awaiting 04:00 start-day analysis for "
            f"{', '.join(sorted(awaiting_symbols))} on "
            f"{_schedule_start_date.isoformat()} {TIMEZONE.key}; "
            f"{len(_dca_schedule)} active target(s)"
        )
    else:
        scheduler = f"running; {len(_dca_schedule)} active target(s)"
    lines.append(f"Scheduler: **{scheduler}**")
    analysis_ready = all(
        decision["STATUS"] == "READY"
        and decision["RULES_HASH"] == rules_hash(symbol, rules[symbol])
        and parse_utc_iso(decision["VALID_UNTIL"]) >= now
        for symbol, decision in analysis["TARGETS"].items()
    )
    pending_count = sum(
        isinstance(entry.get("PENDING_ORDER"), Mapping)
        for entry in execution.values()
    )
    delivery_count = _pending_gist_delivery_count(execution)
    if delivery_count:
        retry_status = (
            "automatic 30-minute retry active"
            if DCA_CRON_ENABLED
            else "automatic retry paused with scheduler"
        )
        lines.append(
            "Portfolio ledger delivery: **WARNING — "
            f"{delivery_count} pending record(s); {retry_status}**"
        )
    else:
        lines.append("Portfolio ledger delivery: **clear (0 pending records)**")
    if (
        all_disabled
        and analysis_ready
        and pending_count == 0
        and DCA_CRON_ENABLED
        and _schedule_error is None
        and _schedule_warning is None
    ):
        lines.append("Trading posture: **ready-but-disabled** (no target can submit a new order).")
    elif all_disabled:
        lines.append("Trading posture: **disabled and fail-closed; readiness needs attention**.")
    await message.reply("\n".join(lines)[:1_990])


async def handle_health(params: dict[str, Any], message: discord.Message) -> None:
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await message.reply(
            f"**DCA health: NOT READY**\n- State validation: FAILED (`{exc}`)\n"
            "- New orders: blocked"
        )
        return
    now = datetime.now(timezone.utc)
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    ready = []
    errors = []
    stale = []
    mismatched = []
    for symbol in ALLOWED_TARGETS:
        decision = analysis["TARGETS"][symbol]
        if symbol in awaiting_symbols:
            continue
        if decision["STATUS"] != "READY":
            errors.append(symbol)
            continue
        if decision["RULES_HASH"] != rules_hash(symbol, rules[symbol]):
            mismatched.append(symbol)
            continue
        if parse_utc_iso(decision["VALID_UNTIL"]) < now:
            stale.append(symbol)
            continue
        ready.append(symbol)
    pending_count = sum(
        isinstance(entry.get("PENDING_ORDER"), Mapping) for entry in execution.values()
    )
    delivery_count = _pending_gist_delivery_count(execution)
    delivery_symbols = sum(
        bool(entry.get("PENDING_GIST_DELIVERIES")) for entry in execution.values()
    )
    delivery_retry_status = (
        "automatic 30-minute retry active"
        if DCA_CRON_ENABLED
        else "automatic retry paused with scheduler"
    )
    enabled_count = sum(rule["BUY_ENABLED"] for rule in rules.values())
    analysis_ok = (
        not errors and not stale and not mismatched and not awaiting_symbols
    )
    scheduler_ok = (
        DCA_CRON_ENABLED
        and _schedule_error is None
        and _schedule_warning is None
    )
    awaiting_start_analysis = (
        enabled_count > 0
        and pending_count == 0
        and scheduler_ok
        and bool(awaiting_symbols)
    )
    posture = (
        "ARMED"
        if awaiting_start_analysis
        else
        "READY-BUT-DISABLED"
        if (
            analysis_ok
            and enabled_count == 0
            and scheduler_ok
            and pending_count == 0
            and delivery_count == 0
        )
        else "ATTENTION REQUIRED"
        if not analysis_ok or not scheduler_ok or pending_count or delivery_count
        else "ACTIVE"
    )
    lines = [
        f"**DCA health: {posture}**",
        f"- Rules: valid ({len(rules)}/3 USD targets; GBP budgets)",
        (
            "- Analysis: awaiting 04:00 start-day analysis"
            if awaiting_start_analysis
            else f"- Analysis: fresh READY {len(ready)}/3"
        ),
        f"- Execution state: valid; pending Kraken recoveries {pending_count}",
        (
            "- Portfolio ledger delivery: WARNING; "
            f"{delivery_count} pending record(s) across {delivery_symbols} target(s); "
            f"{delivery_retry_status}"
            if delivery_count
            else "- Portfolio ledger delivery: clear; 0 pending records"
        ),
        f"- Scheduler: {'running' if DCA_CRON_ENABLED else 'paused'}; "
        f"active targets {len(_dca_schedule)}",
        f"- Buy-enabled targets: {enabled_count}/3",
    ]
    if _schedule_start_date is not None:
        lines.append(
            f"- First permitted trade date: {_schedule_start_date.isoformat()} "
            f"{TIMEZONE.key}"
        )
    if errors and not awaiting_start_analysis:
        lines.append("- Analysis ERROR: " + ", ".join(errors))
    if stale and not awaiting_start_analysis:
        lines.append("- Stale decisions: " + ", ".join(stale))
    if mismatched and not awaiting_start_analysis:
        lines.append("- Rules mismatch: " + ", ".join(mismatched))
    if _schedule_error:
        lines.append(f"- Scheduler validation: FAILED (`{_schedule_error}`)")
    if _schedule_warning:
        lines.append(f"- Scheduler skipped target(s): `{_schedule_warning}`")
    await message.reply("\n".join(lines))


HELP_TEXT = """**Kraken USD-pair DCA controls (GBP budgets)**

`!dca set BTC amounts to 10 low and 20 high`
`!dca disable BTC`
`!dca enable BTC` (then send the exact confirmation returned)
`!dca analyze BTC` or `!dca analyze all`
`show status`
`!dca health`

Budget edits require the target to be disabled. Write and analysis commands
require the exact lowercase `!dca ` prefix and an allowlisted Discord user.
"""


async def handle_help(params: dict[str, Any], message: discord.Message) -> None:
    await message.reply(HELP_TEXT)


# ---------------------------------------------------------------------------
# Absolute-time Railway scheduler
# ---------------------------------------------------------------------------


def _clear_schedule(error: str | None = None) -> None:
    global _schedule_error, _schedule_warning, _schedule_start_date
    _dca_schedule.clear()
    _pending_recovery_symbols.clear()
    _pending_gist_delivery_symbols.clear()
    _awaiting_start_day_symbols.clear()
    _schedule_error = error
    _schedule_warning = None
    _schedule_start_date = None


def refresh_dca_schedule(
    rules_json: str | None,
    analysis_json: str | None,
    execution_json: str | None,
    start_date_value: str | None = None,
    *,
    now: datetime | None = None,
) -> bool:
    """Build a fail-closed schedule from durable GitHub configuration."""

    global _schedule_error, _schedule_warning, _schedule_start_date
    _awaiting_start_day_symbols.clear()
    try:
        if execution_json is None:
            raise ConfigError("DCA_EXECUTION_STATE is unavailable")
        execution = validate_execution_state(execution_json)
    except (ConfigError, ValueError, TypeError) as exc:
        _clear_schedule(str(exc))
        return False

    # Pending reconciliation is independent of analysis. Preserve it even when
    # malformed/missing rules or decisions block every new order.
    _pending_recovery_symbols.clear()
    _pending_recovery_symbols.update(
        symbol
        for symbol, entry in execution.items()
        if isinstance(entry.get("PENDING_ORDER"), Mapping)
    )
    _pending_gist_delivery_symbols.clear()
    _pending_gist_delivery_symbols.update(
        symbol
        for symbol, entry in execution.items()
        if entry.get("PENDING_GIST_DELIVERIES")
    )
    try:
        if rules_json is None or analysis_json is None:
            raise ConfigError("DCA rules or analysis state is unavailable")
        rules = validate_rules_map(rules_json)
        analysis = validate_analysis_state(analysis_json)
    except (ConfigError, ValueError, TypeError) as exc:
        _dca_schedule.clear()
        _schedule_error = str(exc)
        _schedule_warning = None
        return False

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None or current.utcoffset() is None:
        _dca_schedule.clear()
        _schedule_error = "scheduler refresh time must include a timezone"
        _schedule_warning = None
        return False
    try:
        _schedule_start_date = _parse_start_date(start_date_value)
    except ConfigError as exc:
        _dca_schedule.clear()
        _schedule_error = str(exc)
        _schedule_warning = None
        return False
    schedule: dict[str, dict[str, Any]] = {}
    invalid_enabled: list[str] = []
    awaiting_start_symbols = _pending_start_day_analysis_symbols(
        rules, analysis, current, _schedule_start_date
    )
    _awaiting_start_day_symbols.update(awaiting_start_symbols)
    for symbol in ALLOWED_TARGETS:
        rule = rules[symbol]
        if not rule["BUY_ENABLED"]:
            continue
        decision = analysis["TARGETS"][symbol]
        if symbol in awaiting_start_symbols:
            continue
        if decision["STATUS"] != "READY":
            invalid_enabled.append(f"{symbol}: analysis ERROR")
            continue
        if decision["RULES_HASH"] != rules_hash(symbol, rule):
            invalid_enabled.append(f"{symbol}: rules mismatch")
            continue
        if parse_utc_iso(decision["VALID_UNTIL"]) < current.astimezone(timezone.utc):
            invalid_enabled.append(f"{symbol}: stale decision")
            continue
        if decision_age_minutes(decision, now=current) < 0:
            invalid_enabled.append(f"{symbol}: future analysis timestamp")
            continue
        if not decision_analyzed_on_or_after(
            decision, _schedule_start_date, TIMEZONE
        ):
            invalid_enabled.append(f"{symbol}: analysis predates start date")
            continue
        schedule[symbol] = {
            "execute_at": decision["EXECUTE_AT"],
            "valid_until": decision["VALID_UNTIL"],
            "decision_id": decision["DECISION_ID"],
            "last_buy_date": execution.get(symbol, {}).get("LAST_BUY_DATE", ""),
        }

    _dca_schedule.clear()
    _dca_schedule.update(schedule)
    _schedule_error = None
    _schedule_warning = "; ".join(invalid_enabled) if invalid_enabled else None
    return True


def _guard_key(symbol: str, schedule: Mapping[str, Any] | None, now: datetime) -> str:
    if schedule and schedule.get("decision_id"):
        return str(schedule["decision_id"])
    return now.astimezone(TIMEZONE).strftime("recovery-%Y-%m-%d")


def _due_symbols_for_dispatch(now: datetime) -> list[str]:
    """Return assets due for buys, Kraken recovery, or ledger delivery."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler time must include a timezone")
    now_mono = monotonic()
    for guard, dispatched_at in list(_dca_dispatch_guard.items()):
        if now_mono - dispatched_at >= DISPATCH_RETRY_SECONDS:
            _dca_dispatch_guard.pop(guard, None)

    due: list[str] = []
    retry_symbols = _pending_recovery_symbols | _pending_gist_delivery_symbols
    for symbol in sorted(retry_symbols):
        key = _guard_key(symbol, _dca_schedule.get(symbol), now)
        if (symbol, key) not in _dca_dispatch_guard:
            due.append(symbol)

    local_date = now.astimezone(TIMEZONE).date().isoformat()
    if (
        _schedule_start_date is not None
        and now.astimezone(TIMEZONE).date() < _schedule_start_date
    ):
        return due
    for symbol, schedule in sorted(_dca_schedule.items()):
        if schedule.get("last_buy_date") == local_date:
            continue
        # The hard execution window is always -5/+60. VALID_UNTIL may shorten
        # it, but can never extend it.
        if not is_execution_window(now, schedule["execute_at"]):
            continue
        if now.astimezone(timezone.utc) > parse_utc_iso(schedule["valid_until"]):
            continue
        key = _guard_key(symbol, schedule, now)
        if symbol not in due and (symbol, key) not in _dca_dispatch_guard:
            due.append(symbol)
    return due


def _format_cron_status() -> str:
    if not DCA_CRON_ENABLED:
        return "Scheduler paused"
    if _schedule_error:
        retry_activity: list[str] = []
        if _pending_recovery_symbols:
            retry_activity.append(
                f"Kraken recovery for {len(_pending_recovery_symbols)} target(s)"
            )
        if _pending_gist_delivery_symbols:
            retry_activity.append(
                "portfolio ledger delivery for "
                f"{len(_pending_gist_delivery_symbols)} target(s)"
            )
        if retry_activity:
            return (
                "New-order scheduler invalid; retry active for "
                f"{'; '.join(retry_activity)}: {_schedule_error}"
            )
        return f"Scheduler invalid: {_schedule_error}"
    if _schedule_warning:
        return f"Scheduler running with skipped target(s): {_schedule_warning}"
    if _awaiting_start_day_symbols:
        return (
            "Scheduler armed; awaiting 04:00 start-day analysis for "
            f"{', '.join(sorted(_awaiting_start_day_symbols))} on "
            f"{_schedule_start_date.isoformat()} {TIMEZONE.key}"
        )
    if not _dca_schedule:
        return "Scheduler running; no enabled targets"
    parts = [
        f"{symbol} {_local_timestamp(entry['execute_at'])}"
        for symbol, entry in sorted(_dca_schedule.items())
    ]
    return "Scheduler: " + "; ".join(parts)


_FIVE_MINUTE_TICKS = [
    datetime.strptime(f"{hour:02d}:{minute:02d}", "%H:%M")
    .time()
    .replace(tzinfo=TIMEZONE)
    for hour in range(24)
    for minute in range(0, 60, 5)
]


@tasks.loop(time=_FIVE_MINUTE_TICKS)
async def dca_scheduler_tick() -> None:
    retry_pending = _pending_recovery_symbols or _pending_gist_delivery_symbols
    if not DCA_CRON_ENABLED or (_schedule_error and not retry_pending):
        return
    now = datetime.now(timezone.utc)
    due = _due_symbols_for_dispatch(now)
    if not due:
        return
    inputs = {"symbols_json": json.dumps(due, separators=(",", ":"))}
    success = await asyncio.to_thread(trigger_workflow, "daily_dca.yml", inputs)
    _log(
        f"{'INFO' if success else 'ERROR'} scheduler dispatch "
        f"targets={','.join(due)} accepted={str(success).lower()}"
    )
    if success:
        dispatched_at = monotonic()
        for symbol in due:
            key = _guard_key(symbol, _dca_schedule.get(symbol), now)
            _dca_dispatch_guard[(symbol, key)] = dispatched_at


@dca_scheduler_tick.before_loop
async def _before_scheduler_tick() -> None:
    await client.wait_until_ready()


async def _notify(content: str) -> None:
    if not CHANNEL_ID:
        return
    try:
        channel = client.get_channel(int(CHANNEL_ID)) or await client.fetch_channel(
            int(CHANNEL_ID)
        )
        await channel.send(content[:1_990])
    except Exception as exc:
        _log(f"WARN Discord scheduler alert failed: {type(exc).__name__}")


@tasks.loop(minutes=5)
async def dca_schedule_refresh() -> None:
    """Refresh every tick; invalid live state clears the old schedule and alerts."""

    global _last_schedule_alert
    values = await asyncio.gather(
        asyncio.to_thread(get_repo_variable, RULES_VARIABLE),
        asyncio.to_thread(get_repo_variable, ANALYSIS_STATE_VARIABLE),
        asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        asyncio.to_thread(get_repo_variable, START_DATE_VARIABLE),
    )
    valid = refresh_dca_schedule(*values)
    if not valid:
        alert = f"DCA scheduler blocked: {_schedule_error}"
        _log(f"ERROR {alert}")
        if alert != _last_schedule_alert:
            await _notify(alert)
            _last_schedule_alert = alert
        return
    if _schedule_warning:
        alert = f"DCA scheduler skipped invalid target(s): {_schedule_warning}"
        _log(f"WARN {alert}")
        if alert != _last_schedule_alert:
            await _notify(alert)
            _last_schedule_alert = alert
    else:
        _last_schedule_alert = None
    ages: list[str] = []
    try:
        analysis = validate_analysis_state(values[1])
        now = datetime.now(timezone.utc)
        for symbol in ALLOWED_TARGETS:
            ages.append(
                f"{symbol}={decision_age_minutes(analysis['TARGETS'][symbol], now=now):.0f}m"
            )
    except (ConfigError, TypeError, ValueError):
        ages = ["unavailable"]
    _log(
        f"INFO scheduler ready active_targets={len(_dca_schedule)} "
        f"pending_recovery={len(_pending_recovery_symbols)} decision_ages={','.join(ages)} "
        f"pending_portfolio_delivery={len(_pending_gist_delivery_symbols)} "
        f"posture={_format_cron_status()}"
    )


@dca_schedule_refresh.before_loop
async def _before_schedule_refresh() -> None:
    await client.wait_until_ready()


# ---------------------------------------------------------------------------
# Discord client
# ---------------------------------------------------------------------------


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)

ACTION_HANDLERS = {
    "portfolio": handle_portfolio,
    "status": handle_status,
    "help": handle_help,
    "health": handle_health,
    "analyze": handle_analyze,
}


@client.event
async def on_ready() -> None:
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")[:12]
    _log(f"INFO Discord connected commit={commit} timezone={TIMEZONE.key}")
    _log(
        f"INFO access channel_restricted={bool(CHANNEL_ID)} "
        f"allowlisted_users={len(_allowed_user_ids())}"
    )
    if not DCA_CRON_ENABLED:
        _log("INFO scheduler paused by DCA_CRON_ENABLED=false")
        return
    values = await asyncio.gather(
        asyncio.to_thread(get_repo_variable, RULES_VARIABLE),
        asyncio.to_thread(get_repo_variable, ANALYSIS_STATE_VARIABLE),
        asyncio.to_thread(get_repo_variable, EXECUTION_STATE_VARIABLE),
        asyncio.to_thread(get_repo_variable, START_DATE_VARIABLE),
    )
    valid = refresh_dca_schedule(*values)
    if valid:
        _log(
            f"INFO scheduler initial readiness valid_targets={len(ALLOWED_TARGETS)} "
            f"active_targets={len(_dca_schedule)} tick_minutes=5"
        )
        _log(f"INFO scheduler posture={_format_cron_status()}")
        if _schedule_warning:
            _log(f"WARN scheduler skipped invalid target(s): {_schedule_warning}")
            await _notify(f"DCA scheduler skipped invalid target(s): {_schedule_warning}")
    else:
        _log(f"ERROR scheduler initial readiness failed: {_schedule_error}")
        await _notify(f"DCA scheduler blocked: {_schedule_error}")
    if not dca_scheduler_tick.is_running():
        dca_scheduler_tick.start()
    if not dca_schedule_refresh.is_running():
        dca_schedule_refresh.start()


_SET_AMOUNTS_RE = re.compile(
    r"^!dca set ([A-Za-z]+) amounts to "
    r"([0-9]+(?:\.[0-9]{1,2})?) low and "
    r"([0-9]+(?:\.[0-9]{1,2})?) (?:high|up)$"
)
_DISABLE_RE = re.compile(r"^!dca disable ([A-Za-z]+)$")
_ENABLE_RE = re.compile(r"^!dca enable ([A-Za-z]+)$")
_ANALYZE_RE = re.compile(r"^!dca analyze (all|[A-Za-z]+)$")


async def _handle_exact_dca_command(text: str, message: discord.Message) -> bool:
    """Handle exact safety-critical commands without AI interpretation."""

    if text.startswith("!dca confirm"):
        await _handle_enable_confirmation(message, text)
        return True
    match = _SET_AMOUNTS_RE.fullmatch(text)
    if match:
        await handle_set_amounts(match.group(1), match.group(2), match.group(3), message)
        return True
    match = _DISABLE_RE.fullmatch(text)
    if match:
        await handle_disable(match.group(1), message)
        return True
    match = _ENABLE_RE.fullmatch(text)
    if match:
        await handle_enable(match.group(1), message)
        return True
    match = _ANALYZE_RE.fullmatch(text)
    if match:
        await handle_analyze({"symbol": match.group(1)}, message)
        return True
    if text == "!dca health":
        await handle_health({}, message)
        return True
    if text.startswith(CONFIG_WRITE_PREFIX):
        await message.reply(
            "Unrecognized exact DCA command. Use `help`; spelling and spacing are safety checks."
        )
        return True
    return False


@client.event
async def on_message(message: discord.Message) -> None:
    if message.author == client.user:
        return
    if CHANNEL_ID and str(message.channel.id) != CHANNEL_ID:
        return
    allowed = _allowed_user_ids()
    if allowed and _message_author_id(message) not in allowed:
        return
    is_dm = isinstance(message.channel, discord.DMChannel)
    mentions = getattr(message, "mentions", [])
    if not CHANNEL_ID and not is_dm and client.user not in mentions:
        return

    text = str(message.content)
    for mention in mentions:
        text = text.replace(f"<@{mention.id}>", "").replace(f"<@!{mention.id}>", "")
    text = text.strip()
    if not text:
        await message.reply(HELP_TEXT)
        return
    if await _handle_exact_dca_command(text, message):
        return
    if text.casefold() == "show status":
        await handle_status({}, message)
        return
    if text.casefold() in {"help", "!help"}:
        await handle_help({}, message)
        return

    async with message.channel.typing():
        intent = await classify_intent(text)
    handler = ACTION_HANDLERS.get(intent["action"])
    if handler:
        await handler(intent.get("params", {}), message)
    else:
        await message.reply("I did not understand that. Type `help` for exact commands.")


if __name__ == "__main__":
    required = (
        "DISCORD_BOT_TOKEN",
        "GH_PAT",
        "GITHUB_REPO",
        "GITHUB_WORKFLOW_REF",
    )
    missing = [name for name in required if not os.environ.get(name, "").strip()]
    if missing:
        _log("ERROR missing required environment variables: " + ", ".join(missing))
        sys.exit(1)
    _log(
        f"INFO starting Kraken USD-pair DCA Discord service cron_enabled={DCA_CRON_ENABLED}"
    )
    client.run(DISCORD_BOT_TOKEN, log_handler=None)

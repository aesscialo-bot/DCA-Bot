"""Discord and Railway control plane for GBP-budgeted Kraken USD DCA.

The bot never places an order.  It reads persisted GitHub variables,
dispatches serialized GitHub Actions workflows, and runs a five-minute Railway
scheduler against absolute per-asset analysis decisions.  Every write command
is exact, allowlisted, and fail-closed.
"""

from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import date, datetime, time, timedelta, timezone
import hashlib
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
from github_contents import (
    TOKEN_ENV,
    GitHubContentsClient,
    GitHubContentsConfigError,
    GitHubContentsError,
    configured_repository_path,
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
DCA_TRADING_MODE = os.environ.get("DCA_TRADING_MODE", "shadow").strip().lower()
DCA_CANARY_SYMBOL = os.environ.get("DCA_CANARY_SYMBOL", "SOL_GBP").strip().upper()
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
ANALYSIS_WORKFLOW_HEALTH_MAX_AGE = timedelta(hours=30)
# The primary workflow is scheduled for 04:07 Bangkok. Give GitHub Actions a
# bounded startup window before treating a missing start-day decision as an
# operational error.
START_DAY_ANALYSIS_EXPECTED_BY = time(4, 20)

GH_API = "https://api.github.com"
GH_HEADERS = {
    "Authorization": f"token {GH_PAT}",
    "Accept": "application/vnd.github+json",
}
DCA_OUTBOX_REPOSITORY_OWNER = "aesscialo-bot"
DCA_OUTBOX_REPOSITORY_NAME = "portfolio-canonical-ledger"
DCA_OUTBOX_REPOSITORY_BRANCH = "main"
DCA_OUTBOX_EVENT_PATH = "portfolio/kraken_usd_dca_ghostfolio_events.jsonl"
DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH = "portfolio/ghostfolio_sync_receipts.jsonl"

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
_workflow_contract_error: str | None = None
_analysis_watchdog_last_dispatch: float | None = None


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
    aliases = {
        "BTC": "BTC_GBP", "BTC_GBP": "BTC_GBP",
        "HYPE": "HYPE_USD", "HYPE_USD": "HYPE_USD",
        "SOL": "SOL_GBP", "SOL_GBP": "SOL_GBP",
    }
    key = aliases.get(raw)
    if key not in ALLOWED_TARGETS:
        raise ValueError("Supported assets are BTC/GBP, HYPE/USD, SOL/GBP")
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


def validate_workflow_contracts() -> str | None:
    """Validate the configured ref and dispatch input schemas at startup."""
    if not GITHUB_REPO or not GITHUB_WORKFLOW_REF:
        return "GITHUB_REPO and GITHUB_WORKFLOW_REF are required"
    try:
        ref_response = requests.get(
            f"{GH_API}/repos/{GITHUB_REPO}/commits/{GITHUB_WORKFLOW_REF}",
            headers=GH_HEADERS,
            timeout=15,
        )
        if ref_response.status_code != 200:
            return f"workflow ref {GITHUB_WORKFLOW_REF!r} is unavailable"
        for filename, required_input in (
            ("crypto_analysis.yml", "symbol"),
            ("daily_dca.yml", "symbols_json"),
        ):
            response = requests.get(
                f"{GH_API}/repos/{GITHUB_REPO}/contents/.github/workflows/{filename}",
                headers=GH_HEADERS,
                params={"ref": GITHUB_WORKFLOW_REF},
                timeout=15,
            )
            if response.status_code != 200:
                return f"{filename} is unavailable on {GITHUB_WORKFLOW_REF}"
            encoded = response.json().get("content", "")
            source = base64.b64decode(encoded).decode("utf-8")
            if "workflow_dispatch:" not in source or f"{required_input}:" not in source:
                return f"{filename} does not accept expected input {required_input}"
    except (KeyError, TypeError, ValueError, UnicodeError, requests.RequestException) as exc:
        return f"workflow contract validation failed ({type(exc).__name__})"
    return None


def analysis_workflow_active() -> bool:
    """Return whether GitHub already has queued/in-progress analysis on the ref."""

    return get_analysis_workflow_health()["status"] == "ACTIVE"


def get_analysis_workflow_health(*, now: datetime | None = None) -> dict[str, Any]:
    """Return bounded, non-secret evidence about the configured analysis workflow.

    The watchdog already depends on GitHub's workflow-runs endpoint.  Status uses
    the same endpoint so it can distinguish an active/successful workflow from a
    failed run or missing evidence instead of inferring GitHub health from the
    local Railway scheduler.
    """

    result: dict[str, Any] = {
        "status": "UNKNOWN",
        "configured_ref": GITHUB_WORKFLOW_REF or None,
        "actual_ref": None,
        "head_sha": None,
        "run_status": None,
        "conclusion": None,
        "updated_at": None,
        "run_number": None,
        "reason": None,
    }
    if _workflow_contract_error:
        return {
            **result,
            "status": "BLOCKED",
            "reason": _workflow_contract_error,
        }
    missing = [
        name
        for name, value in (
            ("GH_PAT", GH_PAT),
            ("GITHUB_REPO", GITHUB_REPO),
            ("GITHUB_WORKFLOW_REF", GITHUB_WORKFLOW_REF),
        )
        if not value
    ]
    if missing:
        return {
            **result,
            "reason": "missing " + ", ".join(missing),
        }
    try:
        response = requests.get(
            f"{GH_API}/repos/{GITHUB_REPO}/actions/workflows/crypto_analysis.yml/runs",
            headers=GH_HEADERS,
            params={"branch": GITHUB_WORKFLOW_REF, "per_page": 20},
            timeout=15,
        )
    except requests.RequestException as exc:
        return {
            **result,
            "reason": f"GitHub workflow observation failed ({type(exc).__name__})",
        }
    if response.status_code != 200:
        blocked = response.status_code in {401, 403, 404}
        return {
            **result,
            "status": "BLOCKED" if blocked else "UNKNOWN",
            "reason": f"GitHub workflow API HTTP {response.status_code}",
        }
    try:
        runs = response.json().get("workflow_runs")
        if not isinstance(runs, list):
            raise TypeError("workflow_runs must be a list")
        valid_runs = [run for run in runs if isinstance(run, Mapping)]
        latest = valid_runs[0] if valid_runs else None
        if latest is None:
            return {**result, "reason": "no workflow runs found on configured ref"}
        active_statuses = {"queued", "in_progress", "waiting", "pending", "requested"}
        active = next(
            (run for run in valid_runs if run.get("status") in active_statuses),
            None,
        )
        observed = active or latest
        run_status = observed.get("status")
        conclusion = observed.get("conclusion")
        actual_ref = observed.get("head_branch")
        head_sha = observed.get("head_sha")
        updated_at = observed.get("updated_at")
        run_number = observed.get("run_number")
        health_reason = None
        if active is not None:
            health_status = "ACTIVE"
        elif run_status == "completed" and conclusion == "success":
            if not isinstance(updated_at, str):
                health_status = "UNKNOWN"
                health_reason = "successful workflow run has no update timestamp"
            else:
                try:
                    updated = parse_utc_iso(updated_at)
                    reference = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
                    age = reference - updated
                except (ConfigError, TypeError, ValueError):
                    health_status = "UNKNOWN"
                    health_reason = "successful workflow run has an invalid update timestamp"
                else:
                    if age < -timedelta(minutes=5):
                        health_status = "UNKNOWN"
                        health_reason = "successful workflow run timestamp is in the future"
                    elif age > ANALYSIS_WORKFLOW_HEALTH_MAX_AGE:
                        health_status = "STALE"
                        health_reason = "latest successful workflow run is older than 30 hours"
                    else:
                        health_status = "HEALTHY"
        elif run_status == "completed":
            health_status = "FAILING"
        else:
            health_status = "UNKNOWN"
            health_reason = "unrecognised workflow run state"
        return {
            **result,
            "status": health_status,
            "actual_ref": actual_ref if isinstance(actual_ref, str) else None,
            "head_sha": head_sha if isinstance(head_sha, str) else None,
            "run_status": run_status if isinstance(run_status, str) else None,
            "conclusion": conclusion if isinstance(conclusion, str) else None,
            "updated_at": updated_at if isinstance(updated_at, str) else None,
            "run_number": run_number if isinstance(run_number, int) else None,
            "reason": health_reason,
        }
    except (AttributeError, TypeError, ValueError):
        return {**result, "reason": "invalid GitHub workflow response"}


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
    if decision["ANALYSIS_STATUS"] != "READY":
        raise ConfigError(
            f"{symbol} analysis is {decision['ANALYSIS_STATUS']}; run a fresh analysis"
        )
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
        label = "all three Kraken DCA targets"
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


def _history_data_through(decision: Mapping[str, Any]) -> str:
    """Render the last source candle timestamp without implying missing evidence."""

    history = decision.get("HISTORY")
    if not isinstance(history, Mapping):
        return "unknown"
    value = history.get("THROUGH")
    if not isinstance(value, str) or not value.strip():
        return "unknown"
    rendered = _local_timestamp(value)
    return "unknown" if rendered == "not scheduled" else rendered


def _format_analysis_workflow_health(health: Mapping[str, Any]) -> str:
    """Format one concise line of GitHub workflow evidence for Discord."""

    status = str(health.get("status") or "UNKNOWN").upper()
    configured_ref = health.get("configured_ref") or "missing"
    actual_ref = health.get("actual_ref")
    head_sha = health.get("head_sha")
    actual = "unknown"
    if isinstance(actual_ref, str) and actual_ref:
        actual = actual_ref
        if isinstance(head_sha, str) and head_sha:
            actual += f"@{head_sha[:12]}"
    run_state = health.get("run_status") or "unobserved"
    conclusion = health.get("conclusion")
    if conclusion:
        run_state = f"{run_state}/{conclusion}"
    updated_at = health.get("updated_at")
    observed = _local_timestamp(updated_at) if isinstance(updated_at, str) else "unknown"
    line = (
        f"GitHub analysis workflow: **{status}** | configured ref: `{configured_ref}` | "
        f"actual ref: `{actual}` | run: `{run_state}` | observed: `{observed}`"
    )
    reason = health.get("reason")
    if isinstance(reason, str) and reason:
        line += f" | `{reason}`"
    return line


def _analysis_complete_for_watchdog(
    analysis: Mapping[str, Any], now: datetime
) -> bool:
    current_date = now.astimezone(TIMEZONE).date().isoformat()
    targets = analysis.get("TARGETS")
    return (
        analysis.get("ANALYSIS_DATE") == current_date
        and isinstance(targets, Mapping)
        and all(
            isinstance(item, Mapping) and item.get("ANALYSIS_STATUS") == "READY"
            for item in targets.values()
        )
    )


def _analysis_watchdog_health(
    analysis: Mapping[str, Any],
    now: datetime,
    workflow_health: Mapping[str, Any],
    *,
    awaiting_symbols: set[str] | None = None,
) -> dict[str, str]:
    """Describe watchdog evidence independently from local scheduler health."""

    local_now = now.astimezone(TIMEZONE)
    if awaiting_symbols:
        return {
            "status": "WAITING",
            "detail": "start-day analysis is not due yet",
        }
    if _analysis_complete_for_watchdog(analysis, now):
        return {
            "status": "SATISFIED",
            "detail": f"current-date analysis is complete ({local_now.date().isoformat()})",
        }
    if local_now.time() < START_DAY_ANALYSIS_EXPECTED_BY:
        return {
            "status": "WAITING",
            "detail": f"recovery check begins at {START_DAY_ANALYSIS_EXPECTED_BY:%H:%M} {TIMEZONE.key}",
        }
    if _workflow_contract_error:
        return {"status": "BLOCKED", "detail": _workflow_contract_error}
    workflow_status = str(workflow_health.get("status") or "UNKNOWN").upper()
    if workflow_status == "ACTIVE":
        return {
            "status": "MONITORING",
            "detail": "GitHub analysis run is queued or in progress",
        }
    if _analysis_watchdog_last_dispatch is not None:
        return {
            "status": "ATTENTION",
            "detail": "recovery dispatch was accepted but current analysis is still incomplete",
        }
    if workflow_status in {"BLOCKED", "FAILING"}:
        return {
            "status": "BLOCKED",
            "detail": f"GitHub analysis workflow is {workflow_status.lower()}",
        }
    if workflow_status == "UNKNOWN":
        return {
            "status": "UNKNOWN",
            "detail": "GitHub workflow evidence is unavailable",
        }
    return {
        "status": "ATTENTION",
        "detail": "current analysis is incomplete and no recovery run is active",
    }


def _format_analysis_watchdog_health(health: Mapping[str, Any]) -> str:
    status = str(health.get("status") or "UNKNOWN").upper()
    detail = str(health.get("detail") or "no watchdog evidence")
    return f"Analysis watchdog: **{status}** | {detail}"


def _analysis_chain_status(
    *,
    local_scheduler_ok: bool,
    workflow_health: Mapping[str, Any],
    watchdog_health: Mapping[str, Any],
) -> str:
    """Combine independent scheduler evidence without overstating readiness."""

    if not DCA_CRON_ENABLED:
        return "PAUSED"
    workflow_status = str(workflow_health.get("status") or "UNKNOWN").upper()
    watchdog_status = str(watchdog_health.get("status") or "UNKNOWN").upper()
    if workflow_status == "UNKNOWN" or watchdog_status == "UNKNOWN":
        return "UNKNOWN"
    if (
        not local_scheduler_ok
        or workflow_status not in {"HEALTHY", "ACTIVE"}
        or watchdog_status not in {"SATISFIED", "WAITING", "MONITORING"}
    ):
        return "ATTENTION REQUIRED"
    return "OPERATIONAL"


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
    history_data_through = _history_data_through(decision)
    current_date = now.astimezone(TIMEZONE).date().isoformat()
    if (
        decision["ANALYSIS_STATUS"] == "READY"
        and decision["ANALYSIS_DATE"] == current_date
        and parse_utc_iso(decision["VALID_UNTIL"]) >= now
    ):
        regime = decision["REGIME"]
        amount = _display_amount(effective_amount(rule, decision))
        next_time = _local_timestamp(decision["EXECUTE_AT"])
        selected_time = _local_timestamp(decision["SELECTED_AT"])
        age = _decision_age(decision, now)
        current_hash = rules_hash(symbol, rule)
        decision_status = "READY" if decision["RULES_HASH"] == current_hash else "RULES MISMATCH"
    else:
        regime = "ERROR"
        amount = "skipped"
        next_time = "not scheduled"
        age = _decision_age(decision, now)
        selected_time = "none"
        decision_status = (
            "STALE/EXPIRED"
            if decision["ANALYSIS_STATUS"] == "READY"
            else decision["ANALYSIS_STATUS"]
        )
    return (
        f"**{symbol}** — {status} | UPTREND/lower "
        f"{_display_amount(amounts['LOW'])} | SIDEWAYS/midpoint "
        f"{_display_amount(amount_for_tier_gbp(rule, 'MID'))} | DOWNTREND/higher "
        f"{_display_amount(amounts['UP'])}\n"
        f"  Analysis: {decision_status} | Regime: `{regime}` | Effective: {amount} | "
        f"Selected: `{selected_time}` | Effective: `{next_time}` | Age: `{age}`\n"
        f"  Analysis date: `{decision['ANALYSIS_DATE']}` | Decision: `{decision['DECISION_ID']}` | "
        f"Execution: `{decision['EXECUTION_STATUS']}` | Data through: `{history_data_through}`\n"
        f"  Last buy: `{state_entry.get('LAST_BUY_DATE') or 'never'}`"
        f"{pending_text}{delivery_text}"
    )


def _pending_gist_delivery_count(execution: Mapping[str, Any]) -> int:
    """Return queued Portfolio Compass ledger records, not Kraken intents."""

    return sum(
        len(entry.get("PENDING_GIST_DELIVERIES", []))
        for entry in execution.values()
    )


def get_ghostfolio_delivery_health() -> dict[str, Any]:
    """Compare durable portfolio events with local sync receipts."""
    try:
        repository_token = os.environ.get(TOKEN_ENV, "").strip() or GH_PAT.strip()
        if not repository_token:
            raise GitHubContentsConfigError("private repository credential is unavailable")
        client = GitHubContentsClient(
            owner=DCA_OUTBOX_REPOSITORY_OWNER,
            repository=DCA_OUTBOX_REPOSITORY_NAME,
            branch=DCA_OUTBOX_REPOSITORY_BRANCH,
            token=repository_token,
        )
        event_path = configured_repository_path(DCA_OUTBOX_EVENT_PATH)
        receipt_path = configured_repository_path(DCA_OUTBOX_GHOSTFOLIO_EVENT_RECEIPT_PATH)
        if event_path == receipt_path:
            raise GitHubContentsConfigError("event and receipt paths must be distinct")
        commit_sha = client.resolve_commit_sha()
        event_file = client.read_text_at_commit(event_path, commit_sha)
        receipt_file = client.read_text_at_commit(receipt_path, commit_sha)
    except GitHubContentsConfigError:
        return {"status": "UNAVAILABLE", "pending": None, "completed": None}
    except GitHubContentsError:
        return {"status": "ERROR", "pending": None, "completed": None}
    try:
        if not event_file.exists:
            raise ValueError("event ledger is missing")
        event_text = event_file.content
        receipt_text = receipt_file.content if receipt_file.exists else ""
        events = {}
        for line in event_text.splitlines():
            if not line.strip():
                continue
            event = json.loads(line)
            event_id = event["event_id"]
            if event_id in events:
                raise ValueError("duplicate event ID")
            supplied_hash = event["canonical_hash"]
            unhashed = {key: value for key, value in event.items() if key != "canonical_hash"}
            actual_hash = hashlib.sha256(
                json.dumps(
                    unhashed,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()
            if supplied_hash != actual_hash:
                raise ValueError("event hash mismatch")
            events[event_id] = supplied_hash
        receipts = {}
        for line in receipt_text.splitlines():
            if not line.strip():
                continue
            receipt = json.loads(line)
            order_id = receipt["order_id"]
            if order_id in receipts or order_id not in events:
                raise ValueError("invalid receipt identity")
            if receipt["event_hash"] != events[order_id]:
                raise ValueError("receipt hash mismatch")
            receipts[order_id] = receipt
    except (AttributeError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return {"status": "INVALID", "pending": None, "completed": None}
    return {
        "status": "CLEAR" if len(events) == len(receipts) else "PENDING",
        "pending": len(events) - len(receipts),
        "completed": len(receipts),
    }


async def handle_status(params: dict[str, Any], message: discord.Message) -> None:
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await message.reply(
            f"**DCA status: NOT READY**\nConfiguration/state validation failed: `{exc}`\n"
            "Trading and scheduling fail closed."
        )
        return

    ghostfolio_health, workflow_health = await asyncio.gather(
        asyncio.to_thread(get_ghostfolio_delivery_health),
        asyncio.to_thread(get_analysis_workflow_health),
    )
    now = datetime.now(timezone.utc)
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    watchdog_health = _analysis_watchdog_health(
        analysis,
        now,
        workflow_health,
        awaiting_symbols=awaiting_symbols,
    )
    lines = [
        "**Kraken mixed-market DCA status (GBP budgets)**",
        f"Trading mode: **{DCA_TRADING_MODE}**"
        + (f" (`{DCA_CANARY_SYMBOL}` only)" if DCA_TRADING_MODE == "canary" else ""),
        _format_analysis_workflow_health(workflow_health),
    ]
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
    if _schedule_error:
        scheduler = f"INVALID — {_schedule_error}"
    elif _schedule_warning:
        scheduler = f"running with skipped target(s) — {_schedule_warning}"
    elif not DCA_CRON_ENABLED:
        scheduler = "paused by DCA_CRON_ENABLED=false"
    elif awaiting_symbols:
        scheduler = (
            "armed; awaiting 04:07 start-day analysis for "
            f"{', '.join(sorted(awaiting_symbols))} on "
            f"{_schedule_start_date.isoformat()} {TIMEZONE.key}; "
            f"{len(_dca_schedule)} active target(s)"
        )
    else:
        scheduler = f"running; {len(_dca_schedule)} active target(s)"
    local_scheduler_ok = (
        DCA_CRON_ENABLED
        and _schedule_error is None
        and _schedule_warning is None
    )
    chain_status = _analysis_chain_status(
        local_scheduler_ok=local_scheduler_ok,
        workflow_health=workflow_health,
        watchdog_health=watchdog_health,
    )
    lines.append(f"Railway scheduler: **{scheduler}**")
    lines.append(_format_analysis_watchdog_health(watchdog_health))
    lines.append(f"Analysis/scheduling chain: **{chain_status}**")
    analysis_ready = all(
        decision["ANALYSIS_STATUS"] == "READY"
        and decision["ANALYSIS_DATE"] == now.astimezone(TIMEZONE).date().isoformat()
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
    if ghostfolio_health["status"] in {"CLEAR", "PENDING"}:
        lines.append(
            "Local Ghostfolio completion: **"
            f"{ghostfolio_health['status'].lower()}** "
            f"({ghostfolio_health['completed']} receipt(s), "
            f"{ghostfolio_health['pending']} pending)"
        )
    else:
        lines.append(
            "Local Ghostfolio completion: **"
            f"{ghostfolio_health['status'].lower()}**"
        )
    if (
        all_disabled
        and analysis_ready
        and pending_count == 0
        and chain_status == "OPERATIONAL"
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
    ghostfolio_health, workflow_health = await asyncio.gather(
        asyncio.to_thread(get_ghostfolio_delivery_health),
        asyncio.to_thread(get_analysis_workflow_health),
    )
    now = datetime.now(timezone.utc)
    awaiting_symbols = _pending_start_day_analysis_symbols(rules, analysis, now)
    watchdog_health = _analysis_watchdog_health(
        analysis,
        now,
        workflow_health,
        awaiting_symbols=awaiting_symbols,
    )
    ready = []
    errors = []
    stale = []
    mismatched = []
    for symbol in ALLOWED_TARGETS:
        decision = analysis["TARGETS"][symbol]
        if symbol in awaiting_symbols:
            continue
        if decision["ANALYSIS_STATUS"] != "READY":
            errors.append(symbol)
            continue
        if decision["ANALYSIS_DATE"] != now.astimezone(TIMEZONE).date().isoformat():
            stale.append(symbol)
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
    local_scheduler_ok = (
        DCA_CRON_ENABLED
        and _schedule_error is None
        and _schedule_warning is None
    )
    chain_status = _analysis_chain_status(
        local_scheduler_ok=local_scheduler_ok,
        workflow_health=workflow_health,
        watchdog_health=watchdog_health,
    )
    scheduler_ok = chain_status == "OPERATIONAL"
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
        f"- Rules: valid ({len(rules)}/3 mixed targets; GBP budgets)",
        "- " + _format_analysis_workflow_health(workflow_health),
        "- " + _format_analysis_watchdog_health(watchdog_health),
        (
            "- Analysis: awaiting 04:07 start-day analysis"
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
        (
            f"- Local Ghostfolio receipts: {ghostfolio_health['status']}; "
            f"completed {ghostfolio_health['completed']}, pending {ghostfolio_health['pending']}"
            if ghostfolio_health["status"] in {"CLEAR", "PENDING"}
            else f"- Local Ghostfolio receipts: {ghostfolio_health['status']}"
        ),
        f"- Railway scheduler: {'running' if DCA_CRON_ENABLED else 'paused'}; "
        f"active targets {len(_dca_schedule)}",
        f"- Analysis/scheduling chain: {chain_status}",
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
    await message.reply("\n".join(lines)[:1_990])


HELP_TEXT = """**Kraken mixed-market DCA controls (GBP budgets)**

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
    if awaiting_start_symbols:
        _dca_schedule.clear()
        _schedule_error = None
        _schedule_warning = None
        return True
    current_date = current.astimezone(TIMEZONE).date().isoformat()
    global_history_failures = []
    for target in ALLOWED_TARGETS:
        decision = analysis["TARGETS"][target]
        history = decision.get("HISTORY") or {}
        if target in awaiting_start_symbols:
            continue
        if decision["ANALYSIS_STATUS"] != "READY":
            global_history_failures.append(
                f"{target}: analysis {decision['ANALYSIS_STATUS']}"
            )
        elif decision["ANALYSIS_DATE"] != current_date:
            global_history_failures.append(f"{target}: stale analysis date")
        elif history.get("STATUS") != "READY":
            global_history_failures.append(
                f"{target}: history {history.get('STATUS', 'missing')}"
            )
    if global_history_failures:
        _dca_schedule.clear()
        _schedule_error = None
        _schedule_warning = (
            "global all-three Kraken history gate: "
            + "; ".join(global_history_failures)
        )
        return True
    for symbol in ALLOWED_TARGETS:
        rule = rules[symbol]
        if not rule["BUY_ENABLED"]:
            continue
        decision = analysis["TARGETS"][symbol]
        if symbol in awaiting_start_symbols:
            continue
        if decision["ANALYSIS_STATUS"] != "READY":
            invalid_enabled.append(
                f"{symbol}: analysis {decision['ANALYSIS_STATUS']}"
            )
            continue
        if decision["ANALYSIS_DATE"] != current_date:
            invalid_enabled.append(f"{symbol}: stale analysis date")
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
            "Scheduler armed; awaiting 04:07 start-day analysis for "
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


async def _analysis_watchdog(analysis_json: str | None, now: datetime) -> None:
    """Recover a dropped daily analysis after 04:20 Bangkok, idempotently."""
    global _analysis_watchdog_last_dispatch
    local_now = now.astimezone(TIMEZONE)
    if local_now.time() < START_DAY_ANALYSIS_EXPECTED_BY:
        return
    current_date = local_now.date().isoformat()
    complete = False
    try:
        analysis = validate_analysis_state(analysis_json)
        complete = (
            analysis["ANALYSIS_DATE"] == current_date
            and all(
                item["ANALYSIS_STATUS"] == "READY"
                for item in analysis["TARGETS"].values()
            )
        )
    except (ConfigError, TypeError, ValueError):
        complete = False
    if complete:
        _analysis_watchdog_last_dispatch = None
        return
    if _workflow_contract_error:
        await _notify(f"DCA analysis watchdog BLOCKED: {_workflow_contract_error}")
        return
    if await asyncio.to_thread(analysis_workflow_active):
        return
    now_mono = monotonic()
    if (
        _analysis_watchdog_last_dispatch is not None
        and now_mono - _analysis_watchdog_last_dispatch < DISPATCH_RETRY_SECONDS
    ):
        return
    accepted = await asyncio.to_thread(
        trigger_workflow, "crypto_analysis.yml", {"symbol": "all"}
    )
    if accepted:
        _analysis_watchdog_last_dispatch = now_mono
        _log(f"WARN analysis watchdog dispatched recovery for {current_date}")
    else:
        await _notify(
            f"DCA analysis watchdog could not dispatch recovery for {current_date}"
        )


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
    await _analysis_watchdog(values[1], datetime.now(timezone.utc))
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
    global _workflow_contract_error
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")[:12]
    _log(f"INFO Discord connected commit={commit} timezone={TIMEZONE.key}")
    _log(
        f"INFO access channel_restricted={bool(CHANNEL_ID)} "
        f"allowlisted_users={len(_allowed_user_ids())}"
    )
    _workflow_contract_error = await asyncio.to_thread(validate_workflow_contracts)
    if _workflow_contract_error:
        _clear_schedule(_workflow_contract_error)
        _log(f"ERROR workflow contract BLOCKED: {_workflow_contract_error}")
        await _notify(f"DCA scheduler BLOCKED: {_workflow_contract_error}")
        return
    _log(f"INFO workflow contract valid ref={GITHUB_WORKFLOW_REF}")
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
        f"INFO starting Kraken mixed-market DCA Discord service cron_enabled={DCA_CRON_ENABLED}"
    )
    client.run(DISCORD_BOT_TOKEN, log_handler=None)

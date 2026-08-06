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
from google.genai import types
import httpx
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
CHAT_HISTORY_TURNS = 3
CHAT_HISTORY_TTL_SECONDS = 30 * 60
MAX_CHAT_SESSIONS = 100
GEMINI_TIMEOUT_SECONDS = 15
ASSET_EMOJIS = {symbol: "🪙" for symbol in ALLOWED_TARGETS}
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
_awaiting_start_day_symbols: set[str] = set()
_dca_dispatch_guard: dict[tuple[str, str], float] = {}
_schedule_error: str | None = None
_schedule_warning: str | None = None
_last_schedule_alert: str | None = None
_schedule_start_date: date | None = None
_chat_histories: dict[tuple[str, str], list[tuple[str, str]]] = {}
_chat_history_updated_at: dict[tuple[str, str], float] = {}


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
# Optional Gemini chat and read-only conversational routing
# ---------------------------------------------------------------------------


AI_MODEL_CANDIDATES = ["gemini-3.5-flash-lite", "gemini-2.5-flash-lite"]
VALID_ACTIONS = {"portfolio", "status", "health", "help", "chat", "unknown"}
VALID_CHAT_TOPICS = {
    "greeting",
    "dca",
    "regimes",
    "timing",
    "risk",
    "markets",
    "controls",
    "capabilities",
}
CHAT_TOPIC_REPLIES = {
    "greeting": (
        "🐚 Ahoy! I'm Krakie, your tiny Kraken DCA guide. I can explain how the "
        "bot works, show read-only status, or point you to an exact safe command."
    ),
    "dca": (
        "🪙 DCA means investing a fixed rules-based amount at repeated intervals. "
        "It can smooth entry timing, but it cannot remove crypto risk or guarantee returns."
    ),
    "regimes": (
        "🧭 The deterministic engine labels completed-candle conditions as uptrend, "
        "sideways, or downtrend. Those labels choose a configured budget tier; they "
        "are operational rules, not price forecasts."
    ),
    "timing": (
        "⏰ At 04:00 Asia/Bangkok, completed Kraken candles are analyzed and each "
        "asset receives a bounded execution window. Type `show status` for the latest "
        "stored times and decision ages."
    ),
    "risk": (
        "🛟 Crypto prices can move sharply and losses are possible. Krakie keeps "
        "natural-language chat read-only, requires exact commands for changes, and "
        "fails closed when decisions or configuration are invalid."
    ),
    "markets": (
        "🌊 This bot tracks `BTC/USD`, `HYPE/USD`, and `SOL/USD` on Kraken, using "
        "GBP-denominated spending limits. Type `show portfolio` for a read-only "
        "holdings check."
    ),
    "controls": (
        "🧰 Configuration changes require exact, allowlisted `!dca` commands; chat "
        "cannot apply them. Type `help` for every command and confirmation step."
    ),
    "capabilities": (
        "💬 Ask me about DCA, regimes, timing, risk, supported markets, status, or "
        "health. I keep conversation read-only—type `help` whenever you want the "
        "reviewed controls."
    ),
}
CLASSIFY_PROMPT = """You are Krakie, a pocket-sized Kraken octopus and the
intent and topic classifier for a GBP-funded USD-market DCA bot. Understand
warm, casual natural language, but do not write a reply or financial advice.

Choose exactly one action:
- portfolio: the user explicitly asks to run a read-only holdings/balance check.
- status: the user asks about latest regimes, amounts, execution times, or pair status.
- health: the user asks whether the bot, scheduler, configuration, or cloud worker is healthy.
- help: the user asks for commands, capabilities, or instructions.
- chat: every other conversational or educational message. Choose one topic:
  greeting, dca, regimes, timing, risk, markets, controls, or capabilities.
- unknown: only when the message is empty or impossible to answer safely.

Natural language is read-only. Never choose or invent an action that changes
budgets, analyzes markets, enables/disables a target, confirms enablement, or
places an order. If asked for a write or analysis action, use chat and explain
that the user must type an exact command from `help`; never say it was executed.
Do not claim access to live prices or news. The exact supported markets are
BTC/USD, HYPE/USD, and SOL/USD. Downtrend uses the higher GBP endpoint,
sideways the derived midpoint, and uptrend the lower endpoint. Kraken is the
authoritative record.

Respond as JSON only with exactly `action` and `topic`. Use topic `capabilities`
for every non-chat action. Never return reply text, parameters, commands, tools,
prices, amounts, assets, or other fields."""

INTENT_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": sorted(VALID_ACTIONS)},
        "topic": {
            "type": "string",
            "enum": sorted(VALID_CHAT_TOPICS),
        },
    },
    "required": ["action", "topic"],
    "additionalProperties": False,
}


def _rule_based_read_only_intent(text: str) -> dict[str, Any]:
    """Keep common read-only requests useful when Gemini is unavailable."""

    lowered = text.casefold()
    if any(word in lowered for word in ("help", "command", "how do i")):
        action = "help"
    elif any(word in lowered for word in ("portfolio", "balance", "holding")):
        action = "portfolio"
    elif any(word in lowered for word in ("health", "healthy", "online", "scheduler")):
        action = "health"
    elif any(
        word in lowered
        for word in ("status", "regime", "trend", "amount", "execution time")
    ):
        action = "status"
    else:
        action = "unknown"
    return {"action": action, "params": {}, "reply": ""}


def _validate_intent(intent: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(intent, dict) or intent.get("action") not in VALID_ACTIONS:
        return {"action": "unknown", "params": {}, "reply": ""}
    action = intent["action"]
    topic = str(intent.get("topic", "capabilities"))
    if topic not in VALID_CHAT_TOPICS:
        topic = "capabilities"
    return {
        "action": action,
        # Model-supplied parameters never influence a workflow dispatch.
        "params": {},
        # Model-supplied prose is never posted to Discord. For chat, code maps
        # the classified topic to a reviewed, neutral Krakie response.
        "reply": CHAT_TOPIC_REPLIES[topic] if action == "chat" else "",
        "topic": topic,
    }


async def classify_intent(
    text: str, *, history: list[tuple[str, str]] | None = None
) -> dict[str, Any]:
    if not GEMINI_API_KEY:
        return _rule_based_read_only_intent(text)
    history_lines = []
    for user_text, bot_text in (history or [])[-CHAT_HISTORY_TURNS:]:
        history_lines.append(f"User: {user_text[:400]}")
        history_lines.append(f"Krakie: {bot_text[:400]}")
    history_block = "\n".join(history_lines) or "No earlier chat turns."
    prompt = f"Recent conversation:\n{history_block}\n\nCurrent user message: {text[:1_500]}"
    last_error: Exception | None = None
    for model in AI_MODEL_CANDIDATES:
        ai_client = genai.Client(
            api_key=GEMINI_API_KEY,
            http_options=types.HttpOptions(
                timeout=GEMINI_TIMEOUT_SECONDS * 1_000,
                retry_options=types.HttpRetryOptions(attempts=1),
                # Discord.py installs aiohttp. Force the SDK's HTTPX transport
                # so Windows and Railway use the same cancellable DNS/client path.
                async_client_args={
                    "transport": httpx.AsyncHTTPTransport(retries=0),
                },
            ),
        )
        try:
            response = await asyncio.wait_for(
                ai_client.aio.models.generate_content(
                    model=model,
                    contents=prompt,
                    config=types.GenerateContentConfig(
                        system_instruction=CLASSIFY_PROMPT,
                        response_mime_type="application/json",
                        response_json_schema=INTENT_RESPONSE_SCHEMA,
                        max_output_tokens=512,
                    ),
                ),
                timeout=GEMINI_TIMEOUT_SECONDS,
            )
            raw = re.sub(
                r"^```(?:json)?\s*|\s*```$", "", str(response.text or "").strip()
            )
            intent = _validate_intent(json.loads(raw))
            return intent
        except Exception as exc:  # Gemini is optional and never authorizes writes.
            last_error = exc
        finally:
            await ai_client.aio.aclose()
    _log(f"WARN read-only command classifier unavailable: {type(last_error).__name__}")
    fallback = _rule_based_read_only_intent(text)
    if fallback["action"] == "unknown":
        fallback["reply"] = (
            "🌙 My Gemini chat brain is taking a quick nap. Exact commands still "
            "work—type `help` for the full command deck."
        )
    return fallback


def _conversation_key(message: discord.Message) -> tuple[str, str]:
    channel_id = str(getattr(getattr(message, "channel", None), "id", "dm"))
    return channel_id, _message_author_id(message)


def _prune_chat_histories(now: float | None = None) -> None:
    """Expire inactive chat text globally and enforce a hard session bound."""

    current = monotonic() if now is None else now
    keys = set(_chat_histories) | set(_chat_history_updated_at)
    for key in keys:
        updated_at = _chat_history_updated_at.get(key)
        if (
            updated_at is None
            or key not in _chat_histories
            or current - updated_at > CHAT_HISTORY_TTL_SECONDS
        ):
            _chat_histories.pop(key, None)
            _chat_history_updated_at.pop(key, None)

    excess = len(_chat_histories) - MAX_CHAT_SESSIONS
    if excess > 0:
        oldest = sorted(
            _chat_histories,
            key=lambda key: _chat_history_updated_at.get(key, float("-inf")),
        )[:excess]
        for key in oldest:
            _chat_histories.pop(key, None)
            _chat_history_updated_at.pop(key, None)


def _recent_chat_history(message: discord.Message) -> list[tuple[str, str]]:
    now = monotonic()
    _prune_chat_histories(now)
    key = _conversation_key(message)
    return list(_chat_histories.get(key, []))


def _remember_chat_turn(message: discord.Message, user_text: str, reply: str) -> None:
    now = monotonic()
    _prune_chat_histories(now)
    key = _conversation_key(message)
    history = _chat_histories.setdefault(key, [])
    history.append((user_text[:400], reply[:400]))
    del history[:-CHAT_HISTORY_TURNS]
    _chat_history_updated_at[key] = now
    _prune_chat_histories(now)


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
        await message.reply("🛡️ Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
        low = _parse_amount(low_value, "lower amount")
        high = _parse_amount(high_value, "higher amount")
        if low > high:
            raise ValueError("the lower amount must not exceed the higher amount")
    except ValueError as exc:
        await message.reply(f"⚠️ Invalid request: {exc}")
        return

    raw = await asyncio.to_thread(get_repo_variable, RULES_VARIABLE)
    try:
        rules = validate_rules_map(raw or "")
    except ConfigError as exc:
        await message.reply(f"🛡️ Blocked: live rules are invalid ({exc}).")
        return
    if rules[symbol]["BUY_ENABLED"]:
        await message.reply(
            f"🛡️ Blocked: disable **{symbol}** before changing either budget."
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
            f"⏳ Budget update queued — not applied yet for **{symbol}**: "
            f"lower {_display_amount(low)}, "
            f"sideways midpoint {_display_amount(midpoint)}, higher "
            f"{_display_amount(high)}. Run `!dca analyze {symbol.removesuffix('_USD')}` "
            "after the workflow completes."
        )
    else:
        await message.reply("❌ Failed to queue the budget update. No rules were changed.")


async def handle_disable(symbol_value: str, message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("🛡️ Blocked: this write requires an allowlisted Discord user.")
        return
    try:
        symbol = _normalise_usd_key(symbol_value)
    except ValueError as exc:
        await message.reply(f"⚠️ Invalid request: {exc}")
        return
    inputs = {
        "action": "set_enabled",
        "symbol": symbol,
        "enabled_json": "false",
    }
    if await asyncio.to_thread(trigger_workflow, "update_dca_config.yml", inputs):
        _pending_enable_confirmations.pop(_message_author_id(message), None)
        await message.reply(
            f"⏳ Disable queued — not applied yet for **{symbol}**. Once applied, "
            "the trader's live "
            "pre-submit check blocks a new order; an order already accepted by "
            "Kraken will still be reconciled."
        )
    else:
        await message.reply("❌ Failed to queue disable. No change was applied; check GitHub Actions and retry.")


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
        await message.reply("🛡️ Blocked: this write requires an allowlisted Discord user.")
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
        await message.reply(f"🛡️ Blocked: {exc}.")
        return

    command = f"!dca confirm enable {symbol}"
    author_id = _message_author_id(message)
    _pending_enable_confirmations[author_id] = {
        "command": command,
        "review": review,
        "expires_at": monotonic() + ENABLE_CONFIRMATION_TTL_SECONDS,
    }
    await message.reply(
        f"🧾🔐 **Enable review for {symbol}**\n"
        f"UPTREND/lower: {_display_amount(review['low'])} | "
        f"SIDEWAYS/midpoint: {_display_amount(review['mid'])} | "
        f"DOWNTREND/higher: {_display_amount(review['high'])}\n"
        f"Latest regime: `{review['regime']}` | Effective amount: "
        f"{_display_amount(review['effective_amount'])}\n"
        f"Next execution: `{_local_timestamp(review['execute_at'])}` | "
        f"Decision age: `{review['decision_age_minutes']:.0f} min`\n"
        f"Maximum aggregate daily exposure after enable: "
        f"**{_display_amount(review['maximum_exposure'])}**\n"
        "No change has been made. "
        "The serialized workflow will re-read this decision and check Kraken's "
        "current market minimum before enabling.\n"
        f"Send exactly `{command}` within {ENABLE_CONFIRMATION_TTL_SECONDS // 60} minutes."
    )


async def _handle_enable_confirmation(message: discord.Message, raw_text: str) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("🛡️ Blocked: enable confirmations require an allowlisted user.")
        return
    author_id = _message_author_id(message)
    pending = _pending_enable_confirmations.get(author_id)
    if not pending:
        await message.reply("🛡️ Blocked: no enable confirmation is pending for your user.")
        return
    if monotonic() > pending["expires_at"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply("⌛ Blocked: that enable confirmation expired; review again.")
        return
    if raw_text != pending["command"]:
        await message.reply(f"🛡️ Blocked: send exactly `{pending['command']}`.")
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
        await message.reply(f"🛡️ Blocked after live revalidation: {exc}.")
        return

    if current["global_rules_hash"] != expected["global_rules_hash"]:
        _pending_enable_confirmations.pop(author_id, None)
        await message.reply(
            "🛡️ Blocked: the global three-asset DCA rules changed after review. "
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
            "🛡️ Blocked: budgets, decision, execution time, or aggregate exposure changed. "
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
            f"⏳🔐 Enable validation queued for **{expected['symbol']}**. The target is still disabled "
            "unless the workflow confirms the same live rules, decision, and Kraken minimum."
        )
    else:
        await message.reply("❌ Failed to queue enable validation. The target remains disabled.")


async def handle_analyze(params: dict[str, Any], message: discord.Message) -> None:
    if not _is_authorized_config_writer(message):
        await message.reply("🛡️ Blocked: analysis requires an allowlisted Discord user.")
        return
    raw_symbol = str(params.get("symbol") or params.get("symbols") or "all").strip()
    if raw_symbol.lower() == "all":
        workflow_symbol = "all"
        label = "all three Kraken USD targets"
    else:
        try:
            workflow_symbol = _to_usd_pair(raw_symbol)
        except ValueError as exc:
            await message.reply(f"⚠️ Invalid request: {exc}")
            return
        label = workflow_symbol
    inputs = {"symbol": workflow_symbol}
    if await asyncio.to_thread(trigger_workflow, "crypto_analysis.yml", inputs):
        await message.reply(
            f"🔎 Analysis queued for **{label}**. Deterministic Python selects an "
            "operational regime label, budget tier, and execution time; it is not "
            "a return forecast and cannot submit an order. Gemini only explains the result."
        )
    else:
        await message.reply("❌ Failed to queue analysis. Existing decisions will not be reused.")


async def handle_portfolio(params: dict[str, Any], message: discord.Message) -> None:
    inputs = {"short_report": "true" if params.get("short_report", True) else "false"}
    if await asyncio.to_thread(trigger_workflow, "portfolio_check.yml", inputs):
        await message.reply("📊🐙 Read-only Kraken portfolio check queued.")
    else:
        await message.reply("❌ Failed to queue the portfolio check; no trading action was taken.")


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
    status = "🔘 ENABLED" if enabled else "⚪ disabled"
    state_entry = execution.get(symbol, {})
    pending = isinstance(state_entry.get("PENDING_ORDER"), Mapping)
    pending_text = " | 🔒 PENDING ORDER RECOVERY" if pending else ""
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
        f"{ASSET_EMOJIS.get(symbol, '🪙')} **{symbol}** — {status} | UPTREND/lower "
        f"{_display_amount(amounts['LOW'])} | SIDEWAYS/midpoint "
        f"{_display_amount(amount_for_tier_gbp(rule, 'MID'))} | DOWNTREND/higher "
        f"{_display_amount(amounts['UP'])}\n"
        f"  Analysis: {decision_status} | Regime: `{regime}` | "
        f"Configured amount for current rule: {amount} | "
        f"Next: `{next_time}` | Age: `{age}`\n"
        f"  Last buy: `{state_entry.get('LAST_BUY_DATE') or 'never'}`{pending_text}"
    )


async def handle_status(params: dict[str, Any], message: discord.Message) -> None:
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await message.reply(
            f"🛡️🐙 **DCA status: NOT READY**\nConfiguration/state validation failed: `{exc}`\n"
            "🛡️ Trading and scheduling fail closed."
        )
        return

    now = datetime.now(timezone.utc)
    lines = ["📊🐙 **Krakie's Kraken DCA status (GBP budgets)**"]
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
    lines.append(f"⏰ Scheduler: **{scheduler}**")
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
    if (
        all_disabled
        and analysis_ready
        and pending_count == 0
        and DCA_CRON_ENABLED
        and _schedule_error is None
        and _schedule_warning is None
    ):
        lines.append("🌙 Trading posture: **ready-but-disabled** (no target can submit a new order).")
    elif all_disabled:
        lines.append("🛡️ Trading posture: **disabled and fail-closed; readiness needs attention**.")
    await message.reply("\n".join(lines)[:1_990])


async def handle_health(params: dict[str, Any], message: discord.Message) -> None:
    try:
        rules, analysis, execution = await asyncio.to_thread(_load_live_state)
    except ConfigError as exc:
        await message.reply(
            f"🛡️🩺 **DCA health: NOT READY**\n- State validation: FAILED (`{exc}`)\n"
            "- 🛡️ New orders: blocked"
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
        if analysis_ok and enabled_count == 0 and scheduler_ok and pending_count == 0
        else "ATTENTION REQUIRED"
        if not analysis_ok or not scheduler_ok or pending_count
        else "ACTIVE"
    )
    lines = [
        f"🩺🐙 **DCA health: {posture}**",
        f"- 📜 Rules: valid ({len(rules)}/3 USD targets; GBP budgets)",
        (
            "- 🔎 Analysis: awaiting 04:00 start-day analysis"
            if awaiting_start_analysis
            else f"- 🔎 Analysis: fresh READY {len(ready)}/3"
        ),
        f"- 🧾 Execution state: valid; pending intents {pending_count}",
        f"- ⏰ Scheduler: {'running' if DCA_CRON_ENABLED else 'paused'}; "
        f"active targets {len(_dca_schedule)}",
        f"- 🔘 Buy-enabled targets: {enabled_count}/3",
        (
            f"- 💬 Gemini chat: configured (`{AI_MODEL_CANDIDATES[0]}` primary)"
            if GEMINI_API_KEY
            else "- 💬 Gemini chat: not configured; exact commands remain available"
        ),
    ]
    if _schedule_start_date is not None:
        lines.append(
            f"- First permitted trade date: {_schedule_start_date.isoformat()} "
            f"{TIMEZONE.key}"
        )
    if errors and not awaiting_start_analysis:
        lines.append("- ⚠️ Analysis ERROR: " + ", ".join(errors))
    if stale and not awaiting_start_analysis:
        lines.append("- ⌛ Stale decisions: " + ", ".join(stale))
    if mismatched and not awaiting_start_analysis:
        lines.append("- ⚠️ Rules mismatch: " + ", ".join(mismatched))
    if _schedule_error:
        lines.append(f"- Scheduler validation: FAILED (`{_schedule_error}`)")
    if _schedule_warning:
        lines.append(f"- Scheduler skipped target(s): `{_schedule_warning}`")
    await message.reply("\n".join(lines))


HELP_TEXT = """🐙✨ **Krakie's complete DCA command deck**
GBP-funded Kraken markets: `BTC/USD`, `HYPE/USD`, and `SOL/USD`.
Asset aliases: `BTC`/`bitcoin`, `HYPE`/`hype`/`hyperliquid`, and `SOL`/`solana`.

📊 **Look without changing anything**
`show status` or `!dca status` — regimes, amounts, times, and pending state
`!dca health` — scheduler, analysis, and configuration health
`show portfolio` or `!dca portfolio` — queue a read-only Kraken holdings check
`help`, `!help`, or `!dca help` — show this complete list

🔎 **Refresh deterministic analysis** *(allowlisted user)*
`!dca analyze BTC` — replace BTC with `HYPE` or `SOL`
`!dca analyze all` — analyze all three targets

🧾 **Change lower/higher budgets** *(disable first)*
`!dca set BTC amounts to 10 low and 20 high`
Sideways is calculated automatically. Legacy `up` is accepted as an alias for `high`.

⏸️ **Pause one target**
`!dca disable BTC`

🧾🔐 **Review and enable one target**
`!dca enable BTC` — review the live safety summary
If you choose to continue, the same user must copy the bot's exact
`!dca confirm enable BTC_USD` command within 5 minutes.

💬 **Chat with Krakie** *(requires Gemini)*
Talk normally for explanations or read-only requests. Natural language never
changes rules, starts analysis, enables/disables trading, or places an order.
Without Gemini, exact commands and common read-only keyword fallbacks still work.

🛡️ A configured channel and allowlist gate all replies. Without a configured
channel, use a DM or mention the bot. Writes/analysis always require an allowlisted
user. Every `!dca` form requires exact lowercase words and internal spacing.
Queued does not mean applied: run one command at a time and await each workflow.
Regime labels are operational rules, not forecasts.
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
    """Return assets inside their absolute -5/+60 windows or needing recovery."""

    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("scheduler time must include a timezone")
    now_mono = monotonic()
    for guard, dispatched_at in list(_dca_dispatch_guard.items()):
        if now_mono - dispatched_at >= DISPATCH_RETRY_SECONDS:
            _dca_dispatch_guard.pop(guard, None)

    due: list[str] = []
    for symbol in sorted(_pending_recovery_symbols):
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
        if _pending_recovery_symbols:
            return f"New-order scheduler invalid; pending recovery active: {_schedule_error}"
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
    if not DCA_CRON_ENABLED or (_schedule_error and not _pending_recovery_symbols):
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
        await channel.send(
            content[:1_990], allowed_mentions=discord.AllowedMentions.none()
        )
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
        alert = (
            "🛡️ Scheduler blocked — new-order scheduling is fail-closed: "
            f"{_schedule_error}"
        )
        _log(f"ERROR {alert}")
        if alert != _last_schedule_alert:
            await _notify(alert)
            _last_schedule_alert = alert
        return
    if _schedule_warning:
        alert = f"⚠️ Scheduler attention — skipped invalid target(s): {_schedule_warning}"
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

READ_ONLY_ACTION_HANDLERS = {
    "portfolio": handle_portfolio,
    "status": handle_status,
    "help": handle_help,
    "health": handle_health,
}


@client.event
async def on_ready() -> None:
    commit = os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")[:12]
    _log(f"INFO Discord connected commit={commit} timezone={TIMEZONE.key}")
    _log(
        f"INFO access channel_restricted={bool(CHANNEL_ID)} "
        f"allowlisted_users={len(_allowed_user_ids())}"
    )
    _log(
        f"INFO Gemini chat configured={bool(GEMINI_API_KEY)} "
        f"primary_model={AI_MODEL_CANDIDATES[0]} read_only=true"
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
            await _notify(
                f"⚠️ Scheduler attention — skipped invalid target(s): {_schedule_warning}"
            )
    else:
        _log(f"ERROR scheduler initial readiness failed: {_schedule_error}")
        await _notify(
            "🛡️ Scheduler blocked — new-order scheduling is fail-closed: "
            f"{_schedule_error}"
        )
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
_CONFIRM_ENABLE_RE = re.compile(
    r"^!dca confirm enable (?:BTC_USD|HYPE_USD|SOL_USD)$"
)
_COMMAND_LIKE_RE = re.compile(r"(?<!\w)!dca\b", re.IGNORECASE)


async def _handle_exact_dca_command(text: str, message: discord.Message) -> bool:
    """Handle exact safety-critical commands without AI interpretation."""

    if text == "!dca help":
        await handle_help({}, message)
        return True
    if text == "!dca status":
        await handle_status({}, message)
        return True
    if text == "!dca portfolio":
        await handle_portfolio({}, message)
        return True
    if _CONFIRM_ENABLE_RE.fullmatch(text):
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
            "🧭 Command not accepted; no changes were made. Type `help`; spelling "
            "and internal spacing are safety checks."
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

    raw_text = str(message.content)
    for mention in mentions:
        for token in (f"<@{mention.id}>", f"<@!{mention.id}>"):
            if raw_text.startswith(token):
                raw_text = raw_text[len(token) :]
                if raw_text.startswith(" "):
                    raw_text = raw_text[1:]
            else:
                raw_text = raw_text.replace(token, "")
    if not raw_text.strip():
        await handle_help({}, message)
        return
    if await _handle_exact_dca_command(raw_text, message):
        return
    if _COMMAND_LIKE_RE.search(raw_text):
        await message.reply(
            "🧭 Command not accepted; no changes were made. Type `help` and copy "
            "an exact lowercase command."
        )
        return
    text = raw_text.strip()
    if text.casefold() == "show status":
        await handle_status({}, message)
        return
    if text.casefold() == "show portfolio":
        await handle_portfolio({}, message)
        return
    if text.casefold() in {"help", "!help"}:
        await handle_help({}, message)
        return

    async with message.channel.typing():
        intent = await classify_intent(
            text,
            history=_recent_chat_history(message),
        )
    handler = READ_ONLY_ACTION_HANDLERS.get(intent["action"])
    if handler:
        await handler(intent.get("params", {}), message)
    elif intent["action"] == "chat" or intent.get("reply"):
        reply = intent.get("reply") or (
            "🤔 I can chat about the bot, DCA, and its safety rules. Type `help` "
            "whenever you want the exact command deck."
        )
        reply = f"🐙 {reply}"[:1_990]
        _remember_chat_turn(message, text, reply)
        await message.reply(
            reply, allowed_mentions=discord.AllowedMentions.none()
        )
    else:
        await message.reply(
            "🤔 I didn't quite catch that. Ask me about the bot or type `help` "
            "for every exact command."
        )


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
